"""Derive review-only N-central relationship hints from corroborated inventory."""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a mapping view or an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    """Return only mapping rows from a provider collection."""

    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _addresses(value: object) -> list[str]:
    """Normalize bounded IP address values without accepting subnet similarity."""

    values = value if isinstance(value, list) else [value]
    normalized: list[str] = []
    for raw in values[:64]:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0].split("/", 1)[0])
        except ValueError:
            continue
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            continue
        normalized.append(address.compressed.casefold())
    return normalized


def _network_interfaces(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read provider-neutral interface inventory from one normalized record."""

    collections = _mapping(record.get("inventoryCollections"))
    return _rows(collections.get("network_interfaces"))


def _asset_addresses(asset: Mapping[str, Any]) -> list[str]:
    """Return explicit primary addresses already retained on a canonical CI."""

    fields = _mapping(asset.get("fields"))
    metadata = _mapping(asset.get("metadata"))
    return _addresses(
        [
            fields.get("ipAddress"),
            metadata.get("ipAddress"),
        ]
    )


def _record_addresses(record: Mapping[str, Any]) -> list[str]:
    """Return every explicitly observed interface address for one source record."""

    return sorted(
        {
            address
            for interface in _network_interfaces(record)
            for address in _addresses(interface.get("ipAddresses"))
        }
    )


def _dependency_references(record: Mapping[str, Any]) -> dict[str, set[str]]:
    """Collect configured DNS and DHCP addresses without treating gateways as dependencies."""

    references: dict[str, set[str]] = defaultdict(set)
    for interface in _network_interfaces(record):
        for address in _addresses(interface.get("dnsServers")):
            references[address].add("DNS")
        for address in _addresses(interface.get("dhcpServer")):
            references[address].add("DHCP")
        dhcp = _mapping(interface.get("dhcp"))
        for address in _addresses(dhcp.get("serverIpAddress")):
            references[address].add("DHCP")
    return references


def add_ncentral_network_dependency_hints(
    records: Iterable[Mapping[str, Any]],
    mappings: Iterable[Mapping[str, Any]],
    assets: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add review-only DNS/DHCP hints when one IP identifies one mapped CI.

    The relationship remains a suggestion. Duplicate addresses, gateways,
    subnets, mutable names and unmapped endpoints never produce a hint.
    """

    source_records = [deepcopy(dict(record)) for record in records]
    external_by_asset = {
        str(mapping.get("assetId") or ""): str(mapping.get("externalId") or "")
        for mapping in mappings
        if mapping.get("active", True) and mapping.get("assetId") and mapping.get("externalId")
    }
    address_owners: dict[str, set[str]] = defaultdict(set)
    for asset in assets:
        external_id = external_by_asset.get(str(asset.get("id") or ""))
        if external_id:
            for address in _asset_addresses(asset):
                address_owners[address].add(external_id)
    for record in source_records:
        external_id = str(record.get("externalId") or "")
        if external_id:
            for address in _record_addresses(record):
                address_owners[address].add(external_id)

    unique_owner = {
        address: next(iter(external_ids))
        for address, external_ids in address_owners.items()
        if len(external_ids) == 1
    }
    for record in source_records:
        source_external_id = str(record.get("externalId") or "")
        if not source_external_id:
            continue
        dependencies: dict[str, dict[str, set[str]]] = {}
        for address, services in _dependency_references(record).items():
            target_external_id = unique_owner.get(address)
            if not target_external_id or target_external_id == source_external_id:
                continue
            target = dependencies.setdefault(
                target_external_id,
                {"addresses": set(), "services": set()},
            )
            target["addresses"].add(address)
            target["services"].update(services)
        if not dependencies:
            continue
        fields = deepcopy(dict(_mapping(record.get("fields"))))
        existing = fields.get("relationshipHints")
        hints = [deepcopy(dict(item)) for item in _rows(existing)]
        for target_external_id, evidence in sorted(dependencies.items()):
            sorted_services = sorted(evidence["services"])
            sorted_addresses = sorted(evidence["addresses"])
            hints.append(
                {
                    "relationshipType": "provided_by",
                    "sourceExternalId": source_external_id,
                    "targetExternalId": target_external_id,
                    "impactPolicy": "degraded",
                    "confidence": 0.82 if len(sorted_services) > 1 else 0.72,
                    "detector": {
                        "key": "ncentral_configured_infrastructure_address",
                        "version": 1,
                    },
                    "evidence": [
                        (
                            f"Configured {' and '.join(sorted_services)} server address "
                            f"{address} matched one mapped customer CI"
                        )
                        for address in sorted_addresses
                    ],
                }
            )
        fields["relationshipHints"] = hints
        record["fields"] = fields
    return source_records
