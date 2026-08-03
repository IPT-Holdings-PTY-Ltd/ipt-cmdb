"""Tests for opt-in relationship automation policy evaluation."""

from datetime import UTC, datetime

from src.cmdb.relationship_automation import relationship_auto_approval_decision

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _candidate(**overrides: object) -> dict:
    candidate = {
        "fromCiId": "host",
        "toCiId": "guest",
        "relationshipType": "hosts",
        "confidence": 0.99,
        "state": "pending",
        "retiredAt": None,
        "lastSeenAt": "2026-07-31T11:30:00Z",
        "observationCount": 2,
        "evidence": {
            "detector": {
                "key": "ncentral_explicit_virtualization_identity",
                "version": 1,
            }
        },
    }
    candidate.update(overrides)
    return candidate


def _policy(**overrides: object) -> dict:
    policy = {
        "relationshipAutomationMode": "auto_explicit",
        "relationshipAutoApproveTypes": ["hosts"],
        "relationshipMinConfidence": 0.98,
        "relationshipMinObservations": 2,
        "relationshipMaxEvidenceAgeHours": 72,
    }
    policy.update(overrides)
    return policy


def test_explicit_repeated_fresh_identity_is_eligible() -> None:
    decision = relationship_auto_approval_decision(_candidate(), _policy(), now=NOW)

    assert decision["eligible"] is True
    assert decision["reasons"] == []


def test_review_only_policy_never_auto_approves() -> None:
    decision = relationship_auto_approval_decision(
        _candidate(),
        _policy(relationshipAutomationMode="review"),
        now=NOW,
    )

    assert decision["eligible"] is False
    assert "automation_not_enabled" in decision["reasons"]


def test_heuristic_or_single_observation_requires_review() -> None:
    candidate = _candidate(
        observationCount=1,
        evidence={"detector": {"key": "network_proximity", "version": 1}},
    )
    decision = relationship_auto_approval_decision(candidate, _policy(), now=NOW)

    assert decision["eligible"] is False
    assert "insufficient_observations" in decision["reasons"]
    assert "detector_not_auto_approvable" in decision["reasons"]


def test_stale_or_unresolved_evidence_requires_review() -> None:
    candidate = _candidate(toCiId=None, lastSeenAt="2026-07-20T11:30:00Z")
    decision = relationship_auto_approval_decision(candidate, _policy(), now=NOW)

    assert decision["eligible"] is False
    assert "unresolved_endpoint" in decision["reasons"]
    assert "evidence_too_old" in decision["reasons"]
