"""Build explainable, review-only relationship candidates from provider evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

SUPPORTED_RELATIONSHIPS = {
    "hosts",
    "installed_on",
    "managed_by",
    "member_of",
    "protected_by",
    "provided_by",
    "stored_on",
}
SUPPORTED_IMPACT_POLICIES = {"required", "degraded", "redundant", "informational"}
DEFAULT_EVIDENCE_REVISION = 1
EXPLICIT_RELATIONSHIP_HINT_DETECTOR = "provider_explicit_relationship_hint"
EXPLICIT_VIRTUALIZATION_DETECTOR = "ncentral_explicit_virtualization_identity"


def _mapping_index(mappings: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index active provider mappings by immutable external ID."""

    return {
        str(item.get("externalId")): item
        for item in mappings
        if item.get("active", True) and item.get("externalId") and item.get("assetId")
    }


def _as_mapping(value: object) -> Mapping[str, Any]:
    """Return a mapping or an empty immutable view."""

    return value if isinstance(value, Mapping) else {}


def _as_list(value: object) -> list[Any]:
    """Return a concrete list without treating strings as collections."""

    return list(value) if isinstance(value, (list, tuple)) else []


def _virtualization_role(virtualization: Mapping[str, Any]) -> str:
    """Return one canonical virtualization role from current or legacy fields."""

    raw = str(virtualization.get("kind") or virtualization.get("role") or "")
    token = raw.strip().casefold().replace("-", "_").replace(" ", "_")
    return {
        "guest": "virtual_machine",
        "vm": "virtual_machine",
        "virtualmachine": "virtual_machine",
        "host": "hypervisor_host",
        "hypervisor": "hypervisor_host",
        "hypervisorhost": "hypervisor_host",
    }.get(token, token)


