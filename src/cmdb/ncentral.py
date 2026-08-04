"""Bounded, read-only N-central REST API client and inventory normalizers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Lock, RLock, Semaphore
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("cmdb.integrations.ncentral")
MAX_ASSET_ENRICHMENT_WORKERS = 4
MAX_APPLICATION_SUMMARY_ITEMS = 100
MAX_CAPABILITY_SAMPLE_DEVICES = 3
MAX_PROVIDER_READ_ATTEMPTS = 3
MAX_PROVIDER_RETRY_DELAY_SECONDS = 30.0
PROVIDER_RETRY_BASE_DELAY_SECONDS = 0.25
PROVIDER_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


class NcentralConfigurationError(ValueError):
    """Report invalid or incomplete N-central connection settings."""


class NcentralRequestError(RuntimeError):
    """Report a provider failure without exposing tokens or response bodies."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """Retain a safe HTTP status for actionable server-side error mapping."""

        super().__init__(message)
        self.status_code = status_code


class NcentralOperationCancelled(RuntimeError):
    """Stop a read-only provider operation after an operator cancellation."""


def _retry_after_seconds(error: HTTPError) -> int | None:
    """Return a non-negative delta-seconds Retry-After value when one is present."""

    try:
        value = int(str(error.headers.get("Retry-After") or "").strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0, value)


def normalize_base_url(value: str) -> str:
    """Return a safe HTTPS N-central server root without URL credentials."""

    candidate = str(value or "").strip().rstrip("/")
    if candidate.casefold().endswith("/api"):
        candidate = candidate[:-4]
    parsed = urlparse(candidate)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise NcentralConfigurationError(
            "N-central server URL must be HTTPS without embedded credentials, query or fragment"
        )
    return candidate


def normalize_org_unit(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize one N-central customer organization for explicit mapping."""

    external_id = str(record.get("orgUnitId") or "").strip()
    name = str(record.get("orgUnitName") or "").strip()
    if not external_id or not name:
        raise NcentralRequestError("N-central returned an organization without an ID or name")
    unit_type = str(record.get("orgUnitType") or "").strip().upper()
    return {
        "externalId": external_id[:160],
        "identifier": str(record.get("externalId") or record.get("externalId2") or "").strip()[
            :160
        ],
        "name": name[:240],
        "status": "Active",
        "type": unit_type or "CUSTOMER",
        "typeValues": [unit_type or "CUSTOMER"],
        "site": str(record.get("parentId") or "").strip()[:160],
        "deleted": False,
        "lastUpdated": "",
    }


def _key_token(value: object) -> str:
    """Return a case- and separator-insensitive provider-key token."""

    return "".join(character for character in str(value).casefold() if character.isalnum())


def _ci_get(mapping: object, *keys: str) -> Any:
    """Read one mapping value using case- and separator-insensitive aliases."""

    if not isinstance(mapping, dict):
        return None
    aliases = {_key_token(key) for key in keys}
    for raw_key, value in mapping.items():
        if _key_token(raw_key) in aliases:
            return value
    return None


def _asset_section(payload: dict[str, Any], *keys: str) -> Any:
    """Read a nested asset section across documented and legacy response shapes."""

    current: Any = payload
    for key in keys:
        current = _ci_get(current, key)
        if current is None:
            return None
    return current


def _payload_roots(payload: object) -> list[dict[str, Any]]:
    """Return primary and optional ``_extra`` payload roots without copying values."""

    if not isinstance(payload, dict):
        return []
    roots: list[dict[str, Any]] = []
    data = _ci_get(payload, "data")
    if isinstance(data, dict):
        roots.append(data)
        data_extra = _ci_get(data, "_extra", "extra")
        if isinstance(data_extra, dict):
            roots.append(data_extra)
    extra = _ci_get(payload, "_extra", "extra")
    if isinstance(extra, dict):
        roots.append(extra)
    roots.append(payload)
    unique: list[dict[str, Any]] = []
    seen: set[int] = set()
    for root in roots:
        if id(root) not in seen:
            seen.add(id(root))
            unique.append(root)
    return unique


def _find_section(roots: list[dict[str, Any]], *aliases: str) -> Any:
    """Return the first named section from primary or extra provider data."""

    for root in roots:
        value = _ci_get(root, *aliases)
        if value is not None:
            return value
    return None


def _section_rows(section: object) -> list[dict[str, Any]]:
    """Return mapping rows from a list, wrapper or documented single-row section."""

    if isinstance(section, list):
        return [row for row in section if isinstance(row, dict)]
    if not isinstance(section, dict):
        return []
    nested = _ci_get(section, "list", "items", "records", "data")
    if isinstance(nested, list):
        return [row for row in nested if isinstance(row, dict)]
    if isinstance(nested, dict):
        return [nested]
    return [section]


def _clean_text(value: object, maximum: int = 500) -> str:
    """Return bounded scalar text without serializing nested provider values."""

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()[:maximum]


def _first_text(sources: list[object], *aliases: str, maximum: int = 500) -> str:
    """Return the first non-empty scalar value from case-insensitive sources."""

    for source in sources:
        value = _ci_get(source, *aliases)
        text = _clean_text(value, maximum)
        if text:
            return text
    return ""


def _number(value: object) -> int | float | None:
    """Return a finite provider number without interpreting unit-bearing text."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _first_number(sources: list[object], *aliases: str) -> int | float | None:
    """Return the first finite numeric value from provider sources."""

    for source in sources:
        value = _number(_ci_get(source, *aliases))
        if value is not None:
            return value
    return None


def _boolean(value: object) -> bool | None:
    """Normalize common provider boolean representations."""

    if isinstance(value, bool):
        return value
    normalized = _clean_text(value, 20).casefold()
    if normalized in {"true", "yes", "1", "enabled"}:
        return True
    if normalized in {"false", "no", "0", "disabled"}:
        return False
    return None


def _string_list(value: object, *, maximum_items: int = 32) -> list[str]:
    """Return a bounded, de-duplicated list of scalar provider strings."""

    candidates: list[object] = list(value) if isinstance(value, (list, tuple, set)) else [value]
    results: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, str) and ("," in candidate or ";" in candidate):
            parts: tuple[object, ...] = tuple(candidate.replace(";", ",").split(","))
        else:
            parts = (candidate,)
        for part in parts:
            text = _clean_text(part, 240)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                results.append(text)
                if len(results) >= maximum_items:
                    return results
    return results


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional values while preserving ``False`` and zero."""

    return {key: value for key, value in mapping.items() if value not in (None, "", [], {})}


def _section_observed_at(section: object, fallback: str = "") -> str:
    """Return bounded collection freshness without exposing other section values."""

    timestamp_aliases = (
        "observedAt",
        "collectedAt",
        "collectionTime",
        "lastUpdated",
        "updatedAt",
        "lastScanTime",
        "scanTime",
        "assetScanTime",
        "timestamp",
    )
    sources: list[object] = [section]
    sources.extend(_section_rows(section)[:5])
    observed = _first_text(sources, *timestamp_aliases, maximum=80)
    return observed or fallback


def _coverage_entry(section: object, count: int, fallback: str) -> dict[str, Any]:
    """Describe source-section availability and freshness without raw evidence."""

    reported = section is not None
    return _compact(
        {
            # A present empty section is authoritative; an absent section is not.
            "state": "available" if reported else "not_reported",
            "recordCount": count,
            "observedAt": _section_observed_at(section, fallback) if reported else "",
        }
    )


def _operational_status(*sources: object) -> tuple[str, str]:
    """Return canonical and provider-native health without using license state."""

    raw = _first_text(
        list(sources),
        "operationalStatus",
        "deviceStatus",
        "deviceStatusLabel",
        "monitoringStatus",
        "agentStatus",
        "healthStatus",
        "status",
        maximum=160,
    )
    normalized = raw.casefold()
    if any(token in normalized for token in ("offline", "unreachable", "disconnected")):
        canonical = "offline"
    elif any(token in normalized for token in ("critical", "failed", "failure", "error")):
        canonical = "critical"
    elif any(token in normalized for token in ("warning", "degraded", "attention")):
        canonical = "warning"
    elif normalized in {"healthy", "normal", "online", "up", "ok", "active"}:
        canonical = "healthy"
    else:
        canonical = "unknown"
    return canonical, raw or "Unknown"


