"""Tests for review-only provider relationship candidate generation."""

from src.cmdb.ncentral import normalize_device
from src.cmdb.relationship_candidates import build_relationship_candidates


def _mapping(external_id: str, asset_id: str) -> dict:
    return {
        "id": f"mapping-{external_id}",
        "externalId": external_id,
        "assetId": asset_id,
        "active": True,
    }


def test_explicit_host_identity_builds_directional_candidate() -> None:
    records = [
        {
            "externalId": "guest-1",
            "fields": {
                "virtualization": {
                    "role": "virtual_machine",
                    "hostExternalId": "host-1",
                    "hostConfidence": 0.99,
                    "hostEvidence": ["MSVM host identity matched the guest UUID"],
                }
            },
        }
    ]

    grouped = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=records,
        mappings=[
            _mapping("guest-1", "asset-guest"),
            _mapping("host-1", "asset-host"),
        ],
    )

    candidate = grouped["guest-1"][0]
    assert candidate["fromCiId"] == "asset-host"
    assert candidate["toCiId"] == "asset-guest"
    assert candidate["relationshipType"] == "hosts"
    assert candidate["confidence"] == 0.99


def test_normalized_ncentral_kind_builds_explicit_virtualization_candidate() -> None:
    record = normalize_device(
        {
            "deviceId": 19,
            "longName": "APP-19",
            "deviceClass": "WindowsServer",
        },
        {
            "data": {
                "virtualization": {
                    "role": "Virtual Machine",
                    "hostDeviceId": 7,
                }
            }
        },
    )

    grouped = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=[record],
        mappings=[
            _mapping("19", "asset-guest"),
            _mapping("7", "asset-host"),
        ],
    )

    candidate = grouped["19"][0]
    assert record["fields"]["virtualization"]["kind"] == "virtual_machine"
    assert candidate["fromCiId"] == "asset-host"
    assert candidate["toCiId"] == "asset-guest"
    assert candidate["evidence"]["detector"] == "ncentral_explicit_virtualization_identity"
    assert candidate["evidence"]["evidenceRevision"] == 1
    assert len(candidate["evidence"]["evidenceFingerprint"]) == 64


def test_names_and_network_proximity_never_create_relationships() -> None:
    records = [
        {
            "externalId": "device-1",
            "name": "Same subnet device",
            "fields": {
                "network": {
                    "interfaces": [
                        {"ipAddress": "10.0.0.10", "gateway": "10.0.0.1"},
                    ]
                }
            },
        }
    ]

    grouped = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=records,
        mappings=[_mapping("device-1", "asset-1")],
    )

    assert grouped == {"device-1": []}


def test_unmapped_or_existing_edges_are_not_queued() -> None:
    records = [
        {
            "externalId": "host-1",
            "fields": {
                "relationshipHints": [
                    {
                        "relationshipType": "hosts",
                        "targetExternalId": "guest-1",
                        "confidence": 1,
                    },
                    {
                        "relationshipType": "hosts",
                        "targetExternalId": "missing-guest",
                        "confidence": 1,
                    },
                ]
            },
        }
    ]

    grouped = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=records,
        mappings=[
            _mapping("host-1", "asset-host"),
            _mapping("guest-1", "asset-guest"),
        ],
        existing_relationships=[
            {"fromId": "asset-host", "toId": "asset-guest", "type": "hosts"},
        ],
    )

    assert grouped == {"host-1": []}


def test_candidate_key_is_stable_and_evidence_is_bounded() -> None:
    record = {
        "externalId": "host-1",
        "fields": {
            "relationshipHints": [
                {
                    "relationshipType": "hosts",
                    "targetExternalId": "guest-1",
                    "confidence": "0.95",
                    "evidence": [f"explicit guest UUID {index}" for index in range(20)],
                }
            ]
        },
    }
    mappings = [
        _mapping("host-1", "asset-host"),
        _mapping("guest-1", "asset-guest"),
    ]

    first = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=[record],
        mappings=mappings,
    )["host-1"][0]
    second = build_relationship_candidates(
        provider="ncentral",
        company_id="acme",
        records=[record],
        mappings=mappings,
    )["host-1"][0]

    assert first["candidateKey"] == second["candidateKey"]
    assert len(first["evidence"]["messages"]) == 12


def test_evidence_fingerprint_is_order_independent_but_revision_sensitive() -> None:
    def candidate(messages: list[str], revision: int = 1) -> dict:
        record = {
            "externalId": "host-1",
            "fields": {
                "relationshipHints": [
                    {
                        "relationshipType": "hosts",
                        "targetExternalId": "guest-1",
                        "confidence": 0.95,
                        "detector": "ncentral_explicit_virtualization_identity",
                        "evidenceRevision": revision,
                        "evidence": messages,
                    }
                ]
            },
        }
        return build_relationship_candidates(
            provider="ncentral",
            company_id="acme",
            records=[record],
            mappings=[
                _mapping("host-1", "asset-host"),
                _mapping("guest-1", "asset-guest"),
            ],
        )["host-1"][0]

    first = candidate(["explicit guest UUID", "inventory object matched"])
    reordered = candidate(
        ["inventory object matched", "explicit guest UUID", "explicit guest UUID"]
    )
    revised = candidate(["explicit guest UUID", "inventory object matched"], revision=2)

    assert first["candidateKey"] == reordered["candidateKey"] == revised["candidateKey"]
    assert first["evidence"]["messages"] == reordered["evidence"]["messages"]
    assert first["evidence"]["evidenceFingerprint"] == reordered["evidence"]["evidenceFingerprint"]
    assert revised["evidence"]["evidenceFingerprint"] != first["evidence"]["evidenceFingerprint"]
