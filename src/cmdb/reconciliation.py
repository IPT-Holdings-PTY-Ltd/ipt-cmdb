"""Provider-ID-first reconciliation rules for the CMDB integration workers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

STRONG_IDENTIFIERS = (
    "device_uuid",
    "bios_uuid",
    "serial_number",
    "domain_sid",
    "license_key_hash",
)


@dataclass(frozen=True)
class MatchDecision:
    """Describe how an external record should map to a canonical CI."""

    action: str  # mapped | matched | review | create
    ci_id: str | None
    confidence: float
    reason: str


def decide_ci_match(
    *,
    connection_id: str,
    external_type: str,
    external_id: str,
    identifiers: Mapping[str, str],
    mappings: Iterable[Mapping[str, str]],
    identifier_index: Mapping[tuple[str, str], str],
) -> MatchDecision:
    """Resolve identity without ever using a mutable name as a primary key.

    Existing provider mappings always win. A unique strong identifier can safely
    attach a new provider mapping. Names only create a review candidate elsewhere.
    """
    for mapping in mappings:
        if (
            mapping.get("connection_id"),
            mapping.get("external_type"),
            mapping.get("external_id"),
        ) == (connection_id, external_type, external_id):
            return MatchDecision("mapped", mapping["ci_id"], 1.0, "existing provider ID mapping")
    matches = {
        identifier_index[(kind, value)]
        for kind, value in identifiers.items()
        if kind in STRONG_IDENTIFIERS and value and (kind, value) in identifier_index
    }
    if len(matches) == 1:
        return MatchDecision("matched", matches.pop(), 0.98, "unique strong identifier")
    if len(matches) > 1:
        return MatchDecision("review", None, 0.0, "conflicting strong identifiers")
    return MatchDecision("create", None, 0.0, "no existing mapping or verified identifier")


def can_apply_field(
    provider: str,
    field: str,
    priorities: Mapping[tuple[str, str], int],
    current_provider: str | None,
) -> bool:
    """Field authority prevents a lower-trust integration from renaming a CI."""
    incoming = priorities.get((provider, field), 100)
    current = priorities.get((current_provider or "", field), 100)
    return incoming <= current