def _lifecycle_status(*sources: object) -> tuple[str, str]:
    """Return canonical and provider-native lifecycle without using license state."""

    raw = _first_text(
        list(sources),
        "lifecycleStatus",
        "assetStatus",
        "disposition",
        "lifecycle",
        maximum=160,
    )
    normalized = raw.casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "planned": "planned",
        "ordered": "ordered",
        "received": "received",
        "in_stock": "in_stock",
        "stock": "in_stock",
        "active": "in_service",
        "managed": "in_service",
        "in_service": "in_service",
        "maintenance": "maintenance",
        "retired": "retired",
        "decommissioned": "retired",
        "disposed": "disposed",
    }
    return aliases.get(normalized, "in_service"), raw


def _network_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize every reported network interface using a strict safe allow-list."""

    interfaces: list[dict[str, Any]] = []
    for index, row in enumerate(_section_rows(section)):
        ip_addresses = _string_list(_ci_get(row, "ipAddresses", "ipAddress", "ip", "IPAddress"))
        gateways = _string_list(
            _ci_get(row, "gateways", "defaultGateway", "gateway", "gatewayAddresses")
        )
        dns_servers = _string_list(_ci_get(row, "dnsServers", "dnsServer", "dnsAddresses"))
        subnet_masks = _string_list(_ci_get(row, "subnetMasks", "subnetMask", "ipSubnet"))
        name = _first_text([row], "name", "adapterName", "netConnectionId", maximum=240)
        description = _first_text(
            [row], "description", "adapterDescription", "productName", maximum=500
        )
        mac_address = _first_text([row], "macAddress", "mac", "physicalAddress", maximum=80)
        provider_id = _first_text(
            [row], "id", "interfaceId", "deviceId", "index", "interfaceIndex", maximum=160
        )
        stable_key = provider_id or mac_address or name or f"interface-{index + 1}"
        interface = _compact(
            {
                "key": stable_key,
                "id": provider_id or stable_key,
                "name": name,
                "description": description,
                "macAddress": mac_address,
                "ipAddresses": ip_addresses,
                "gateways": gateways,
                "dnsServers": dns_servers,
                "subnetMasks": subnet_masks,
                "dhcpEnabled": _boolean(_ci_get(row, "dhcpEnabled", "dhcp")),
                "dhcpServer": _first_text([row], "dhcpServer", maximum=240),
                "vlanId": _first_text([row], "vlanId", "vlan", maximum=80),
                "state": _first_text([row], "state", "status", "netConnectionStatus", maximum=120),
                "speedMbps": _first_number([row], "speedMbps", "linkSpeedMbps"),
                "adapterType": _first_text(
                    [row], "adapterType", "type", "interfaceType", maximum=160
                ),
            }
        )
        if len(interface) > 2 or ip_addresses or mac_address:
            interfaces.append(interface)
    return sorted(interfaces, key=lambda item: str(item.get("key") or "").casefold())


def _processor_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize processor inventory without retaining opaque provider blobs."""

    processors = [
        _compact(
            {
                "name": _first_text([row], "name", "caption", maximum=240),
                "manufacturer": _first_text([row], "manufacturer", maximum=160),
                "description": _first_text([row], "description", maximum=500),
                "processorId": _first_text([row], "processorId", "id", maximum=160),
                "cores": _first_number([row], "numberOfCores", "cores", "coreCount"),
                "logicalProcessors": _first_number(
                    [row],
                    "numberOfLogicalProcessors",
                    "logicalProcessors",
                    "logicalProcessorCount",
                ),
                "maxClockSpeedMhz": _first_number(
                    [row], "maxClockSpeed", "maxClockSpeedMhz", "clockSpeed"
                ),
                "architecture": _first_text([row], "architecture", maximum=80),
            }
        )
        for row in _section_rows(section)
    ]
    return [processor for processor in processors if processor]


def _memory_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize physical memory modules using non-secret component fields."""

    modules = [
        _compact(
            {
                "manufacturer": _first_text([row], "manufacturer", maximum=160),
                "partNumber": _first_text([row], "partNumber", maximum=160),
                "serialNumber": _first_text([row], "serialNumber", maximum=160),
                "capacityBytes": _first_number(
                    [row], "capacityBytes", "capacity", "sizeBytes", "size"
                ),
                "speedMhz": _first_number([row], "speedMhz", "speed", "configuredClockSpeed"),
                "bankLabel": _first_text([row], "bankLabel", maximum=120),
                "deviceLocator": _first_text(
                    [row], "deviceLocator", "slot", "location", maximum=160
                ),
                "memoryType": _first_text([row], "memoryType", "type", maximum=120),
            }
        )
        for row in _section_rows(section)
    ]
    return [module for module in modules if module]


def _physical_disk_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize physical disks without paths, accounts or arbitrary attributes."""

    disks = [
        _compact(
            {
                "id": _first_text([row], "id", "index", "deviceId", maximum=160),
                "model": _first_text([row], "model", "name", maximum=240),
                "manufacturer": _first_text([row], "manufacturer", maximum=160),
                "serialNumber": _first_text([row], "serialNumber", maximum=160),
                "sizeBytes": _first_number([row], "sizeBytes", "size", "capacity"),
                "mediaType": _first_text([row], "mediaType", "type", maximum=120),
                "interfaceType": _first_text([row], "interfaceType", "busType", maximum=120),
                "state": _first_text([row], "state", "status", maximum=120),
            }
        )
        for row in _section_rows(section)
    ]
    return [disk for disk in disks if disk]


def _volume_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize logical volumes without executable or remote filesystem paths."""

    volumes = [
        _compact(
            {
                "id": _first_text([row], "id", "deviceId", "driveLetter", maximum=160),
                "name": _first_text([row], "volumeName", "label", "name", "caption", maximum=240),
                "fileSystem": _first_text([row], "fileSystem", maximum=80),
                "sizeBytes": _first_number([row], "sizeBytes", "size", "capacity"),
                "freeBytes": _first_number([row], "freeBytes", "freeSpace", "availableBytes"),
                "driveType": _first_text([row], "driveType", "type", maximum=120),
                "state": _first_text([row], "state", "status", maximum=120),
            }
        )
        for row in _section_rows(section)
    ]
    return [volume for volume in volumes if volume]


def _software_inventory(section: object) -> dict[str, Any]:
    """Return a bounded application summary that never retains product keys or paths."""

    applications: list[dict[str, Any]] = []
    publishers: set[str] = set()
    for row in _section_rows(section):
        application = _compact(
            {
                "name": _first_text(
                    [row], "name", "displayName", "productName", "caption", maximum=240
                ),
                "publisher": _first_text([row], "publisher", "vendor", maximum=160),
                "version": _first_text([row], "version", "displayVersion", maximum=120),
                "installedOn": _first_text(
                    [row], "installedOn", "installDate", "installationDate", maximum=80
                ),
            }
        )
        if application.get("name"):
            applications.append(application)
            if application.get("publisher"):
                publishers.add(str(application["publisher"]))
    applications.sort(
        key=lambda item: (
            str(item.get("name") or "").casefold(),
            str(item.get("version") or "").casefold(),
        )
    )
    return {
        "count": len(applications),
        "publishers": sorted(publishers, key=str.casefold)[:50],
        "items": applications[:MAX_APPLICATION_SUMMARY_ITEMS],
        "truncated": len(applications) > MAX_APPLICATION_SUMMARY_ITEMS,
    }


def _feature_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize Windows/server features without retaining arbitrary provider fields."""

    features = [
        _compact(
            {
                "id": _first_text([row], "id", "featureId", maximum=160),
                "name": _first_text([row], "name", "displayName", "caption", maximum=240),
                "type": _first_text([row], "featureType", "type", "classification", maximum=120),
                "state": _first_text([row], "state", "installState", "status", maximum=120),
            }
        )
        for row in _section_rows(section)
    ]
    return sorted(
        [feature for feature in features if feature.get("name")],
        key=lambda item: str(item.get("name") or "").casefold(),
    )


