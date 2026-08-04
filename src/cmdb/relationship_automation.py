"""Evaluate guarded policies for provider-discovered CI relationships."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from src.cmdb.integration_reconciliation import normalize_ci_policy

EXPLICIT_RELATIONSHIP_DETECTORS = {
    "ncentral_explicit_virtualization_identity",
}


def _timestamp(value: object) -> datetime | None:
    """Parse a provider timestamp as an aware UTC datetime."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _detector_key(evidence: object) -> str:
    """Read a stable detector key from a versioned evidence envelope."""

    if not isinstance(evidence, Mapping):
        return ""
    detector = evidence.get("detector")
    if isinstance(detector, Mapping):
        return str(detector.get("key") or "").strip()
    return str(detector or "").strip()


def relationship_auto_approval_decision(
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explain whether one candidate satisfies an opt-in auto-approval policy.

    This evaluator deliberately accepts only allow-listed detectors that rely on
    immutable provider identities. It never creates a relationship itself.
    """

    normalized = normalize_ci_policy(policy)
    reasons: list[str] = []
    if normalized["relationshipAutomationMode"] != "auto_explicit":
        reasons.append("automation_not_enabled")
    if candidate.get("state") != "pending":
        reasons.append("candidate_not_pending")
    if candidate.get("retiredAt"):
        reasons.append("evidence_retired")
    if not candidate.get("fromCiId") or not candidate.get("toCiId"):
        reasons.append("unresolved_endpoint")

    relationship_type = str(candidate.get("relationshipType") or "")
    allowed_types = set(normalized["relationshipAutoApproveTypes"])
    if relationship_type not in allowed_types:
        reasons.append("relationship_type_not_allowed")

    try:
        confidence = float(candidate.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < float(normalized["relationshipMinConfidence"]):
        reasons.append("confidence_below_policy")

    try:
        observation_count = int(candidate.get("observationCount", 0))
    except (TypeError, ValueError):
        observation_count = 0
    if observation_count < int(normalized["relationshipMinObservations"]):
        reasons.append("insufficient_observations")

    detector_key = _detector_key(candidate.get("evidence"))
    if detector_key not in EXPLICIT_RELATIONSHIP_DETECTORS:
        reasons.append("detector_not_auto_approvable")

    observed_at = _timestamp(candidate.get("lastSeenAt"))
    evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
    maximum_age = timedelta(hours=int(normalized["relationshipMaxEvidenceAgeHours"]))
    if observed_at is None or observed_at > evaluated_at + timedelta(minutes=5):
        reasons.append("invalid_evidence_time")
    elif evaluated_at - observed_at > maximum_age:
        reasons.append("evidence_too_old")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "detectorKey": detector_key,
        "relationshipType": relationship_type,
        "confidence": confidence,
        "observationCount": observation_count,
    }
