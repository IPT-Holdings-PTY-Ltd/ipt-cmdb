"""Bounded, read-only client for the N-able Platform GraphQL API.

The provider accepts only static operations defined in :data:`QUERY_CATALOGUE`.
Callers supply variables, never GraphQL documents, which keeps the integration
read-only and prevents arbitrary queries from becoming an administrative API.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_GRAPHQL_ENDPOINT = "https://api.n-able.com/graphql"
MAX_GRAPHQL_PAGE_SIZE = 100
MAX_GRAPHQL_PAGES = 100
MAX_GRAPHQL_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CUSTOMER_SCOPE = 100
MAX_CUSTOMER_CATALOGUE_CANDIDATES = 500
MAX_GRAPHQL_ASSET_REFRESH_ITEMS = 10_000
MAX_PROVIDER_READ_ATTEMPTS = 3
MAX_PROVIDER_RETRY_DELAY_SECONDS = 30.0
PROVIDER_RETRY_BASE_DELAY_SECONDS = 0.25
PROVIDER_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent bearer-bearing POST requests from following provider redirects."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        del request, fp, code, msg, headers, newurl
        return None


def _open_without_redirect(request: Request, *, timeout: int):
    """Open one request without forwarding authorization across redirects."""

    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


class NableGraphqlConfigurationError(ValueError):
    """Report invalid GraphQL settings without exposing a bearer token."""


class NableGraphqlRequestError(RuntimeError):
    """Report a sanitized provider or response-contract failure."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _retry_after_seconds(error: HTTPError) -> int | None:
    """Return a non-negative delta-seconds Retry-After value when one is present."""

    try:
        value = int(str(error.headers.get("Retry-After") or "").strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0, value)


@dataclass(frozen=True)
class GraphqlQuery:
    """Describe one immutable, read-only provider operation."""

    key: str
    label: str
    description: str
    root_field: str
    document: str
    paginated: bool = True
    customer_scoped: bool = True