def _os_capability_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize safe N-central OS properties without calling them server roles.

    N-central's ``osfeatures`` section is a property bag (for example a
    PowerShell version), not Windows Server Roles and Features. Only a small
    allow-list of operational capability keys is retained.
    """

    exact_keys = {
        "powershellversion",
        "supportedampcapabilities",
        "windowsupdateclientinstalledversion",
    }
    prefix_keys = ("powershellsnapin",)
    capabilities: list[dict[str, Any]] = []
    for row in _section_rows(section):
        name = _first_text([row], "pkey", "key", "name", maximum=160)
        token = _key_token(name)
        if token not in exact_keys and not any(token.startswith(prefix) for prefix in prefix_keys):
            continue
        capability = _compact(
            {
                "name": name,
                "value": _first_text([row], "pvalue", "value", maximum=240),
            }
        )
        if capability.get("name"):
            capabilities.append(capability)
    return sorted(capabilities[:128], key=lambda item: str(item.get("name") or "").casefold())


def _monitoring_inventory(section: object) -> dict[str, Any]:
    """Normalize monitoring state while excluding accounts, commands and remote URIs."""

    services: list[dict[str, Any]] = []
    for row in _section_rows(section):
        service = _compact(
            {
                "id": _first_text([row], "serviceId", "id", maximum=160),
                "name": _first_text([row], "serviceName", "name", maximum=240),
                "type": _first_text([row], "serviceType", "type", maximum=160),
                "state": _first_text(
                    [row],
                    "state",
                    "status",
                    "stateStatus",
                    "monitorStatus",
                    maximum=120,
                ),
                "lastTransitionAt": _first_text(
                    [row], "lastTransitionAt", "transitionTime", maximum=80
                ),
                "lastScanAt": _first_text([row], "lastScanAt", "lastScanTime", maximum=80),
                "timeToStaleSeconds": _first_number([row], "timeToStaleSeconds", "timeToStale"),
            }
        )
        if service.get("name") or service.get("state"):
            services.append(service)
    state_counts: dict[str, int] = {}
    for service in services:
        state = str(service.get("state") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "count": len(services),
        "stateCounts": dict(sorted(state_counts.items(), key=lambda item: item[0].casefold())),
        "services": services[:100],
        "truncated": len(services) > 100,
    }


def _maintenance_inventory(section: object) -> list[dict[str, Any]]:
    """Normalize maintenance windows without retaining commands or provider blobs."""

    windows = [
        _compact(
            {
                "id": _first_text(
                    [row],
                    "maintenanceWindowId",
                    "windowId",
                    "id",
                    maximum=160,
                ),
                "name": _first_text([row], "name", "description", maximum=240),
                "type": _first_text(
                    [row],
                    "maintenanceWindowType",
                    "windowType",
                    "type",
                    maximum=120,
                ),
                "enabled": _boolean(_ci_get(row, "enabled", "isEnabled", "active")),
                "startAt": _first_text(
                    [row],
                    "startAt",
                    "startTime",
                    "nextStartTime",
                    maximum=80,
                ),
                "endAt": _first_text([row], "endAt", "endTime", maximum=80),
                "durationMinutes": _first_number(
                    [row],
                    "durationMinutes",
                    "duration",
                ),
                "schedule": _first_text(
                    [row],
                    "schedule",
                    "cronExpression",
                    "recurrence",
                    maximum=240,
                ),
            }
        )
        for row in _section_rows(section)
    ]
    return [window for window in windows if window]


def _date_text(sources: list[object], *aliases: str) -> str:
    """Return a bounded provider date string suitable for later canonical validation."""

    return _first_text(sources, *aliases, maximum=80)


def _lifecycle_inventory(sources: list[object]) -> dict[str, Any]:
    """Normalize provider lifecycle planning fields using a strict allow-list."""

    return _compact(
        {
            "purchaseDate": _date_text(sources, "purchaseDate", "datePurchased"),
            "warrantyEnd": _date_text(
                sources, "warrantyExpiryDate", "warrantyEnd", "warrantyExpirationDate"
            ),
            "leaseEnd": _date_text(sources, "leaseExpiryDate", "leaseEnd", "leaseExpirationDate"),
            "expectedReplacementDate": _date_text(
                sources, "expectedReplacementDate", "replacementDate"
            ),
            "endOfLifeDate": _date_text(sources, "endOfLifeDate", "eolDate", "retirementDate"),
            "cost": _first_number(sources, "cost", "purchaseCost"),
            "location": _first_text(sources, "location", "assetLocation", maximum=240),
            "assetTag": _first_text(sources, "assetTag", "tag", maximum=160),
        }
    )


def _virtualization_inventory(
    section: object,
    system: object,
    features: list[dict[str, Any]],
    network_interfaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return explicit or explainable virtualization evidence without inventing links."""

    rows = _section_rows(section)
    explicit = rows[0] if rows else {}
    manufacturer = _first_text([system], "manufacturer", maximum=160)
    model = _first_text([system], "model", maximum=240)
    evidence_text = f"{manufacturer} {model}".casefold()
    detected_platform = ""
    for token, platform in (
        ("microsoft corporation virtual machine", "Hyper-V"),
        ("vmware", "VMware"),
        ("virtualbox", "VirtualBox"),
        ("kvm", "KVM"),
        ("qemu", "QEMU"),
        ("xen", "Xen"),
    ):
        if token in evidence_text:
            detected_platform = platform
            break
    feature_names = " ".join(str(item.get("name") or "") for item in features).casefold()
    feature_host_role = "hyper-v" in feature_names or "hyperv" in feature_names
    interface_texts = [
        " ".join(
            str(interface.get(key) or "")
            for key in ("name", "displayName", "description", "caption")
        ).casefold()
        for interface in (network_interfaces or [])
    ]
    # These are host-side virtual-switch interfaces. The generic Microsoft
    # Hyper-V Network Adapter signature is deliberately excluded because it is
    # normally guest evidence.
    network_host_role = any(
        "hyper-v virtual ethernet adapter" in text
        or ("vethernet" in text and "virtual switch" in text)
        for text in interface_texts
    )
    host_role = feature_host_role or network_host_role
    kind = _first_text([explicit], "kind", "virtualizationType", "role", maximum=120)
    normalized_kind = _key_token(kind)
    kind = {
        "vm": "virtual_machine",
        "guest": "virtual_machine",
        "virtualmachine": "virtual_machine",
        "virtual_machine": "virtual_machine",
        "host": "hypervisor_host",
        "hypervisor": "hypervisor_host",
        "hypervisorhost": "hypervisor_host",
        "hypervisor_host": "hypervisor_host",
    }.get(normalized_kind, kind)
    if not kind and detected_platform:
        kind = "virtual_machine"
    if not kind and host_role:
        kind = "hypervisor_host"
    host_external_id = _first_text(
        [explicit],
        "hostDeviceId",
        "hypervisorDeviceId",
        "parentDeviceId",
        maximum=160,
    )
    guest_external_ids = _string_list(
        _ci_get(
            explicit,
            "guestDeviceIds",
            "virtualMachineDeviceIds",
            "childDeviceIds",
        )
    )
    cluster_external_id = _first_text(
        [explicit],
        "clusterDeviceId",
        "clusterExternalId",
        maximum=160,
    )
    storage_external_id = _first_text(
        [explicit],
        "storageDeviceId",
        "storageExternalId",
        maximum=160,
    )
    return _compact(
        {
            "kind": kind,
            "platform": _first_text(
                [explicit], "platform", "virtualizationPlatform", "hypervisor", maximum=160
            )
            or detected_platform
            or ("Hyper-V" if host_role else ""),
            "hostName": _first_text([explicit], "hostName", "hypervisorHost", maximum=240),
            "clusterName": _first_text([explicit], "clusterName", "cluster", maximum=240),
            "hostExternalId": host_external_id,
            "guestExternalIds": guest_external_ids,
            "clusterExternalId": cluster_external_id,
            "storageExternalId": storage_external_id,
            "powerState": _first_text(
                [explicit], "powerState", "enabledState", "state", maximum=120
            ),
            "confidence": "high" if explicit else ("medium" if kind else ""),
            "evidence": (
                ["explicit_provider_inventory"]
                if explicit
                else (
                    ["manufacturer_model"]
                    if detected_platform
                    else (
                        ["installed_server_feature"]
                        if feature_host_role
                        else (["hyperv_virtual_switch_adapter"] if network_host_role else [])
                    )
                )
            ),
            "hostEvidence": (["explicit_provider_device_id"] if host_external_id else []),
            "guestEvidence": (["explicit_provider_device_ids"] if guest_external_ids else []),
            "clusterEvidence": (["explicit_provider_device_id"] if cluster_external_id else []),
            "storageEvidence": (["explicit_provider_device_id"] if storage_external_id else []),
        }
    )


