"""Provider-neutral preview and apply helpers for reviewed CI imports."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from src.cmdb.reconciliation import decide_ci_match

DEFAULT_CI_POLICY: dict[str, Any] = {
    "typeMode": "all",
    "includedTypeIds": [],
    "typeMappings": {},
    "blockUnmappedTypes": False,
    "statusMode": "all",
    "includedStatusIds": [],
    "excludedExternalIds": [],
    "syncMode": "manual",
    "intervalMinutes": 360,
    "enabled": False,
}


def ci_sync_retry_delay_minutes(consecutive_failures: int) -> int:
    """Return a bounded exponential delay for an unattended CI sync retry.

    The first retry waits 15 minutes. Repeated failures back off to at most one
    day, while a successful run returns to the policy's normal interval.
    """

    failures = max(1, int(consecutive_failures))
    return min(15 * (2 ** min(failures - 1, 7)), 1440)


def normalize_ci_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return an explicit, provider-neutral CI discovery and sync policy."""

    source = policy or {}

    def identifiers(key: str) -> list[str]:
        values = source.get(key)
        if not isinstance(values, list):
            return []
        return sorted({str(value).strip()[:160] for value in values if str(value).strip()})

    def type_mappings() -> dict[str, str]:
        """Normalize immutable provider-type IDs to canonical CMDB type labels."""

        values = source.get("typeMappings")
        if not isinstance(values, Mapping):
            return {}
        normalized: dict[str, str] = {}
        for raw_identifier, raw_target in list(values.items())[:500]:
            identifier = str(raw_identifier).strip()[:160]
            target = str(raw_target).strip()[:100]
            if identifier and target:
                normalized[identifier] = target
        return dict(sorted(normalized.items()))

    type_mode = str(source.get("typeMode") or "all")
    status_mode = str(source.get("statusMode") or "all")
    sync_mode = str(source.get("syncMode") or "manual")
    interval = int(source.get("intervalMinutes") or 360)
    return {
        "typeMode": type_mode if type_mode in {"all", "selected"} else "all",
        "includedTypeIds": identifiers("includedTypeIds"),
        "typeMappings": type_mappings(),
        "blockUnmappedTypes": bool(source.get("blockUnmappedTypes", False)),
        "statusMode": status_mode if status_mode in {"all", "selected"} else "all",
        "includedStatusIds": identifiers("includedStatusIds"),
        "excludedExternalIds": identifiers("excludedExternalIds"),
        "syncMode": sync_mode if sync_mode in {"manual", "continuous_preview"} else "manual",
        "intervalMinutes": max(15, min(interval, 10080)),
        "enabled": bool(source.get("enabled", False)),
    }