QUERY_CATALOGUE: dict[str, GraphqlQuery] = {
    "customer_catalogue": GraphqlQuery(
        key="customer_catalogue",
        label="Customer organizations",
        description="List token-visible organizations for explicit administrator mapping.",
        root_field="organizationSearch",
        customer_scoped=False,
        document="""
query IptCmdbCustomerCatalogue($first: Int, $after: String) {
  organizationSearch(first: $first, after: $after) {
    edges { cursor node { id name __typename } }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip(),
    ),
    "source_server_detection": GraphqlQuery(
        key="source_server_detection",
        label="N-central source servers",
        description=(
            "Detect N-central server and device identity pairs inside an explicitly saved "
            "Customer scope."
        ),
        root_field="assetSearch",
        document="""
query IptCmdbSourceServerDetection(
  $first: Int
  $after: String
  $organizationIds: [ID!]
) {
  assetSearch(
    first: $first
    after: $after
    inOrganizations: $organizationIds
    orderBy: [{ field: NAME, direction: ASC }]
  ) {
    totalCount
    nodes {
      customer { id }
      ncentralDevice { deviceId server { id } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip(),
    ),
    "asset_identity": GraphqlQuery(
        key="asset_identity",
        label="Asset identity",
        description="Read immutable GraphQL and N-central REST identity crosswalks.",
        root_field="assetSearch",
        document="""
query IptCmdbAssetIdentity(
  $first: Int
  $after: String
  $organizationIds: [ID!]
) {
  assetSearch(
    first: $first
    after: $after
    inOrganizations: $organizationIds
    orderBy: [{ field: NAME, direction: ASC }]
  ) {
    totalCount
    nodes {
      id
      name
      customer { id name }
      site { id name }
      serviceOrganization { id name }
      ncentralDevice { deviceId server { id } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip(),
    ),
    "asset_inventory": GraphqlQuery(
        key="asset_inventory",
        label="Asset inventory",
        description="Read bounded hardware, OS, CPU and agent-health enrichment.",
        root_field="assetSearch",
        document="""
query IptCmdbAssetInventory(
  $first: Int
  $after: String
  $organizationIds: [ID!]
) {
  assetSearch(
    first: $first
    after: $after
    inOrganizations: $organizationIds
    orderBy: [{ field: NAME, direction: ASC }]
  ) {
    totalCount
    nodes {
      id
      name
      description
      customer { id name }
      site { id name }
      serviceOrganization { id name }
      ncentralDevice { deviceId server { id } }
      operatingSystemInfo {
        name version type architecture buildNumber installedOn
      }
      agentConnection { status statusChangedAt }
      systemInfo {
        hostname manufacturer model serialNumber memoryTotalSizeBytes
      }
      cpu { name cores }
      chassis { types }
      bios { biosReleasedOn biosVersion smbiosBiosVersion version }
      memoryDevices {
        location manufacturer maxSpeedMts partNumber serialNumber sizeMb type
      }
      physicalDrives {
        description diskIndex name manufacturer model partitions serialNumber sizeBytes type
      }
      logicalDrives { driveId description totalSizeBytes fileSystem }
      motherboard { manufacturer model serialNumber version }
      networkInterfaces {
        name displayName description macAddress
        dhcp { serverIpAddress expiresAt }
        addresses { address mask type }
        dnsHostName dnsServers pciSlot linkSpeedMegabitsPerSecond
      }
      azureVmInstance { vmId region size subscriptionId }
      reboot { isRequired }
      vulnerabilityManagement { status lastUpdatedAt }
      lastBootedAt
      externalIpAddress
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip(),
    ),
    "patch_installations": GraphqlQuery(
        key="patch_installations",
        label="Patch installations",
        description="Read patch installation and reboot evidence separately from inventory.",
        root_field="patchInstallationSearch",
        document="""
query IptCmdbPatchInstallations(
  $first: Int
  $after: String
  $organizationIds: [ID!]
) {
  patchInstallationSearch(
    first: $first
    after: $after
    inOrganizations: $organizationIds
  ) {
    totalCount
    nodes {
      patch {
        id name patchId severity classification publishedOn isRebootRequired
      }
      asset {
        id name customer { id name } site { id name }
      }
      status
      lastUpdatedAt
      failureCount
      errorDetails { code message }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""".strip(),
    ),
}


def query_catalogue() -> list[dict[str, Any]]:
    """Return token-safe operation metadata without exposing query documents."""

    return [
        {
            "key": query.key,
            "label": query.label,
            "description": query.description,
            "paginated": query.paginated,
            "customerScoped": query.customer_scoped,
            "readOnly": True,
        }
        for query in QUERY_CATALOGUE.values()
    ]


def normalize_graphql_endpoint(value: str) -> str:
    """Accept only N-able's documented HTTPS GraphQL endpoint."""

    candidate = str(value or DEFAULT_GRAPHQL_ENDPOINT).strip().rstrip("/")
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError as error:
        raise NableGraphqlConfigurationError(
            "N-able GraphQL endpoint must be https://api.n-able.com/graphql"
        ) from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname != "api.n-able.com"
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/graphql"
    ):
        raise NableGraphqlConfigurationError(
            "N-able GraphQL endpoint must be https://api.n-able.com/graphql"
        )
    return DEFAULT_GRAPHQL_ENDPOINT


def normalize_customer_scope(values: Iterable[object]) -> list[str]:
    """Return a non-empty, bounded set of immutable GraphQL Customer IDs."""

    normalized_values: set[str] = set()
    for value in values:
        identifier = str(value or "").strip()
        if not identifier:
            continue
        if len(identifier) > 160 or any(
            character.isspace() or ord(character) < 32 for character in identifier
        ):
            raise NableGraphqlConfigurationError("GraphQL Customer organization ID is invalid")
        normalized_values.add(identifier)
    normalized = sorted(normalized_values)
    if not normalized:
        raise NableGraphqlConfigurationError(
            "Choose at least one explicitly mapped GraphQL Customer organization"
        )
    if len(normalized) > MAX_CUSTOMER_SCOPE:
        raise NableGraphqlConfigurationError(
            f"GraphQL Customer scope cannot exceed {MAX_CUSTOMER_SCOPE} organizations"
        )
    return normalized


def normalize_graphql_server_id(value: object) -> str:
    """Return an optional immutable N-central server ID without truncation."""

    identifier = str(value or "").strip()
    if identifier and (
        len(identifier) > 160
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in identifier
        )
    ):
        raise NableGraphqlConfigurationError("N-central GraphQL server ID is invalid")
    return identifier


def normalize_graphql_api_token(value: object, *, allow_empty: bool = False) -> str:
    """Validate a write-only GraphQL bearer before storage or request construction."""

    token = str(value or "")
    if allow_empty and not token:
        return ""
    if not 1 <= len(token) <= 12000 or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token
    ):
        raise NableGraphqlConfigurationError("N-able GraphQL API token is missing or invalid")
    return token


def _bounded_text(value: object, maximum: int) -> str:
    """Return a trimmed provider value within the storage boundary."""

    return str(value or "").strip()[:maximum]


def _provider_identifier(value: object, label: str) -> str:
    """Return an immutable provider ID without truncation or normalization collisions."""

    identifier = str(value or "").strip()
    if identifier and (
        len(identifier) > 160
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in identifier
        )
    ):
        raise NableGraphqlRequestError(f"N-able GraphQL returned an invalid {label}")
    return identifier


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a provider mapping or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _compact(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove empty values from one already allow-listed mapping."""

    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _bounded_string_list(value: object, *, limit: int, maximum: int) -> list[str]:
    """Return a deduplicated, bounded provider string list."""

    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(text for item in value[:limit] if (text := _bounded_text(item, maximum)))
    )


def _bounded_integer(value: object) -> int | None:
    """Return a non-negative integer provider value or ``None``."""

    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized >= 0 else None


def _organization(value: object) -> dict[str, str] | None:
    """Return one safe organization reference or ``None``."""

    if not isinstance(value, Mapping):
        return None
    identifier = _provider_identifier(value.get("id"), "organization ID")
    if not identifier:
        return None
    return {"id": identifier, "name": _bounded_text(value.get("name"), 240)}


def normalize_customer_candidate(node: Mapping[str, Any]) -> dict[str, str] | None:
    """Normalize one selectable Customer organization from organization search."""

    identifier = _provider_identifier(node.get("id"), "Customer organization ID")
    name = _bounded_text(node.get("name"), 240)
    type_name = _bounded_text(node.get("__typename"), 80)
    if not identifier or not name or type_name.casefold() != "customer":
        return None
    return {"id": identifier, "name": name, "typeName": type_name}


def normalize_asset(node: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize an allow-listed asset and its composite REST crosswalk."""

    graphql_asset_id = _provider_identifier(node.get("id"), "asset ID")
    if not graphql_asset_id:
        raise NableGraphqlRequestError("N-able GraphQL returned an asset without an ID")
    customer = _organization(node.get("customer"))
    if not customer:
        raise NableGraphqlRequestError(
            "N-able GraphQL returned an asset without a Customer organization"
        )
    raw_ncentral = node.get("ncentralDevice")
    ncentral: Mapping[str, Any] = raw_ncentral if isinstance(raw_ncentral, Mapping) else {}
    raw_server = ncentral.get("server")
    server: Mapping[str, Any] = raw_server if isinstance(raw_server, Mapping) else {}
    server_id = _provider_identifier(server.get("id"), "N-central server ID")
    device_id = _provider_identifier(ncentral.get("deviceId"), "N-central device ID")
    rest_identity = (
        {
            "provider": "ncentral",
            "namespace": "ncentral_rest_device",
            "serverId": server_id,
            "deviceId": device_id,
            "crosswalkKey": f"{server_id}:{device_id}",
        }
        if server_id and device_id
        else None
    )
    operating_system = _mapping(node.get("operatingSystemInfo"))
    system = _mapping(node.get("systemInfo"))
    agent = _mapping(node.get("agentConnection"))
    raw_cpu = node.get("cpu")
    cpu_rows = (
        raw_cpu
        if isinstance(raw_cpu, list)
        else ([raw_cpu] if isinstance(raw_cpu, Mapping) else [])
    )
    cpu = [
        _compact(
            {
                "name": _bounded_text(item.get("name"), 240),
                "cores": _bounded_integer(item.get("cores")),
            }
        )
        for item in cpu_rows[:32]
        if isinstance(item, Mapping)
    ]
    chassis = _mapping(node.get("chassis"))
    bios = _mapping(node.get("bios"))
    motherboard = _mapping(node.get("motherboard"))
    azure_vm = _mapping(node.get("azureVmInstance"))
    reboot = _mapping(node.get("reboot"))
    vulnerability = _mapping(node.get("vulnerabilityManagement"))
    raw_memory = node.get("memoryDevices")
    memory_rows = raw_memory if isinstance(raw_memory, list) else []
    memory_devices = [
        _compact(
            {
                "location": _bounded_text(item.get("location"), 240),
                "manufacturer": _bounded_text(item.get("manufacturer"), 160),
                "maxSpeedMts": _bounded_integer(item.get("maxSpeedMts")),
                "partNumber": _bounded_text(item.get("partNumber"), 160),
                "serialNumber": _bounded_text(item.get("serialNumber"), 160),
                "sizeMb": _bounded_integer(item.get("sizeMb")),
                "type": _bounded_text(item.get("type"), 120),
            }
        )
        for item in memory_rows[:256]
        if isinstance(item, Mapping)
    ]
    raw_physical = node.get("physicalDrives")
    physical_rows = raw_physical if isinstance(raw_physical, list) else []
    physical_drives = [
        _compact(
            {
                "description": _bounded_text(item.get("description"), 500),
                "diskIndex": _bounded_integer(item.get("diskIndex")),
                "name": _bounded_text(item.get("name"), 240),
                "manufacturer": _bounded_text(item.get("manufacturer"), 160),
                "model": _bounded_text(item.get("model"), 240),
                "partitions": _bounded_integer(item.get("partitions")),
                "serialNumber": _bounded_text(item.get("serialNumber"), 160),
                "sizeBytes": _bounded_integer(item.get("sizeBytes")),
                "type": _bounded_text(item.get("type"), 120),
            }
        )
        for item in physical_rows[:128]
        if isinstance(item, Mapping)
    ]
    raw_volumes = node.get("logicalDrives")
    volume_rows = raw_volumes if isinstance(raw_volumes, list) else []
    logical_drives = [
        _compact(
            {
                "driveId": _bounded_text(item.get("driveId"), 160),
                "description": _bounded_text(item.get("description"), 500),
                "totalSizeBytes": _bounded_integer(item.get("totalSizeBytes")),
                "fileSystem": _bounded_text(item.get("fileSystem"), 120),
            }
        )
        for item in volume_rows[:256]
        if isinstance(item, Mapping)
    ]
    raw_network = node.get("networkInterfaces")
    network_rows = raw_network if isinstance(raw_network, list) else []
    network_interfaces: list[dict[str, Any]] = []
    for item in network_rows[:128]:
        if not isinstance(item, Mapping):
            continue
        raw_addresses = item.get("addresses")
        address_rows = raw_addresses if isinstance(raw_addresses, list) else []
        addresses = [
            _compact(
                {
                    "address": _bounded_text(address.get("address"), 80),
                    "mask": _bounded_text(address.get("mask"), 80),
                    "type": _bounded_text(address.get("type"), 80),
                }
            )
            for address in address_rows[:64]
            if isinstance(address, Mapping)
        ]
        dhcp = _mapping(item.get("dhcp"))
        network_interfaces.append(
            _compact(
                {
                    "name": _bounded_text(item.get("name"), 240),
                    "displayName": _bounded_text(item.get("displayName"), 240),
                    "description": _bounded_text(item.get("description"), 500),
                    "macAddress": _bounded_text(item.get("macAddress"), 80),
                    "dhcp": _compact(
                        {
                            "serverIpAddress": _bounded_text(dhcp.get("serverIpAddress"), 80),
                            "expiresAt": _bounded_text(dhcp.get("expiresAt"), 80),
                        }
                    ),
                    "addresses": [item for item in addresses if item],
                    "dnsHostName": _bounded_text(item.get("dnsHostName"), 240),
                    "dnsServers": _bounded_string_list(
                        item.get("dnsServers"), limit=32, maximum=80
                    ),
                    "pciSlot": _bounded_text(item.get("pciSlot"), 120),
                    "linkSpeedMegabitsPerSecond": _bounded_integer(
                        item.get("linkSpeedMegabitsPerSecond")
                    ),
                }
            )
        )
    summary = {
        "description": _bounded_text(node.get("description"), 500),
        "operatingSystem": {
            key: value
            for key, value in {
                "name": _bounded_text(operating_system.get("name"), 240),
                "version": _bounded_text(operating_system.get("version"), 120),
                "type": _bounded_text(operating_system.get("type"), 120),
                "architecture": _bounded_text(operating_system.get("architecture"), 80),
                "buildNumber": _bounded_text(operating_system.get("buildNumber"), 120),
                "installedOn": _bounded_text(operating_system.get("installedOn"), 80),
            }.items()
            if value not in {None, ""}
        },
        "system": {
            key: value
            for key, value in {
                "hostname": _bounded_text(system.get("hostname"), 240),
                "manufacturer": _bounded_text(system.get("manufacturer"), 160),
                "model": _bounded_text(system.get("model"), 240),
                "serialNumber": _bounded_text(system.get("serialNumber"), 160),
                "memoryTotalSizeBytes": _bounded_integer(system.get("memoryTotalSizeBytes")),
            }.items()
            if value not in {None, ""}
        },
        "cpu": cpu,
        "chassis": _compact(
            {"types": _bounded_string_list(chassis.get("types"), limit=32, maximum=120)}
        ),
        "bios": _compact(
            {
                "biosReleasedOn": _bounded_text(bios.get("biosReleasedOn"), 80),
                "biosVersion": _bounded_text(bios.get("biosVersion"), 160),
                "smbiosBiosVersion": _bounded_text(bios.get("smbiosBiosVersion"), 160),
                "version": _bounded_text(bios.get("version"), 160),
            }
        ),
        "memoryDevices": [item for item in memory_devices if item],
        "physicalDrives": [item for item in physical_drives if item],
        "logicalDrives": [item for item in logical_drives if item],
        "motherboard": _compact(
            {
                "manufacturer": _bounded_text(motherboard.get("manufacturer"), 160),
                "model": _bounded_text(motherboard.get("model"), 240),
                "serialNumber": _bounded_text(motherboard.get("serialNumber"), 160),
                "version": _bounded_text(motherboard.get("version"), 120),
            }
        ),
        "networkInterfaces": [item for item in network_interfaces if item],
        "azureVmInstance": _compact(
            {
                "vmId": _bounded_text(azure_vm.get("vmId"), 160),
                "region": _bounded_text(azure_vm.get("region"), 120),
                "size": _bounded_text(azure_vm.get("size"), 120),
                "subscriptionId": _bounded_text(azure_vm.get("subscriptionId"), 160),
            }
        ),
        "reboot": (
            {"isRequired": bool(reboot.get("isRequired"))}
            if reboot.get("isRequired") is not None
            else {}
        ),
        "vulnerabilityManagement": _compact(
            {
                "status": _bounded_text(vulnerability.get("status"), 120),
                "lastUpdatedAt": _bounded_text(vulnerability.get("lastUpdatedAt"), 80),
            }
        ),
        "agent": {
            key: value
            for key, value in {
                "status": _bounded_text(agent.get("status"), 120),
                "statusChangedAt": _bounded_text(agent.get("statusChangedAt"), 80),
            }.items()
            if value
        },
        "lastBootedAt": _bounded_text(node.get("lastBootedAt"), 80),
        "externalIpAddress": _bounded_text(node.get("externalIpAddress"), 80),
    }
    return {
        "graphqlAssetId": graphql_asset_id,
        "name": _bounded_text(node.get("name"), 240),
        "customer": customer,
        "site": _organization(node.get("site")),
        "serviceOrganization": _organization(node.get("serviceOrganization")),
        "sourceIdentity": {
            "provider": "ncentral",
            "namespace": "nable_graphql_asset",
            "externalId": graphql_asset_id,
        },
        "restIdentity": rest_identity,
        "summary": {
            key: value for key, value in summary.items() if value not in (None, "", [], {})
        },
    }


def normalize_patch_installation(node: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one patch observation without merging it into base inventory."""

    patch = _mapping(node.get("patch"))
    asset = _mapping(node.get("asset"))
    patch_id = _provider_identifier(patch.get("id"), "patch ID")
    asset_id = _provider_identifier(asset.get("id"), "patch asset ID")
    customer = _organization(asset.get("customer"))
    if not patch_id or not asset_id or not customer:
        raise NableGraphqlRequestError(
            "N-able GraphQL returned patch evidence without immutable asset context"
        )
    source_id = hashlib.sha256(f"{asset_id}:{patch_id}".encode()).hexdigest()
    raw_errors = node.get("errorDetails")
    error_rows = raw_errors if isinstance(raw_errors, list) else []
    errors = [
        _compact(
            {
                "code": _bounded_text(item.get("code"), 120),
            }
        )
        for item in error_rows[:20]
        if isinstance(item, Mapping)
    ]
    return {
        "patchInstallationId": source_id,
        "sourceIdentity": {
            "provider": "ncentral",
            "namespace": "nable_graphql_patch_installation",
            "externalId": source_id,
        },
        "asset": {
            "id": asset_id,
            "name": _bounded_text(asset.get("name"), 240),
            "customer": customer,
            "site": _organization(asset.get("site")),
        },
        "patch": _compact(
            {
                "id": patch_id,
                "name": _bounded_text(patch.get("name"), 240),
                "patchId": _bounded_text(patch.get("patchId"), 160),
                "severity": _bounded_text(patch.get("severity"), 120),
                "classification": _bounded_text(patch.get("classification"), 160),
                "publishedOn": _bounded_text(patch.get("publishedOn"), 80),
                "isRebootRequired": (
                    bool(patch.get("isRebootRequired"))
                    if patch.get("isRebootRequired") is not None
                    else None
                ),
            }
        ),
        "status": _bounded_text(node.get("status"), 120),
        "lastUpdatedAt": _bounded_text(node.get("lastUpdatedAt"), 80),
        "failureCount": _bounded_integer(node.get("failureCount")),
        "errorDetails": [item for item in errors if item],
    }


def _graphql_inventory_collections(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Translate GraphQL summary groups into provider-neutral inventory collections."""

    system = _mapping(summary.get("system"))
    bios = _mapping(summary.get("bios"))
    chassis = _mapping(summary.get("chassis"))
    motherboard = _mapping(summary.get("motherboard"))
    hardware = _compact(
        {
            **dict(system),
            "bios": dict(bios),
            "chassis": dict(chassis),
            "motherboard": dict(motherboard),
        }
    )
    raw_network = summary.get("networkInterfaces")
    network_rows = raw_network if isinstance(raw_network, list) else []
    network = []
    for item in network_rows[:128]:
        if not isinstance(item, Mapping):
            continue
        raw_addresses = item.get("addresses")
        addresses = raw_addresses if isinstance(raw_addresses, list) else []
        network.append(
            _compact(
                {
                    **dict(item),
                    "interfaceKey": _bounded_text(
                        item.get("macAddress") or item.get("name") or item.get("displayName"),
                        255,
                    ),
                    "ipAddresses": [
                        address
                        for row in addresses[:64]
                        if isinstance(row, Mapping)
                        and (address := _bounded_text(row.get("address"), 80))
                    ],
                    "speedMbps": item.get("linkSpeedMegabitsPerSecond"),
                    "dhcpEnabled": bool(item.get("dhcp")) if "dhcp" in item else None,
                }
            )
        )
    raw_memory = summary.get("memoryDevices")
    memory_rows = raw_memory if isinstance(raw_memory, list) else []
    memory = []
    for item in memory_rows[:256]:
        if not isinstance(item, Mapping):
            continue
        size_mb = _bounded_integer(item.get("sizeMb"))
        memory.append(
            _compact(
                {
                    **dict(item),
                    "capacityBytes": size_mb * 1024 * 1024 if size_mb is not None else None,
                    "speedMts": item.get("maxSpeedMts"),
                }
            )
        )
    azure_vm = _mapping(summary.get("azureVmInstance"))
    virtualization = (
        _compact(
            {
                "kind": "virtual_machine",
                "platform": "Microsoft Azure",
                **dict(azure_vm),
                "evidence": ["nable_graphql_azure_vm_instance"],
            }
        )
        if azure_vm
        else {}
    )
    monitoring = _compact(
        {
            "agent": dict(_mapping(summary.get("agent"))),
            "reboot": dict(_mapping(summary.get("reboot"))),
            "vulnerabilityManagement": dict(_mapping(summary.get("vulnerabilityManagement"))),
        }
    )
    collections = {
        "hardware": hardware,
        "operating_system": dict(_mapping(summary.get("operatingSystem"))),
        "network_interfaces": [item for item in network if item],
        "processors": list(summary.get("cpu") or [])[:32],
        "memory_modules": [item for item in memory if item],
        "physical_disks": list(summary.get("physicalDrives") or [])[:128],
        "volumes": list(summary.get("logicalDrives") or [])[:256],
        "monitoring": monitoring,
        "virtualization": virtualization,
    }
    return {key: value for key, value in collections.items() if value not in (None, "", [], {})}


def _merge_inventory_collections(existing: object, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge non-empty GraphQL groups while retaining unrelated REST collections."""

    merged = deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
    for key, value in incoming.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**deepcopy(dict(merged[key])), **deepcopy(dict(value))}
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_graphql_enrichment(
    rest_records: Iterable[Mapping[str, Any]],
    graphql_assets: Iterable[Mapping[str, Any]],
    *,
    server_id: str,
) -> list[dict[str, Any]]:
    """Merge GraphQL summaries into REST records by server ID plus device ID only."""

    selected_server = str(server_id or "").strip()
    if not selected_server:
        return [deepcopy(dict(record)) for record in rest_records]
    crosswalk: dict[str, Mapping[str, Any]] = {}
    conflicts: set[str] = set()
    for asset in graphql_assets:
        identity = asset.get("restIdentity")
        if (
            not isinstance(identity, Mapping)
            or str(identity.get("serverId") or "") != selected_server
        ):
            continue
        device_id = str(identity.get("deviceId") or "").strip()
        if not device_id:
            continue
        if device_id in crosswalk:
            conflicts.add(device_id)
        else:
            crosswalk[device_id] = asset
    for conflict in conflicts:
        crosswalk.pop(conflict, None)
    merged: list[dict[str, Any]] = []
    for raw in rest_records:
        record = deepcopy(dict(raw))
        matched_asset = crosswalk.get(str(record.get("externalId") or ""))
        if matched_asset:
            fields = dict(record.get("fields") or {})
            metadata = dict(record.get("metadata") or {})
            identifiers = dict(record.get("identifiers") or {})
            graphql_summary = _mapping(matched_asset.get("summary"))
            system = _mapping(graphql_summary.get("system"))
            operating_system = _mapping(graphql_summary.get("operatingSystem"))

            def fill(target: dict[str, Any], key: str, value: Any) -> None:
                """Fill a canonical field only when REST did not already supply it."""

                if value not in (None, "", [], {}) and target.get(key) in (None, "", [], {}):
                    target[key] = deepcopy(value)

            fields["nableGraphqlAssetId"] = matched_asset.get("graphqlAssetId")
            fields["nableGraphqlServerId"] = selected_server
            manufacturer = _bounded_text(system.get("manufacturer"), 160)
            model = _bounded_text(system.get("model"), 240)
            serial_number = _bounded_text(system.get("serialNumber"), 160)
            operating_system_label = " ".join(
                value
                for value in (
                    _bounded_text(operating_system.get("name"), 240),
                    _bounded_text(operating_system.get("version"), 120),
                )
                if value
            )
            fill(fields, "manufacturer", manufacturer)
            fill(fields, "vendor", manufacturer)
            fill(fields, "model", model)
            fill(fields, "serialNumber", serial_number)
            fill(fields, "operatingSystem", operating_system_label)
            fill(metadata, "vendor", manufacturer)
            fill(metadata, "model", model)
            fill(metadata, "serialNumber", serial_number)
            fill(identifiers, "serial_number", serial_number)
            rest_fingerprint = (
                str(metadata.get("nableGraphqlRestFingerprint") or "")
                if "nableGraphqlRestFingerprint" in metadata
                else str(record.get("providerFingerprint") or record.get("providerVersion") or "")
            )
            metadata["nableGraphqlRestFingerprint"] = rest_fingerprint
            metadata["nableGraphql"] = deepcopy(graphql_summary)
            metadata["nableGraphqlSourceIdentity"] = deepcopy(
                matched_asset.get("sourceIdentity") or {}
            )
            inventory = _graphql_inventory_collections(graphql_summary)
            record["inventoryCollections"] = _merge_inventory_collections(
                record.get("inventoryCollections"), inventory
            )
            for collection_key, field_key in (
                ("processors", "processorCount"),
                ("memory_modules", "memoryModuleCount"),
                ("network_interfaces", "networkInterfaceCount"),
                ("physical_disks", "physicalDiskCount"),
                ("volumes", "volumeCount"),
            ):
                collection_value = inventory.get(collection_key)
                if isinstance(collection_value, list):
                    fields[field_key] = len(collection_value)
            graphql_virtualization = inventory.get("virtualization")
            if isinstance(graphql_virtualization, Mapping):
                current_virtualization = fields.get("virtualization")
                fields["virtualization"] = {
                    **(
                        deepcopy(dict(current_virtualization))
                        if isinstance(current_virtualization, Mapping)
                        else {}
                    ),
                    **deepcopy(dict(graphql_virtualization)),
                }
                fill(
                    metadata,
                    "virtualizationPlatform",
                    graphql_virtualization.get("platform"),
                )
            raw_coverage = metadata.get("sourceCoverage")
            coverage = deepcopy(dict(raw_coverage)) if isinstance(raw_coverage, Mapping) else {}
            observed_at = _bounded_text(
                matched_asset.get("observedAt") or graphql_summary.get("lastUpdatedAt"),
                80,
            )
            for collection_key, collection_value in inventory.items():
                if isinstance(collection_value, list):
                    record_count = len(collection_value)
                elif isinstance(collection_value, Mapping):
                    raw_count = collection_value.get("count")
                    record_count = (
                        max(0, int(raw_count)) if isinstance(raw_count, (int, float)) else 1
                    )
                else:
                    continue
                coverage[collection_key] = _compact(
                    {
                        "state": "available",
                        "recordCount": record_count,
                        "observedAt": observed_at,
                        "source": "nable_graphql",
                    }
                )
            metadata["sourceCoverage"] = coverage
            record["fields"] = fields
            record["metadata"] = metadata
            record["identifiers"] = identifiers
            fingerprint = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "rest": rest_fingerprint,
                            "graphqlAssetId": matched_asset.get("graphqlAssetId"),
                            "graphqlSummary": graphql_summary,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )
            record["providerFingerprint"] = fingerprint
            record["providerVersion"] = fingerprint
        merged.append(record)
    return merged


class NableGraphqlClient:
    """Execute static N-able GraphQL read operations with fail-closed scoping."""

    def __init__(
        self,
        configuration: Mapping[str, Any],
        *,
        opener: Callable[..., Any] = _open_without_redirect,
        timeout: int = 20,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.endpoint = normalize_graphql_endpoint(
            str(configuration.get("graphqlEndpoint") or DEFAULT_GRAPHQL_ENDPOINT)
        )
        self.api_token = normalize_graphql_api_token(configuration.get("graphqlApiToken"))
        try:
            page_size = int(configuration.get("graphqlPageSize") or MAX_GRAPHQL_PAGE_SIZE)
        except (TypeError, ValueError) as error:
            raise NableGraphqlConfigurationError(
                "N-able GraphQL page size must be a number"
            ) from error
        if not 1 <= page_size <= MAX_GRAPHQL_PAGE_SIZE:
            raise NableGraphqlConfigurationError(
                f"N-able GraphQL page size must be between 1 and {MAX_GRAPHQL_PAGE_SIZE}"
            )
        self.page_size = page_size
        self.timeout = max(5, min(int(timeout), 60))
        self.opener = opener
        self.sleeper = sleeper
        self.jitter = jitter

    def _open_json(self, request: Request) -> dict[str, Any]:
        """Execute one static read query with bounded transient HTTP retries."""

        payload: Any = None
        for attempt in range(MAX_PROVIDER_READ_ATTEMPTS):
            try:
                with self.opener(request, timeout=self.timeout) as response:  # nosec B310
                    raw = response.read(MAX_GRAPHQL_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_GRAPHQL_RESPONSE_BYTES:
                        raise NableGraphqlRequestError(
                            "N-able GraphQL response exceeded the size safety limit"
                        )
                    payload = json.loads(raw)
                    break
            except HTTPError as error:
                status = int(error.code)
                retry_after = _retry_after_seconds(error)
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
                raise NableGraphqlRequestError(
                    f"N-able GraphQL returned HTTP {status}", status_code=status
                ) from error
            except (URLError, TimeoutError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL could not be reached before timeout"
                ) from error
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned an invalid JSON response"
                ) from error
        else:
            raise AssertionError("N-able GraphQL retry loop exhausted without returning or raising")
        if not isinstance(payload, dict):
            raise NableGraphqlRequestError("N-able GraphQL returned an invalid response envelope")
        errors = payload.get("errors")
        if errors:
            raise NableGraphqlRequestError("N-able GraphQL rejected the allow-listed query")
        if not isinstance(payload.get("data"), Mapping):
            raise NableGraphqlRequestError("N-able GraphQL response is missing data")
        return payload

    def _execute(self, query_key: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        """Execute one named operation from the immutable read-only catalogue."""

        query = QUERY_CATALOGUE.get(query_key)
        if not query:
            raise NableGraphqlConfigurationError("Choose a supported N-able GraphQL query")
        try:
            request = Request(
                self.endpoint,
                data=json.dumps(
                    {
                        "operationName": query.document.split("(", 1)[0].split()[-1],
                        "query": query.document,
                        "variables": dict(variables),
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_token}",
                    "User-Agent": "IPT-CMDB/1.0 (read-only N-able GraphQL inventory)",
                },
                method="POST",
            )
        except (TypeError, ValueError) as error:
            raise NableGraphqlConfigurationError(
                "N-able GraphQL request configuration is invalid"
            ) from error
        payload = self._open_json(request)
        root = payload["data"].get(query.root_field)
        if not isinstance(root, Mapping):
            raise NableGraphqlRequestError(
                "N-able GraphQL response is missing the expected query result"
            )
        return root

    @staticmethod
    def _nodes(root: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """Support documented nodes and Relay edge wrappers without accepting scalars."""

        raw_nodes = root.get("nodes")
        if isinstance(raw_nodes, list):
            if any(not isinstance(item, Mapping) for item in raw_nodes):
                raise NableGraphqlRequestError("N-able GraphQL returned an invalid result node")
            return raw_nodes
        edges = root.get("edges")
        if not isinstance(edges, list):
            raise NableGraphqlRequestError("N-able GraphQL returned an invalid connection result")
        nodes: list[Mapping[str, Any]] = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            if not isinstance(node, Mapping):
                raise NableGraphqlRequestError("N-able GraphQL returned an invalid result edge")
            nodes.append(node)
        return nodes

    @staticmethod
    def _page_info(root: Mapping[str, Any]) -> tuple[bool, str | None]:
        """Return a validated forward-pagination state."""

        raw = root.get("pageInfo")
        if not isinstance(raw, Mapping):
            return False, None
        has_next = bool(raw.get("hasNextPage"))
        cursor = _bounded_text(raw.get("endCursor"), 1000) or None
        if has_next and not cursor:
            raise NableGraphqlRequestError(
                "N-able GraphQL pagination did not return a continuation cursor"
            )
        return has_next, cursor

    def customer_catalogue(self, *, limit: int = 100) -> dict[str, Any]:
        """Return a bounded token-visible Customer list for explicit selection."""

        maximum = max(1, min(int(limit), 500))
        candidates: list[dict[str, str]] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_GRAPHQL_PAGES):
            root = self._execute(
                "customer_catalogue",
                {"first": min(self.page_size, maximum - len(candidates)), "after": after},
            )
            for node in self._nodes(root):
                candidate = normalize_customer_candidate(node)
                if candidate and candidate["id"] not in {item["id"] for item in candidates}:
                    candidates.append(candidate)
                    if len(candidates) >= maximum:
                        break
            has_next, cursor = self._page_info(root)
            if len(candidates) >= maximum or not has_next:
                return {"customers": candidates, "truncated": bool(has_next)}
            if not cursor or cursor in seen_cursors:
                raise NableGraphqlRequestError("N-able GraphQL pagination cursor repeated")
            seen_cursors.add(cursor)
            after = cursor
        raise NableGraphqlRequestError(
            f"N-able GraphQL organization search exceeded the {MAX_GRAPHQL_PAGES}-page safety limit"
        )

    def source_server_candidates(
        self,
        *,
        organization_ids: Iterable[object],
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return only N-central server/device identities from a saved Customer scope.

        Device IDs remain internal to the backend so it can corroborate a candidate
        against the mapped REST customer. API routes must aggregate this result and
        never return the raw device-ID sets to the browser.
        """

        scope = normalize_customer_scope(organization_ids)
        allowed = set(scope)
        maximum = max(1, min(int(limit), 500))
        device_ids_by_server: dict[str, set[str]] = {}
        after: str | None = None
        seen_cursors: set[str] = set()
        inspected_assets = 0
        total_count = 0
        truncated = False
        for _page in range(MAX_GRAPHQL_PAGES):
            remaining = maximum - inspected_assets
            root = self._execute(
                "source_server_detection",
                {
                    "first": min(self.page_size, remaining),
                    "after": after,
                    "organizationIds": scope,
                },
            )
            try:
                total_count = max(total_count, int(root.get("totalCount") or 0))
            except (TypeError, ValueError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned an invalid total count"
                ) from error
            nodes = self._nodes(root)
            if len(nodes) > remaining:
                nodes = nodes[:remaining]
                truncated = True
            for node in nodes:
                customer = _mapping(node.get("customer"))
                customer_id = _provider_identifier(customer.get("id"), "Customer organization ID")
                if not customer_id or customer_id not in allowed:
                    raise NableGraphqlRequestError(
                        "N-able GraphQL returned a source identity outside the saved Customer scope"
                    )
                inspected_assets += 1
                ncentral = _mapping(node.get("ncentralDevice"))
                server = _mapping(ncentral.get("server"))
                server_id = _provider_identifier(server.get("id"), "N-central server ID")
                device_id = _provider_identifier(ncentral.get("deviceId"), "N-central device ID")
                if server_id and device_id:
                    device_ids_by_server.setdefault(server_id, set()).add(device_id)
            has_next, cursor = self._page_info(root)
            if inspected_assets >= maximum:
                truncated = truncated or has_next or total_count > inspected_assets
                break
            if not has_next:
                truncated = truncated or total_count > inspected_assets
                break
            if not cursor or cursor in seen_cursors:
                raise NableGraphqlRequestError("N-able GraphQL pagination cursor repeated")
            seen_cursors.add(cursor)
            after = cursor
        else:
            raise NableGraphqlRequestError(
                f"N-able GraphQL source-server detection exceeded the "
                f"{MAX_GRAPHQL_PAGES}-page safety limit"
            )
        return {
            "organizationIds": scope,
            "totalCount": max(total_count, inspected_assets),
            "inspectedAssetCount": inspected_assets,
            "candidates": [
                {
                    "serverId": server_id,
                    "deviceIds": sorted(device_ids),
                    "graphqlDeviceCount": len(device_ids),
                }
                for server_id, device_ids in sorted(device_ids_by_server.items())
            ],
            "truncated": truncated,
        }

    def assets(
        self,
        query_key: str,
        *,
        organization_ids: Iterable[object],
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read assets within saved Customer IDs and verify every returned boundary."""

        query = QUERY_CATALOGUE.get(query_key)
        if query_key not in {"asset_identity", "asset_inventory"} or not query:
            raise NableGraphqlConfigurationError("Choose a supported asset query")
        scope = normalize_customer_scope(organization_ids)
        allowed = set(scope)
        maximum = max(1, min(int(limit), 10000))
        assets: list[dict[str, Any]] = []
        seen_assets: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        total_count = 0
        for _page in range(MAX_GRAPHQL_PAGES):
            root = self._execute(
                query_key,
                {
                    "first": min(self.page_size, maximum - len(assets)),
                    "after": after,
                    "organizationIds": scope,
                },
            )
            try:
                total_count = max(total_count, int(root.get("totalCount") or 0))
            except (TypeError, ValueError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned an invalid total count"
                ) from error
            for node in self._nodes(root):
                asset = normalize_asset(node)
                if asset["customer"]["id"] not in allowed:
                    raise NableGraphqlRequestError(
                        "N-able GraphQL returned an asset outside the saved Customer scope"
                    )
                if asset["graphqlAssetId"] not in seen_assets:
                    seen_assets.add(asset["graphqlAssetId"])
                    assets.append(asset)
                if len(assets) >= maximum:
                    break
            has_next, cursor = self._page_info(root)
            if len(assets) >= maximum or not has_next:
                return {
                    "queryKey": query_key,
                    "organizationIds": scope,
                    "totalCount": max(total_count, len(assets)),
                    "items": assets,
                    "truncated": bool(has_next),
                }
            if not cursor or cursor in seen_cursors:
                raise NableGraphqlRequestError("N-able GraphQL pagination cursor repeated")
            seen_cursors.add(cursor)
            after = cursor
        raise NableGraphqlRequestError(
            f"N-able GraphQL asset search exceeded the {MAX_GRAPHQL_PAGES}-page safety limit"
        )

    def full_asset_inventory(
        self,
        *,
        organization_ids: Iterable[object],
        maximum: int = MAX_GRAPHQL_ASSET_REFRESH_ITEMS,
    ) -> dict[str, Any]:
        """Read a complete, bounded asset-inventory snapshot for cache refresh.

        This operation is intentionally separate from :meth:`assets`, whose
        caller-selected ``limit`` remains suitable for interactive previews.
        Full refresh follows the provider cursor until the saved Customer scope
        is exhausted or the hard asset cap is reached.  No partial result is
        returned when a later page fails or when ``totalCount`` proves that the
        provider stopped before all reported rows were inspected.
        """

        scope = normalize_customer_scope(organization_ids)
        allowed = set(scope)
        try:
            bounded_maximum = int(maximum)
        except (TypeError, ValueError) as error:
            raise NableGraphqlConfigurationError(
                "N-able GraphQL asset refresh maximum must be a number"
            ) from error
        if bounded_maximum < 1:
            raise NableGraphqlConfigurationError(
                "N-able GraphQL asset refresh maximum must be at least 1"
            )
        bounded_maximum = min(bounded_maximum, MAX_GRAPHQL_ASSET_REFRESH_ITEMS)

        assets: list[dict[str, Any]] = []
        seen_assets: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        total_count = 0
        inspected_count = 0
        # The configured page size may be as small as one.  Derive the page
        # budget from the hard item cap so a valid small-page configuration can
        # still complete without creating an unbounded provider loop.
        page_budget = (bounded_maximum + self.page_size - 1) // self.page_size + 1

        for pages_read in range(1, page_budget + 1):
            remaining = bounded_maximum - len(assets)
            if remaining <= 0:
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": scope,
                    "totalCount": max(total_count, inspected_count),
                    "items": assets,
                    "truncated": total_count > inspected_count,
                    "pagesRead": pages_read,
                }
            root = self._execute(
                "asset_inventory",
                {
                    "first": min(self.page_size, remaining),
                    "after": after,
                    "organizationIds": scope,
                },
            )
            try:
                total_count = max(total_count, int(root.get("totalCount") or 0))
            except (TypeError, ValueError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned an invalid total count"
                ) from error
            nodes = self._nodes(root)
            if len(nodes) > min(self.page_size, remaining):
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned more asset rows than requested"
                )
            inspected_count += len(nodes)
            for node in nodes:
                asset = normalize_asset(node)
                if asset["customer"]["id"] not in allowed:
                    raise NableGraphqlRequestError(
                        "N-able GraphQL returned an asset outside the saved Customer scope"
                    )
                asset_id = asset["graphqlAssetId"]
                if asset_id not in seen_assets:
                    seen_assets.add(asset_id)
                    assets.append(asset)

            has_next, cursor = self._page_info(root)
            if len(assets) >= bounded_maximum:
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": scope,
                    "totalCount": max(total_count, inspected_count),
                    "items": assets,
                    "truncated": bool(has_next or total_count > inspected_count),
                    "pagesRead": pages_read,
                }
            if not has_next:
                if total_count > inspected_count:
                    raise NableGraphqlRequestError(
                        "N-able GraphQL ended asset pagination before all reported rows were read"
                    )
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": scope,
                    "totalCount": max(total_count, inspected_count),
                    "items": assets,
                    "truncated": False,
                    "pagesRead": pages_read,
                }
            if not cursor or cursor in seen_cursors:
                raise NableGraphqlRequestError("N-able GraphQL pagination cursor repeated")
            seen_cursors.add(cursor)
            after = cursor

        raise NableGraphqlRequestError(
            "N-able GraphQL asset refresh exceeded its bounded pagination safety limit"
        )

    def patch_installations(
        self,
        *,
        organization_ids: Iterable[object],
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read patch evidence separately and enforce every returned Customer boundary."""

        scope = normalize_customer_scope(organization_ids)
        allowed = set(scope)
        maximum = max(1, min(int(limit), 10000))
        installations: list[dict[str, Any]] = []
        seen_installations: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        total_count = 0
        for _page in range(MAX_GRAPHQL_PAGES):
            root = self._execute(
                "patch_installations",
                {
                    "first": min(self.page_size, maximum - len(installations)),
                    "after": after,
                    "organizationIds": scope,
                },
            )
            try:
                total_count = max(total_count, int(root.get("totalCount") or 0))
            except (TypeError, ValueError) as error:
                raise NableGraphqlRequestError(
                    "N-able GraphQL returned an invalid total count"
                ) from error
            for node in self._nodes(root):
                installation = normalize_patch_installation(node)
                if installation["asset"]["customer"]["id"] not in allowed:
                    raise NableGraphqlRequestError(
                        "N-able GraphQL returned patch evidence outside the saved Customer scope"
                    )
                identity = installation["patchInstallationId"]
                if identity not in seen_installations:
                    seen_installations.add(identity)
                    installations.append(installation)
                if len(installations) >= maximum:
                    break
            has_next, cursor = self._page_info(root)
            if len(installations) >= maximum or not has_next:
                return {
                    "queryKey": "patch_installations",
                    "organizationIds": scope,
                    "totalCount": max(total_count, len(installations)),
                    "items": installations,
                    "truncated": bool(has_next),
                }
            if not cursor or cursor in seen_cursors:
                raise NableGraphqlRequestError("N-able GraphQL pagination cursor repeated")
            seen_cursors.add(cursor)
            after = cursor
        raise NableGraphqlRequestError(
            f"N-able GraphQL patch search exceeded the {MAX_GRAPHQL_PAGES}-page safety limit"
        )

    def test_connection(self) -> dict[str, Any]:
        """Verify the bearer token and return only bounded organization candidates."""

        result = self.customer_catalogue(limit=MAX_CUSTOMER_CATALOGUE_CANDIDATES)
        return {
            "reachable": True,
            "customerCandidates": result["customers"],
            "candidateCount": len(result["customers"]),
            "truncated": result["truncated"],
        }