def _stable_fingerprint(payload: dict[str, Any]) -> str:
    """Return a stable version over normalized non-volatile provider evidence."""

    volatile_keys = {
        "lastagentcheckin",
        "lastprovidercheckin",
        "lastinventoryobservedat",
        "observedat",
        "collectedat",
        "collectiontime",
        "lastupdated",
        "updatedat",
        "lastscantime",
        "scantime",
        "assetscantime",
        "lastscanat",
        "timestamp",
    }

    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: stable(child)
                for key, child in value.items()
                if _key_token(key) not in volatile_keys
            }
        if isinstance(value, list):
            return [stable(child) for child in value]
        return value

    encoded = json.dumps(
        stable(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_type(value: object) -> str:
    """Return a public JSON-shape type without serializing the value."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _shape_fields(mapping: object, *, maximum: int = 200) -> list[dict[str, str]]:
    """Return bounded field-name/type metadata and never provider values."""

    if not isinstance(mapping, dict):
        return []
    return [
        {"name": _clean_text(key, 120), "type": _json_type(value)}
        for key, value in sorted(mapping.items(), key=lambda item: str(item[0]).casefold())[
            :maximum
        ]
        if _clean_text(key, 120)
    ]


def _payload_shape(payload: object, *, include_field_names: bool = True) -> dict[str, Any]:
    """Summarize response structure using counts and types only."""

    summary: dict[str, Any] = {"type": _json_type(payload)}
    if isinstance(payload, list):
        summary["count"] = len(payload)
        item_types: dict[str, int] = {}
        for item in payload[:50]:
            item_type = _json_type(item)
            item_types[item_type] = item_types.get(item_type, 0) + 1
        summary["itemTypes"] = item_types
        if include_field_names:
            field_map: dict[str, str] = {}
            for item in payload[:10]:
                for field in _shape_fields(item):
                    field_map[field["name"]] = field["type"]
            summary["itemFields"] = [
                {"name": name, "type": field_type}
                for name, field_type in sorted(field_map.items(), key=lambda item: item[0])
            ][:200]
    elif isinstance(payload, dict):
        summary["count"] = len(payload)
        if include_field_names:
            summary["fields"] = _shape_fields(payload)
            sections: list[dict[str, Any]] = []
            for raw_name, value in sorted(
                payload.items(), key=lambda item: str(item[0]).casefold()
            )[:200]:
                if not isinstance(value, (dict, list)):
                    continue
                section = {
                    "name": _clean_text(raw_name, 120),
                    "type": _json_type(value),
                    "count": len(value),
                }
                if isinstance(value, dict):
                    section["fields"] = _shape_fields(value)
                elif value and isinstance(value[0], dict):
                    section["itemFields"] = _shape_fields(value[0])
                sections.append(section)
            summary["sections"] = sections
        else:
            summary["valueTypes"] = dict(
                sorted(
                    {
                        value_type: sum(
                            1 for value in payload.values() if _json_type(value) == value_type
                        )
                        for value_type in {_json_type(value) for value in payload.values()}
                    }.items()
                )
            )
    return summary


def normalize_device(
    record: dict[str, Any], asset_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize one device into safe summary and provider-neutral inventory records."""

    external_id = _first_text([record], "deviceId", maximum=160)
    name = _first_text([record], "longName", "discoveredName", "deviceName", maximum=240)
    if not external_id or not name:
        raise NcentralRequestError("N-central returned a device without an ID or name")
    device_class = _first_text([record], "deviceClassLabel", "deviceClass", maximum=160) or "Device"
    device_class_id = (
        _first_text([record], "deviceClass", maximum=160) or device_class or "__unassigned__"
    )
    license_mode = _first_text([record], "licenseMode", maximum=160) or "Unspecified"
    roots = _payload_roots(asset_payload)

    system_section = _find_section(
        roots,
        "computerSystem",
        "system",
        "win32ComputerSystem",
    )
    system_rows = _section_rows(system_section)
    system = system_rows[0] if system_rows else {}
    os_section = _find_section(roots, "os", "operatingSystem", "win32OperatingSystem")
    os_rows = _section_rows(os_section)
    operating_system = os_rows[0] if os_rows else {}
    network_section = _find_section(roots, "networkAdapter", "networkAdapters", "networkInterface")
    processor_section = _find_section(roots, "processor", "processors", "win32Processor")
    memory_section = _find_section(roots, "physicalMemory", "memoryModules", "memoryModule")
    physical_disk_section = _find_section(
        roots, "physicalDisk", "physicalDisks", "diskDrive", "diskDrives"
    )
    volume_section = _find_section(roots, "logicalDisk", "logicalDisks", "volume", "volumes")
    software_section = _find_section(
        roots, "application", "applications", "software", "installedApplications"
    )
    feature_section = _find_section(
        roots,
        "win32ServerFeature",
        "serverFeature",
        "serverFeatures",
        "windowsFeatures",
    )
    role_section = _find_section(roots, "serverRole", "serverRoles", "osRoles")
    os_capability_section = _find_section(
        roots,
        "osFeatures",
        "operatingSystemCapabilities",
    )
    service_monitoring_section = _find_section(
        roots,
        "serviceMonitorStatus",
        "monitoring",
        "monitoringServices",
    )
    active_issues_section = _find_section(roots, "activeIssues")
    monitoring_section: object = {
        "list": [
            *_section_rows(service_monitoring_section),
            *_section_rows(active_issues_section),
        ]
    }
    maintenance_section = _find_section(
        roots,
        "maintenanceWindows",
        "maintenanceWindow",
    )
    lifecycle_section = _find_section(roots, "lifecycle", "lifecycleInfo", "assetLifecycle")
    virtualization_section = _find_section(
        roots,
        "virtualization",
        "virtualMachine",
        "msvmComputerSystem",
        "hypervisor",
    )

    serial = _first_text([system, record], "serialNumber", "serial", maximum=160)
    model = _first_text([system, record], "model", maximum=240)
    manufacturer = _first_text([system, record], "manufacturer", "vendor", maximum=160)
    system_uuid = _first_text(
        [system, record],
        "systemUuid",
        "uuid",
        "biosUuid",
        "identifyingNumber",
        maximum=160,
    )
    fqdn = _first_text(
        [system, record], "fqdn", "fullyQualifiedDomainName", "dnsHostName", maximum=240
    )
    os_label = _first_text(
        [operating_system],
        "caption",
        "reportedOs",
        "name",
        "operatingSystem",
        maximum=240,
    ) or _first_text([record], "supportedOsLabel", "supportedOs", maximum=240)
    network_interfaces = _network_inventory(network_section)
    primary_network = next(
        (
            interface
            for interface in network_interfaces
            if interface.get("ipAddresses") or interface.get("macAddress")
        ),
        {},
    )
    ip_addresses = primary_network.get("ipAddresses")
    ip_address = (
        str(ip_addresses[0])
        if isinstance(ip_addresses, list) and ip_addresses
        else _first_text([record], "ipAddress", maximum=80)
    )
    mac_address = str(primary_network.get("macAddress") or "") or _first_text(
        [record], "macAddress", maximum=80
    )

    processors = _processor_inventory(processor_section)
    memory_modules = _memory_inventory(memory_section)
    physical_disks = _physical_disk_inventory(physical_disk_section)
    volumes = _volume_inventory(volume_section)
    software = _software_inventory(software_section)
    server_features = _feature_inventory(feature_section)
    server_roles = _feature_inventory(role_section)
    os_capabilities = _os_capability_inventory(os_capability_section)
    monitoring = _monitoring_inventory(monitoring_section)
    maintenance_windows = _maintenance_inventory(maintenance_section)
    lifecycle_sources: list[object] = [
        *_section_rows(lifecycle_section),
        lifecycle_section,
        system,
        record,
        *roots,
    ]
    lifecycle = _lifecycle_inventory(lifecycle_sources)
    virtualization = _virtualization_inventory(
        virtualization_section,
        system,
        [*server_features, *server_roles],
        network_interfaces,
    )
    operational_status, provider_status = _operational_status(record, monitoring_section, *roots)
    provider_status_id = _first_text(
        [record, monitoring_section],
        "deviceStatusId",
        "monitoringStatusId",
        "statusId",
        maximum=160,
    )
    canonical_lifecycle, provider_lifecycle = _lifecycle_status(lifecycle_section, record, *roots)
    provider_check_in = _first_text(
        [record],
        "lastApplianceCheckinTime",
        "lastAgentCheckIn",
        "lastCheckIn",
        maximum=80,
    )
    asset_observed_at = _first_text(
        [*roots],
        "assetScanTime",
        "lastAssetScanTime",
        "lastUpdated",
        "updatedAt",
        "collectedAt",
        maximum=80,
    )
    freshness_fallback = asset_observed_at or provider_check_in

    bios_section = _find_section(roots, "bios", "win32Bios")
    bios_rows = _section_rows(bios_section)
    bios = bios_rows[0] if bios_rows else {}
    baseboard_section = _find_section(
        roots, "baseboard", "baseboards", "motherboard", "motherboards"
    )
    baseboards = [
        _compact(
            {
                "manufacturer": _first_text([row], "manufacturer", maximum=160),
                "model": _first_text([row], "model", "product", maximum=240),
                "serialNumber": _first_text([row], "serialNumber", maximum=160),
                "version": _first_text([row], "version", maximum=120),
            }
        )
        for row in _section_rows(baseboard_section)
    ]
    hardware = _compact(
        {
            "manufacturer": manufacturer,
            "model": model,
            "serialNumber": serial,
            "systemUuid": system_uuid,
            "fqdn": fqdn,
            "systemType": _first_text(
                [system], "systemType", "pcSystemType", "chassisType", maximum=160
            ),
            "totalMemoryBytes": _first_number(
                [system], "totalPhysicalMemory", "totalMemoryBytes", "memoryBytes"
            ),
            "processorCount": len(processors),
            "memoryModuleCount": len(memory_modules),
            "bios": _compact(
                {
                    "manufacturer": _first_text([bios], "manufacturer", maximum=160),
                    "name": _first_text([bios], "name", "caption", maximum=240),
                    "version": _first_text([bios], "version", "smbiosBiosVersion", maximum=160),
                    "serialNumber": _first_text([bios], "serialNumber", maximum=160),
                    "releaseDate": _date_text([bios], "releaseDate"),
                }
            ),
            "baseboards": [item for item in baseboards if item],
        }
    )
    operating_system_details = _compact(
        {
            "name": os_label,
            "version": _first_text([operating_system, record], "version", "osVersion", maximum=120),
            "buildNumber": _first_text(
                [operating_system, record], "buildNumber", "build", maximum=120
            ),
            "architecture": _first_text(
                [operating_system, record],
                "osArchitecture",
                "architecture",
                maximum=80,
            ),
            "installDate": _date_text([operating_system], "installDate"),
            "lastBootAt": _date_text([operating_system], "lastBootUpTime", "lastBootTime"),
        }
    )
    candidate_inventory_collections: dict[str, Any] = {
        "hardware": hardware,
        "operating_system": operating_system_details,
        "network_interfaces": network_interfaces,
        "processors": processors,
        "memory_modules": memory_modules,
        "physical_disks": physical_disks,
        "volumes": volumes,
        "software": software,
        "server_roles": server_roles,
        "server_features": server_features,
        "os_capabilities": os_capabilities,
        "monitoring": monitoring,
        "maintenance_windows": maintenance_windows,
        "lifecycle": lifecycle,
        "virtualization": virtualization,
    }
    monitoring_source = (
        monitoring_section
        if service_monitoring_section is not None or active_issues_section is not None
        else None
    )
    virtualization_source = virtualization_section
    if virtualization_source is None and virtualization:
        if "hyperv_virtual_switch_adapter" in virtualization.get("evidence", []):
            virtualization_source = network_section
        else:
            virtualization_source = feature_section or role_section or system_section
    coverage_sections = {
        "hardware": (system_section, 1 if hardware else 0),
        "operating_system": (os_section, 1 if operating_system_details else 0),
        "network_interfaces": (network_section, len(network_interfaces)),
        "processors": (processor_section, len(processors)),
        "memory_modules": (memory_section, len(memory_modules)),
        "physical_disks": (physical_disk_section, len(physical_disks)),
        "volumes": (volume_section, len(volumes)),
        "software": (software_section, int(software["count"])),
        "server_roles": (role_section, len(server_roles)),
        "server_features": (feature_section, len(server_features)),
        "os_capabilities": (os_capability_section, len(os_capabilities)),
        "monitoring": (monitoring_source, int(monitoring["count"])),
        "maintenance_windows": (maintenance_section, len(maintenance_windows)),
        "lifecycle": (lifecycle_section, 1 if lifecycle else 0),
        "virtualization": (virtualization_source, 1 if virtualization else 0),
    }
    source_coverage = {
        name: _coverage_entry(section, count, freshness_fallback)
        for name, (section, count) in coverage_sections.items()
    }
    inventory_collections = {
        name: value
        for name, value in candidate_inventory_collections.items()
        if source_coverage.get(name, {}).get("state") == "available"
    }
    fields = {
        "serialNumber": serial,
        "model": model,
        "manufacturer": manufacturer,
        "vendor": manufacturer,
        "ipAddress": ip_address,
        "macAddress": mac_address,
        "operatingSystem": os_label,
        "deviceIdentifier": system_uuid,
        "fqdn": fqdn,
        "ncentralDeviceId": external_id,
        "ncentralDeviceClass": device_class,
        "ncentralLicenseMode": license_mode,
        "ncentralCustomerId": _first_text([record], "customerId", maximum=160),
        "ncentralSiteId": _first_text([record], "siteId", "orgUnitId", maximum=160),
        "ncentralSiteName": _first_text([record], "siteName", maximum=240),
        "lastAgentCheckIn": provider_check_in,
        "processorCount": (
            len(processors) if source_coverage["processors"]["state"] == "available" else None
        ),
        "memoryModuleCount": (
            len(memory_modules)
            if source_coverage["memory_modules"]["state"] == "available"
            else None
        ),
        "networkInterfaceCount": (
            len(network_interfaces)
            if source_coverage["network_interfaces"]["state"] == "available"
            else None
        ),
        "physicalDiskCount": (
            len(physical_disks)
            if source_coverage["physical_disks"]["state"] == "available"
            else None
        ),
        "volumeCount": (
            len(volumes) if source_coverage["volumes"]["state"] == "available" else None
        ),
        "installedApplicationCount": (
            int(software["count"]) if source_coverage["software"]["state"] == "available" else None
        ),
        "serverRoleCount": (
            len(server_roles) if source_coverage["server_roles"]["state"] == "available" else None
        ),
        "serverFeatureCount": (
            len(server_features)
            if source_coverage["server_features"]["state"] == "available"
            else None
        ),
        "osCapabilityCount": (
            len(os_capabilities)
            if source_coverage["os_capabilities"]["state"] == "available"
            else None
        ),
        "virtualization": virtualization,
    }
    safe_fields = _compact(fields)
    metadata = _compact(
        {
            "lifecycle": canonical_lifecycle,
            "operationalStatus": operational_status,
            "site": fields["ncentralSiteName"],
            "vendor": manufacturer,
            "ipAddress": ip_address,
            "model": model,
            "serialNumber": serial,
            "purchaseDate": lifecycle.get("purchaseDate", ""),
            "warrantyEnd": lifecycle.get("warrantyEnd", ""),
            "endOfLifeDate": lifecycle.get("endOfLifeDate", ""),
            "virtualizationPlatform": virtualization.get("platform", ""),
            "clusterName": virtualization.get("clusterName", ""),
            "powerState": virtualization.get("powerState", ""),
            "guestOs": os_label if virtualization.get("kind") == "virtual_machine" else "",
            "sourceSystem": "N-central",
            "sourceCoverage": source_coverage,
            "lastProviderCheckIn": provider_check_in,
            "lastInventoryObservedAt": asset_observed_at,
            "providerLifecycleStatus": provider_lifecycle,
        }
    )
    identifiers = {
        key: value
        for key, value in {
            "serial_number": serial,
            "mac_address": mac_address,
            "device_uuid": system_uuid,
            "fqdn": fqdn,
        }.items()
        if value
    }
    fingerprint_material = {
        "externalId": external_id,
        "name": name,
        "type": device_class,
        "providerStatus": provider_status,
        "providerStatusId": provider_status_id or _key_token(provider_status),
        "lifecycle": canonical_lifecycle,
        "fields": {key: value for key, value in safe_fields.items() if key != "lastAgentCheckIn"},
        "inventoryCollections": inventory_collections,
        "identifiers": identifiers,
    }
    fingerprint = _stable_fingerprint(fingerprint_material)
    return {
        "externalId": external_id[:160],
        "name": name[:240],
        "type": device_class or "Device",
        "status": provider_status,
        "providerTypeId": device_class_id[:160] or "__unassigned__",
        "providerTypeName": device_class[:160] or "Device",
        "providerStatusId": provider_status_id
        or _key_token(provider_status)[:160]
        or "__unknown__",
        "providerStatusName": provider_status[:160],
        "fields": safe_fields,
        "metadata": metadata,
        "identifiers": identifiers,
        "inventoryCollections": inventory_collections,
        "providerFingerprint": fingerprint,
        "providerVersion": fingerprint,
        "providerObservedAt": provider_check_in or asset_observed_at,
    }


class NcentralClient:
    """Read N-central organizations and devices through short-lived access tokens."""

    def __init__(
        self,
        configuration: dict[str, Any],
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: int = 20,
        telemetry_callback: Callable[[dict[str, Any]], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.base_url = normalize_base_url(str(configuration.get("baseUrl") or ""))
        self.user_api_token = str(configuration.get("userApiToken") or "").strip()
        if not self.user_api_token:
            raise NcentralConfigurationError("N-central connection is missing userApiToken")
        page_size = int(configuration.get("pageSize") or 250)
        if not 25 <= page_size <= 1000:
            raise NcentralConfigurationError("N-central page size must be between 25 and 1000")
        self.page_size = page_size
        self.timeout = max(5, min(int(timeout), 60))
        self.opener = opener
        self.telemetry_callback = telemetry_callback
        self.sleeper = sleeper
        self.jitter = jitter
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._access_token_lock = RLock()
        self._last_success_telemetry_at = 0.0

    def _emit_telemetry(
        self, request: Request, status: int, retry_after: int | None = None
    ) -> None:
        """Emit only safe request-path and concurrency-throttle evidence."""

        if not self.telemetry_callback:
            return
        now = time.monotonic()
        if status < 400:
            if now - self._last_success_telemetry_at < 30:
                return
            self._last_success_telemetry_at = now
        try:
            self.telemetry_callback(
                {
                    "httpStatus": status,
                    "limit": None,
                    "remaining": None,
                    "resetAt": None,
                    "retryAfterSeconds": retry_after,
                    "limited": status == 429,
                    "requestPath": urlparse(request.full_url).path[:500],
                    "metadata": {"providerLimit": "in_flight_requests"},
                }
            )
        except Exception:
            LOGGER.exception("Could not persist sanitized N-central request telemetry")

    def _decode(self, response: Any, request: Request) -> Any:
        """Decode JSON and record provider status without exposing its body."""

        status = int(getattr(response, "status", None) or response.getcode() or 200)
        self._emit_telemetry(request, status)
        return json.loads(response.read())

    def _open_json(self, request: Request) -> Any:
        """Execute one allow-listed read/auth request with bounded transient retries."""

        for attempt in range(MAX_PROVIDER_READ_ATTEMPTS):
            try:
                # The administrator-supplied endpoint is validated as HTTPS above.
                with self.opener(request, timeout=self.timeout) as response:  # nosec B310
                    return self._decode(response, request)
            except HTTPError as error:
                status = int(error.code)
                retry_after = _retry_after_seconds(error)
                self._emit_telemetry(request, status, retry_after)
                if (
                    status in PROVIDER_RETRYABLE_STATUS_CODES
                    and attempt + 1 < MAX_PROVIDER_READ_ATTEMPTS
                ):
                    try:
                        jitter = max(0.0, min(float(self.jitter()), 1.0))
                    except (TypeError, ValueError):
                        jitter = 0.0
                    base_delay = (
                        float(retry_after)
                        if retry_after is not None
                        else PROVIDER_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                    )
                    delay = min(
                        MAX_PROVIDER_RETRY_DELAY_SECONDS,
                        base_delay + PROVIDER_RETRY_BASE_DELAY_SECONDS * jitter,
                    )
                    self.sleeper(delay)
                    continue
                raise NcentralRequestError(
                    f"N-central returned HTTP {status}",
                    status_code=status,
                ) from error
            except (URLError, TimeoutError) as error:
                raise NcentralRequestError(
                    "N-central could not be reached before timeout"
                ) from error
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
                raise NcentralRequestError("N-central returned an invalid JSON response") from error
        raise AssertionError("N-central retry loop exhausted without returning or raising")

    @staticmethod
    def _access_token_from(payload: Any) -> tuple[str, int]:
        """Extract the documented access token while tolerating wrapper differences."""

        if not isinstance(payload, dict):
            raise NcentralRequestError("N-central returned an unexpected authentication response")
        tokens = payload.get("tokens")
        access = tokens.get("access") if isinstance(tokens, dict) else None
        token = str(access.get("token") or "").strip() if isinstance(access, dict) else ""
        try:
            expiry = int(access.get("expirySeconds") or 3600) if isinstance(access, dict) else 3600
        except (TypeError, ValueError):
            expiry = 3600
        if not token:
            raise NcentralRequestError("N-central authentication did not return an access token")
        return token, max(60, min(expiry, 86400))

    def authenticate(self) -> dict[str, Any]:
        """Exchange the permanent User-API token without persisting the access token."""

        with self._access_token_lock:
            request = Request(
                f"{self.base_url}/api/auth/authenticate",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.user_api_token}",
                    "User-Agent": "IPT-CMDB/1.0 (read-only N-central inventory)",
                },
                method="POST",
            )
            token, expiry = self._access_token_from(self._open_json(request))
            self._access_token = token
            self._access_token_expires_at = time.monotonic() + expiry - 30
        return {"authenticated": True, "expirySeconds": expiry}

    def _token(self) -> str:
        """Return a live in-memory access token, exchanging when necessary."""

        with self._access_token_lock:
            if not self._access_token or time.monotonic() >= self._access_token_expires_at:
                self.authenticate()
            return self._access_token

    def _get(self, path: str, parameters: dict[str, Any] | None = None) -> Any:
        """Read one hard-coded API path, refreshing a rejected access token once."""

        query = urlencode(parameters or {}, doseq=True)
        url = f"{self.base_url}{path}{'?' + query if query else ''}"

        def request_with(token: str) -> Request:
            return Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "IPT-CMDB/1.0 (read-only N-central inventory)",
                },
                method="GET",
            )

        # A 401 from token exchange itself must propagate without recursively
        # invoking authentication. Only a previously issued access token gets
        # one fresh exchange and one replay of the allow-listed GET.
        token = self._token()
        try:
            return self._open_json(request_with(token))
        except NcentralRequestError as error:
            if error.status_code != 401:
                raise
        with self._access_token_lock:
            if self._access_token == token:
                self._access_token = ""
                self._access_token_expires_at = 0.0
                self.authenticate()
            refreshed_token = self._access_token
        return self._open_json(request_with(refreshed_token))

    @staticmethod
    def _rows(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return collection rows and safe pagination metadata."""

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise NcentralRequestError("N-central returned an unexpected collection response")
        rows = payload["data"]
        if any(not isinstance(item, dict) for item in rows):
            raise NcentralRequestError("N-central returned an invalid collection item")
        page_size = int(payload.get("pageSize") or len(rows) or 1)
        total_items = int(payload.get("totalItems") or len(rows))
        return rows, {
            "pageNumber": int(payload.get("pageNumber") or 1),
            "pageSize": page_size,
            "totalPages": int(
                payload.get("totalPages") or max(1, math.ceil(total_items / page_size))
            ),
            "totalItems": total_items,
        }

    def validate_access(self) -> dict[str, Any]:
        """Validate the short-lived token with the provider's read-only endpoint."""

        payload = self._get("/api/auth/validate")
        return (
            {"valid": True, "message": str(payload.get("message") or "")[:240]}
            if isinstance(payload, dict)
            else {"valid": True, "message": str(payload)[:240]}
        )

    def test_connection(self) -> dict[str, Any]:
        """Prove token exchange, validation and organization-read permission."""

        authentication = self.authenticate()
        self.validate_access()
        payload = self._get(
            "/api/org-units",
            {"pageNumber": 1, "pageSize": 1, "sortBy": "orgUnitId", "sortOrder": "asc"},
        )
        rows, metadata = self._rows(payload)
        return {
            "reachable": True,
            "accessExpirySeconds": authentication["expirySeconds"],
            "sampleOrganization": normalize_org_unit(rows[0]) if rows else None,
            "accessibleOrganizations": metadata["totalItems"],
        }

    def _paged(
        self,
        path: str,
        parameters: dict[str, Any] | None = None,
        *,
        max_pages: int = 100,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Read a bounded collection using documented one-based pagination."""

        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max(1, min(int(max_pages), 100)) + 1):
            if cancel_requested and cancel_requested():
                raise NcentralOperationCancelled("N-central preview was cancelled")
            payload = self._get(
                path,
                {
                    **(parameters or {}),
                    "pageNumber": page,
                    "pageSize": self.page_size,
                },
            )
            rows, metadata = self._rows(payload)
            for row in rows:
                key = json.dumps(row, sort_keys=True, separators=(",", ":"))
                if key not in seen:
                    seen.add(key)
                    discovered.append(row)
            if progress_callback:
                progress_callback(
                    {
                        "phase": "discovering",
                        "current": len(discovered),
                        "total": max(len(discovered), int(metadata["totalItems"] or 0)),
                        "page": page,
                        "pages": max(page, int(metadata["totalPages"] or 0)),
                        "discovered": len(discovered),
                        "enriched": 0,
                    }
                )
            if (
                page >= metadata["totalPages"]
                or len(discovered) >= metadata["totalItems"]
                or not rows
            ):
                return discovered
        raise NcentralRequestError(
            f"N-central collection exceeded the {max_pages}-page safety limit"
        )

    def discover_customers(self) -> list[dict[str, Any]]:
        """Return accessible CUSTOMER organization units for explicit CMDB mapping."""

        rows = self._paged(
            "/api/org-units",
            {
                "sortBy": "orgUnitId",
                "sortOrder": "asc",
            },
        )
        return [
            normalize_org_unit(row)
            for row in rows
            if str(row.get("orgUnitType") or "").upper() == "CUSTOMER"
        ]

    def list_device_filters(self) -> list[dict[str, str]]:
        """Return immutable filter IDs available to the configured N-central user."""

        rows = self._paged(
            "/api/device-filters",
            {"viewScope": "ALL", "sortBy": "filterName", "sortOrder": "asc"},
        )
        return sorted(
            [
                {
                    "id": str(row.get("filterId") or "").strip()[:160],
                    "name": str(row.get("filterName") or "").strip()[:240],
                    "description": str(row.get("description") or "").strip()[:500],
                }
                for row in rows
                if str(row.get("filterId") or "").strip()
                and str(row.get("filterName") or "").strip()
            ],
            key=lambda item: (item["name"].casefold(), item["id"]),
        )

    @staticmethod
    def _capability_access_status(status_code: int | None) -> str:
        """Return an operator-safe access state for one optional read endpoint."""

        return {
            401: "unauthorized",
            403: "forbidden",
            404: "not_supported_or_not_found",
            429: "rate_limited",
        }.get(status_code or 0, "unavailable")

    def _probe_read_endpoint(
        self,
        path: str,
        *,
        parameters: dict[str, Any] | None = None,
        cached_payload: object | None = None,
        include_field_names: bool = True,
    ) -> dict[str, Any]:
        """Probe one read-only endpoint and expose only access and response shape."""

        try:
            payload = cached_payload if cached_payload is not None else self._get(path, parameters)
        except NcentralRequestError as error:
            return _compact(
                {
                    "accessStatus": self._capability_access_status(error.status_code),
                    "httpStatus": error.status_code,
                }
            )
        return {
            "accessStatus": "available",
            "httpStatus": 200,
            "shape": _payload_shape(payload, include_field_names=include_field_names),
        }

    def probe_device_capabilities(
        self,
        org_unit_id: str,
        *,
        device_id: str = "",
        sample_size: int = MAX_CAPABILITY_SAMPLE_DEVICES,
    ) -> dict[str, Any]:
        """Return a bounded, value-free schema probe for documented device reads.

        A specified device must prove membership in the explicit customer boundary.
        Custom-property names and all response values are deliberately omitted.
        """

        parent_id = str(org_unit_id or "").strip()
        if not parent_id or not parent_id.replace("-", "").isalnum():
            raise NcentralConfigurationError("Choose a valid N-central organization ID")
        selected_device = str(device_id or "").strip()
        if selected_device and not selected_device.isdigit():
            raise NcentralConfigurationError("Choose a valid numeric N-central device ID")
        bounded_sample = max(1, min(int(sample_size), MAX_CAPABILITY_SAMPLE_DEVICES))

        selected: list[tuple[str, object | None]] = []
        if selected_device:
            details = self._get(f"/api/devices/{int(selected_device)}")
            if not isinstance(details, dict):
                raise NcentralRequestError(
                    "N-central returned an unexpected device detail response"
                )
            detail_roots = _payload_roots(details)
            detail_sources: list[object] = [*detail_roots]
            returned_device = _first_text(detail_sources, "deviceId", maximum=160)
            if returned_device and returned_device != selected_device:
                raise NcentralRequestError(
                    "N-central returned details for an unexpected device identity"
                )
            scope_ids = {
                _first_text(detail_sources, key, maximum=160)
                for key in ("orgUnitId", "customerId", "soId", "siteId")
            }
            if parent_id not in scope_ids:
                raise NcentralConfigurationError(
                    "The selected N-central device is outside the chosen organization"
                )
            selected.append((selected_device, details))
        else:
            payload = self._get(
                f"/api/org-units/{parent_id}/devices",
                {
                    "pageNumber": 1,
                    "pageSize": bounded_sample,
                    "sortBy": "deviceId",
                    "sortOrder": "asc",
                },
            )
            rows, _metadata = self._rows(payload)
            for row in rows[:bounded_sample]:
                candidate = _first_text([row], "deviceId", maximum=160)
                if candidate.isdigit():
                    selected.append((candidate, None))

        endpoint_descriptors = (
            ("device_details", "", True),
            ("assets", "/assets", True),
            ("lifecycle", "/assets/lifecycle-info", True),
            ("custom_properties", "/custom-properties", False),
            ("service_monitor_status", "/service-monitor-status", True),
            ("maintenance_windows", "/maintenance-windows", True),
        )
        samples: list[dict[str, Any]] = []
        for sample_index, (candidate, cached_details) in enumerate(selected, start=1):
            endpoints: dict[str, Any] = {}
            for label, suffix, include_names in endpoint_descriptors:
                endpoints[label] = self._probe_read_endpoint(
                    f"/api/devices/{int(candidate)}{suffix}",
                    cached_payload=cached_details if not suffix else None,
                    include_field_names=include_names,
                )
            samples.append(
                {
                    "sample": sample_index,
                    "endpoints": endpoints,
                }
            )

        active_issues = self._probe_read_endpoint(
            f"/api/org-units/{parent_id}/active-issues",
            parameters={
                "pageNumber": 1,
                "pageSize": 1,
                "sortBy": "deviceId",
                "sortOrder": "asc",
            },
        )
        return {
            "provider": "ncentral",
            "readOnly": True,
            "requestedDevice": bool(selected_device),
            "sampleCount": len(samples),
            "maximumSampleCount": MAX_CAPABILITY_SAMPLE_DEVICES,
            "samples": samples,
            "organizationEndpoints": {"active_issues": active_issues},
        }

    def discover_devices(
        self,
        org_unit_id: str,
        *,
        filter_id: str = "",
        enrich_limit: int = 0,
        enrichment_offset: int = 0,
        priority_external_ids: list[str] | tuple[str, ...] | set[str] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Return devices for one explicitly mapped organization unit."""

        parent_id = str(org_unit_id or "").strip()
        if not parent_id or not parent_id.replace("-", "").isalnum():
            raise NcentralConfigurationError("Choose a valid N-central organization ID")
        parameters: dict[str, Any] = {
            "sortBy": "deviceId",
            "sortOrder": "asc",
        }
        selected_filter = str(filter_id or "").strip()
        if selected_filter:
            if not selected_filter.replace("-", "").replace("_", "").isalnum():
                raise NcentralConfigurationError("Choose a valid N-central device filter ID")
            parameters["filterId"] = selected_filter
        rows = self._paged(
            f"/api/org-units/{parent_id}/devices",
            parameters,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )
        if cancel_requested and cancel_requested():
            raise NcentralOperationCancelled("N-central preview was cancelled")
        bounded_enrichment = max(0, min(int(enrich_limit), 250))
        prioritized = {
            str(value).strip()
            for value in (priority_external_ids or ())
            if str(value).strip().isdigit()
        }
        ordered_indices: list[int] = [
            index
            for index, row in enumerate(rows)
            if _first_text([row], "deviceId", maximum=160) in prioritized
        ]
        if rows:
            rotation_start = max(0, int(enrichment_offset)) % len(rows)
            ordered_indices.extend(
                index
                for index in (
                    list(range(rotation_start, len(rows))) + list(range(0, rotation_start))
                )
                if index not in ordered_indices
            )
        enrichment_targets = [
            (index, _first_text([rows[index]], "deviceId", maximum=160))
            for index in ordered_indices[:bounded_enrichment]
            if _first_text([rows[index]], "deviceId", maximum=160).isdigit()
        ]
        full_enrichment = bounded_enrichment >= 250
        active_issues_by_device: dict[str, list[dict[str, Any]]] = {}
        if full_enrichment:
            try:
                active_issues = self._paged(
                    f"/api/org-units/{parent_id}/active-issues",
                    {"sortBy": "deviceId", "sortOrder": "asc"},
                    max_pages=10,
                    cancel_requested=cancel_requested,
                )
            except NcentralRequestError as error:
                LOGGER.warning(
                    "Skipped optional N-central active-issue enrichment (HTTP status %s)",
                    error.status_code or "unavailable",
                )
            else:
                for issue in active_issues:
                    device_id = _first_text([issue], "deviceId", maximum=160)
                    if device_id:
                        active_issues_by_device.setdefault(device_id, []).append(issue)
        asset_payloads: dict[int, dict[str, Any]] = {}
        if enrichment_targets:
            worker_count = min(MAX_ASSET_ENRICHMENT_WORKERS, len(enrichment_targets))
            completed = 0
            succeeded = 0
            failed = 0
            target_iterator = iter(enrichment_targets)
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="ncentral-asset-read",
            ) as executor:
                pending: dict[Future[Any], int] = {}
                unavailable_optional_endpoints: set[str] = set()
                unavailable_optional_endpoints_lock = Lock()
                lifecycle_read_gate = Semaphore(1)

                def read_enrichment(device_id: str) -> dict[str, Any]:
                    """Read required assets plus optional full-mode operational detail."""

                    payload = self._get(f"/api/devices/{int(device_id)}/assets")
                    if not isinstance(payload, dict) or not full_enrichment:
                        return payload
                    extra = _ci_get(payload, "_extra", "extra")
                    combined_extra = dict(extra) if isinstance(extra, dict) else {}
                    optional_endpoints = (
                        ("lifecycleInfo", "/assets/lifecycle-info"),
                        ("serviceMonitorStatus", "/service-monitor-status"),
                        ("maintenanceWindows", "/maintenance-windows"),
                    )
                    for section_name, suffix in optional_endpoints:
                        with unavailable_optional_endpoints_lock:
                            if section_name in unavailable_optional_endpoints:
                                continue
                        try:
                            endpoint_path = f"/api/devices/{int(device_id)}{suffix}"
                            if section_name == "lifecycleInfo":
                                # N-central documents a concurrency limit of one for
                                # lifecycle reads. Keep the wider asset worker pool,
                                # but serialize only this constrained endpoint.
                                with lifecycle_read_gate:
                                    combined_extra[section_name] = self._get(endpoint_path)
                            else:
                                combined_extra[section_name] = self._get(endpoint_path)
                        except NcentralRequestError as error:
                            if error.status_code in {403, 404, 429}:
                                with unavailable_optional_endpoints_lock:
                                    unavailable_optional_endpoints.add(section_name)
                            LOGGER.warning(
                                "Skipped optional N-central %s enrichment (HTTP status %s)",
                                section_name,
                                error.status_code or "unavailable",
                            )
                    issues = active_issues_by_device.get(device_id)
                    if issues:
                        combined_extra["activeIssues"] = {"list": issues}
                    return {**payload, "_extra": combined_extra}

                def submit_next() -> bool:
                    try:
                        index, device_id = next(target_iterator)
                    except StopIteration:
                        return False
                    pending[executor.submit(read_enrichment, device_id)] = index
                    return True

                for _index in range(worker_count):
                    submit_next()
                if progress_callback:
                    progress_callback(
                        {
                            "phase": "enriching",
                            "current": 0,
                            "total": len(enrichment_targets),
                            "discovered": len(rows),
                            "enriched": 0,
                            "failed": 0,
                        }
                    )
                while pending:
                    if cancel_requested and cancel_requested():
                        for future in pending:
                            future.cancel()
                        raise NcentralOperationCancelled("N-central preview was cancelled")
                    completed_futures, _pending_futures = wait(
                        pending,
                        timeout=0.5,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed_futures:
                        continue
                    for future in completed_futures:
                        index = pending.pop(future)
                        try:
                            payload = future.result()
                        except NcentralRequestError as error:
                            failed += 1
                            LOGGER.warning(
                                "Skipped N-central asset enrichment after a safe provider "
                                "failure (device index %s, HTTP status %s)",
                                index,
                                error.status_code or "unavailable",
                            )
                        else:
                            if isinstance(payload, dict):
                                asset_payloads[index] = payload
                                succeeded += 1
                            else:
                                failed += 1
                        completed += 1
                        if progress_callback:
                            progress_callback(
                                {
                                    "phase": "enriching",
                                    "current": completed,
                                    "total": len(enrichment_targets),
                                    "discovered": len(rows),
                                    "enriched": succeeded,
                                    "failed": failed,
                                }
                            )
                        if not (cancel_requested and cancel_requested()):
                            submit_next()

        records: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            records.append(normalize_device(row, asset_payloads.get(index)))
        return records
