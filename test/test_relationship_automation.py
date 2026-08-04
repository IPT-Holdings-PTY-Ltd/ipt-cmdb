"""Tests for opt-in relationship automation policy evaluation."""

import unittest
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


class RelationshipAutomationTests(unittest.TestCase):
    """Verify opt-in relationship automation policy decisions."""

    def test_explicit_repeated_fresh_identity_is_eligible(self) -> None:
        decision = relationship_auto_approval_decision(_candidate(), _policy(), now=NOW)

        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["reasons"], [])

    def test_review_only_policy_never_auto_approves(self) -> None:
        decision = relationship_auto_approval_decision(
            _candidate(),
            _policy(relationshipAutomationMode="review"),
            now=NOW,
        )

        self.assertFalse(decision["eligible"])
        self.assertIn("automation_not_enabled", decision["reasons"])

    def test_heuristic_or_single_observation_requires_review(self) -> None:
        candidate = _candidate(
            observationCount=1,
            evidence={"detector": {"key": "network_proximity", "version": 1}},
        )
        decision = relationship_auto_approval_decision(candidate, _policy(), now=NOW)

        self.assertFalse(decision["eligible"])
        self.assertIn("insufficient_observations", decision["reasons"])
        self.assertIn("detector_not_auto_approvable", decision["reasons"])

    def test_stale_or_unresolved_evidence_requires_review(self) -> None:
        candidate = _candidate(toCiId=None, lastSeenAt="2026-07-20T11:30:00Z")
        decision = relationship_auto_approval_decision(candidate, _policy(), now=NOW)

        self.assertFalse(decision["eligible"])
        self.assertIn("unresolved_endpoint", decision["reasons"])
        self.assertIn("evidence_too_old", decision["reasons"])
