"""Extract bounded, provider-neutral technical inventory collections from CI records."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

COLLECTION_LIMITS = {
    "network_interfaces": 128,
    "processors": 32,
    "memory_modules": 256,
    "physical_disks": 128,
    "logical_volumes": 256,
    "applications": 500,
    "server_roles": 256,
    "patches": 500,
}
PROVIDER_COLLECTION_LIMITS = {
    "hardware": 1,
    "operating_system": 1,
    "network_interfaces": 128,
    "processors": 32,
    "memory_modules": 256,
    "physical_disks": 128,
    "volumes": 256,
    "software": 1,
    "server_roles": 256,
    "server_features": 256,
    "os_capabilities": 128,
    "monitoring": 1,
    "maintenance_windows": 256,
    "lifecycle": 1,
    "virtualization": 1,
}
SENSITIVE_KEYS = {
    "credential",
    "credentials",
    "executable",
    "executablename",
    "licensekey",
    "password",
    "remotecontroluri",
    "secret",
    "token",
    "useraccount",
}


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a mapping view or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    """Return object rows, wrapping safe scalar labels as named records."""

    if not isinstance(value, list):
        return []
    return [
        item if isinstance(item, Mapping) else {"name": item}
        for item in value
        if isinstance(item, (Mapping, str, int, float, bool))
    ]


def _safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively remove credential-like fields and empty values."""

    safe: dict[str, Any] = {}
    for raw_key, raw_value in row.items():
        key = str(raw_key)
        if key.casefold().replace("_", "") in SENSITIVE_KEYS:
            continue
        if raw_value in (None, ""):
            continue
        if isinstance(raw_value, Mapping):
            nested = _safe_row(raw_value)
            if nested:
                safe[key] = nested
        elif isinstance(raw_value, list):
            values = [
                _safe_row(item) if isinstance(item, Mapping) else deepcopy(item)
                for item in raw_value[:128]
            ]
            values = [item for item in values if item not in (None, "", {}, [])]
            if values:
                safe[key] = values
        elif isinstance(raw_value, (str, int, float, bool)):
            safe[key] = deepcopy(raw_value)
    return safe


def _bounded_collection(value: object, collection_type: str) -> list[dict[str, Any]]:
    """Sanitize and bound one inventory collection."""

    limit = COLLECTION_LIMITS[collection_type]
    return [safe for row in _rows(value)[:limit] if (safe := _safe_row(row))]


def _provider_inventory_collections(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return sanitized rich collections emitted by a provider normalizer."""

    raw_collections = record.get("inventoryCollections")
    if not isinstance(raw_collections, Mapping):
        return {}
    collections: dict[str, Any] = {}
    for collection_type, limit in PROVIDER_COLLECTION_LIMITS.items():
        if collection_type not in raw_collections:
            continue
        raw_value = raw_collections.get(collection_type)
        if isinstance(raw_value, Mapping):
            collections[collection_type] = _safe_row(raw_value)
        elif isinstance(raw_value, list):
            collections[collection_type] = [
                safe for row in _rows(raw_value)[:limit] if (safe := _safe_row(row))
            ]
    return collections


def technical_inventory_collections(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized collections suitable for versioned inventory persistence."""

    provider_collections = _provider_inventory_collections(record)
    if provider_collections:
        return provider_collections

    fields = _mapping(record.get("fields"))
    hardware = _mapping(fields.get("hardware"))
    network = _mapping(fields.get("network"))
    software = _mapping(fields.get("software"))
    operating_system = _mapping(fields.get("operatingSystem"))

    roles = (
        software.get("roles") or software.get("serverRoles") or operating_system.get("roles") or []
    )
    candidates = {
        "network_interfaces": (
            "interfaces" in network or "networkInterfaces" in fields,
            _bounded_collection(
                network.get("interfaces") or fields.get("networkInterfaces") or [],
                "network_interfaces",
            ),
        ),
        "processors": (
            "processors" in hardware or "processors" in fields,
            _bounded_collection(
                hardware.get("processors") or fields.get("processors") or [],
                "processors",
            ),
        ),
        "memory_modules": (
            "memoryModules" in hardware or "memoryModules" in fields,
            _bounded_collection(
                hardware.get("memoryModules") or fields.get("memoryModules") or [],
                "memory_modules",
            ),
        ),
        "physical_disks": (
            "physicalDisks" in hardware or "physicalDisks" in fields,
            _bounded_collection(
                hardware.get("physicalDisks") or fields.get("physicalDisks") or [],
                "physical_disks",
            ),
        ),
        "logical_volumes": (
            "logicalVolumes" in hardware or "logicalVolumes" in fields,
            _bounded_collection(
                hardware.get("logicalVolumes") or fields.get("logicalVolumes") or [],
                "logical_volumes",
            ),
        ),
        "applications": (
            "applications" in software or "applications" in fields,
            _bounded_collection(
                software.get("applications") or fields.get("applications") or [],
                "applications",
            ),
        ),
        "server_roles": (
            any(key in software for key in ("roles", "serverRoles")) or "roles" in operating_system,
            _bounded_collection(roles, "server_roles"),
        ),
        "patches": (
            "patches" in software or "patches" in fields,
            _bounded_collection(
                software.get("patches") or fields.get("patches") or [],
                "patches",
            ),
        ),
    }
    return {key: rows for key, (present, rows) in candidates.items() if present}