def apply_ci_type_mappings(
    records: Iterable[Mapping[str, Any]], policy: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map provider type IDs to canonical types and report unmapped evidence.

    Empty mappings retain the provider type name for backwards compatibility. When
    ``blockUnmappedTypes`` is enabled, the record is marked for reconciliation review
    rather than silently becoming importable.
    """

    normalized = normalize_ci_policy(policy)
    mappings = normalized["typeMappings"]
    mapped_records: list[dict[str, Any]] = []
    unmapped_types: dict[str, dict[str, Any]] = {}
    mapped_count = 0
    unmapped_count = 0
    for raw in records:
        record = deepcopy(dict(raw))
        provider_type_id = str(record.get("providerTypeId") or "__unassigned__")
        provider_type_name = str(record.get("providerTypeName") or "Unclassified")
        canonical_type = str(mappings.get(provider_type_id) or "").strip()
        if canonical_type:
            record["type"] = canonical_type
            mapped_count += 1
        else:
            unmapped_count += 1
            item = unmapped_types.setdefault(
                provider_type_id,
                {"id": provider_type_id, "name": provider_type_name, "count": 0},
            )
            item["count"] += 1
            if normalized["blockUnmappedTypes"]:
                record["_typeMappingBlocked"] = True
        mapped_records.append(record)
    return mapped_records, {
        "mapped": mapped_count,
        "unmapped": unmapped_count,
        "blocked": unmapped_count if normalized["blockUnmappedTypes"] else 0,
        "unmappedTypes": sorted(
            unmapped_types.values(), key=lambda item: (item["name"].casefold(), item["id"])
        ),
    }


def configuration_catalogue(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Summarize immutable provider type and status identifiers for filter menus."""

    catalogues: dict[str, dict[str, dict[str, Any]]] = {"types": {}, "statuses": {}}
    for record in records:
        for collection, id_key, name_key, fallback in (
            ("types", "providerTypeId", "providerTypeName", "Unclassified"),
            ("statuses", "providerStatusId", "providerStatusName", "Unassigned"),
        ):
            identifier = str(record.get(id_key) or "__unassigned__")
            name = str(record.get(name_key) or fallback)
            item = catalogues[collection].setdefault(
                identifier, {"id": identifier, "name": name, "count": 0}
            )
            item["count"] += 1
    return {
        key: sorted(values.values(), key=lambda item: (item["name"].casefold(), item["id"]))
        for key, values in catalogues.items()
    }


def apply_ci_policy(
    records: Iterable[Mapping[str, Any]], policy: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply saved immutable-ID filters and return included records plus exclusion counts."""

    normalized = normalize_ci_policy(policy)
    type_ids = set(normalized["includedTypeIds"])
    status_ids = set(normalized["includedStatusIds"])
    excluded_ids = set(normalized["excludedExternalIds"])
    included: list[dict[str, Any]] = []
    reasons: dict[str, int] = defaultdict(int)
    for raw in records:
        record = dict(raw)
        external_id = str(record.get("externalId") or "")
        type_id = str(record.get("providerTypeId") or "__unassigned__")
        status_id = str(record.get("providerStatusId") or "__unassigned__")
        if external_id in excluded_ids:
            reasons["provider_id_excluded"] += 1
        elif normalized["typeMode"] == "selected" and type_id not in type_ids:
            reasons["type_filtered"] += 1
        elif normalized["statusMode"] == "selected" and status_id not in status_ids:
            reasons["status_filtered"] += 1
        else:
            included.append(record)
    return included, dict(reasons)


def _normalized_name(value: object) -> str:
    """Return a stable comparison key without treating a name as an identity."""

    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _asset_identifiers(asset: Mapping[str, Any]) -> dict[str, str]:
    """Read strong identifiers from current rich metadata and legacy fields."""

    raw_fields = asset.get("fields")
    raw_metadata = asset.get("metadata")
    fields: Mapping[str, Any] = raw_fields if isinstance(raw_fields, Mapping) else {}
    metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    return {
        key: str(value).strip()
        for key, value in {
            "serial_number": fields.get("serialNumber") or metadata.get("serialNumber"),
            "device_uuid": fields.get("mobileGuid") or fields.get("deviceIdentifier"),
        }.items()
        if str(value or "").strip()
    }


FIELD_ALIASES = {
    "display_name": "name",
    "ci_type": "type",
    "operational_status": "status",
}


def _authority_priorities(
    rules: Iterable[Mapping[str, Any]], ci_type: str, field: str
) -> dict[str, int]:
    """Return the most-specific provider priorities for one canonical field."""

    normalized_field = FIELD_ALIASES.get(field, field)
    matching = [
        rule
        for rule in rules
        if FIELD_ALIASES.get(str(rule.get("fieldName") or ""), str(rule.get("fieldName") or ""))
        == normalized_field
        and str(rule.get("ciType") or "*") in {"*", ci_type}
    ]
    exact = [rule for rule in matching if str(rule.get("ciType")) == ci_type]
    selected = exact or matching
    return {
        str(rule.get("provider") or ""): int(rule.get("priority", 100))
        for rule in selected
        if rule.get("provider")
    }


def _source_name(value: object) -> str:
    """Normalize canonical/manual source labels for authority comparison."""

    source = str(value or "").strip().casefold()
    return "cmdb" if source in {"manual", "cmdb_managed", "cmdb"} else source


def _field_decision(
    *,
    provider: str,
    field: str,
    ci_type: str,
    rules: Iterable[Mapping[str, Any]],
    asset: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain whether one provider may replace a current canonical value."""

    priorities = _authority_priorities(rules, ci_type, field)
    raw_metadata = asset.get("metadata")
    metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    raw_field_sources = metadata.get("fieldSources")
    field_sources: Mapping[str, Any] = (
        raw_field_sources if isinstance(raw_field_sources, Mapping) else {}
    )
    current_provider = _source_name(field_sources.get(field) or asset.get("source"))
    incoming_provider = _source_name(provider)
    if not priorities:
        return {
            "field": field,
            "provider": incoming_provider,
            "currentProvider": current_provider,
            "allowed": True,
            "reason": "no explicit field-authority rule",
        }
    incoming_priority = priorities.get(incoming_provider)
    if incoming_priority is None:
        return {
            "field": field,
            "provider": incoming_provider,
            "currentProvider": current_provider,
            "allowed": False,
            "reason": "incoming provider is not authorized for this field",
        }
    current_priority = priorities.get(current_provider)
    winning_priority = min(priorities.values())
    allowed = (
        incoming_priority <= current_priority
        if current_priority is not None
        else incoming_priority == winning_priority
    )
    return {
        "field": field,
        "provider": incoming_provider,
        "currentProvider": current_provider,
        "incomingPriority": incoming_priority,
        "currentPriority": current_priority,
        "allowed": allowed,
        "reason": (
            "incoming provider has equal or higher authority"
            if allowed
            else "a higher-authority source owns the current field"
        ),
    }


def provider_change_plan(
    record: Mapping[str, Any],
    asset: Mapping[str, Any],
    *,
    provider: str,
    field_authority: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Plan provider changes and retain an explanation for every changed field."""

    rules = list(field_authority)
    ci_type = str(asset.get("type") or record.get("type") or "*")
    changes: dict[str, Any] = {}
    applied_fields: list[str] = []
    blocked_fields: list[str] = []
    decisions: list[dict[str, Any]] = []

    def consider(field: str, incoming: Any, current: Any) -> bool:
        if incoming in (None, "") or incoming == current:
            return False
        decision = _field_decision(
            provider=provider,
            field=field,
            ci_type=ci_type,
            rules=rules,
            asset=asset,
        )
        decisions.append(decision)
        (applied_fields if decision["allowed"] else blocked_fields).append(field)
        return bool(decision["allowed"])

    for field in ("name", "type", "status"):
        incoming = record.get(field)
        if consider(field, incoming, asset.get(field)):
            changes[field] = incoming

    raw_current_fields = asset.get("fields")
    raw_incoming_fields = record.get("fields")
    current_fields: Mapping[str, Any] = (
        raw_current_fields if isinstance(raw_current_fields, Mapping) else {}
    )
    incoming_fields: Mapping[str, Any] = (
        raw_incoming_fields if isinstance(raw_incoming_fields, Mapping) else {}
    )
    allowed_fields: dict[str, Any] = {}
    for key, value in incoming_fields.items():
        path = f"fields.{key}"
        if consider(path, value, current_fields.get(key)):
            allowed_fields[str(key)] = deepcopy(value)
    if allowed_fields:
        changes["fields"] = {**deepcopy(dict(current_fields)), **allowed_fields}

    raw_current_metadata = asset.get("metadata")
    raw_incoming_metadata = record.get("metadata")
    current_metadata: Mapping[str, Any] = (
        raw_current_metadata if isinstance(raw_current_metadata, Mapping) else {}
    )
    incoming_metadata: Mapping[str, Any] = (
        raw_incoming_metadata if isinstance(raw_incoming_metadata, Mapping) else {}
    )
    allowed_metadata: dict[str, Any] = {}
    for key, value in incoming_metadata.items():
        if key == "fieldSources":
            continue
        path = f"metadata.{key}"
        if consider(path, value, current_metadata.get(key)):
            allowed_metadata[str(key)] = deepcopy(value)
    if allowed_metadata:
        changes["metadata"] = {**deepcopy(dict(current_metadata)), **allowed_metadata}
    return {
        "changes": changes,
        "appliedFields": sorted(applied_fields),
        "blockedFields": sorted(blocked_fields),
        "fieldDecisions": decisions,
    }


def provider_changes(record: Mapping[str, Any], asset: Mapping[str, Any]) -> dict[str, Any]:
    """Return provider-managed changes using the backwards-compatible open policy."""

    return provider_change_plan(record, asset, provider="", field_authority=())["changes"]


def reconcile_configuration_items(
    *,
    connection_id: str,
    records: Iterable[Mapping[str, Any]],
    assets: Iterable[Mapping[str, Any]],
    mappings: Iterable[Mapping[str, Any]],
    field_authority: Iterable[Mapping[str, Any]] = (),
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """Classify provider records without changing canonical configuration items."""

    asset_rows = [dict(item) for item in assets]
    assets_by_id = {str(item.get("id")): item for item in asset_rows}
    names: dict[str, list[str]] = defaultdict(list)
    identifier_candidates: dict[tuple[str, str], set[str]] = defaultdict(set)
    for asset in asset_rows:
        asset_id = str(asset.get("id") or "")
        names[_normalized_name(asset.get("name"))].append(asset_id)
        for kind, value in _asset_identifiers(asset).items():
            key = (kind, value.casefold())
            identifier_candidates[key].add(asset_id)
    identifier_index = {
        key: next(iter(asset_ids))
        for key, asset_ids in identifier_candidates.items()
        if len(asset_ids) == 1
    }

    mapping_rows = [
        {
            "connection_id": connection_id,
            "external_type": "configuration",
            "external_id": str(item.get("externalId") or ""),
            "ci_id": str(item.get("assetId") or ""),
        }
        for item in mappings
        if item.get("externalId") and item.get("assetId")
    ]
    results: list[dict[str, Any]] = []
    authority_rules = [dict(item) for item in field_authority]
    source_provider = provider or connection_id
    for raw in records:
        record = deepcopy(dict(raw))
        type_mapping_blocked = bool(record.pop("_typeMappingBlocked", False))
        identifiers = {
            str(key): str(value).casefold()
            for key, value in (record.get("identifiers") or {}).items()
            if str(value).strip()
        }
        decision = decide_ci_match(
            connection_id=connection_id,
            external_type="configuration",
            external_id=str(record["externalId"]),
            identifiers=identifiers,
            mappings=mapping_rows,
            identifier_index=identifier_index,
        )
        matched_asset = assets_by_id.get(str(decision.ci_id or ""))
        action = decision.action
        reason = decision.reason
        confidence = decision.confidence
        changes: dict[str, Any] = {}
        applied_fields: list[str] = []
        blocked_fields: list[str] = []
        field_decisions: list[dict[str, Any]] = []
        if type_mapping_blocked:
            action = "conflict"
            reason = "provider configuration type is not mapped to a canonical CMDB type"
            confidence = 0.0
        elif action in {"mapped", "matched"}:
            matched_without_mapping = action == "matched"
            if not matched_asset:
                action = "conflict"
                reason = "provider mapping points to an unavailable configuration item"
                confidence = 0.0
            else:
                plan = provider_change_plan(
                    record,
                    matched_asset,
                    provider=source_provider,
                    field_authority=authority_rules,
                )
                changes = plan["changes"]
                applied_fields = plan["appliedFields"]
                blocked_fields = plan["blockedFields"]
                field_decisions = plan["fieldDecisions"]
                action = (
                    "update" if changes else ("link" if matched_without_mapping else "unchanged")
                )
        elif action == "review":
            action = "conflict"
        else:
            possible = names.get(_normalized_name(record.get("name")), [])
            if possible:
                action = "conflict"
                reason = "possible duplicate name; provider ID or strong identifier required"
                confidence = 0.0
            else:
                action = "create"
                reason = "new provider ID with no strong-identifier or name conflict"
                applied_fields = sorted(
                    ["name", "status", "type"]
                    + [f"fields.{key}" for key in (record.get("fields") or {})]
                    + [
                        f"metadata.{key}"
                        for key in (record.get("metadata") or {})
                        if key != "fieldSources"
                    ]
                )
        results.append(
            {
                "externalId": str(record["externalId"]),
                "name": record.get("name", ""),
                "type": record.get("type", "Configuration item"),
                "status": record.get("status", "Active"),
                "action": action,
                "reason": reason,
                "confidence": confidence,
                "assetId": str(matched_asset.get("id")) if matched_asset else None,
                "assetName": str(matched_asset.get("name") or "") if matched_asset else "",
                "changedFields": applied_fields or sorted(changes),
                "appliedFields": applied_fields,
                "blockedFields": blocked_fields,
                "fieldDecisions": field_decisions,
                "record": record,
                "changes": changes,
            }
        )
    return results