def _virtualization_hints(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate explicit virtualization identities into relationship hints."""

    fields = _as_mapping(record.get("fields"))
    virtualization = _as_mapping(fields.get("virtualization"))
    hints: list[dict[str, Any]] = []
    role = _virtualization_role(virtualization)
    current_external_id = str(record.get("externalId") or "").strip()

    host_external_id = str(
        virtualization.get("hostExternalId") or virtualization.get("hypervisorExternalId") or ""
    ).strip()
    if role == "virtual_machine" and host_external_id:
        hints.append(
            {
                "relationshipType": "hosts",
                "sourceExternalId": host_external_id,
                "targetExternalId": current_external_id,
                "confidence": virtualization.get("hostConfidence", 1.0),
                "impactPolicy": "required",
                "detector": EXPLICIT_VIRTUALIZATION_DETECTOR,
                "evidenceRevision": DEFAULT_EVIDENCE_REVISION,
                "evidence": _as_list(virtualization.get("hostEvidence"))
                or ["N-central supplied an explicit hypervisor identity"],
            }
        )

    for guest_external_id in _as_list(virtualization.get("guestExternalIds")):
        identifier = str(guest_external_id or "").strip()
        if role == "hypervisor_host" and identifier:
            hints.append(
                {
                    "relationshipType": "hosts",
                    "sourceExternalId": current_external_id,
                    "targetExternalId": identifier,
                    "confidence": virtualization.get("guestConfidence", 1.0),
                    "impactPolicy": "required",
                    "detector": EXPLICIT_VIRTUALIZATION_DETECTOR,
                    "evidenceRevision": DEFAULT_EVIDENCE_REVISION,
                    "evidence": _as_list(virtualization.get("guestEvidence"))
                    or ["N-central supplied an explicit guest identity"],
                }
            )

    cluster_external_id = str(virtualization.get("clusterExternalId") or "").strip()
    if cluster_external_id:
        hints.append(
            {
                "relationshipType": "member_of",
                "sourceExternalId": current_external_id,
                "targetExternalId": cluster_external_id,
                "confidence": virtualization.get("clusterConfidence", 1.0),
                "impactPolicy": "required",
                "detector": EXPLICIT_VIRTUALIZATION_DETECTOR,
                "evidenceRevision": DEFAULT_EVIDENCE_REVISION,
                "evidence": _as_list(virtualization.get("clusterEvidence"))
                or ["N-central supplied an explicit virtualization cluster identity"],
            }
        )

    storage_external_id = str(virtualization.get("storageExternalId") or "").strip()
    if storage_external_id:
        hints.append(
            {
                "relationshipType": "stored_on",
                "sourceExternalId": current_external_id,
                "targetExternalId": storage_external_id,
                "confidence": virtualization.get("storageConfidence", 1.0),
                "impactPolicy": "required",
                "detector": EXPLICIT_VIRTUALIZATION_DETECTOR,
                "evidenceRevision": DEFAULT_EVIDENCE_REVISION,
                "evidence": _as_list(virtualization.get("storageEvidence"))
                or ["N-central supplied an explicit storage identity"],
            }
        )
    return hints


def _record_hints(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return explicit, provider-generated hints plus virtualization shortcuts."""

    fields = _as_mapping(record.get("fields"))
    explicit = [
        item for item in _as_list(fields.get("relationshipHints")) if isinstance(item, Mapping)
    ]
    return [*explicit, *_virtualization_hints(record)]


def _safe_evidence(value: object) -> list[str]:
    """Return bounded, de-duplicated evidence in a deterministic order."""

    evidence: set[str] = set()
    for item in _as_list(value):
        text = str(item or "").strip()
        if text:
            evidence.add(text[:500])
    return sorted(evidence, key=lambda item: (item.casefold(), item))[:12]


def _evidence_revision(value: object) -> int:
    """Return a bounded positive revision for one evidence detector."""

    try:
        revision = int(value) if isinstance(value, (int, str)) else DEFAULT_EVIDENCE_REVISION
    except (TypeError, ValueError):
        revision = DEFAULT_EVIDENCE_REVISION
    return max(1, min(revision, 2_147_483_647))


def _detector_identity(hint: Mapping[str, Any]) -> tuple[str, int]:
    """Return a stable detector key and revision from either evidence shape."""

    raw_detector = hint.get("detector")
    if isinstance(raw_detector, Mapping):
        detector = str(raw_detector.get("key") or "").strip()
        revision_value = hint.get("evidenceRevision", raw_detector.get("version"))
    else:
        detector = str(raw_detector or "").strip()
        revision_value = hint.get("evidenceRevision")
    return (
        detector[:160] or EXPLICIT_RELATIONSHIP_HINT_DETECTOR,
        _evidence_revision(revision_value),
    )


def _evidence_fingerprint(material: Mapping[str, Any]) -> str:
    """Return a deterministic digest over topology-relevant provider evidence."""

    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_relationship_candidates(
    *,
    provider: str,
    company_id: str,
    records: Iterable[Mapping[str, Any]],
    mappings: Iterable[Mapping[str, Any]],
    existing_relationships: Iterable[Mapping[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Return review candidates grouped by the evidence-producing external record.

    Only immutable provider identities may resolve endpoints. Names, shared sites,
    subnets and gateways are deliberately ignored because they cannot prove a
    physical or virtualization relationship.
    """

    mapping_by_external_id = _mapping_index(mappings)
    existing = {
        (
            str(item.get("fromId") or ""),
            str(item.get("toId") or ""),
            str(item.get("type") or ""),
        )
        for item in existing_relationships
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for record in records:
        evidence_external_id = str(record.get("externalId") or "").strip()
        source_mapping = mapping_by_external_id.get(evidence_external_id)
        if not evidence_external_id or not source_mapping:
            continue
        grouped.setdefault(evidence_external_id, [])
        for hint in _record_hints(record):
            relationship_type = str(hint.get("relationshipType") or hint.get("type") or "").strip()
            if relationship_type not in SUPPORTED_RELATIONSHIPS:
                continue
            source_external_id = str(hint.get("sourceExternalId") or evidence_external_id).strip()
            target_external_id = str(hint.get("targetExternalId") or "").strip()
            source = mapping_by_external_id.get(source_external_id)
            target = mapping_by_external_id.get(target_external_id)
            if not source or not target:
                continue
            from_id = str(source.get("assetId") or "")
            to_id = str(target.get("assetId") or "")
            if not from_id or not to_id or from_id == to_id:
                continue
            if (from_id, to_id, relationship_type) in existing:
                continue
            try:
                confidence = float(hint.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(confidence, 1.0))
            if confidence < 0.5:
                continue
            impact_policy = str(hint.get("impactPolicy") or "required")
            if impact_policy not in SUPPORTED_IMPACT_POLICIES:
                impact_policy = "required"
            detector, evidence_revision = _detector_identity(hint)
            evidence_messages = _safe_evidence(hint.get("evidence"))
            stable_material = {
                "provider": provider,
                "companyId": company_id,
                "evidenceExternalId": evidence_external_id,
                "fromId": from_id,
                "toId": to_id,
                "relationshipType": relationship_type,
            }
            candidate_key = hashlib.sha256(
                json.dumps(
                    stable_material,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            fingerprint_material = {
                "detector": detector,
                "evidenceRevision": evidence_revision,
                "provider": provider,
                "companyId": company_id,
                "evidenceExternalId": evidence_external_id,
                "sourceExternalId": source_external_id,
                "targetExternalId": target_external_id,
                "relationshipType": relationship_type,
                "impactPolicy": impact_policy,
                "confidence": confidence,
                "messages": evidence_messages,
            }
            grouped[evidence_external_id].append(
                {
                    "candidateKey": candidate_key,
                    "fromCiId": from_id,
                    "toCiId": to_id,
                    "fromExternalIdentity": {
                        "provider": provider,
                        "externalId": source_external_id,
                    },
                    "toExternalIdentity": {
                        "provider": provider,
                        "externalId": target_external_id,
                    },
                    "relationshipType": relationship_type,
                    "impactPolicy": impact_policy,
                    "confidence": confidence,
                    "evidence": {
                        "detector": detector,
                        "evidenceRevision": evidence_revision,
                        "evidenceFingerprint": _evidence_fingerprint(fingerprint_material),
                        "messages": evidence_messages,
                        "providerEvidence": deepcopy(
                            {
                                "provider": provider,
                                "evidenceExternalId": evidence_external_id,
                            }
                        ),
                        "impactPolicy": impact_policy,
                        "sourceExternalId": source_external_id,
                        "targetExternalId": target_external_id,
                    },
                }
            )
    return grouped
