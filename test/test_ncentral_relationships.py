"""Tests for review-only relationships derived from N-central inventory."""

from src.cmdb.ncentral_relationships import add_ncentral_network_dependency_hints
from src.cmdb.relationship_candidates import build_relationship_candidates


def _record(external_id: str, interface: dict) -> dict:
    return {
        "externalId": external_id,
        "fields": {},
        "inventoryCollections": {"network_interfaces": [interface]},
    }


def _mapping(external_id: str, asset_id: str) -> dict:
    return {"externalId": external_id, "assetId": asset_id, "active": True}


def test_unique_configured_dns_and_dhcp_address_creates_one_review_hint() -> None:
    records = [
        _record(
            "client-1",
            {
                "ipAddresses": ["10.0.0.20"],
                "dnsServers": ["10.0.0.10"],
                "dhcp": {"serverIpAddress": "10.0.0.10"},
                "gateways": ["10.0.0.1"],
            },
        ),
        _record("infra-1", {"ipAddresses": ["10.0.0.10"]}),
    ]

    enriched = add_ncentral_network_dependency_hints(
        records,
        [_mapping("client-1", "client"), _mapping("infra-1", "infra")],
        [],
    )

    hints = enriched[0]["fields"]["relationshipHints"]
    assert len(hints) == 1
    assert hints[0]["targetExternalId"] == "infra-1"
    assert hints[0]["relationshipType"] == "provided_by"
    assert hints[0]["impactPolicy"] == "degraded"
    assert hints[0]["confidence"] == 0.82
    assert "DHCP and DNS" in hints[0]["evidence"][0]


def test_duplicate_address_or_gateway_alone_never_creates_a_hint() -> None:
    records = [
        _record(
            "client-1",
            {
                "ipAddresses": ["10.0.0.20"],
                "dnsServers": ["10.0.0.10"],
                "gateways": ["10.0.0.1"],
            },
        ),
        _record("infra-1", {"ipAddresses": ["10.0.0.10"]}),
        _record("infra-2", {"ipAddresses": ["10.0.0.10"]}),
        _record("router-1", {"ipAddresses": ["10.0.0.1"]}),
    ]

    enriched = add_ncentral_network_dependency_hints(
        records,
        [
            _mapping("client-1", "client"),
            _mapping("infra-1", "infra-1"),
            _mapping("infra-2", "infra-2"),
            _mapping("router-1", "router"),
        ],
        [],
    )

    assert "relationshipHints" not in enriched[0]["fields"]


def test_existing_canonical_primary_ip_can_resolve_a_mapped_target() -> None:
    records = [
        _record("client-1", {"dnsServers": ["10.0.0.10"]}),
    ]
    assets = [{"id": "infra", "fields": {"ipAddress": "10.0.0.10"}, "metadata": {}}]

    enriched = add_ncentral_network_dependency_hints(
        records,
        [_mapping("client-1", "client"), _mapping("infra-1", "infra")],
        assets,
    )

    assert enriched[0]["fields"]["relationshipHints"][0]["targetExternalId"] == "infra-1"


def test_exact_address_hint_enters_review_queue_but_is_not_explicit_identity() -> None:
    mappings = [_mapping("client-1", "client"), _mapping("infra-1", "infra")]
    records = add_ncentral_network_dependency_hints(
        [
            _record("client-1", {"dnsServers": ["10.0.0.10"]}),
            _record("infra-1", {"ipAddresses": ["10.0.0.10"]}),
        ],
        mappings,
        [],
    )

    grouped = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=records,
        mappings=mappings,
    )

    candidate = grouped["client-1"][0]
    assert candidate["fromCiId"] == "client"
    assert candidate["toCiId"] == "infra"
    assert candidate["relationshipType"] == "provided_by"
    assert candidate["confidence"] == 0.72
    assert candidate["evidence"]["impactPolicy"] == "degraded"
