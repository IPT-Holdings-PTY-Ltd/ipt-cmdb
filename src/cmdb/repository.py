"""Resource repositories for the canonical CMDB data model.

The local implementation exists only for setup and lightweight development.
The PostgreSQL implementation is the sole operational source of truth when a
database is configured; portable exports are assembled from canonical tables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import sql
from psycopg.errors import UniqueViolation

from src.cmdb.audit import (
    current_audit_context,
    entity_name,
    event_category,
    field_changes,
    sanitize_audit_value,
)
from src.cmdb.change_templates import DEFAULT_CHANGE_TEMPLATES, normalize_template_content
from src.cmdb.integration_reconciliation import (
    ci_sync_retry_delay_minutes,
    normalize_ci_policy,
)
from src.cmdb.notifications import DEFAULT_NOTIFICATION_RULES, DEFAULT_NOTIFICATION_TEMPLATES

CMDB_NAMESPACE = uuid.UUID("a12d44c4-64a7-4d6f-b829-3a8b691f0fa4")
PROVIDER_TO_DB = {
    "connectwise": "connectwise_manage",
    "ncentral": "ncentral",
    "passportal": "passportal",
}
PROVIDER_FROM_DB = {value: key for key, value in PROVIDER_TO_DB.items()}
CREDENTIAL_REFERENCES = {
    "connectwise": "env://CW_BASE_URL,CW_COMPANY_ID,CW_PUBLIC_KEY,CW_PRIVATE_KEY,CW_CLIENT_ID",
    "ncentral": (
        "env://NCENTRAL_BASE_URL,"
        "NCENTRAL_USER_API_TOKEN|NCENTRAL_USER_API_TOKEN_FILE|NCENTRAL_API_TOKEN,"
        "NCENTRAL_GRAPHQL_API_TOKEN|NCENTRAL_GRAPHQL_API_TOKEN_FILE"
    ),
    "passportal": "env://PASSPORTAL_BASE_URL,PASSPORTAL_API_TOKEN",
}
DEFAULT_MSP_BRANDING = {
    "name": "CMDB Hub",
    "logoText": "C",
    "accent": "#50d5b9",
    "secondaryAccent": "#7997ff",
    "logoDataUrl": "",
    "logoFileName": "",
    "supportEmail": "",
    "supportUrl": "",
    "supportPhone": "",
    "welcomeMessage": "",
    "reportFooter": "",
    "confidentialityLabel": "Internal use only",
}
DEFAULT_EMAIL_CONNECTION = {
    "id": "msp-email",
    "scope": "msp",
    "provider": "microsoft_graph",
    "enabled": False,
    "authMode": "managed_identity",
    "tenantId": "",
    "clientId": "",
    "servicePrincipalObjectId": "",
    "managedIdentityClientId": "",
    "senderAddress": "",
    "senderName": "",
    "replyTo": "",
    "graphBaseUrl": "https://graph.microsoft.com/v1.0",
    "status": "not_configured",
    "lastTestAt": None,
    "lastError": "",
    "revision": 1,
}
OWNER_RESPONSIBILITY_ROLES = {
    "business_owner",
    "service_owner",
    "technical_owner",
    "custodian",
    "change_approver",
    "signoff_delegate",
    "support_contact",
}
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$310000$Y21kYi1sb2dpbi1kdW1teQ==$xMuEeu90JGuMJzKANciKVeEkYTk3jjOl1qiV2EV5DOs="
)


def default_company_branding(name: str) -> dict:
    return {
        "name": name,
        "logoText": (name[:1] or "C").upper(),
        "accent": "#50d5b9",
        "secondaryAccent": "#7997ff",
        "logoDataUrl": "",
        "logoFileName": "",
    }


def branding_audit_value(brand: dict) -> dict:
    value = deepcopy(brand)
    if value.get("logoDataUrl"):
        value["logoDataUrl"] = f"[embedded logo: {value.get('logoFileName') or 'unnamed'}]"
    return value


def email_connection_audit_value(connection: dict) -> dict:
    """Return email configuration metadata without encrypted credentials."""

    hidden = {
        "clientSecretEncrypted",
        "clientSecretNonce",
        "certificatePasswordEncrypted",
        "certificatePasswordNonce",
    }
    value = {key: deepcopy(item) for key, item in connection.items() if key not in hidden}
    value["hasClientSecret"] = bool(connection.get("clientSecretEncrypted"))
    return value


def integration_connection_audit_value(connection: dict) -> dict:
    """Return integration metadata without installation-bound credential material."""

    hidden = {"credentialsEncrypted", "credentialsNonce"}
    value = {key: deepcopy(item) for key, item in connection.items() if key not in hidden}
    value["hasCredentials"] = bool(connection.get("credentialsEncrypted"))
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a stored UTC timestamp used by the local worker repository."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


ACTIVE_CI_PREVIEW_RUN_STATUSES = {"queued", "running"}
RETRYABLE_CI_PREVIEW_RUN_STATUSES = {"failed", "cancelled"}
INVENTORY_COLLECTION_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
INVENTORY_MAX_COLLECTION_BYTES = 2 * 1024 * 1024
INVENTORY_MAX_COLLECTION_ITEMS = 5_000
CI_PRESENCE_MAX_OBSERVED_IDENTITIES = 25_000
INVENTORY_MAX_NETWORK_INTERFACES = 512
RELATIONSHIP_CANDIDATE_STATES = {"pending", "approved", "rejected", "ignored"}
CI_PRESENCE_STATES = {
    "observed",
    "monitoring",
    "eligible",
    "not_evaluated",
    "retired",
    "restore_ready",
}
CI_PRESENCE_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _provider_ci_import_decision(
    *,
    mapping_exists: bool,
    mapping_active: bool,
    lifecycle_state: str | None,
    company_matches: bool,
    provider_parent_matches: bool,
) -> tuple[str, str]:
    """Return the fail-closed disposition for one immutable provider identity."""

    if mapping_exists and (not company_matches or not provider_parent_matches):
        return (
            "scope_conflict",
            "This immutable provider identity is scoped to a different customer or provider "
            "parent; review its existing mapping before importing.",
        )
    if mapping_exists and not mapping_active:
        if lifecycle_state in {"retired", "restore_ready"}:
            return (
                "restore_required",
                "This immutable provider mapping is retired; restore it from Missing devices "
                "before importing.",
            )
        return (
            "inactive_mapping",
            "This immutable provider mapping is inactive without a governed restore action; "
            "an administrator must review it before importing.",
        )
    return "allow", "No immutable mapping conflict blocks this provider identity."


CANONICAL_RELATIONSHIP_TYPES = {
    "connected_to",
    "depends_on",
    "installed_on",
    "licensed_to",
    "used_by",
    "related_to",
    "hosts",
    "backs_up",
    "managed_by",
    "member_of",
    "stored_on",
    "provided_by",
    "protected_by",
}
RELATIONSHIP_IMPACT_POLICIES = {"required", "degraded", "redundant", "informational"}
SYMMETRIC_RELATIONSHIP_TYPES = {"connected_to", "related_to"}
INTEGRATION_CAPABILITY_CACHE_MAX_BYTES = 64 * 1024
INTEGRATION_ENRICHMENT_CACHE_MAX_BYTES = 256 * 1024
INTEGRATION_CACHE_MIN_TTL_SECONDS = 30
INTEGRATION_CACHE_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
INTEGRATION_CAPABILITY_STATUSES = {"supported", "unsupported", "unavailable", "error"}
INTEGRATION_ENRICHMENT_STATUSES = {"ready", "partial", "unavailable", "error"}
INTEGRATION_CACHE_KEY = re.compile(r"^[a-z][a-z0-9_.:-]{0,119}$")
INTEGRATION_SOURCE_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
INTEGRATION_CACHE_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "apitoken",
    "authorization",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "graphqlapitoken",
    "header",
    "headers",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "setcookie",
    "token",
    "userapitoken",
    "xapikey",
}


def _json_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 fingerprint for one JSON-safe value."""

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bounded_integration_cache_summary(value: Any, *, maximum_bytes: int) -> tuple[dict, str]:
    """Validate one bounded normalized cache summary and return its fingerprint.

    Provider response envelopes and credential-shaped fields are rejected rather
    than scrubbed.  This keeps accidental raw GraphQL responses and tokens out of
    PostgreSQL while still allowing tenant-scoped normalized inventory fields.
    """

    if not isinstance(value, dict):
        raise ValueError("Integration cache summary must be an object")
    entry_count = 0

    def inspect(item: Any, depth: int = 0) -> None:
        nonlocal entry_count
        if depth > 12:
            raise ValueError("Integration cache summary is too deeply nested")
        if isinstance(item, dict):
            entry_count += len(item)
            if entry_count > 10_000:
                raise ValueError("Integration cache summary contains too many fields")
            for raw_key, child in item.items():
                key = re.sub(r"[^a-z0-9]", "", str(raw_key).casefold())
                if key in {"data", "errors"}:
                    raise ValueError("Raw GraphQL response envelopes cannot be cached")
                if key in INTEGRATION_CACHE_SENSITIVE_KEYS or key.endswith(
                    (
                        "apikey",
                        "authorization",
                        "cookie",
                        "credential",
                        "password",
                        "privatekey",
                        "secret",
                        "token",
                    )
                ):
                    raise ValueError("Integration cache summary contains a sensitive field")
                inspect(child, depth + 1)
        elif isinstance(item, list):
            entry_count += len(item)
            if entry_count > 10_000:
                raise ValueError("Integration cache summary contains too many entries")
            for child in item:
                inspect(child, depth + 1)

    inspect(value)
    summary = deepcopy(value)
    try:
        encoded = json.dumps(
            summary,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Integration cache summary must be JSON serializable") from error
    if len(encoded) > maximum_bytes:
        raise ValueError("Integration cache summary is too large")
    return summary, hashlib.sha256(encoded).hexdigest()


def _bounded_integration_cache_ttl(value: Any, *, default: int = 900) -> int:
    """Return a finite cache lifetime within the operational safety bounds."""

    try:
        ttl_seconds = int(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise ValueError("Integration cache TTL must be an integer") from error
    if not INTEGRATION_CACHE_MIN_TTL_SECONDS <= ttl_seconds <= INTEGRATION_CACHE_MAX_TTL_SECONDS:
        raise ValueError(
            "Integration cache TTL must be between "
            f"{INTEGRATION_CACHE_MIN_TTL_SECONDS} and "
            f"{INTEGRATION_CACHE_MAX_TTL_SECONDS} seconds"
        )
    return ttl_seconds


def _integration_cache_identifier(value: Any, label: str, maximum: int = 255) -> str:
    """Return a required bounded source identity without changing its value."""

    identifier = str(value or "").strip()
    if not identifier or len(identifier) > maximum:
        raise ValueError(f"{label} must contain between 1 and {maximum} characters")
    return identifier


def _integration_cache_optional_uuid(value: Any, label: str) -> str | None:
    """Return a canonical optional UUID used by a tenant-scoped cache link."""

    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error


def _inventory_collection_type(value: Any) -> str:
    """Normalize a provider collection label to a bounded provider-neutral key."""

    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(value or "").strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    if not INVENTORY_COLLECTION_TYPE.fullmatch(normalized):
        raise ValueError("Inventory collection type is invalid")
    return normalized


def _bounded_inventory_payload(value: Any) -> tuple[Any, int, str]:
    """Validate and fingerprint one bounded inventory collection payload."""

    if not isinstance(value, (dict, list)):
        raise ValueError("Inventory collection payload must be an object or array")
    item_count = len(value) if isinstance(value, list) else 1
    if item_count > INVENTORY_MAX_COLLECTION_ITEMS:
        raise ValueError("Inventory collection contains too many items")
    payload = deepcopy(value)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > INVENTORY_MAX_COLLECTION_BYTES:
        raise ValueError("Inventory collection payload is too large")
    return payload, item_count, hashlib.sha256(encoded).hexdigest()


def _normalized_interface(value: Any, ordinal: int) -> dict[str, Any]:
    """Return one bounded, provider-neutral network interface observation."""

    if not isinstance(value, dict):
        raise ValueError("Network interface observations must be objects")
    item = deepcopy(value)
    explicit_key = str(
        item.get("interfaceKey")
        or item.get("key")
        or item.get("externalId")
        or item.get("id")
        or ""
    ).strip()
    mac_address = str(item.get("macAddress") or item.get("mac") or "").strip()
    name = str(item.get("name") or item.get("interfaceName") or "").strip()
    key_material = explicit_key or mac_address.casefold() or f"{name.casefold()}:{ordinal}"
    interface_key = (
        key_material[:255]
        if explicit_key
        else hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    )

    def string_list(*names: str) -> list[str]:
        raw: Any = []
        for candidate in names:
            if candidate in item:
                raw = item.get(candidate)
                break
        if raw is None:
            return []
        values = raw if isinstance(raw, list) else [raw]
        return list(
            dict.fromkeys(str(entry).strip()[:255] for entry in values if str(entry).strip())
        )

    speed = item.get("speedMbps", item.get("speed"))
    try:
        speed_mbps = max(0, int(speed)) if speed is not None and str(speed).strip() else None
    except (TypeError, ValueError):
        speed_mbps = None
    known = {
        "interfaceKey",
        "key",
        "externalId",
        "id",
        "name",
        "interfaceName",
        "description",
        "macAddress",
        "mac",
        "ipAddresses",
        "addresses",
        "ipAddress",
        "gateways",
        "gateway",
        "dnsServers",
        "dns",
        "dhcpEnabled",
        "vlanId",
        "operationalState",
        "state",
        "speedMbps",
        "speed",
        "attributes",
    }
    raw_attributes = item.get("attributes")
    attributes: dict[str, Any] = (
        deepcopy(raw_attributes) if isinstance(raw_attributes, dict) else {}
    )
    attributes.update({key: deepcopy(entry) for key, entry in item.items() if key not in known})
    normalized = {
        "interfaceKey": interface_key,
        "name": name[:500],
        "description": str(item.get("description") or "")[:1000],
        "macAddress": mac_address[:64],
        "ipAddresses": string_list("ipAddresses", "addresses", "ipAddress"),
        "gateways": string_list("gateways", "gateway"),
        "dnsServers": string_list("dnsServers", "dns"),
        "dhcpEnabled": (
            bool(item.get("dhcpEnabled")) if item.get("dhcpEnabled") is not None else None
        ),
        "vlanId": str(item.get("vlanId") or "")[:128],
        "operationalState": str(item.get("operationalState") or item.get("state") or "")[:80],
        "speedMbps": speed_mbps,
        "attributes": attributes,
    }
    normalized["fingerprint"] = _json_fingerprint(normalized)
    return normalized


def _relationship_candidate_key(value: dict[str, Any]) -> str:
    """Return the caller-supplied SHA-256 key or derive one from stable identity."""

    supplied = str(value.get("candidateKey") or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", supplied):
        return supplied
    identity = {
        "fromCiId": str(value.get("fromCiId") or ""),
        "toCiId": str(value.get("toCiId") or ""),
        "fromExternalIdentity": value.get("fromExternalIdentity") or {},
        "toExternalIdentity": value.get("toExternalIdentity") or {},
        "relationshipType": str(value.get("relationshipType") or value.get("type") or ""),
    }
    return _json_fingerprint(identity)


def _relationship_evidence_fingerprint(value: object) -> str:
    """Return a validated provider evidence fingerprint or an empty value."""

    if not isinstance(value, dict):
        return ""
    fingerprint = str(value.get("evidenceFingerprint") or "").strip().casefold()
    return fingerprint if re.fullmatch(r"[0-9a-f]{64}", fingerprint) else ""


def _relationship_candidate_evidence_changed(
    stored: dict[str, Any], incoming_evidence: dict[str, Any]
) -> bool:
    """Return whether fresh versioned evidence should reopen a closed proposal."""

    if stored.get("state") not in {"ignored", "rejected"}:
        return False
    stored_fingerprint = _relationship_evidence_fingerprint(stored.get("evidence"))
    incoming_fingerprint = _relationship_evidence_fingerprint(incoming_evidence)
    return bool(
        stored_fingerprint and incoming_fingerprint and stored_fingerprint != incoming_fingerprint
    )


def _required_relationship_provider_context(evidence: Any) -> dict[str, Any]:
    """Return one complete stored provider generation or require a fresh preview."""

    raw_evidence = evidence if isinstance(evidence, dict) else {}
    raw_context = raw_evidence.get("providerContext")
    if not isinstance(raw_context, dict):
        raise ValueError(
            "Provider relationship context is stale: provider generation evidence is "
            "missing. Run a new preview."
        )
    try:
        context: dict[str, Any] = {
            "policyId": str(raw_context.get("policyId") or "").strip(),
            "policyRevision": int(str(raw_context.get("policyRevision"))),
            "connectionRevision": int(str(raw_context.get("connectionRevision"))),
            "providerParentId": str(raw_context.get("providerParentId") or "").strip(),
        }
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Provider relationship context is stale: provider generation evidence is "
            "invalid. Run a new preview."
        ) from error
    if (
        not context["policyId"]
        or int(context["policyRevision"]) < 1
        or int(context["connectionRevision"]) < 1
        or not context["providerParentId"]
    ):
        raise ValueError(
            "Provider relationship context is stale: provider generation evidence is "
            "invalid. Run a new preview."
        )
    return context


def _supplied_relationship_provider_context(
    *,
    policy_id: str | None,
    expected_policy_revision: int | None,
    expected_connection_revision: int | None,
    provider_parent_id: str | None,
) -> dict[str, Any] | None:
    """Normalize an optional caller generation for equality with stored evidence."""

    values = (
        policy_id,
        expected_policy_revision,
        expected_connection_revision,
        provider_parent_id,
    )
    if not any(value is not None for value in values):
        return None
    if any(value is None for value in values) or not policy_id or not provider_parent_id:
        raise ValueError(
            "Provider relationship context is stale: the expected provider generation "
            "is incomplete. Run a new preview."
        )
    assert expected_policy_revision is not None
    assert expected_connection_revision is not None
    try:
        return {
            "policyId": str(policy_id).strip(),
            "policyRevision": int(expected_policy_revision),
            "connectionRevision": int(expected_connection_revision),
            "providerParentId": str(provider_parent_id).strip(),
        }
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Provider relationship context is stale: the expected provider generation "
            "is invalid. Run a new preview."
        ) from error


def _matching_relationship(
    relationships: Iterable[dict[str, Any]],
    from_ci_id: str,
    to_ci_id: str,
    relationship_type: str,
) -> dict[str, Any] | None:
    """Return an exact edge or the reverse edge for a symmetric type."""

    for item in relationships:
        if item.get("type") != relationship_type:
            continue
        if item.get("fromId") == from_ci_id and item.get("toId") == to_ci_id:
            return item
        if (
            relationship_type in SYMMETRIC_RELATIONSHIP_TYPES
            and item.get("fromId") == to_ci_id
            and item.get("toId") == from_ci_id
        ):
            return item
    return None


def _dependency_cycle(
    relationships: Iterable[dict[str, Any]],
    from_ci_id: str,
    to_ci_id: str,
) -> bool:
    """Return true when adding ``from depends_on to`` would close a cycle."""

    adjacency: dict[str, list[str]] = {}
    for item in relationships:
        if item.get("type") == "depends_on":
            adjacency.setdefault(str(item.get("fromId") or ""), []).append(
                str(item.get("toId") or "")
            )
    pending = [to_ci_id]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == from_ci_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, []))
    return False


def _ci_preview_dedupe_key(
    kind: str,
    company_id: str,
    provider_parent_id: str,
    policy_id: str | None,
) -> str:
    """Return a stable, non-secret key that prevents concurrent scope previews."""

    scope = str(policy_id or f"{company_id}:{provider_parent_id}").strip()
    return hashlib.sha256(f"{kind}:ci_preview:{scope}".encode()).hexdigest()


def _ci_preview_operation(kind: str) -> str:
    """Return the provider-neutral history operation for a CI preview."""

    return "device_preview" if kind == "ncentral" else "configuration_preview"


def _ci_preview_schedule_trigger(value: dict[str, Any]) -> str:
    """Return the original trigger that controls policy schedule bookkeeping."""

    raw_attributes = value.get("attributes")
    attributes = raw_attributes if isinstance(raw_attributes, dict) else {}
    return str(
        attributes.get("scheduleTrigger") or attributes.get("trigger") or value.get("trigger") or ""
    )


def _bounded_preview_progress(value: Any, *, fallback_phase: str = "") -> dict[str, Any]:
    """Return a small JSON-safe progress document without provider record payloads."""

    source = value if isinstance(value, dict) else {}
    counts = source.get("counts") if isinstance(source.get("counts"), dict) else {}
    progress = {
        key: deepcopy(source[key])
        for key in (
            "phase",
            "pagesCompleted",
            "devicesDiscovered",
            "devicesEnriched",
            "discovered",
            "enriched",
            "reviewed",
            "current",
            "total",
            "percent",
            "included",
            "excluded",
        )
        if key in source
    }
    discovered = source.get("discovered", source.get("devicesDiscovered"))
    enriched = source.get("enriched", source.get("devicesEnriched"))
    if discovered is not None:
        progress["discovered"] = max(0, int(discovered or 0))
    if enriched is not None:
        progress["enriched"] = max(0, int(enriched or 0))
    if "current" in progress:
        progress["current"] = max(0, int(progress["current"] or 0))
    if "total" in progress:
        progress["total"] = max(0, int(progress["total"] or 0))
    if "reviewed" in progress:
        progress["reviewed"] = max(0, int(progress["reviewed"] or 0))
    if "percent" in progress:
        progress["percent"] = max(0, min(float(progress["percent"] or 0), 100))
    if counts:
        progress["counts"] = {
            key: max(0, int(counts.get(key) or 0))
            for key in ("create", "update", "link", "unchanged", "conflict")
        }
    phase = str(progress.get("phase") or fallback_phase or "")[:80]
    if phase:
        progress["phase"] = phase
    else:
        progress.pop("phase", None)
    return progress


def _sanitized_preview_summary(value: Any) -> dict[str, Any]:
    """Retain aggregate preview evidence while excluding full provider records."""

    source: dict[str, Any] = value if isinstance(value, dict) else {}
    raw_counts = source.get("counts")
    counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
    raw_queue = source.get("queueSummary")
    queue: dict[str, Any] = raw_queue if isinstance(raw_queue, dict) else {}
    raw_policy = source.get("appliedPolicy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    raw_exclusions = source.get("exclusionReasons")
    exclusions: dict[str, Any] = raw_exclusions if isinstance(raw_exclusions, dict) else {}
    raw_mapping = source.get("typeMappingSummary")
    mapping: dict[str, Any] = raw_mapping if isinstance(raw_mapping, dict) else {}
    raw_unmapped = mapping.get("unmappedTypes")
    unmapped = raw_unmapped if isinstance(raw_unmapped, list) else []
    raw_enrichment = source.get("enrichment")
    enrichment: dict[str, Any] = raw_enrichment if isinstance(raw_enrichment, dict) else {}
    raw_presence = source.get("presenceSummary")
    presence: dict[str, Any] = raw_presence if isinstance(raw_presence, dict) else {}
    raw_relationships = source.get("relationshipSummary")
    relationships: dict[str, Any] = raw_relationships if isinstance(raw_relationships, dict) else {}
    applied_policy: dict[str, Any] = {}
    if policy:
        for key in (
            "id",
            "provider",
            "companyId",
            "providerParentId",
            "revision",
        ):
            if key in policy:
                applied_policy[key] = deepcopy(policy[key])
        applied_policy.update(normalize_ci_policy(policy))
    return {
        "discovered": max(0, int(source.get("discovered") or 0)),
        "included": max(0, int(source.get("included") or 0)),
        "excluded": max(0, int(source.get("excluded") or 0)),
        "exclusionReasons": {
            str(key)[:120]: max(0, int(exclusions[key] or 0))
            for key in sorted(exclusions, key=str)[:50]
        },
        "counts": {
            key: max(0, int(counts.get(key) or 0))
            for key in ("create", "update", "link", "unchanged", "conflict")
        },
        "queueSummary": {
            key: max(0, int(queue.get(key) or 0))
            for key in ("pending", "created", "updated", "resolved")
        },
        "typeMappingSummary": {
            key: max(0, int(mapping.get(key) or 0)) for key in ("mapped", "unmapped", "blocked")
        }
        | {
            "unmappedTypes": [
                {
                    "id": str(item.get("id") or "")[:160],
                    "name": str(item.get("name") or "")[:240],
                    "count": max(0, int(item.get("count") or 0)),
                }
                for item in unmapped[:100]
                if isinstance(item, dict)
            ]
        },
        "appliedPolicy": applied_policy,
        "enrichment": {
            "mode": (
                str(enrichment.get("mode"))
                if str(enrichment.get("mode")) in {"fast", "balanced", "full"}
                else "balanced"
            ),
            "assetDetailsRequested": max(
                0,
                min(int(enrichment.get("assetDetailsRequested") or 0), 250),
            ),
            # Retaining these bounded cursors lets balanced previews advance
            # through the fleet without storing any provider record values.
            "offset": max(0, min(int(enrichment.get("offset") or 0), 1_000_000)),
            "nextOffset": max(
                0,
                min(int(enrichment.get("nextOffset") or 0), 1_000_000),
            ),
            "bounded": bool(enrichment.get("bounded", True)),
            "reason": str(enrichment.get("reason") or "")[:240],
        },
        "presenceSummary": {
            "providerReadComplete": bool(presence.get("providerReadComplete")),
            "observedCount": max(0, int(presence.get("observedCount") or 0)),
            "providerFilterApplied": bool(presence.get("providerFilterApplied")),
            "scopeMode": (
                "provider_filtered"
                if str(presence.get("scopeMode") or "").replace("-", "_")
                in {"provider_filtered", "native_filtered", "filtered"}
                else "unfiltered"
            ),
            "discoveryScopeFingerprint": str(presence.get("discoveryScopeFingerprint") or "")[:64],
            "policyDecisionFingerprint": str(presence.get("policyDecisionFingerprint") or "")[:64],
            "connectionRevision": max(0, int(presence.get("connectionRevision") or 0)),
            "policyRevision": max(0, int(presence.get("policyRevision") or 0)),
            "requiredAbsences": max(
                2,
                min(int(presence.get("requiredAbsences") or 3), 10),
            ),
            "minimumMissingHours": max(
                1,
                min(int(presence.get("minimumMissingHours") or 24), 720),
            ),
            "snapshotStartedAt": str(presence.get("snapshotStartedAt") or "")[:160] or None,
            "providerReadCompletedAt": str(presence.get("providerReadCompletedAt") or "")[:160]
            or None,
        },
        "relationshipSummary": {
            key: max(0, int(relationships.get(key) or 0))
            for key in ("observed", "evaluated", "autoApproved", "reviewRequired", "errors")
        }
        | {
            "skippedStalePolicy": bool(relationships.get("skippedStalePolicy")),
            "message": str(relationships.get("message") or "")[:500],
        },
    }


def _normalized_ci_presence_snapshot(value: Any) -> dict[str, Any]:
    """Validate one bounded provider-presence observation envelope."""

    if not isinstance(value, dict):
        raise ValueError("Presence snapshot must be an object")
    raw_records = value.get("observedRecords")
    if not isinstance(raw_records, list):
        raise ValueError("Presence snapshot observedRecords must be an array")
    if len(raw_records) > CI_PRESENCE_MAX_OBSERVED_IDENTITIES:
        raise ValueError("Presence snapshot contains too many observed records")

    provider_filter_id = str(value.get("providerFilterId") or "").strip()[:500]
    raw_scope_mode = str(value.get("scopeMode") or "unfiltered").strip().casefold()
    raw_scope_mode = raw_scope_mode.replace("-", "_")
    if provider_filter_id or raw_scope_mode in {"provider_filtered", "native_filtered", "filtered"}:
        scope_mode = "provider_filtered"
    elif raw_scope_mode == "unfiltered":
        scope_mode = "unfiltered"
    else:
        raise ValueError("Presence snapshot scopeMode is invalid")

    fingerprints: dict[str, str] = {}
    for key, label in (
        ("discoveryScopeFingerprint", "discovery scope fingerprint"),
        ("policyDecisionFingerprint", "policy decision fingerprint"),
    ):
        fingerprint = str(value.get(key) or "").strip().casefold()
        if not CI_PRESENCE_FINGERPRINT.fullmatch(fingerprint):
            raise ValueError(f"Presence snapshot {label} is invalid")
        fingerprints[key] = fingerprint

    started_at = str(value.get("snapshotStartedAt") or "").strip()
    completed_at = str(value.get("providerReadCompletedAt") or "").strip()
    started = parse_timestamp(started_at)
    completed = parse_timestamp(completed_at) if completed_at else None
    if started is None:
        raise ValueError("Presence snapshot start time is invalid")
    if completed_at and completed is None:
        raise ValueError("Presence snapshot completion time is invalid")
    if completed is not None and completed < started:
        raise ValueError("Presence snapshot completion precedes its start")

    observed_records: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise ValueError("Presence snapshot records must be objects")
        external_id = str(raw_record.get("externalId") or "").strip()
        provider_parent_id = str(raw_record.get("providerParentId") or "").strip()
        if not external_id or len(external_id) > 500:
            raise ValueError("Presence snapshot externalId is invalid")
        if not provider_parent_id or len(provider_parent_id) > 500:
            raise ValueError("Presence snapshot providerParentId is invalid")
        if external_id in seen:
            continue
        seen.add(external_id)
        observed_records.append(
            {
                "externalId": external_id,
                "externalName": str(raw_record.get("externalName") or "")[:1000],
                "providerParentId": provider_parent_id,
            }
        )

    def bounded_integer(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            result = int(value.get(key, default))
        except (TypeError, ValueError) as error:
            raise ValueError(f"Presence snapshot {key} must be an integer") from error
        return max(minimum, min(result, maximum))

    return {
        "observedRecords": observed_records,
        "providerReadComplete": bool(value.get("providerReadComplete")),
        "providerFilterId": provider_filter_id,
        "scopeMode": scope_mode,
        **fingerprints,
        "connectionRevision": bounded_integer("connectionRevision", 0, 0, 2_147_483_647),
        "policyRevision": bounded_integer("policyRevision", 0, 0, 2_147_483_647),
        "requiredAbsences": bounded_integer("requiredAbsences", 3, 2, 10),
        "minimumMissingHours": bounded_integer("minimumMissingHours", 24, 1, 720),
        "snapshotStartedAt": started.isoformat().replace("+00:00", "Z"),
        "providerReadCompletedAt": (
            completed.isoformat().replace("+00:00", "Z") if completed is not None else None
        ),
    }


def _ci_presence_action(value: dict[str, Any]) -> tuple[str | None, str]:
    """Return the only safe operator action for a lifecycle row."""

    state = str(value.get("state") or "")
    active = bool(value.get("mappingActive", value.get("active", True)))
    if state == "eligible" and active:
        return "retire", "Required complete snapshots and minimum missing time were reached."
    if state == "restore_ready" and not active:
        return "restore", "The provider observed this previously retired mapping again."
    reasons = {
        "observed": "The provider currently reports this configuration item.",
        "monitoring": "More complete missing snapshots or elapsed time are required.",
        "not_evaluated": "A filtered or incomplete provider read cannot prove absence.",
        "retired": "The immutable provider mapping is retired until it is observed again.",
        "restore_ready": "The mapping must remain retired before it can be restored.",
        "eligible": "The mapping must remain active before it can be retired.",
    }
    return None, reasons.get(state, "No lifecycle action is currently available.")


def _ci_presence_reference_time() -> datetime:
    """Return the trusted clock used to expire provider action evidence."""

    return datetime.now(UTC)


def _ci_presence_evidence_max_age_hours(policy: Any) -> int:
    """Derive a bounded action window from three policy cadences."""

    normalized = normalize_ci_policy(policy if isinstance(policy, dict) else {})
    cadence_minutes = max(15, int(normalized.get("intervalMinutes") or 360))
    three_cadences_hours = (cadence_minutes * 3 + 59) // 60
    return min(168, max(24, three_cadences_hours))


def _ci_presence_stale_evidence_reason(value: dict[str, Any]) -> str:
    """Explain why stored provider evidence no longer authorizes an action."""

    restore_evidence = str(value.get("state") or "") == "restore_ready"
    if not value.get("providerReadComplete"):
        return "The provider read was incomplete. Run a new complete preview."
    try:
        max_age_hours = min(
            168,
            max(24, int(value.get("presenceEvidenceMaxAgeHours") or 24)),
        )
    except (TypeError, ValueError):
        max_age_hours = 24
    completed_at = parse_timestamp(value.get("providerReadCompletedAt"))
    if completed_at is None:
        return "The provider read completion time is unavailable. Run a new complete preview."
    reference_time = _ci_presence_reference_time()
    if reference_time > completed_at + timedelta(hours=max_age_hours):
        return (
            f"The provider evidence is older than the allowed {max_age_hours}-hour window. "
            "Run a new complete preview."
        )
    if restore_evidence:
        observed_at = parse_timestamp(value.get("lastObservedAt") or value.get("reappearedAt"))
        if observed_at is None:
            return (
                "A fresh positive provider observation is required before restoring this mapping."
            )
        if reference_time > observed_at + timedelta(hours=max_age_hours):
            return (
                "The positive provider observation has expired. Run a new complete preview "
                "before restoring this mapping."
            )
    if not restore_evidence and value.get("scopeMode") != "unfiltered":
        return "The stored provider scope was filtered. Run an unfiltered preview."
    if not value.get("policyExists", True):
        return "The CI policy no longer exists. Reconfigure it and run a new preview."
    if not restore_evidence and value.get("currentPolicyFiltered"):
        return "The current CI policy uses provider filtering. Run an unfiltered preview."
    if int(value.get("currentPolicyRevision", value.get("policyRevision") or 0)) != int(
        value.get("policyRevision") or 0
    ):
        return "The CI policy changed after this evidence was captured. Run a new preview."
    if not value.get("integrationExists", True):
        return "The integration connection no longer exists."
    if (
        not value.get("currentIntegrationEnabled", True)
        or str(value.get("currentIntegrationLifecycle") or "active") != "active"
    ):
        return "The integration is not active. Re-enable it and run a new preview."
    if int(value.get("currentConnectionRevision", value.get("connectionRevision") or 0)) != int(
        value.get("connectionRevision") or 0
    ):
        return (
            "The integration settings changed after this evidence was captured. Run a new preview."
        )
    if not value.get("providerCompanyMappingValid", True):
        return (
            "The provider customer mapping changed or is inactive. Remap it and run a new preview."
        )
    mapping_changed = parse_timestamp(value.get("providerCompanyMappingChangedAt"))
    snapshot_started = parse_timestamp(value.get("snapshotStartedAt"))
    if mapping_changed and snapshot_started and mapping_changed > snapshot_started:
        return (
            "The provider customer mapping changed after this preview started. Run a new preview."
        )
    return ""


def _public_ci_presence(value: dict[str, Any]) -> dict[str, Any]:
    """Return one provider-presence row using the stable frontend contract."""

    public = {
        "id": value.get("id"),
        "provider": value.get("provider"),
        "companyId": value.get("companyId"),
        "companyName": value.get("companyName") or "",
        "providerParentId": value.get("providerParentId"),
        "mappingId": value.get("mappingId"),
        "assetId": value.get("assetId") or value.get("ciId"),
        "assetName": value.get("assetName") or "",
        "externalId": value.get("externalId"),
        "externalName": value.get("externalName") or "",
        "state": value.get("state"),
        "mappingActive": bool(value.get("mappingActive", value.get("active", True))),
        "absenceCount": max(0, int(value.get("absenceCount") or 0)),
        "requiredAbsences": max(2, min(int(value.get("requiredAbsences") or 3), 10)),
        "minimumMissingHours": max(1, min(int(value.get("minimumMissingHours") or 24), 720)),
        "firstMissingAt": value.get("firstMissingAt"),
        "lastMissingAt": value.get("lastMissingAt"),
        "candidateSince": value.get("candidateSince"),
        "lastObservedAt": value.get("lastObservedAt"),
        "lastEvaluatedAt": value.get("lastEvaluatedAt"),
        "lastEvaluatedRunId": value.get("lastEvaluatedRunId"),
        "scopeMode": value.get("scopeMode") or "unfiltered",
        "providerReadComplete": bool(value.get("providerReadComplete")),
        "evaluationReason": value.get("evaluationReason") or "",
        "discoveryScopeFingerprint": value.get("discoveryScopeFingerprint") or "",
        "policyDecisionFingerprint": value.get("policyDecisionFingerprint") or "",
        "connectionRevision": max(0, int(value.get("connectionRevision") or 0)),
        "policyRevision": max(0, int(value.get("policyRevision") or 0)),
        "snapshotStartedAt": value.get("snapshotStartedAt"),
        "providerReadCompletedAt": value.get("providerReadCompletedAt"),
        "presenceEvidenceMaxAgeHours": max(
            24,
            min(int(value.get("presenceEvidenceMaxAgeHours") or 24), 168),
        ),
        "revision": max(1, int(value.get("revision") or 1)),
        "reviewedBy": value.get("reviewedBy"),
        "reviewedAt": value.get("reviewedAt"),
        "reviewNotes": value.get("reviewNotes") or "",
        "createdAt": value.get("createdAt"),
        "updatedAt": value.get("updatedAt"),
        "staleEvidenceReason": value.get("staleEvidenceReason") or "",
    }
    public["consecutiveCompleteAbsences"] = public["absenceCount"]
    public["firstAbsentAt"] = public["firstMissingAt"]
    public["retiredAt"] = value.get("retiredAt") or (
        public["reviewedAt"] if public["state"] in {"retired", "restore_ready"} else None
    )
    public["retiredByName"] = value.get("retiredByName") or ""
    public["retirementNotes"] = value.get("retirementNotes") or (
        public["reviewNotes"] if public["state"] in {"retired", "restore_ready"} else ""
    )
    public["reappearedAt"] = (
        public["lastObservedAt"] if public["state"] == "restore_ready" else None
    )
    action, reason = _ci_presence_action(public)
    stale_reason = str(public["staleEvidenceReason"] or _ci_presence_stale_evidence_reason(value))
    if stale_reason:
        action = None
        reason = stale_reason
        public["staleEvidenceReason"] = stale_reason
    public["actionAllowed"] = action is not None
    public["availableAction"] = action
    public["actionReason"] = reason
    return public


def _ci_presence_collection(items: list[dict[str, Any]], total: int | None = None) -> dict:
    """Return a paged lifecycle collection with complete state counts."""

    summary = {
        "total": len(items) if total is None else total,
        "observed": 0,
        "monitoring": 0,
        "eligible": 0,
        "notEvaluated": 0,
        "retired": 0,
        "restoreReady": 0,
    }
    public_keys = {"not_evaluated": "notEvaluated", "restore_ready": "restoreReady"}
    for item in items:
        state = str(item.get("state") or "")
        key = public_keys.get(state, state)
        if key in summary:
            summary[key] += 1
    return {
        "items": items,
        "total": len(items) if total is None else total,
        "summary": summary,
    }


def _public_sync_run(
    value: dict[str, Any],
    *,
    include_internal: bool = False,
) -> dict[str, Any]:
    """Return a worker-safe run without exposing internal ownership or dedupe state."""

    run = deepcopy(value)
    for key in ("leaseOwner", "leaseUntil", "dedupeKey"):
        run.pop(key, None)
    if run.get("status") == "succeeded":
        run["status"] = "success"
    status = str(run.get("status") or "")
    progress = _bounded_preview_progress(
        run.get("progress"),
        fallback_phase=(
            "completed"
            if status in {"success", "review_required"}
            else ("cancelled" if status == "cancelled" else status)
        ),
    )
    run["progress"] = progress
    run["phase"] = progress.get("phase") or status
    run["cancelRequested"] = bool(run.get("cancelRequestedAt"))
    run["canCancel"] = status in ACTIVE_CI_PREVIEW_RUN_STATUSES and not bool(
        run.get("cancelRequestedAt")
    )
    run["canRetry"] = status in RETRYABLE_CI_PREVIEW_RUN_STATUSES
    raw_attributes = run.get("attributes")
    attributes: dict[str, Any] = raw_attributes if isinstance(raw_attributes, dict) else {}
    run.setdefault("providerCompanyId", attributes.get("providerCompanyId"))
    run.setdefault("policyId", attributes.get("policyId"))
    run.setdefault("trigger", attributes.get("trigger", ""))
    run.setdefault("requestedByUserId", run.pop("requestedBy", None))
    summary = _sanitized_preview_summary(attributes.get("resultSummary") or {})
    run["previewSummary"] = summary if attributes.get("resultSummary") else None
    if include_internal:
        run["attributes"] = attributes
        run["policySnapshot"] = deepcopy(attributes.get("policySnapshot") or {})
    else:
        run["attributes"] = {
            key: deepcopy(attributes[key])
            for key in (
                "operation",
                "trigger",
                "companyId",
                "providerCompanyId",
                "policyId",
                "policyRevision",
                "connectionRevision",
                "readOnly",
            )
            if key in attributes
        }
    return run


def updated_worker_runtime(
    current: dict[str, Any] | None,
    worker_name: str,
    worker_id: str,
    deployment_mode: str,
    interval_seconds: int,
    event: str,
    *,
    processed: int = 0,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one worker lifecycle event to a JSON-safe runtime record."""

    now = utc_now()
    record = {
        "workerName": worker_name[:80],
        "workerId": worker_id[:160],
        "deploymentMode": deployment_mode,
        "status": "starting",
        "intervalSeconds": max(1, min(int(interval_seconds), 86400)),
        "lastStartedAt": None,
        "lastHeartbeatAt": now,
        "lastCycleStartedAt": None,
        "lastCycleFinishedAt": None,
        "lastSuccessAt": None,
        "lastErrorAt": None,
        "lastError": "",
        "cyclesCompleted": 0,
        "itemsProcessed": 0,
        "metadata": {},
        **deepcopy(current or {}),
    }
    record.update(
        workerName=worker_name[:80],
        workerId=worker_id[:160],
        deploymentMode=deployment_mode,
        intervalSeconds=max(1, min(int(interval_seconds), 86400)),
        lastHeartbeatAt=now,
    )
    if event == "starting":
        record.update(status="starting", lastStartedAt=now)
    elif event == "cycle_started":
        record.update(status="running", lastCycleStartedAt=now)
    elif event == "heartbeat":
        record["status"] = "running"
    elif event == "cycle_succeeded":
        record.update(
            status="running",
            lastCycleFinishedAt=now,
            lastSuccessAt=now,
            lastError="",
            cyclesCompleted=int(record.get("cyclesCompleted") or 0) + 1,
            itemsProcessed=int(record.get("itemsProcessed") or 0) + max(0, int(processed)),
            metadata=deepcopy(metadata or {}),
        )
    elif event == "cycle_failed":
        record.update(
            status="degraded",
            lastCycleFinishedAt=now,
            lastErrorAt=now,
            lastError=error[:500],
            cyclesCompleted=int(record.get("cyclesCompleted") or 0) + 1,
            metadata=deepcopy(metadata or {}),
        )
    elif event == "stopped":
        record["status"] = "stopped"
    else:
        raise ValueError(f"Unsupported worker runtime event: {event}")
    return record


def canonical_uuid(kind: str, current_id: str) -> str:
    """Keep existing UUIDs and deterministically migrate prototype string IDs."""
    try:
        return str(uuid.UUID(str(current_id)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(CMDB_NAMESPACE, f"{kind}:{current_id}"))


def change_template_version_uuid(template_id: str, version: int) -> str:
    """Return the canonical identity for one immutable template version."""

    return canonical_uuid("change_template_version", f"{template_id}:{int(version)}")


def default_change_template_records() -> list[dict]:
    """Return isolated state-repository copies of the standard catalogue."""

    timestamp = utc_now()
    return [
        {
            **deepcopy(template),
            "id": canonical_uuid("change_template", str(template["key"])),
            "version": 1,
            "versions": [
                {
                    "version": 1,
                    "content": normalize_template_content(template["content"]),
                    "createdBy": None,
                    "createdAt": timestamp,
                }
            ],
            "content": normalize_template_content(template["content"]),
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        for template in DEFAULT_CHANGE_TEMPLATES
    ]


def normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def hash_password(password: str, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iteration_text))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class StateRepository:
    """Local setup/development repository and in-memory unit-test implementation."""

    mode = "state"

    def __init__(self, state: dict, save_state: Callable[[dict], None]):
        self.state = state
        self.save_state = save_state
        self._authentication_lock = threading.RLock()
        self._integration_enrichment_lock_guard = threading.Lock()
        self._integration_enrichment_locks: dict[str, threading.RLock] = {}
        self.state.setdefault("auditEvents", [])
        self.state.setdefault("dataQualityExceptions", [])
        self.state.setdefault("reconciliationCandidates", [])
        self.state.setdefault("fieldAuthority", [])
        self.state.setdefault("integrationObjectSuppressions", [])
        self.state.setdefault("contacts", [])
        self.state.setdefault("contactResponsibilities", [])
        self.state.setdefault("apiTokens", [])
        self.state.setdefault("mfaCredentials", [])
        self.state.setdefault("mfaRecoveryCodes", [])
        self.state.setdefault("loginChallenges", [])
        self.state.setdefault("loginAttempts", [])
        self.state.setdefault("sessions", [])
        self.state.setdefault("passwordResets", [])
        self.state.setdefault("emailConnection", deepcopy(DEFAULT_EMAIL_CONNECTION))
        self.state.setdefault("emailOutbox", [])
        self.state.setdefault(
            "notificationRules",
            [
                {
                    **deepcopy(rule),
                    "id": canonical_uuid("notification_rule", str(rule["key"])),
                    "companyId": None,
                    "fallbackAddresses": [],
                    "maxAttempts": 5,
                    "lastRunAt": None,
                    "revision": 1,
                }
                for rule in DEFAULT_NOTIFICATION_RULES
            ],
        )
        self.state.setdefault(
            "notificationTemplates",
            [
                {
                    **deepcopy(template),
                    "id": canonical_uuid("notification_template", str(template["key"])),
                    "companyId": None,
                    "enabled": True,
                    "version": 1,
                }
                for template in DEFAULT_NOTIFICATION_TEMPLATES
            ],
        )
        self.state.setdefault("notificationPreferences", [])
        self.state.setdefault("notificationEvents", [])
        self.state.setdefault("changeApprovalRequests", [])
        self.state.setdefault("changeTemplates", default_change_template_records())
        self.state.setdefault("providerCompanyObservations", [])
        self.state.setdefault("providerCompanyMappings", [])
        self.state.setdefault("integrationCiPresence", [])
        self.state.setdefault("ciInventorySnapshots", [])
        self.state.setdefault("ciNetworkInterfaces", [])
        self.state.setdefault("ciRelationshipCandidates", [])
        for candidate in self.state["ciRelationshipCandidates"]:
            candidate.setdefault("revision", 1)
            candidate.setdefault("observationCount", 1)
        self.state.setdefault("workerRuntimeStatus", {})
        self.state.setdefault("providerRateLimitStatus", {})

    def record_worker_runtime(
        self,
        worker_name: str,
        worker_id: str,
        deployment_mode: str,
        interval_seconds: int,
        event: str,
        *,
        processed: int = 0,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
        """Persist the latest worker heartbeat for local development and tests."""

        current = self.state["workerRuntimeStatus"].get(worker_name)
        stored = updated_worker_runtime(
            current,
            worker_name,
            worker_id,
            deployment_mode,
            interval_seconds,
            event,
            processed=processed,
            error=error,
            metadata=metadata,
        )
        self.state["workerRuntimeStatus"][worker_name] = stored
        self.save_state(self.state)
        return deepcopy(stored)

    def get_worker_runtime(self, worker_name: str) -> dict | None:
        """Return one worker's latest local runtime evidence."""

        stored = self.state.get("workerRuntimeStatus", {}).get(worker_name)
        return deepcopy(stored) if stored else None

    def record_provider_rate_limit(self, provider: str, observation: dict[str, Any]) -> dict:
        """Retain only the latest sanitized provider throttle observation."""

        stored = {
            "provider": provider[:80],
            "observedAt": utc_now(),
            "httpStatus": observation.get("httpStatus"),
            "limit": observation.get("limit"),
            "remaining": observation.get("remaining"),
            "resetAt": str(observation.get("resetAt") or "")[:160] or None,
            "retryAfterSeconds": observation.get("retryAfterSeconds"),
            "limited": bool(observation.get("limited")),
            "requestPath": str(observation.get("requestPath") or "")[:500],
            "metadata": deepcopy(observation.get("metadata") or {}),
        }
        self.state["providerRateLimitStatus"][provider] = stored
        self.save_state(self.state)
        return deepcopy(stored)

    def get_provider_rate_limit(self, provider: str) -> dict | None:
        """Return the latest local throttle observation for one provider."""

        stored = self.state.get("providerRateLimitStatus", {}).get(provider)
        return deepcopy(stored) if stored else None

    def list_companies(self) -> list[dict]:
        return deepcopy(self.state["companies"])

    def _effective_user(self, user: dict) -> dict:
        record = deepcopy(user)
        direct_company_ids = list(record.get("directCompanyIds", record.get("companyIds", [])))
        if record.get("role") == "platform_admin":
            record["companyIds"] = ["*"]
            direct_company_ids = ["*"]
        else:
            company_ids = set(direct_company_ids)
            groups = {item["id"]: item for item in self.state.get("accessGroups", [])}
            all_company_ids = {item["id"] for item in self.state.get("companies", [])}
            for group_id in record.get("groupIds", []):
                group = groups.get(group_id)
                if not group:
                    continue
                group_company_ids = set(group.get("companyIds", []))
                company_ids.update(
                    all_company_ids if "*" in group_company_ids else group_company_ids
                )
            record["companyIds"] = sorted(company_ids & all_company_ids)
        tokens = [item for item in self.state["apiTokens"] if item["userId"] == record["id"]]
        active_tokens = [
            item
            for item in tokens
            if not item.get("revokedAt") and item.get("expiresAt", "") > utc_now()
        ]
        record.setdefault("displayName", record["email"].split("@", 1)[0])
        record.setdefault("status", "active")
        record.setdefault("apiAccessEnabled", False)
        record.setdefault("mfaRequired", False)
        record.setdefault("groupIds", [])
        record["directCompanyIds"] = direct_company_ids
        record["authSource"] = (
            "entra"
            if record.get("identityProviderSubject")
            else "local"
            if record.get("passwordHash") or record.get("password")
            else "none"
        )
        record["apiTokenCount"] = len(active_tokens)
        record["lastApiUsedAt"] = (
            max((item.get("lastUsedAt") or "" for item in tokens), default="") or None
        )
        credential = next(
            (item for item in self.state["mfaCredentials"] if item["userId"] == record["id"]),
            None,
        )
        record["mfaEnabled"] = bool(credential and credential.get("status") == "enabled")
        record["mfaRecoveryCodesRemaining"] = sum(
            item["userId"] == record["id"] and not item.get("usedAt")
            for item in self.state["mfaRecoveryCodes"]
        )
        return record

    def list_users(
        self, include_inactive: bool = False, include_credentials: bool = False
    ) -> list[dict]:
        del include_credentials
        return [
            self._effective_user(item)
            for item in self.state["users"]
            if include_inactive or item.get("status", "active") not in {"disabled", "archived"}
        ]

    def authenticate(self, email: str, password: str) -> dict | None:
        user = next(
            (item for item in self.state["users"] if item["email"].lower() == email.lower()),
            None,
        )
        if not user or user.get("status", "active") != "active":
            verify_password(password, _DUMMY_PASSWORD_HASH)
            return None
        password_hash = user.get("passwordHash")
        if password_hash:
            return self._effective_user(user) if verify_password(password, password_hash) else None
        plaintext = user.get("password")
        if plaintext:
            # Legacy JSON state still receives one PBKDF2 operation so it does
            # not create a materially faster password-enumeration path.
            verify_password(password, _DUMMY_PASSWORD_HASH)
            if hmac.compare_digest(plaintext, password):
                user["passwordHash"] = hash_password(password)
                user.pop("password", None)
                self.save_state(self.state)
                return self._effective_user(user)
            return None
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None

    def reserve_local_login_attempt(
        self,
        identifier_hash: str,
        requester_hash: str,
        *,
        source_limit: int = 20,
        source_window_seconds: int = 900,
        identifier_limit: int = 5,
        identifier_window_seconds: int = 900,
        pending_ttl_seconds: int = 120,
        audit_window_seconds: int = 300,
    ) -> dict:
        """Atomically reserve local-login capacity in the development repository."""

        now = datetime.now(UTC)
        source_window = timedelta(seconds=max(1, source_window_seconds))
        identifier_window = timedelta(seconds=max(1, identifier_window_seconds))
        pending_window = timedelta(seconds=max(1, pending_ttl_seconds))
        audit_window = timedelta(seconds=max(1, audit_window_seconds))
        with self._authentication_lock:
            retained = [
                item
                for item in self.state.get("loginAttempts", [])
                if (parse_timestamp(item.get("createdAt")) or datetime.min.replace(tzinfo=UTC))
                >= now - timedelta(days=30)
            ]

            def active_expirations(kind: str) -> list[datetime]:
                expirations: list[datetime] = []
                for item in retained:
                    created_at = parse_timestamp(item.get("createdAt"))
                    if not created_at:
                        continue
                    outcome = item.get("outcome")
                    if outcome == "pending":
                        expiration = created_at + pending_window
                    elif outcome == "password_failed":
                        if kind == "identifier" and item.get("identifierClearedAt"):
                            continue
                        expiration = created_at + (
                            identifier_window if kind == "identifier" else source_window
                        )
                    else:
                        continue
                    matches = (
                        item.get("identifierHash") == identifier_hash
                        if kind == "identifier"
                        else item.get("requesterHash") == requester_hash
                    )
                    if matches and expiration > now:
                        expirations.append(expiration)
                return sorted(expirations)

            source_expirations = active_expirations("source")
            identifier_expirations = active_expirations("identifier")
            source_blocked = len(source_expirations) >= max(1, source_limit)
            identifier_blocked = len(identifier_expirations) >= max(1, identifier_limit)
            if source_blocked or identifier_blocked:
                limited_by = "source" if source_blocked else "identifier"
                expirations = source_expirations if source_blocked else identifier_expirations
                limit = max(1, source_limit if source_blocked else identifier_limit)
                release_at = expirations[max(0, len(expirations) - limit)]
                retry_after = max(1, int((release_at - now).total_seconds() + 0.999))
                recent_audit = any(
                    item.get("outcome") == "throttled"
                    and item.get("throttleReason") == limited_by
                    and (
                        item.get("requesterHash") == requester_hash
                        if limited_by == "source"
                        else item.get("identifierHash") == identifier_hash
                    )
                    and (parse_timestamp(item.get("createdAt")) or datetime.min.replace(tzinfo=UTC))
                    >= now - audit_window
                    for item in retained
                )
                if not recent_audit:
                    retained.insert(
                        0,
                        {
                            "id": str(uuid.uuid4()),
                            "identifierHash": identifier_hash,
                            "requesterHash": requester_hash,
                            "outcome": "throttled",
                            "throttleReason": limited_by,
                            "completedAt": utc_now(),
                            "createdAt": utc_now(),
                        },
                    )
                self.state["loginAttempts"] = retained[:5000]
                self.save_state(self.state)
                return {
                    "allowed": False,
                    "attemptId": None,
                    "retryAfterSeconds": retry_after,
                    "auditRequired": not recent_audit,
                }

            attempt = {
                "id": str(uuid.uuid4()),
                "identifierHash": identifier_hash,
                "requesterHash": requester_hash,
                "outcome": "pending",
                "completedAt": None,
                "identifierClearedAt": None,
                "createdAt": utc_now(),
            }
            self.state["loginAttempts"] = [attempt, *retained][:5000]
            self.save_state(self.state)
            return {
                "allowed": True,
                "attemptId": attempt["id"],
                "retryAfterSeconds": 0,
                "auditRequired": False,
            }

    def finish_local_login_attempt(self, attempt_id: str, outcome: str) -> bool:
        """Finalize a reserved password step exactly once."""

        if outcome not in {"password_failed", "password_verified"}:
            raise ValueError("Unsupported local-login attempt outcome")
        with self._authentication_lock:
            attempt = next(
                (
                    item
                    for item in self.state.get("loginAttempts", [])
                    if item.get("id") == attempt_id and item.get("outcome") == "pending"
                ),
                None,
            )
            if not attempt:
                return False
            attempt["outcome"] = outcome
            attempt["completedAt"] = utc_now()
            self.save_state(self.state)
            return True

    def clear_local_login_failures(self, identifier_hash: str) -> int:
        """Clear identifier failures after recovery or a governed account update."""

        cleared = 0
        completed_at = utc_now()
        with self._authentication_lock:
            for attempt in self.state.get("loginAttempts", []):
                if (
                    attempt.get("identifierHash") == identifier_hash
                    and attempt.get("outcome") == "password_failed"
                    and not attempt.get("identifierClearedAt")
                ):
                    attempt["identifierClearedAt"] = completed_at
                    cleared += 1
            if cleared:
                self.save_state(self.state)
        return cleared

    def complete_local_login(self, identifier_hash: str) -> int:
        """Record full authentication and clear only the identifier failure bucket."""

        with self._authentication_lock:
            changed = False
            for attempt in self.state.get("loginAttempts", []):
                if (
                    attempt.get("identifierHash") == identifier_hash
                    and attempt.get("outcome") == "password_verified"
                ):
                    attempt["outcome"] = "succeeded"
                    changed = True
            cleared = self.clear_local_login_failures(identifier_hash)
            if changed and not cleared:
                self.save_state(self.state)
            return cleared

    def create_user(self, user: dict, password: str, actor_id: str | None = None) -> dict:
        stored = {
            "displayName": user["email"].split("@", 1)[0],
            "status": "active",
            "apiAccessEnabled": False,
            "mfaRequired": False,
            "lastLoginAt": None,
            **deepcopy(user),
            "passwordHash": hash_password(password),
        }
        stored.pop("password", None)
        self.state["users"].append(stored)
        company_id = stored.get("companyIds", [None])[0] if stored.get("companyIds") else None
        self._audit(
            company_id,
            actor_id,
            "user",
            stored["id"],
            "created",
            None,
            {key: value for key, value in stored.items() if key != "passwordHash"},
        )
        self.save_state(self.state)
        return self._effective_user(stored)

    def update_user(
        self, user_id: str, changes: dict, actor_id: str | None = None, *, reason: str = ""
    ) -> dict | None:
        user = next((item for item in self.state["users"] if item["id"] == user_id), None)
        if not user:
            return None
        before = {
            key: value for key, value in user.items() if key not in {"password", "passwordHash"}
        }
        user.update(deepcopy(changes))
        user["updatedAt"] = utc_now()
        after = {
            key: value for key, value in user.items() if key not in {"password", "passwordHash"}
        }
        company_id = next(iter(self._effective_user(user).get("companyIds", [])), None)
        self._audit(
            company_id,
            actor_id,
            "user",
            user_id,
            "updated",
            before,
            after,
            reason=reason,
        )
        self.save_state(self.state)
        return self._effective_user(user)

    def set_user_password(
        self,
        user_id: str,
        password: str,
        actor_id: str | None = None,
        *,
        action: str = "password_reset",
    ) -> bool:
        """Replace a local credential and record the supplied audit action."""

        user = next((item for item in self.state["users"] if item["id"] == user_id), None)
        if not user:
            return False
        user["passwordHash"] = hash_password(password)
        user.pop("password", None)
        user["updatedAt"] = utc_now()
        self._audit(
            next(iter(self._effective_user(user).get("companyIds", [])), None),
            actor_id,
            "user",
            user_id,
            action,
            None,
            {"passwordChanged": True},
        )
        self.save_state(self.state)
        return True

    def record_user_login(self, user_id: str) -> None:
        user = next((item for item in self.state["users"] if item["id"] == user_id), None)
        if user:
            user["lastLoginAt"] = utc_now()
            self.save_state(self.state)

    def get_mfa_credential(self, user_id: str) -> dict | None:
        """Return encrypted MFA material for an internal authentication flow."""

        credential = next(
            (item for item in self.state["mfaCredentials"] if item["userId"] == user_id),
            None,
        )
        return deepcopy(credential) if credential else None

    def save_mfa_enrollment(
        self,
        user_id: str,
        encrypted_secret: str,
        nonce: str,
        actor_id: str | None = None,
    ) -> dict:
        """Create or replace a pending TOTP enrollment."""

        self.state["mfaCredentials"] = [
            item for item in self.state["mfaCredentials"] if item["userId"] != user_id
        ]
        self.state["mfaRecoveryCodes"] = [
            item for item in self.state["mfaRecoveryCodes"] if item["userId"] != user_id
        ]
        credential = {
            "userId": user_id,
            "method": "totp",
            "status": "pending",
            "encryptedSecret": encrypted_secret,
            "secretNonce": nonce,
            "keyVersion": 1,
            "lastAcceptedCounter": None,
            "enabledAt": None,
            "updatedAt": utc_now(),
        }
        self.state["mfaCredentials"].append(credential)
        self._audit(
            None,
            actor_id,
            "authentication",
            user_id,
            "mfa_enrollment_started",
            None,
            {"method": "totp", "status": "pending"},
        )
        self.save_state(self.state)
        return deepcopy(credential)

    def enable_mfa(
        self,
        user_id: str,
        counter: int,
        recovery_hashes: list[str],
        actor_id: str | None = None,
    ) -> bool:
        """Activate a verified TOTP credential and replace its recovery codes."""

        credential = next(
            (item for item in self.state["mfaCredentials"] if item["userId"] == user_id),
            None,
        )
        if not credential:
            return False
        credential.update(
            {
                "status": "enabled",
                "lastAcceptedCounter": counter,
                "enabledAt": utc_now(),
                "updatedAt": utc_now(),
            }
        )
        self.state["mfaRecoveryCodes"] = [
            item for item in self.state["mfaRecoveryCodes"] if item["userId"] != user_id
        ] + [
            {
                "id": str(uuid.uuid4()),
                "userId": user_id,
                "codeHash": code_hash,
                "usedAt": None,
                "createdAt": utc_now(),
            }
            for code_hash in recovery_hashes
        ]
        self._audit(
            None,
            actor_id,
            "authentication",
            user_id,
            "mfa_enabled",
            None,
            {"method": "totp", "recoveryCodeCount": len(recovery_hashes)},
        )
        self.save_state(self.state)
        return True

    def accept_mfa_counter(self, user_id: str, counter: int) -> bool:
        """Atomically record a newer accepted TOTP time step."""

        credential = next(
            (item for item in self.state["mfaCredentials"] if item["userId"] == user_id),
            None,
        )
        if (
            not credential
            or credential.get("status") != "enabled"
            or (
                credential.get("lastAcceptedCounter") is not None
                and counter <= credential["lastAcceptedCounter"]
            )
        ):
            return False
        credential["lastAcceptedCounter"] = counter
        credential["updatedAt"] = utc_now()
        self.save_state(self.state)
        return True

    def consume_recovery_code(self, user_id: str, code: str) -> bool:
        """Redeem one matching recovery code and make it unusable thereafter."""

        for item in self.state["mfaRecoveryCodes"]:
            if (
                item["userId"] == user_id
                and not item.get("usedAt")
                and verify_password(code.upper(), item["codeHash"])
            ):
                item["usedAt"] = utc_now()
                self.save_state(self.state)
                return True
        return False

    def replace_recovery_codes(
        self, user_id: str, recovery_hashes: list[str], actor_id: str | None = None
    ) -> bool:
        """Replace recovery codes after a freshly verified MFA challenge."""

        credential = next(
            (
                item
                for item in self.state["mfaCredentials"]
                if item["userId"] == user_id and item.get("status") == "enabled"
            ),
            None,
        )
        if not credential:
            return False
        self.state["mfaRecoveryCodes"] = [
            item for item in self.state["mfaRecoveryCodes"] if item["userId"] != user_id
        ] + [
            {
                "id": str(uuid.uuid4()),
                "userId": user_id,
                "codeHash": code_hash,
                "usedAt": None,
                "createdAt": utc_now(),
            }
            for code_hash in recovery_hashes
        ]
        self._audit(
            None,
            actor_id,
            "authentication",
            user_id,
            "mfa_recovery_codes_regenerated",
            None,
            {"recoveryCodeCount": len(recovery_hashes)},
        )
        self.save_state(self.state)
        return True

    def disable_mfa(
        self,
        user_id: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
        action: str = "mfa_disabled",
        metadata: dict | None = None,
    ) -> bool:
        """Remove TOTP and recovery material while preserving its audit trail."""

        before_count = len(self.state["mfaCredentials"])
        self.state["mfaCredentials"] = [
            item for item in self.state["mfaCredentials"] if item["userId"] != user_id
        ]
        self.state["mfaRecoveryCodes"] = [
            item for item in self.state["mfaRecoveryCodes"] if item["userId"] != user_id
        ]
        if len(self.state["mfaCredentials"]) == before_count:
            return False
        self._audit(
            None,
            actor_id,
            "authentication",
            user_id,
            action,
            {"status": "enabled"},
            {"status": "disabled"},
            reason=reason,
            metadata=metadata,
        )
        self.save_state(self.state)
        return True

    def create_login_challenge(self, challenge: dict) -> None:
        """Persist a password-verified, short-lived MFA login transaction."""

        with self._authentication_lock:
            now = datetime.now(UTC)
            retained = [
                item
                for item in self.state["loginChallenges"]
                if (parse_timestamp(item.get("expiresAt")) or datetime.min.replace(tzinfo=UTC))
                > now - timedelta(days=1)
            ]
            live_for_user = [
                item
                for item in retained
                if item.get("userId") == challenge["userId"]
                and not item.get("consumedAt")
                and (parse_timestamp(item.get("expiresAt")) or datetime.min.replace(tzinfo=UTC))
                > now
            ]
            for stale in sorted(
                live_for_user,
                key=lambda item: str(item.get("expiresAt") or ""),
                reverse=True,
            )[4:]:
                stale["consumedAt"] = utc_now()
            self.state["loginChallenges"] = [deepcopy(challenge), *retained]
            self.save_state(self.state)

    def get_login_challenge(self, token_hash: str) -> dict | None:
        """Return a live, unconsumed login challenge."""

        challenge = next(
            (
                item
                for item in self.state["loginChallenges"]
                if item["tokenHash"] == token_hash
                and not item.get("consumedAt")
                and item["expiresAt"] > utc_now()
                and item.get("attempts", 0) < item.get("maxAttempts", 5)
            ),
            None,
        )
        return deepcopy(challenge) if challenge else None

    def record_login_challenge_attempt(self, token_hash: str) -> int:
        """Increment a live challenge, or return the token's current attempt count."""

        with self._authentication_lock:
            challenge = next(
                (item for item in self.state["loginChallenges"] if item["tokenHash"] == token_hash),
                None,
            )
            if not challenge:
                return 0
            attempts = int(challenge.get("attempts", 0))
            expires_at = parse_timestamp(challenge.get("expiresAt"))
            if (
                not challenge.get("consumedAt")
                and expires_at
                and expires_at > datetime.now(UTC)
                and attempts < int(challenge.get("maxAttempts", 5))
            ):
                attempts += 1
                challenge["attempts"] = attempts
                self.save_state(self.state)
            return attempts

    def consume_login_challenge(self, token_hash: str) -> bool:
        """Mark a successful login transaction as single-use."""

        with self._authentication_lock:
            challenge = next(
                (
                    item
                    for item in self.state["loginChallenges"]
                    if item["tokenHash"] == token_hash
                    and not item.get("consumedAt")
                    and (parse_timestamp(item.get("expiresAt")) or datetime.min.replace(tzinfo=UTC))
                    > datetime.now(UTC)
                    and item.get("attempts", 0) < item.get("maxAttempts", 5)
                ),
                None,
            )
            if not challenge:
                return False
            challenge["consumedAt"] = utc_now()
            self.save_state(self.state)
            return True

    def create_session(self, token_hash: str, user_id: str, expires_at: str) -> None:
        """Persist a hashed browser session token."""

        self.state["sessions"].append(
            {
                "tokenHash": token_hash,
                "userId": user_id,
                "expiresAt": expires_at,
                "createdAt": utc_now(),
                "lastSeenAt": utc_now(),
                "revokedAt": None,
            }
        )
        self.save_state(self.state)

    def authenticate_session(self, token_hash: str) -> str | None:
        """Resolve a live hashed browser session to its user identifier."""

        session = next(
            (
                item
                for item in self.state["sessions"]
                if item["tokenHash"] == token_hash
                and not item.get("revokedAt")
                and item["expiresAt"] > utc_now()
            ),
            None,
        )
        if not session:
            return None
        session["lastSeenAt"] = utc_now()
        self.save_state(self.state)
        return session["userId"]

    def revoke_session(self, token_hash: str) -> bool:
        """Revoke one browser session by its stored hash."""

        session = next(
            (item for item in self.state["sessions"] if item["tokenHash"] == token_hash), None
        )
        if not session:
            return False
        session["revokedAt"] = session.get("revokedAt") or utc_now()
        self.save_state(self.state)
        return True

    def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke every live browser session belonging to one user."""

        count = 0
        for session in self.state["sessions"]:
            if session["userId"] == user_id and not session.get("revokedAt"):
                session["revokedAt"] = utc_now()
                count += 1
        if count:
            self.save_state(self.state)
        return count

    def create_password_reset(
        self,
        reset: dict,
        *,
        identifier_limit: int = 3,
        requester_limit: int = 20,
    ) -> dict | None:
        """Rate-limit and persist one hashed, short-lived recovery transaction."""

        now = datetime.now(UTC)
        identifier_cutoff = now - timedelta(minutes=15)
        requester_cutoff = now - timedelta(hours=1)
        records = self.state.get("passwordResets", [])
        identifier_count = sum(
            item.get("identifierHash") == reset["identifierHash"]
            and (parse_timestamp(item.get("createdAt")) or datetime.min.replace(tzinfo=UTC))
            >= identifier_cutoff
            for item in records
        )
        requester_count = sum(
            item.get("requesterHash") == reset["requesterHash"]
            and (parse_timestamp(item.get("createdAt")) or datetime.min.replace(tzinfo=UTC))
            >= requester_cutoff
            for item in records
        )
        if identifier_count >= identifier_limit or requester_count >= requester_limit:
            return None
        stored = {
            "id": str(uuid.uuid4()),
            "userId": None,
            "consumedAt": None,
            "invalidatedAt": None,
            "createdAt": utc_now(),
            **deepcopy(reset),
        }
        self.state["passwordResets"] = [stored, *records[:999]]
        if stored.get("userId"):
            self._audit(
                None,
                stored["userId"],
                "authentication",
                stored["userId"],
                "password_reset_requested",
                None,
                {"expiresAt": stored["expiresAt"]},
                metadata={"delivery": "email"},
            )
        self.save_state(self.state)
        return deepcopy(stored)

    def get_password_reset(self, token_hash: str) -> dict | None:
        """Return one live recovery transaction without exposing other tokens."""

        reset = next(
            (
                item
                for item in self.state.get("passwordResets", [])
                if item.get("tokenHash") == token_hash
                and item.get("userId")
                and not item.get("consumedAt")
                and not item.get("invalidatedAt")
                and (parse_timestamp(item.get("expiresAt")) or datetime.min.replace(tzinfo=UTC))
                > datetime.now(UTC)
            ),
            None,
        )
        if not reset:
            return None
        user = next(
            (
                item
                for item in self.state["users"]
                if item["id"] == reset["userId"]
                and item.get("status", "active") == "active"
                and (item.get("passwordHash") or item.get("password"))
            ),
            None,
        )
        return {**deepcopy(reset), "email": user["email"]} if user else None

    def complete_password_reset(
        self,
        token_hash: str,
        password: str,
        *,
        revoke_api_tokens: bool = True,
    ) -> dict | None:
        """Consume a recovery token, replace the password, and revoke credentials."""

        reset = self.get_password_reset(token_hash)
        if not reset:
            return None
        user = next(item for item in self.state["users"] if item["id"] == reset["userId"])
        existing_hash = user.get("passwordHash")
        if existing_hash and verify_password(password, existing_hash):
            raise ValueError("New password must be different from the current password")
        user["passwordHash"] = hash_password(password)
        user.pop("password", None)
        user["updatedAt"] = utc_now()
        now = utc_now()
        for item in self.state.get("passwordResets", []):
            if item["tokenHash"] == token_hash:
                item["consumedAt"] = now
            elif item.get("userId") == user["id"] and not item.get("consumedAt"):
                item["invalidatedAt"] = now
        revoked_sessions = 0
        for session in self.state.get("sessions", []):
            if session["userId"] == user["id"] and not session.get("revokedAt"):
                session["revokedAt"] = now
                revoked_sessions += 1
        revoked_tokens = 0
        if revoke_api_tokens:
            for token in self.state.get("apiTokens", []):
                if token["userId"] == user["id"] and not token.get("revokedAt"):
                    token["revokedAt"] = now
                    revoked_tokens += 1
        self._audit(
            None,
            user["id"],
            "authentication",
            user["id"],
            "password_reset_completed",
            None,
            {"passwordChanged": True},
            metadata={
                "revokedSessions": revoked_sessions,
                "revokedApiTokens": revoked_tokens,
                "mfaPreserved": True,
            },
        )
        self.save_state(self.state)
        return {
            "userId": user["id"],
            "email": user["email"],
            "revokedSessions": revoked_sessions,
            "revokedApiTokens": revoked_tokens,
        }

    @staticmethod
    def _public_api_token(token: dict) -> dict:
        return {key: deepcopy(value) for key, value in token.items() if key not in {"tokenHash"}}

    def list_api_tokens(self, user_id: str) -> list[dict]:
        return sorted(
            [
                self._public_api_token(item)
                for item in self.state["apiTokens"]
                if item["userId"] == user_id
            ],
            key=lambda item: item.get("createdAt", ""),
            reverse=True,
        )

    def create_api_token(self, token: dict, actor_id: str | None = None) -> dict:
        stored = {**deepcopy(token), "createdAt": utc_now(), "lastUsedAt": None, "revokedAt": None}
        self.state["apiTokens"].append(stored)
        public = self._public_api_token(stored)
        self._audit(
            next(iter(token.get("companyIds", [])), None),
            actor_id,
            "api_token",
            token["id"],
            "created",
            None,
            public,
        )
        self.save_state(self.state)
        return public

    def authenticate_api_token(self, token_hash: str) -> dict | None:
        token = next(
            (item for item in self.state["apiTokens"] if item["tokenHash"] == token_hash), None
        )
        if not token or token.get("revokedAt") or token.get("expiresAt", "") <= utc_now():
            return None
        user = next((item for item in self.state["users"] if item["id"] == token["userId"]), None)
        if (
            not user
            or user.get("status", "active") != "active"
            or not user.get("apiAccessEnabled", False)
        ):
            return None
        token["lastUsedAt"] = utc_now()
        self.save_state(self.state)
        return {"user": self._effective_user(user), "token": self._public_api_token(token)}

    def revoke_api_token(
        self, token_id: str, actor_id: str | None = None, *, reason: str = ""
    ) -> dict | None:
        token = next((item for item in self.state["apiTokens"] if item["id"] == token_id), None)
        if not token:
            return None
        before = self._public_api_token(token)
        token["revokedAt"] = token.get("revokedAt") or utc_now()
        token["revokedByUserId"] = actor_id
        public = self._public_api_token(token)
        self._audit(
            next(iter(token.get("companyIds", [])), None),
            actor_id,
            "api_token",
            token_id,
            "revoked",
            before,
            public,
            reason=reason,
        )
        self.save_state(self.state)
        return public

    def revoke_user_api_tokens(self, user_id: str, actor_id: str | None = None) -> int:
        count = 0
        for token in self.state["apiTokens"]:
            if token["userId"] == user_id and not token.get("revokedAt"):
                self.revoke_api_token(token["id"], actor_id, reason="User access disabled")
                count += 1
        return count

    def set_user_status(
        self,
        user_id: str,
        status: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> bool:
        user = next((item for item in self.state["users"] if item["id"] == user_id), None)
        if not user:
            return False
        before = {
            key: value for key, value in user.items() if key not in {"password", "passwordHash"}
        }
        user["status"] = status
        user["archivedAt"] = utc_now() if status == "archived" else None
        user["updatedAt"] = utc_now()
        after = {
            key: value for key, value in user.items() if key not in {"password", "passwordHash"}
        }
        company_id = user.get("companyIds", [None])[0] if user.get("companyIds") else None
        self._audit(
            company_id,
            actor_id,
            "user",
            user_id,
            "status_changed",
            before,
            after,
            reason=reason,
        )
        self.save_state(self.state)
        return True

    def list_contacts(self, company_id: str | None = None) -> list[dict]:
        responsibilities = self.state.get("contactResponsibilities", [])
        users = {item["id"]: item for item in self.state.get("users", [])}
        records = []
        for item in self.state.get("contacts", []):
            if company_id and item.get("companyId") != company_id:
                continue
            stored = deepcopy(item)
            stored["responsibilityCount"] = sum(
                assignment.get("contactId") == item["id"] and not assignment.get("effectiveUntil")
                for assignment in responsibilities
            )
            linked_user = users.get(item.get("linkedUserId"))
            stored["portalUser"] = (
                {
                    "id": linked_user["id"],
                    "email": linked_user["email"],
                    "role": linked_user["role"],
                    "status": linked_user.get("status", "active"),
                }
                if linked_user
                else None
            )
            records.append(stored)
        return sorted(records, key=lambda value: (value.get("displayName") or "").casefold())

    def get_contact(self, contact_id: str) -> dict | None:
        return next((item for item in self.list_contacts() if item["id"] == contact_id), None)

    def create_contact(self, contact: dict, actor_id: str | None = None) -> dict:
        stored = {
            **deepcopy(contact),
            "createdAt": contact.get("createdAt") or utc_now(),
            "updatedAt": contact.get("updatedAt") or utc_now(),
        }
        self.state["contacts"].append(stored)
        self._audit(
            stored["companyId"],
            actor_id,
            "contact",
            stored["id"],
            "created",
            None,
            stored,
        )
        self.save_state(self.state)
        created = self.get_contact(stored["id"])
        if created is None:
            raise RuntimeError("Created contact could not be reloaded")
        return created

    def update_contact(
        self,
        contact_id: str,
        changes: dict,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> dict | None:
        contact = next((item for item in self.state["contacts"] if item["id"] == contact_id), None)
        if not contact:
            return None
        before = deepcopy(contact)
        contact.update(deepcopy(changes))
        contact["updatedAt"] = utc_now()
        self._audit(
            contact["companyId"],
            actor_id,
            "contact",
            contact_id,
            "updated",
            before,
            contact,
            reason=reason,
        )
        self.save_state(self.state)
        return self.get_contact(contact_id)

    def list_contact_responsibilities(
        self,
        company_id: str | None = None,
        *,
        contact_id: str | None = None,
        asset_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict]:
        contacts = {item["id"]: item for item in self.state.get("contacts", [])}
        assets = {item["id"]: item for item in self.state.get("assets", [])}
        records = []
        for item in self.state.get("contactResponsibilities", []):
            if company_id and item.get("companyId") != company_id:
                continue
            if contact_id and item.get("contactId") != contact_id:
                continue
            if asset_id and item.get("assetId") != asset_id:
                continue
            if not include_inactive and item.get("effectiveUntil"):
                continue
            contact = contacts.get(item.get("contactId"), {})
            asset = assets.get(item.get("assetId"), {})
            records.append(
                {
                    **deepcopy(item),
                    "contactName": contact.get("displayName", "Unknown contact"),
                    "contactEmail": contact.get("email", ""),
                    "assetName": asset.get("name", "Unknown CI"),
                    "assetType": asset.get("type", ""),
                }
            )
        return sorted(
            records,
            key=lambda value: (
                value.get("effectiveUntil") is not None,
                value.get("role", ""),
                value.get("contactName", ""),
            ),
        )

    def replace_asset_responsibilities(
        self,
        asset_id: str,
        assignments: list[dict],
        company_id: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> list[dict]:
        before = self.list_contact_responsibilities(company_id, asset_id=asset_id)
        normalized_before = sorted(
            (
                item["role"],
                item["contactId"],
                bool(item.get("isPrimary", True)),
                int(item.get("escalationOrder", 1)),
            )
            for item in before
        )
        normalized_after = sorted(
            (
                item["role"],
                item["contactId"],
                bool(item.get("isPrimary", True)),
                int(item.get("escalationOrder", 1)),
            )
            for item in assignments
        )
        if normalized_before == normalized_after:
            return before
        ended_at = utc_now()
        for item in self.state["contactResponsibilities"]:
            if item.get("assetId") == asset_id and not item.get("effectiveUntil"):
                item["effectiveUntil"] = ended_at
                item["endedBy"] = actor_id
        for item in assignments:
            self.state["contactResponsibilities"].append(
                {
                    "id": str(uuid.uuid4()),
                    "companyId": company_id,
                    "assetId": asset_id,
                    "contactId": item["contactId"],
                    "role": item["role"],
                    "isPrimary": bool(item.get("isPrimary", True)),
                    "effectiveFrom": ended_at,
                    "effectiveUntil": None,
                    "escalationOrder": int(item.get("escalationOrder", 1)),
                    "notes": str(item.get("notes") or ""),
                    "source": str(item.get("source") or "manual"),
                    "createdBy": actor_id,
                    "endedBy": None,
                }
            )
        after = self.list_contact_responsibilities(company_id, asset_id=asset_id)
        self._audit(
            company_id,
            actor_id,
            "contact_responsibility",
            asset_id,
            "reassigned",
            {"assignments": before},
            {"assignments": after},
            reason=reason,
        )
        before_by_contact = {(item["contactId"], item["role"]): item for item in before}
        after_by_contact = {(item["contactId"], item["role"]): item for item in after}
        for key in sorted(set(before_by_contact) | set(after_by_contact)):
            if key in before_by_contact and key in after_by_contact:
                continue
            contact_id, role = key
            assigned = key in after_by_contact
            self._audit(
                company_id,
                actor_id,
                "contact",
                contact_id,
                "responsibility_assigned" if assigned else "responsibility_ended",
                before_by_contact.get(key),
                after_by_contact.get(key),
                reason=reason,
                metadata={"assetId": asset_id, "responsibilityRole": role},
            )
        self.save_state(self.state)
        return after

    def list_access_groups(self) -> list[dict]:
        users = {item["id"]: item for item in self.state.get("users", [])}
        records = []
        for item in self.state["accessGroups"]:
            group = deepcopy(item)
            owner = users.get(group.get("ownerUserId"))
            group.setdefault("description", "")
            group.setdefault("membershipMode", "dynamic" if group.get("system") else "manual")
            group.setdefault(
                "membershipRules",
                {"rule": "all_managed_customers"} if group.get("system") else {},
            )
            group.setdefault("revision", 1)
            group.setdefault("updatedAt", None)
            group["ownerLabel"] = (
                "System"
                if group.get("system")
                else (owner.get("displayName") or owner["email"] if owner else "Unassigned")
            )
            group["assignedUserCount"] = sum(
                group["id"] in user.get("groupIds", [])
                and user.get("status", "active") != "archived"
                for user in users.values()
            )
            records.append(group)
        return sorted(records, key=lambda item: (not item.get("system", False), item["name"]))

    def create_access_group(self, group: dict, actor_id: str | None = None) -> dict:
        stored = {
            "description": "",
            "membershipMode": "manual",
            "membershipRules": {},
            "ownerUserId": actor_id,
            "revision": 1,
            "updatedAt": utc_now(),
            **deepcopy(group),
        }
        self.state["accessGroups"].append(stored)
        self._audit(None, actor_id, "access_group", stored["id"], "created", None, stored)
        self.save_state(self.state)
        return next(item for item in self.list_access_groups() if item["id"] == stored["id"])

    def update_access_group(
        self, group_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        group = next(
            (item for item in self.state["accessGroups"] if item["id"] == group_id),
            None,
        )
        if not group:
            return None
        before = deepcopy(group)
        values = deepcopy(changes)
        expected_revision = values.pop("expectedRevision", group.get("revision", 1))
        if expected_revision != group.get("revision", 1):
            return None
        group.update(values)
        group["revision"] = group.get("revision", 1) + 1
        group["updatedAt"] = utc_now()
        self._audit(None, actor_id, "access_group", group_id, "updated", before, group)
        self.save_state(self.state)
        return next(item for item in self.list_access_groups() if item["id"] == group_id)

    def delete_access_group(self, group_id: str, actor_id: str | None = None) -> bool:
        group = next(
            (item for item in self.state["accessGroups"] if item["id"] == group_id),
            None,
        )
        if not group:
            return False
        self.state["accessGroups"] = [
            item for item in self.state["accessGroups"] if item["id"] != group_id
        ]
        for user in self.state.get("users", []):
            user["groupIds"] = [item for item in user.get("groupIds", []) if item != group_id]
        self._audit(None, actor_id, "access_group", group_id, "deleted", group, None)
        self.save_state(self.state)
        return True

    def create_company(self, company: dict, actor_id: str | None = None) -> dict:
        self.state["companies"].append(deepcopy(company))
        self._audit(company["id"], actor_id, "company", company["id"], "created", None, company)
        self.save_state(self.state)
        return deepcopy(company)

    def list_assets(self) -> list[dict]:
        assignments = self.list_contact_responsibilities()
        by_asset: dict[str, list[dict]] = {}
        for item in assignments:
            by_asset.setdefault(item["assetId"], []).append(item)
        return [
            {**deepcopy(item), "responsibilities": by_asset.get(item["id"], [])}
            for item in self.state["assets"]
        ]

    def get_asset(self, asset_id: str) -> dict | None:
        value = next((item for item in self.list_assets() if item["id"] == asset_id), None)
        return deepcopy(value) if value else None

    def create_asset(self, asset: dict, actor_id: str | None = None) -> dict:
        self.state["assets"].append(deepcopy(asset))
        self._audit(
            asset["companyId"],
            actor_id,
            "configuration_item",
            asset["id"],
            "created",
            None,
            asset,
        )
        self.save_state(self.state)
        return deepcopy(asset)

    def update_asset(
        self, asset_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        asset = next((item for item in self.state["assets"] if item["id"] == asset_id), None)
        if not asset:
            return None
        before = deepcopy(asset)
        asset.update(deepcopy(changes))
        asset["updatedAt"] = utc_now()
        self._audit(
            asset["companyId"],
            actor_id,
            "configuration_item",
            asset_id,
            "updated",
            before,
            asset,
        )
        self.save_state(self.state)
        return deepcopy(asset)

    def list_relationships(self) -> list[dict]:
        return deepcopy(self.state["relationships"])

    def create_relationship(
        self, relationship: dict, company_id: str, actor_id: str | None = None
    ) -> dict:
        evidence = deepcopy(relationship.get("evidence") or {})
        if not isinstance(evidence, dict):
            raise ValueError("Relationship evidence must be an object")
        confidence = float(relationship.get("confidence", 1))
        if confidence < 0 or confidence > 1:
            raise ValueError("Relationship confidence must be between 0 and 1")
        source_mapping_id = relationship.get("sourceMappingId") or None
        provenance = str(
            relationship.get("provenance") or ("provider" if source_mapping_id else "manual")
        )
        if provenance not in {"manual", "provider"}:
            raise ValueError("Relationship provenance is invalid")
        stored = {
            **deepcopy(relationship),
            "sourceMappingId": source_mapping_id,
            "confidence": confidence,
            "evidence": evidence,
            "provenance": provenance,
        }
        existing = next(
            (
                item
                for item in self.state["relationships"]
                if item.get("fromId") == stored.get("fromId")
                and item.get("toId") == stored.get("toId")
                and item.get("type") == stored.get("type")
            ),
            None,
        )
        if existing:
            before = deepcopy(existing)
            existing.setdefault("sourceMappingId", None)
            existing.setdefault("confidence", 1.0)
            existing.setdefault("evidence", {})
            existing.setdefault("provenance", "manual")
            if provenance == "manual" or existing.get("provenance", "manual") != "manual":
                existing.update(stored)
            stored = existing
            action = "reactivated"
        else:
            self.state["relationships"].append(stored)
            before = None
            action = "created"
        self._audit(
            company_id,
            actor_id,
            "relationship",
            stored["id"],
            action,
            before,
            stored,
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def delete_relationship(
        self, relationship_id: str, company_id: str, actor_id: str | None = None
    ) -> bool:
        relationship = next(
            (item for item in self.state["relationships"] if item["id"] == relationship_id),
            None,
        )
        if not relationship:
            return False
        self.state["relationships"] = [
            item for item in self.state["relationships"] if item["id"] != relationship_id
        ]
        self._audit(
            company_id,
            actor_id,
            "relationship",
            relationship_id,
            "retired",
            relationship,
            None,
        )
        self.save_state(self.state)
        return True

    def list_data_quality_exceptions(self, company_id: str | None = None) -> list[dict]:
        return deepcopy(
            [
                item
                for item in self.state.get("dataQualityExceptions", [])
                if company_id is None or item.get("companyId") == company_id
            ]
        )

    def create_data_quality_exception(self, exception: dict, actor_id: str | None = None) -> dict:
        stored = {
            **deepcopy(exception),
            "id": canonical_uuid("data_quality_exception", exception["id"]),
            "state": "active",
            "createdAt": utc_now(),
        }
        existing = next(
            (
                item
                for item in self.state["dataQualityExceptions"]
                if item.get("companyId") == stored["companyId"]
                and item.get("ruleKey") == stored["ruleKey"]
                and item.get("entityId") == stored["entityId"]
                and item.get("state") == "active"
            ),
            None,
        )
        if existing:
            before = deepcopy(existing)
            existing.update(stored)
            stored = existing
            action = "updated"
        else:
            self.state["dataQualityExceptions"].append(stored)
            before = None
            action = "created"
        self._audit(
            stored["companyId"],
            actor_id,
            "data_quality_exception",
            stored["id"],
            action,
            before,
            stored,
            reason=stored.get("reason", ""),
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def resolve_data_quality_exception(
        self, exception_id: str, actor_id: str | None = None
    ) -> dict | None:
        item = next(
            (
                value
                for value in self.state.get("dataQualityExceptions", [])
                if value["id"] == exception_id
            ),
            None,
        )
        if not item:
            return None
        before = deepcopy(item)
        item.update(state="resolved", resolvedAt=utc_now(), resolvedBy=actor_id)
        self._audit(
            item["companyId"],
            actor_id,
            "data_quality_exception",
            item["id"],
            "resolved",
            before,
            item,
        )
        self.save_state(self.state)
        return deepcopy(item)

    def list_reconciliation_candidates(
        self, company_id: str | None = None, state: str | None = None
    ) -> list[dict]:
        return deepcopy(
            [
                item
                for item in self.state.get("reconciliationCandidates", [])
                if (company_id is None or item.get("companyId") == company_id)
                and (not state or item.get("state", "pending") == state)
            ]
        )

    def resolve_reconciliation_candidate(
        self,
        candidate_id: str,
        decision: str,
        notes: str,
        target_ci_id: str | None,
        actor_id: str | None = None,
    ) -> dict | None:
        item = next(
            (
                value
                for value in self.state.get("reconciliationCandidates", [])
                if value["id"] == candidate_id
            ),
            None,
        )
        if not item:
            return None
        before = deepcopy(item)
        item.update(
            state="approved" if decision in {"use_existing", "create_new"} else "rejected",
            decision=decision,
            decisionNotes=notes,
            targetAssetId=target_ci_id,
            reviewedBy=actor_id,
            reviewedAt=utc_now(),
        )
        self._audit(
            item.get("companyId"),
            actor_id,
            "reconciliation_candidate",
            item["id"],
            "decision_recorded",
            before,
            item,
            reason=notes,
        )
        self.save_state(self.state)
        return deepcopy(item)

    def list_field_authority(self, company_id: str | None = None) -> list[dict]:
        return deepcopy(
            [
                item
                for item in self.state.get("fieldAuthority", [])
                if company_id is None or item.get("companyId") == company_id
            ]
        )

    def upsert_field_authority(self, rule: dict, actor_id: str | None = None) -> dict:
        existing = next(
            (
                item
                for item in self.state["fieldAuthority"]
                if all(
                    item.get(key) == rule.get(key)
                    for key in ("companyId", "ciType", "fieldName", "provider")
                )
            ),
            None,
        )
        before = deepcopy(existing) if existing else None
        if existing:
            existing.update(deepcopy(rule))
            stored = existing
        else:
            stored = deepcopy(rule)
            self.state["fieldAuthority"].append(stored)
        rule_id = canonical_uuid(
            "field_authority",
            f"{rule['companyId']}:{rule['ciType']}:{rule['fieldName']}:{rule['provider']}",
        )
        self._audit(
            rule["companyId"],
            actor_id,
            "field_authority",
            rule_id,
            "updated" if before else "created",
            before,
            stored,
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def delete_field_authority(
        self,
        company_id: str,
        ci_type: str,
        field_name: str,
        provider: str,
        actor_id: str | None = None,
    ) -> bool:
        existing = next(
            (
                item
                for item in self.state["fieldAuthority"]
                if item.get("companyId") == company_id
                and item.get("ciType") == ci_type
                and item.get("fieldName") == field_name
                and item.get("provider") == provider
            ),
            None,
        )
        if not existing:
            return False
        self.state["fieldAuthority"].remove(existing)
        rule_id = canonical_uuid(
            "field_authority", f"{company_id}:{ci_type}:{field_name}:{provider}"
        )
        self._audit(company_id, actor_id, "field_authority", rule_id, "deleted", existing, None)
        self.save_state(self.state)
        return True

    def _postgres_list_changes(
        self,
        company_id: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict]:
        # canonical_uuid is intentionally idempotent for UUID input while retaining
        # compatibility with legacy prototype IDs migrated deterministically to UUIDs.
        asset_uuid = canonical_uuid("configuration_item", asset_id) if asset_id else None
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            # The appended fragment is fixed; customer and CI values remain bound parameters.
            cursor.execute(
                self._change_select_sql()
                + """
                  WHERE (%s::text IS NULL OR c.slug = %s)
                    AND (
                        %s::uuid IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM change_impact_snapshots impact
                            WHERE impact.change_id = cr.id
                              AND impact.ci_id = %s::uuid
                              AND impact.included = true
                        )
                    )
                  ORDER BY cr.created_at DESC, cr.change_number DESC
                """,  # nosec B608
                (company_id, company_id, asset_uuid, asset_uuid),
            )
            return [self._change_from_row(cursor, row) for row in cursor.fetchall()]

    def _postgres_get_change(self, change_id: str) -> dict | None:
        try:
            parsed_id = str(uuid.UUID(change_id))
        except (ValueError, TypeError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(self._change_select_sql() + " WHERE cr.id = %s::uuid", (parsed_id,))
            row = cursor.fetchone()
            return self._change_from_row(cursor, row) if row else None

    def _postgres_next_change_number(self, year: int) -> str:
        prefix = f"CHG-{year}-"
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                """
                SELECT COALESCE(MAX(substring(change_number from '[0-9]+$')::integer), 0) + 1
                FROM change_requests WHERE change_number ~ %s
                """,
                (f"^{prefix}[0-9]+$",),
            )
            sequence = int(cursor.fetchone()[0])
        return f"{prefix}{sequence:04d}"

    def _postgres_create_change(self, change: dict, actor_id: str | None = None) -> dict:
        stored = {
            **deepcopy(change),
            "id": canonical_uuid("change_request", change["id"]),
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            self._write_change(cursor, stored, actor_id)
            self._insert_audit(  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
                cursor,
                stored["companyId"],
                actor_id,
                "change_request",
                stored["id"],
                "created",
                None,
                stored,
            )
        self._refresh_state_mirror()  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
        self.save_state(self.state)
        created = self.get_change(stored["id"])
        if created is None:
            raise RuntimeError("Created change could not be reloaded")
        return created

    def _postgres_update_change(
        self,
        change_id: str,
        change: dict,
        actor_id: str | None = None,
        action: str = "updated",
        reason: str = "",
    ) -> dict | None:
        before = self.get_change(change_id)
        if not before:
            return None
        stored = {**deepcopy(change), "id": canonical_uuid("change_request", change_id)}
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            self._write_change(cursor, stored, actor_id)
            self._insert_audit(  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
                cursor,
                stored["companyId"],
                actor_id,
                "change_request",
                stored["id"],
                action,
                before,
                stored,
                reason=reason,
            )
        self._refresh_state_mirror()  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
        self.save_state(self.state)
        return self.get_change(stored["id"])

    @staticmethod
    def _approval_from_row(row: tuple, *, include_token: bool = False) -> dict:
        (
            request_id,
            change_id,
            company_slug,
            batch_id,
            change_revision,
            approver_contact_id,
            approver_user_id,
            approver_name,
            approver_email,
            responsibility_role,
            scope_value,
            status,
            token_hash,
            expires_at,
            delivery_status,
            provider_request_id,
            last_error,
            decision_comments,
            decided_at,
            created_at,
            updated_at,
        ) = row
        record = {
            "id": str(request_id),
            "changeId": str(change_id),
            "companyId": company_slug,
            "batchId": str(batch_id),
            "changeRevision": int(change_revision),
            "approverContactId": str(approver_contact_id) if approver_contact_id else None,
            "approverUserId": str(approver_user_id) if approver_user_id else None,
            "approverName": approver_name,
            "approverEmail": str(approver_email),
            "responsibilityRole": responsibility_role,
            "scope": scope_value or [],
            "status": status,
            "expiresAt": StateRepository._timestamp(expires_at),
            "deliveryStatus": delivery_status,
            "providerRequestId": provider_request_id or "",
            "lastError": last_error or "",
            "decisionComments": decision_comments or "",
            "decidedAt": StateRepository._timestamp(decided_at),
            "createdAt": StateRepository._timestamp(created_at),
            "updatedAt": StateRepository._timestamp(updated_at),
        }
        if include_token:
            record["tokenHash"] = token_hash
        return record

    @staticmethod
    def _approval_select_sql() -> str:
        return """
            SELECT request.id, request.change_id, company.slug, request.batch_id,
                   request.change_revision, request.approver_contact_id,
                   request.approver_user_id, request.approver_name,
                   request.approver_email::text, request.responsibility_role,
                   request.scope, request.status, request.token_hash,
                   request.expires_at, request.delivery_status,
                   request.provider_request_id, request.last_error,
                   request.decision_comments, request.decided_at,
                   request.created_at, request.updated_at
            FROM change_approval_requests request
            JOIN companies company ON company.id = request.company_id
        """

    def _postgres_create_change_approval_request(
        self, record: dict, actor_id: str | None = None
    ) -> dict:
        stored = {**deepcopy(record), "id": str(record.get("id") or uuid.uuid4())}
        actor_uuid = canonical_uuid("user", actor_id) if actor_id else None
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                """
                INSERT INTO change_approval_requests (
                    id, change_id, company_id, batch_id, change_revision,
                    approver_contact_id, approver_user_id, approver_name,
                    approver_email, responsibility_role, scope, status, token_hash,
                    expires_at, delivery_status, provider_request_id, last_error, created_by
                ) VALUES (
                    %s::uuid, %s::uuid,
                    (SELECT id FROM companies WHERE slug = %s), %s::uuid, %s,
                    %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s,
                    %s::timestamptz, %s, %s, %s, %s::uuid
                )
                """,
                (
                    stored["id"],
                    stored["changeId"],
                    stored["companyId"],
                    stored["batchId"],
                    int(stored["changeRevision"]),
                    stored.get("approverContactId"),
                    stored.get("approverUserId"),
                    stored["approverName"],
                    stored["approverEmail"],
                    stored["responsibilityRole"],
                    json.dumps(stored.get("scope") or []),
                    stored.get("status", "pending"),
                    stored["tokenHash"],
                    stored["expiresAt"],
                    stored.get("deliveryStatus", "pending"),
                    stored.get("providerRequestId") or None,
                    stored.get("lastError") or None,
                    actor_uuid,
                ),
            )
            cursor.execute(
                self._approval_select_sql() + " WHERE request.id = %s::uuid", (stored["id"],)
            )
            created = self._approval_from_row(cursor.fetchone())
            self._insert_audit(  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
                cursor,
                stored["companyId"],
                actor_id,
                "change_approval_request",
                stored["id"],
                "created",
                None,
                created,
            )
        return created

    def _postgres_list_change_approval_requests(self, change_id: str) -> list[dict]:
        try:
            parsed_id = str(uuid.UUID(change_id))
        except (ValueError, TypeError):
            return []
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                self._approval_select_sql()
                + " WHERE request.change_id = %s::uuid ORDER BY request.created_at DESC, request.id",
                (parsed_id,),
            )
            return [self._approval_from_row(row) for row in cursor.fetchall()]

    def _postgres_get_change_approval_request_by_token(self, token_hash: str) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                self._approval_select_sql() + " WHERE request.token_hash = %s",
                (token_hash,),
            )
            row = cursor.fetchone()
            return self._approval_from_row(row, include_token=True) if row else None

    def _postgres_update_change_approval_request(
        self,
        request_id: str,
        changes: dict,
        actor_id: str | None = None,
    ) -> dict | None:
        try:
            parsed_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                self._approval_select_sql() + " WHERE request.id = %s::uuid", (parsed_id,)
            )
            existing_row = cursor.fetchone()
            if not existing_row:
                return None
            existing = self._approval_from_row(existing_row)
            cursor.execute(
                """
                UPDATE change_approval_requests SET
                    delivery_status = COALESCE(%s, delivery_status),
                    provider_request_id = CASE WHEN %s THEN %s ELSE provider_request_id END,
                    last_error = CASE WHEN %s THEN %s ELSE last_error END,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    changes.get("deliveryStatus"),
                    "providerRequestId" in changes,
                    changes.get("providerRequestId") or None,
                    "lastError" in changes,
                    changes.get("lastError") or None,
                    parsed_id,
                ),
            )
            if not cursor.rowcount:
                return None
            cursor.execute(
                self._approval_select_sql() + " WHERE request.id = %s::uuid", (parsed_id,)
            )
            updated = self._approval_from_row(cursor.fetchone())
            self._insert_audit(  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
                cursor,
                updated["companyId"],
                actor_id,
                "change_approval_request",
                parsed_id,
                "delivery_updated",
                existing,
                updated,
            )
        return updated

    def _postgres_decide_change_approval_request(
        self, token_hash: str, decision: str, comments: str
    ) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                """
                UPDATE change_approval_requests SET
                    status = %s, decision_comments = %s, decided_at = now(), updated_at = now()
                WHERE token_hash = %s AND status = 'pending' AND expires_at > now()
                RETURNING id
                """,
                (decision, str(comments or "").strip()[:8000], token_hash),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    """
                    UPDATE change_approval_requests SET status = 'expired', updated_at = now()
                    WHERE token_hash = %s AND status = 'pending' AND expires_at <= now()
                    """,
                    (token_hash,),
                )
                return None
            cursor.execute(
                self._approval_select_sql() + " WHERE request.id = %s::uuid", (str(row[0]),)
            )
            return self._approval_from_row(cursor.fetchone())

    def _postgres_revoke_change_approval_requests(
        self,
        change_id: str,
        actor_id: str | None = None,
        reason: str = "Approval request replaced",
        batch_id: str | None = None,
    ) -> int:
        try:
            parsed_change_id = str(uuid.UUID(change_id))
            parsed_batch_id = str(uuid.UUID(batch_id)) if batch_id else None
        except (ValueError, TypeError):
            return 0
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                self._approval_select_sql()
                + """
                  WHERE request.change_id = %s::uuid AND request.status = 'pending'
                    AND (%s::uuid IS NULL OR request.batch_id = %s::uuid)
                  ORDER BY request.created_at, request.id
                """,
                (parsed_change_id, parsed_batch_id, parsed_batch_id),
            )
            existing = [self._approval_from_row(row) for row in cursor.fetchall()]
            if not existing:
                return 0
            cursor.execute(
                """
                UPDATE change_approval_requests SET
                    status = 'revoked', last_error = %s, updated_at = now()
                WHERE change_id = %s::uuid AND status = 'pending'
                  AND (%s::uuid IS NULL OR batch_id = %s::uuid)
                """,
                (reason[:2000], parsed_change_id, parsed_batch_id, parsed_batch_id),
            )
            revoked_count = max(0, cursor.rowcount)
            for before in existing:
                cursor.execute(
                    self._approval_select_sql() + " WHERE request.id = %s::uuid",
                    (before["id"],),
                )
                after = self._approval_from_row(cursor.fetchone())
                self._insert_audit(  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
                    cursor,
                    before["companyId"],
                    actor_id,
                    "change_approval_request",
                    before["id"],
                    "revoked",
                    before,
                    after,
                    reason=reason,
                )
            return revoked_count

    @staticmethod
    def _change_select_sql() -> str:
        return """
            SELECT cr.id, c.slug, c.name, cr.change_number, cr.title, cr.status,
                   cr.change_type, cr.category, cr.priority, cr.risk_level, cr.risk_source,
                   cr.outage_expected, cr.planned_start, cr.planned_end, cr.reason,
                   cr.business_impact, cr.implementation_plan, cr.validation_plan,
                   cr.rollback_plan, cr.communication_status, cr.communication_plan,
                   cr.assigned_user_id, cr.assigned_technician, cr.assignment_history,
                   cr.template_id, cr.template_version, cr.template_snapshot,
                   cr.template_parameters,
                   cr.approver, cr.notes, cr.impact_summary,
                   cr.risk_assessment, cr.revision, cr.actual_start, cr.actual_end,
                   cr.actual_outage_minutes, cr.outcome, cr.failure_reason,
                   cr.validation_result, cr.rollback_executed, cr.rollback_result,
                   cr.closure_notes, cr.closure_assessment,
                   cr.approvals, cr.status_history, u.id, u.email::text,
                   cr.created_at, cr.updated_at
            FROM change_requests cr
            JOIN companies c ON c.id = cr.company_id
            LEFT JOIN users u ON u.id = cr.created_by
        """

    def _change_from_row(self, cursor: Any, row: tuple) -> dict:
        (
            change_id,
            company_slug,
            company_name,
            number,
            title,
            status,
            change_type,
            category,
            priority,
            risk_level,
            risk_source,
            outage_expected,
            planned_start,
            planned_end,
            reason,
            business_impact,
            implementation_plan,
            validation_plan,
            rollback_plan,
            communication_status,
            communication_plan,
            assigned_user_id,
            assigned_technician,
            assignment_history,
            template_id,
            template_version,
            template_snapshot,
            template_parameters,
            approver,
            notes,
            impact_summary,
            risk_assessment,
            revision,
            actual_start,
            actual_end,
            actual_outage_minutes,
            outcome,
            failure_reason,
            validation_result,
            rollback_executed,
            rollback_result,
            closure_notes,
            closure_assessment,
            approvals,
            status_history,
            creator_id,
            creator_email,
            created_at,
            updated_at,
        ) = row
        change_id_text = str(change_id)
        cursor.execute(
            "SELECT ci_id, ci_snapshot FROM change_scope_items WHERE change_id = %s::uuid ORDER BY ordinal, id",
            (change_id_text,),
        )
        scope_rows = cursor.fetchall()
        scope_asset_ids = [
            str(ci_id) if ci_id else snapshot.get("assetId") for ci_id, snapshot in scope_rows
        ]
        cursor.execute(
            """
            SELECT ci_id, impact_role, depth, relationship_path, ci_snapshot
            FROM change_impact_snapshots
            WHERE change_id = %s::uuid AND included = true
            ORDER BY ordinal, depth, id
            """,
            (change_id_text,),
        )
        impact_snapshot = []
        role_names = {
            "scope": "Scope",
            "direct": "Direct impact",
            "downstream": "Downstream impact",
        }
        for ci_id, impact_role, depth, path_value, snapshot in cursor.fetchall():
            item = dict(snapshot or {})
            path_value = path_value or {}
            if isinstance(path_value, list):
                relationship_types, path_asset_ids = (
                    path_value,
                    item.get("pathAssetIds", []),
                )
            else:
                relationship_types = path_value.get("relationshipTypes", [])
                path_asset_ids = path_value.get("assetIds", item.get("pathAssetIds", []))
            item.update(
                assetId=str(ci_id) if ci_id else item.get("assetId"),
                role=role_names.get(impact_role, item.get("role", "Downstream impact")),
                depth=depth,
                relationshipPath=relationship_types,
                pathAssetIds=path_asset_ids,
            )
            impact_snapshot.append(item)
        cursor.execute(
            """
            SELECT provider, external_id, external_url, sync_status, last_attempt_at, last_error
            FROM change_external_links WHERE change_id = %s::uuid ORDER BY provider
            """,
            (change_id_text,),
        )
        external_rows = cursor.fetchall()
        integration_state = {}
        external_references = []
        for (
            provider,
            external_id,
            external_url,
            sync_status,
            last_attempt_at,
            last_error,
        ) in external_rows:
            integration_state[provider] = {
                "status": sync_status,
                "ticketId": external_id,
                "ticketUrl": external_url,
                "lastAttemptAt": self._timestamp(last_attempt_at) or None,
                "error": last_error,
            }
            if external_id or external_url:
                external_references.append(
                    {
                        "provider": provider,
                        "externalId": external_id,
                        "url": external_url,
                    }
                )
        integration_state.setdefault(
            "connectwise",
            {
                "status": "not_published",
                "ticketId": None,
                "ticketUrl": None,
                "lastAttemptAt": None,
                "error": None,
            },
        )
        return {
            "id": change_id_text,
            "number": number,
            "companyId": company_slug,
            "companyName": company_name,
            "title": title,
            "status": status,
            "changeType": change_type,
            "category": category,
            "priority": priority,
            "riskLevel": risk_level,
            "riskSource": risk_source,
            "riskAssessment": risk_assessment or {},
            "outageExpected": outage_expected,
            "plannedStart": self._timestamp(planned_start),
            "plannedEnd": self._timestamp(planned_end),
            "reason": reason,
            "businessImpact": business_impact or "",
            "implementationPlan": implementation_plan,
            "validationPlan": validation_plan,
            "rollbackPlan": rollback_plan,
            "communicationStatus": communication_status,
            "communicationPlan": communication_plan or "",
            "assignedUserId": str(assigned_user_id) if assigned_user_id else None,
            "assignedTechnician": assigned_technician or "",
            "assignmentHistory": assignment_history or [],
            "templateId": str(template_id) if template_id else None,
            "templateVersion": int(template_version) if template_version else None,
            "templateSnapshot": template_snapshot,
            "templateParameters": template_parameters or {},
            "approver": approver or "",
            "notes": notes or "",
            "scopeAssetIds": [item for item in scope_asset_ids if item],
            "impactSnapshot": impact_snapshot,
            "impactSummary": impact_summary or {},
            "createdBy": {
                "id": str(creator_id) if creator_id else None,
                "email": creator_email or "",
            },
            "createdAt": self._timestamp(created_at),
            "updatedAt": self._timestamp(updated_at),
            "revision": revision,
            "actualStart": self._timestamp(actual_start),
            "actualEnd": self._timestamp(actual_end),
            "actualOutageMinutes": int(actual_outage_minutes or 0),
            "outcome": outcome or "pending",
            "failureReason": failure_reason or "",
            "validationResult": validation_result or "",
            "rollbackExecuted": bool(rollback_executed),
            "rollbackResult": rollback_result or "",
            "closureNotes": closure_notes or "",
            "closureAssessment": closure_assessment or {},
            "approvals": approvals or [],
            "statusHistory": status_history or [],
            "externalReferences": external_references,
            "integrationState": integration_state,
        }

    def _write_change(
        self, cursor: Any, change: dict, revision_actor_id: str | None = None
    ) -> None:
        change_uuid = canonical_uuid("change_request", change["id"])
        cursor.execute("SELECT id FROM companies WHERE slug = %s", (change["companyId"],))
        company_row = cursor.fetchone()
        if not company_row:
            raise ValueError("Customer not found")
        company_uuid = str(company_row[0])
        creator = change.get("createdBy") or {}
        creator_uuid = None
        if creator.get("email"):
            cursor.execute("SELECT id FROM users WHERE email = %s", (creator["email"],))
            creator_row = cursor.fetchone()
            creator_uuid = str(creator_row[0]) if creator_row else None
        revision_actor_uuid = (
            canonical_uuid("user", revision_actor_id) if revision_actor_id else creator_uuid
        )
        assigned_user_uuid = (
            canonical_uuid("user", change["assignedUserId"])
            if change.get("assignedUserId")
            else None
        )
        template_uuid = (
            canonical_uuid("change_template", change["templateId"])
            if change.get("templateId")
            else None
        )
        cursor.execute(
            """
            INSERT INTO change_requests (
                id, company_id, change_number, title, status, change_type, category,
                priority, risk_level, risk_source, outage_expected, planned_start,
                planned_end, reason, business_impact, implementation_plan, validation_plan,
                rollback_plan, communication_status, communication_plan, assigned_user_id,
                assigned_technician, assignment_history, template_id, template_version,
                template_snapshot, template_parameters, approver, notes, impact_summary,
                risk_assessment, revision,
                actual_start, actual_end, actual_outage_minutes, outcome, failure_reason,
                validation_result, rollback_executed, rollback_result, closure_notes,
                closure_assessment, approvals, status_history, created_by,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::timestamp, %s::timestamp, %s, %s, %s, %s, %s, %s, %s,
                %s::uuid, %s, %s::jsonb, %s::uuid, %s, %s::jsonb, %s::jsonb,
                %s, %s, %s::jsonb, %s::jsonb, %s,
                %s::timestamptz, %s::timestamptz, %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::uuid,
                COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now())
            )
            ON CONFLICT (id) DO UPDATE SET
                company_id = EXCLUDED.company_id, change_number = EXCLUDED.change_number,
                title = EXCLUDED.title, status = EXCLUDED.status, change_type = EXCLUDED.change_type,
                category = EXCLUDED.category, priority = EXCLUDED.priority,
                risk_level = EXCLUDED.risk_level, risk_source = EXCLUDED.risk_source,
                outage_expected = EXCLUDED.outage_expected, planned_start = EXCLUDED.planned_start,
                planned_end = EXCLUDED.planned_end, reason = EXCLUDED.reason,
                business_impact = EXCLUDED.business_impact,
                implementation_plan = EXCLUDED.implementation_plan,
                validation_plan = EXCLUDED.validation_plan, rollback_plan = EXCLUDED.rollback_plan,
                communication_status = EXCLUDED.communication_status,
                communication_plan = EXCLUDED.communication_plan,
                assigned_user_id = EXCLUDED.assigned_user_id,
                assigned_technician = EXCLUDED.assigned_technician,
                assignment_history = EXCLUDED.assignment_history,
                template_id = EXCLUDED.template_id,
                template_version = EXCLUDED.template_version,
                template_snapshot = EXCLUDED.template_snapshot,
                template_parameters = EXCLUDED.template_parameters,
                approver = EXCLUDED.approver,
                notes = EXCLUDED.notes, impact_summary = EXCLUDED.impact_summary,
                risk_assessment = EXCLUDED.risk_assessment, revision = EXCLUDED.revision,
                actual_start = EXCLUDED.actual_start, actual_end = EXCLUDED.actual_end,
                actual_outage_minutes = EXCLUDED.actual_outage_minutes, outcome = EXCLUDED.outcome,
                failure_reason = EXCLUDED.failure_reason, validation_result = EXCLUDED.validation_result,
                rollback_executed = EXCLUDED.rollback_executed, rollback_result = EXCLUDED.rollback_result,
                closure_notes = EXCLUDED.closure_notes,
                closure_assessment = EXCLUDED.closure_assessment,
                approvals = EXCLUDED.approvals,
                status_history = EXCLUDED.status_history,
                created_by = EXCLUDED.created_by, updated_at = EXCLUDED.updated_at
            """,
            (
                change_uuid,
                company_uuid,
                change["number"],
                change["title"],
                change.get("status", "draft"),
                change.get("changeType", "normal"),
                change.get("category", "infrastructure"),
                change.get("priority", "medium"),
                change.get("riskLevel", "medium"),
                change.get("riskSource", "cmdb_suggestion"),
                bool(change.get("outageExpected")),
                change.get("plannedStart") or None,
                change.get("plannedEnd") or None,
                change.get("reason", ""),
                change.get("businessImpact") or None,
                change.get("implementationPlan", ""),
                change.get("validationPlan", ""),
                change.get("rollbackPlan", ""),
                change.get("communicationStatus", "required"),
                change.get("communicationPlan") or None,
                assigned_user_uuid,
                change.get("assignedTechnician") or None,
                json.dumps(change.get("assignmentHistory") or []),
                template_uuid,
                int(change.get("templateVersion") or 0) or None,
                json.dumps(change.get("templateSnapshot"))
                if change.get("templateSnapshot")
                else None,
                json.dumps(change.get("templateParameters") or {}),
                change.get("approver") or None,
                change.get("notes") or None,
                json.dumps(change.get("impactSummary") or {}),
                json.dumps(change.get("riskAssessment") or {}),
                int(change.get("revision") or 1),
                change.get("actualStart") or None,
                change.get("actualEnd") or None,
                int(change.get("actualOutageMinutes") or 0),
                change.get("outcome", "pending"),
                change.get("failureReason") or None,
                change.get("validationResult") or None,
                bool(change.get("rollbackExecuted")),
                change.get("rollbackResult") or None,
                change.get("closureNotes") or None,
                json.dumps(change.get("closureAssessment") or {}),
                json.dumps(change.get("approvals") or []),
                json.dumps(change.get("statusHistory") or []),
                creator_uuid,
                change.get("createdAt") or None,
                change.get("updatedAt") or None,
            ),
        )
        cursor.execute("DELETE FROM change_scope_items WHERE change_id = %s::uuid", (change_uuid,))
        impacts = change.get("impactSnapshot") or []
        impact_by_id = {item.get("assetId"): item for item in impacts}
        for ordinal, asset_id in enumerate(change.get("scopeAssetIds") or []):
            ci_uuid = canonical_uuid("configuration_item", asset_id)
            snapshot = impact_by_id.get(asset_id) or {"assetId": ci_uuid}
            cursor.execute(
                """
                INSERT INTO change_scope_items (change_id, ci_id, ci_snapshot, ordinal)
                VALUES (%s::uuid, (SELECT id FROM configuration_items WHERE id = %s::uuid), %s::jsonb, %s)
                """,
                (change_uuid, ci_uuid, json.dumps(snapshot), ordinal),
            )
        cursor.execute(
            "DELETE FROM change_impact_snapshots WHERE change_id = %s::uuid",
            (change_uuid,),
        )
        role_values = {
            "Scope": "scope",
            "Direct impact": "direct",
            "Downstream impact": "downstream",
        }
        for ordinal, item in enumerate(impacts):
            ci_uuid = canonical_uuid("configuration_item", item["assetId"])
            relationship_path = {
                "relationshipTypes": item.get("relationshipPath", []),
                "assetIds": item.get("pathAssetIds", []),
            }
            cursor.execute(
                """
                INSERT INTO change_impact_snapshots (
                    change_id, ci_id, impact_role, depth, relationship_path,
                    ci_snapshot, automatically_detected, included, ordinal
                ) VALUES (
                    %s::uuid, (SELECT id FROM configuration_items WHERE id = %s::uuid),
                    %s, %s, %s::jsonb, %s::jsonb, true, true, %s
                )
                """,
                (
                    change_uuid,
                    ci_uuid,
                    role_values.get(item.get("role"), "downstream"),
                    int(item.get("depth") or 0),
                    json.dumps(relationship_path),
                    json.dumps(item),
                    ordinal,
                ),
            )
        revision = int(change.get("revision") or 1)
        cursor.execute(
            """
            INSERT INTO change_revisions (change_id, revision, document, created_by)
            VALUES (%s::uuid, %s, %s::jsonb, %s::uuid)
            ON CONFLICT (change_id, revision) DO UPDATE SET document = EXCLUDED.document
            """,
            (change_uuid, revision, json.dumps(change), revision_actor_uuid),
        )
        connectwise = (change.get("integrationState") or {}).get("connectwise") or {}
        sync_status = connectwise.get("status", "not_published")
        if sync_status not in {"not_published", "queued", "published", "failed"}:
            sync_status = "not_published"
        cursor.execute(
            """
            INSERT INTO change_external_links (
                change_id, provider, external_type, external_id, external_url,
                sync_status, idempotency_key, last_attempt_at, last_error, updated_at
            ) VALUES (%s::uuid, 'connectwise', 'ticket', %s, %s, %s, %s, %s::timestamptz, %s, now())
            ON CONFLICT (change_id, provider, external_type) DO UPDATE SET
                external_id = EXCLUDED.external_id, external_url = EXCLUDED.external_url,
                sync_status = EXCLUDED.sync_status, last_attempt_at = EXCLUDED.last_attempt_at,
                last_error = EXCLUDED.last_error, updated_at = now()
            """,
            (
                change_uuid,
                connectwise.get("ticketId"),
                connectwise.get("ticketUrl"),
                sync_status,
                f"change:{change_uuid}:connectwise:ticket",
                connectwise.get("lastAttemptAt") or None,
                connectwise.get("error"),
            ),
        )

    @staticmethod
    def _timestamp(value: Any) -> str:
        if not value:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat().replace("+00:00", "Z")
        return str(value)

    def list_changes(
        self,
        company_id: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict]:
        changes = self.state.get("changes", [])
        if company_id:
            changes = [item for item in changes if item.get("companyId") == company_id]
        if asset_id:
            changes = [
                item
                for item in changes
                if asset_id in (item.get("scopeAssetIds") or [])
                or any(
                    impact.get("assetId") == asset_id
                    for impact in (item.get("impactSnapshot") or [])
                )
            ]
        return deepcopy(changes)

    def get_change(self, change_id: str) -> dict | None:
        change = next(
            (item for item in self.state.get("changes", []) if item["id"] == change_id),
            None,
        )
        return deepcopy(change) if change else None

    def next_change_number(self, year: int) -> str:
        sequence = 1 + sum(
            str(item.get("number", "")).startswith(f"CHG-{year}-")
            for item in self.state.get("changes", [])
        )
        return f"CHG-{year}-{sequence:04d}"

    def create_change(self, change: dict, actor_id: str | None = None) -> dict:
        self.state.setdefault("changes", []).append(deepcopy(change))
        self._audit(
            change["companyId"],
            actor_id,
            "change_request",
            change["id"],
            "created",
            None,
            change,
        )
        self.save_state(self.state)
        return deepcopy(change)

    def update_change(
        self,
        change_id: str,
        change: dict,
        actor_id: str | None = None,
        action: str = "updated",
        reason: str = "",
    ) -> dict | None:
        current = next(
            (item for item in self.state.get("changes", []) if item["id"] == change_id),
            None,
        )
        if not current:
            return None
        before = deepcopy(current)
        current.clear()
        current.update(deepcopy(change))
        self._audit(
            current["companyId"],
            actor_id,
            "change_request",
            change_id,
            action,
            before,
            current,
            reason=reason,
        )
        self.save_state(self.state)
        return deepcopy(current)

    @staticmethod
    def _change_template_public(template: dict, version: int | None = None) -> dict:
        """Return one template with the requested immutable content version."""

        target_version = int(version or template.get("version") or 1)
        version_record = next(
            (
                item
                for item in template.get("versions", [])
                if int(item.get("version") or 0) == target_version
            ),
            None,
        )
        if not version_record and target_version == int(template.get("version") or 1):
            version_record = {"content": template.get("content") or {}}
        if not version_record:
            raise ValueError("Change template version not found")
        return {
            key: deepcopy(value)
            for key, value in template.items()
            if key not in {"versions", "content"}
        } | {
            "version": target_version,
            "content": deepcopy(version_record["content"]),
        }

    def list_change_templates(self, company_id: str | None = None) -> list[dict]:
        """List global and optionally customer-scoped change templates."""

        records = [
            item
            for item in self.state.get("changeTemplates", [])
            if company_id is None or item.get("companyId") in {None, company_id}
        ]
        return [
            self._change_template_public(item)
            for item in sorted(
                records,
                key=lambda item: (
                    item.get("companyId") or "",
                    str(item.get("name") or "").casefold(),
                ),
            )
        ]

    def get_change_template(self, template_id: str, version: int | None = None) -> dict | None:
        """Load one template identity and pinned content version."""

        template = next(
            (item for item in self.state.get("changeTemplates", []) if item["id"] == template_id),
            None,
        )
        if not template:
            return None
        try:
            return self._change_template_public(template, version)
        except ValueError:
            return None

    def create_change_template(self, template: dict, actor_id: str | None = None) -> dict:
        """Create a template identity and immutable version one."""

        key = str(template["key"]).casefold()
        company_id = template.get("companyId")
        if any(
            item.get("companyId") == company_id and str(item.get("key") or "").casefold() == key
            for item in self.state.get("changeTemplates", [])
        ):
            raise ValueError("A template with that key already exists in this scope")
        timestamp = utc_now()
        stored = {
            **deepcopy(template),
            "id": canonical_uuid("change_template", template.get("id") or str(uuid.uuid4())),
            "content": normalize_template_content(template["content"]),
            "version": 1,
            "versions": [
                {
                    "version": 1,
                    "content": normalize_template_content(template["content"]),
                    "createdBy": actor_id,
                    "createdAt": timestamp,
                }
            ],
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        self.state.setdefault("changeTemplates", []).append(stored)
        public = self._change_template_public(stored)
        self._audit(
            company_id,
            actor_id,
            "change_template",
            stored["id"],
            "created",
            None,
            public,
        )
        self.save_state(self.state)
        return public

    def update_change_template(
        self,
        template_id: str,
        changes: dict,
        expected_version: int,
        actor_id: str | None = None,
    ) -> dict:
        """Create a new immutable content version and update template metadata."""

        template = next(
            (item for item in self.state.get("changeTemplates", []) if item["id"] == template_id),
            None,
        )
        if not template:
            raise ValueError("Change template not found")
        if int(template.get("version") or 1) != expected_version:
            raise ValueError("Change template changed. Reload it before saving.")
        before = self._change_template_public(template)
        new_version = expected_version + 1
        content = normalize_template_content(changes.get("content") or template["content"])
        template.update(
            {
                key: deepcopy(value)
                for key, value in changes.items()
                if key
                in {
                    "name",
                    "description",
                    "tags",
                    "status",
                    "ownerUserId",
                    "reviewDueDate",
                }
            }
        )
        template["content"] = content
        template["version"] = new_version
        template["updatedAt"] = utc_now()
        template.setdefault("versions", []).append(
            {
                "version": new_version,
                "content": content,
                "createdBy": actor_id,
                "createdAt": template["updatedAt"],
            }
        )
        stored = self._change_template_public(template)
        self._audit(
            template.get("companyId"),
            actor_id,
            "change_template",
            template_id,
            "version_created",
            before,
            stored,
        )
        self.save_state(self.state)
        return stored

    @staticmethod
    def _approval_public(record: dict) -> dict:
        """Return approval evidence without the bearer-token verifier."""

        return {key: deepcopy(value) for key, value in record.items() if key != "tokenHash"}

    def create_change_approval_request(self, record: dict, actor_id: str | None = None) -> dict:
        """Persist a hashed, expiring approval request."""

        stored = deepcopy(record)
        stored.setdefault("id", str(uuid.uuid4()))
        stored.setdefault("status", "pending")
        stored.setdefault("deliveryStatus", "pending")
        stored.setdefault("createdAt", utc_now())
        stored.setdefault("updatedAt", stored["createdAt"])
        self.state["changeApprovalRequests"].append(stored)
        self._audit(
            stored.get("companyId"),
            actor_id,
            "change_approval_request",
            stored["id"],
            "created",
            None,
            self._approval_public(stored),
        )
        self.save_state(self.state)
        return self._approval_public(stored)

    def list_change_approval_requests(self, change_id: str) -> list[dict]:
        """List public approval evidence for one change."""

        records = [
            self._approval_public(item)
            for item in self.state["changeApprovalRequests"]
            if item.get("changeId") == change_id
        ]
        return sorted(records, key=lambda item: item.get("createdAt", ""), reverse=True)

    def get_change_approval_request_by_token(self, token_hash: str) -> dict | None:
        """Resolve an approval request by its one-way bearer-token verifier."""

        record = next(
            (
                item
                for item in self.state["changeApprovalRequests"]
                if hmac.compare_digest(str(item.get("tokenHash") or ""), token_hash)
            ),
            None,
        )
        return deepcopy(record) if record else None

    def update_change_approval_request(
        self,
        request_id: str,
        changes: dict,
        actor_id: str | None = None,
    ) -> dict | None:
        """Update delivery metadata without changing approval identity or scope."""

        allowed = {"deliveryStatus", "providerRequestId", "lastError"}
        record = next(
            (item for item in self.state["changeApprovalRequests"] if item["id"] == request_id),
            None,
        )
        if not record:
            return None
        before = self._approval_public(record)
        for key in allowed:
            if key in changes:
                record[key] = deepcopy(changes[key])
        record["updatedAt"] = utc_now()
        self._audit(
            record.get("companyId"),
            actor_id,
            "change_approval_request",
            request_id,
            "delivery_updated",
            before,
            self._approval_public(record),
        )
        self.save_state(self.state)
        return self._approval_public(record)

    def decide_change_approval_request(
        self, token_hash: str, decision: str, comments: str
    ) -> dict | None:
        """Atomically consume a pending approval token in the local repository."""

        record = next(
            (
                item
                for item in self.state["changeApprovalRequests"]
                if hmac.compare_digest(str(item.get("tokenHash") or ""), token_hash)
            ),
            None,
        )
        if not record or record.get("status") != "pending":
            return None
        expires_at = parse_timestamp(record.get("expiresAt"))
        if not expires_at or expires_at <= datetime.now(UTC):
            record["status"] = "expired"
            record["updatedAt"] = utc_now()
            self.save_state(self.state)
            return None
        timestamp = utc_now()
        record.update(
            status=decision,
            decisionComments=str(comments or "").strip()[:8000],
            decidedAt=timestamp,
            updatedAt=timestamp,
        )
        self.save_state(self.state)
        return self._approval_public(record)

    def revoke_change_approval_requests(
        self,
        change_id: str,
        actor_id: str | None = None,
        reason: str = "Approval request replaced",
        batch_id: str | None = None,
    ) -> int:
        """Revoke reusable approval links after replacement or lifecycle change."""

        count = 0
        for record in self.state["changeApprovalRequests"]:
            if record.get("changeId") != change_id or record.get("status") != "pending":
                continue
            if batch_id and record.get("batchId") != batch_id:
                continue
            before = self._approval_public(record)
            record.update(status="revoked", lastError=reason[:2000], updatedAt=utc_now())
            self._audit(
                record.get("companyId"),
                actor_id,
                "change_approval_request",
                record["id"],
                "revoked",
                before,
                self._approval_public(record),
                reason=reason,
            )
            count += 1
        if count:
            self.save_state(self.state)
        return count

    def list_integrations(self) -> list[dict]:
        return [
            integration_connection_audit_value(item)
            for item in deepcopy(self.state.get("integrations", []))
        ]

    def get_integration_connection(self, kind: str) -> dict | None:
        """Return one root integration, including encrypted material for server-side use."""

        record = next(
            (item for item in self.state.get("integrations", []) if item.get("type") == kind),
            None,
        )
        if not record:
            return None
        return {
            "configuration": {},
            "credentialsEncrypted": "",
            "credentialsNonce": "",
            "revision": 1,
            "lifecycleStatus": "active",
            "lifecycleReason": "",
            "lifecycleChangedAt": None,
            "lifecycleChangedBy": None,
            "connectionStatus": "not_configured",
            "lastTestAt": None,
            "lastError": "",
            **deepcopy(record),
        }

    def upsert_integration_capability_snapshot(
        self,
        kind: str,
        capability_key: str,
        snapshot: dict,
        *,
        ttl_seconds: int = 900,
    ) -> dict:
        """Cache one bounded provider capability in local development state."""

        if not self.get_integration_connection(kind):
            raise ValueError("Integration connection not found")
        key = str(capability_key or "").strip().casefold()
        if not INTEGRATION_CACHE_KEY.fullmatch(key):
            raise ValueError("Integration capability key is invalid")
        status = str(snapshot.get("status") or "").strip().casefold()
        if status not in INTEGRATION_CAPABILITY_STATUSES:
            raise ValueError("Integration capability status is invalid")
        summary, summary_fingerprint = _bounded_integration_cache_summary(
            snapshot.get("summary") or {},
            maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
        )
        schema_fingerprint = (
            str(snapshot.get("schemaFingerprint") or summary_fingerprint).strip().casefold()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", schema_fingerprint):
            raise ValueError("Integration capability schema fingerprint is invalid")
        ttl = _bounded_integration_cache_ttl(ttl_seconds)
        now = datetime.now(UTC)
        rows = self.state.setdefault("integrationCapabilitySnapshots", [])
        record = next(
            (
                item
                for item in rows
                if item.get("provider") == kind and item.get("capabilityKey") == key
            ),
            None,
        )
        if not record:
            record = {
                "id": canonical_uuid("integration_capability", f"{kind}:{key}"),
                "provider": kind,
                "capabilityKey": key,
                "createdAt": utc_now(),
            }
            rows.append(record)
        record.update(
            status=status,
            summary=summary,
            schemaFingerprint=schema_fingerprint,
            errorCategory=str(snapshot.get("errorCategory") or "").strip()[:80],
            checkedAt=now.isoformat().replace("+00:00", "Z"),
            expiresAt=(now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"),
            updatedAt=utc_now(),
        )
        self.save_state(self.state)
        return {**deepcopy(record), "stale": False}

    def get_integration_capability_snapshot(
        self,
        kind: str,
        capability_key: str,
        *,
        include_expired: bool = False,
    ) -> dict | None:
        """Return one local capability cache entry."""

        key = str(capability_key or "").strip().casefold()
        if not INTEGRATION_CACHE_KEY.fullmatch(key):
            raise ValueError("Integration capability key is invalid")
        record = next(
            (
                item
                for item in self.state.get("integrationCapabilitySnapshots", [])
                if item.get("provider") == kind and item.get("capabilityKey") == key
            ),
            None,
        )
        if not record:
            return None
        expires_at = parse_timestamp(record.get("expiresAt"))
        stale = not expires_at or expires_at <= datetime.now(UTC)
        return {**deepcopy(record), "stale": stale} if include_expired or not stale else None

    def upsert_integration_enrichment_preview(
        self,
        kind: str,
        company_id: str,
        preview: dict,
        *,
        ttl_seconds: int = 900,
    ) -> dict:
        """Cache normalized GraphQL enrichment in local development state."""

        if not self.get_integration_connection(kind):
            raise ValueError("Integration connection not found")
        if not any(
            company.get("id") == company_id and company.get("status") != "inactive"
            for company in self.state.get("companies", [])
        ):
            raise ValueError("Customer not found")
        namespace = str(preview.get("sourceNamespace") or "").strip().casefold()
        if not INTEGRATION_SOURCE_NAMESPACE.fullmatch(namespace):
            raise ValueError("Integration source namespace is invalid")
        server_id = _integration_cache_identifier(preview.get("sourceServerId"), "Source server ID")
        device_id = _integration_cache_identifier(preview.get("sourceDeviceId"), "Source device ID")
        provider_parent_id = _integration_cache_identifier(
            preview.get("providerParentId"), "Provider parent ID"
        )
        status = str(preview.get("status") or "").strip().casefold()
        if status not in INTEGRATION_ENRICHMENT_STATUSES:
            raise ValueError("Integration enrichment status is invalid")
        summary, summary_fingerprint = _bounded_integration_cache_summary(
            preview.get("summary") or {},
            maximum_bytes=INTEGRATION_ENRICHMENT_CACHE_MAX_BYTES,
        )
        source_fingerprint = (
            str(preview.get("sourceFingerprint") or summary_fingerprint).strip().casefold()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
            raise ValueError("Integration enrichment source fingerprint is invalid")
        ttl = _bounded_integration_cache_ttl(ttl_seconds)
        now = datetime.now(UTC)
        rows = self.state.setdefault("integrationEnrichmentPreviews", [])
        record = next(
            (
                item
                for item in rows
                if item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("sourceNamespace") == namespace
                and item.get("sourceServerId") == server_id
                and item.get("sourceDeviceId") == device_id
            ),
            None,
        )
        if not record:
            record = {
                "id": canonical_uuid(
                    "integration_enrichment",
                    f"{kind}:{company_id}:{namespace}:{server_id}:{device_id}",
                ),
                "provider": kind,
                "companyId": company_id,
                "sourceNamespace": namespace,
                "sourceServerId": server_id,
                "sourceDeviceId": device_id,
                "createdAt": utc_now(),
            }
            rows.append(record)
        record.update(
            providerParentId=provider_parent_id,
            policyId=preview.get("policyId") or None,
            syncRunId=preview.get("syncRunId") or None,
            assetId=preview.get("assetId") or preview.get("canonicalCiId") or None,
            sourceMappingId=preview.get("sourceMappingId") or None,
            status=status,
            summary=summary,
            sourceFingerprint=source_fingerprint,
            observedAt=now.isoformat().replace("+00:00", "Z"),
            expiresAt=(now + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z"),
            updatedAt=utc_now(),
        )
        self.save_state(self.state)
        return {**deepcopy(record), "stale": False}

    @contextmanager
    def integration_enrichment_scope_lock(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
    ) -> Iterator[None]:
        """Serialize one local cache publication scope across worker threads."""

        scope_key = ":".join((kind, company_id, str(provider_parent_id), str(source_server_id)))
        with self._integration_enrichment_lock_guard:
            scope_lock = self._integration_enrichment_locks.setdefault(
                scope_key,
                threading.RLock(),
            )
        with scope_lock:
            yield

    def list_integration_enrichment_previews(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str | None = None,
        *,
        source_server_id: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """List a bounded tenant-scoped local enrichment cache."""

        bounded_limit = max(1, min(int(limit), 25_000))
        rows = []
        for record in self.state.get("integrationEnrichmentPreviews", []):
            if record.get("provider") != kind or record.get("companyId") != company_id:
                continue
            if provider_parent_id is not None and record.get("providerParentId") != str(
                provider_parent_id
            ):
                continue
            if source_server_id is not None and record.get("sourceServerId") != str(
                source_server_id
            ):
                continue
            expires_at = parse_timestamp(record.get("expiresAt"))
            stale = not expires_at or expires_at <= datetime.now(UTC)
            if stale and not include_expired:
                continue
            rows.append({**deepcopy(record), "stale": stale})
        return sorted(
            rows,
            key=lambda item: (item.get("observedAt") or "", item.get("id") or ""),
            reverse=True,
        )[:bounded_limit]

    def replace_integration_enrichment_generation(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
        generation_id: str | None,
    ) -> int:
        """Remove superseded rows after a complete tenant-scoped cache refresh."""

        retained_generation = str(generation_id or "")
        rows = self.state.get("integrationEnrichmentPreviews", [])
        retained: list[dict[str, Any]] = []
        removed = 0
        for record in rows:
            summary = record.get("summary")
            record_generation = (
                str(summary.get("cacheGenerationId") or "") if isinstance(summary, dict) else ""
            )
            in_scope = (
                record.get("provider") == kind
                and record.get("companyId") == company_id
                and record.get("providerParentId") == str(provider_parent_id)
                and record.get("sourceServerId") == str(source_server_id)
            )
            if in_scope and (not retained_generation or record_generation != retained_generation):
                removed += 1
                continue
            retained.append(record)
        if removed:
            self.state["integrationEnrichmentPreviews"] = retained
            self.save_state(self.state)
        return removed

    def delete_integration_enrichment_generation(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
        generation_id: str,
    ) -> int:
        """Delete one unpublished tenant-scoped cache generation."""

        selected_generation = str(generation_id or "")
        if not selected_generation:
            raise ValueError("Cache generation ID is required")
        rows = self.state.get("integrationEnrichmentPreviews", [])
        retained: list[dict[str, Any]] = []
        removed = 0
        for record in rows:
            summary = record.get("summary")
            record_generation = (
                str(summary.get("cacheGenerationId") or "") if isinstance(summary, dict) else ""
            )
            matches = (
                record.get("provider") == kind
                and record.get("companyId") == company_id
                and record.get("providerParentId") == str(provider_parent_id)
                and record.get("sourceServerId") == str(source_server_id)
                and record_generation == selected_generation
            )
            if matches:
                removed += 1
                continue
            retained.append(record)
        if removed:
            self.state["integrationEnrichmentPreviews"] = retained
            self.save_state(self.state)
        return removed

    def prune_integration_enrichment_previews(
        self,
        kind: str,
        *,
        max_entries: int = 5_000,
    ) -> int:
        """Remove stale rows without splitting a published cache generation."""

        del max_entries
        candidates = [
            item
            for item in self.state.get("integrationEnrichmentPreviews", [])
            if item.get("provider") == kind
        ]
        now = datetime.now(UTC)

        def expired(item: dict[str, Any]) -> bool:
            expires_at = parse_timestamp(item.get("expiresAt"))
            return not expires_at or expires_at <= now

        expired_generations = {
            str(summary.get("cacheGenerationId"))
            for item in candidates
            if expired(item)
            and isinstance((summary := item.get("summary")), dict)
            and summary.get("cacheGenerationId")
        }
        doomed = set()
        for item in candidates:
            summary = item.get("summary")
            generation_id = (
                str(summary.get("cacheGenerationId") or "") if isinstance(summary, dict) else ""
            )
            if (generation_id and generation_id in expired_generations) or (
                not generation_id and expired(item)
            ):
                doomed.add(item.get("id"))
        if doomed:
            self.state["integrationEnrichmentPreviews"] = [
                item
                for item in self.state.get("integrationEnrichmentPreviews", [])
                if item.get("id") not in doomed
            ]
            self.save_state(self.state)
        return len(doomed)

    def ensure_integration_connection(
        self, kind: str, name: str, actor_id: str | None = None
    ) -> dict:
        """Install a supported root provider row before its first configuration save."""

        existing = self.get_integration_connection(kind)
        if existing:
            return existing
        if kind not in CREDENTIAL_REFERENCES:
            raise ValueError("Unsupported integration provider")
        record = {
            "id": kind,
            "name": name[:160],
            "type": kind,
            "enabled": False,
            "mode": "configured_in_application",
            "lastSync": None,
            "status": "Not configured",
            "scope": "msp",
            "configuration": {"scope": "msp", "mode": "configured_in_application"},
            "credentialsEncrypted": "",
            "credentialsNonce": "",
            "connectionStatus": "not_configured",
            "lastTestAt": None,
            "lastError": "",
            "revision": 1,
            "lifecycleStatus": "active",
            "lifecycleReason": "",
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
        }
        self.state.setdefault("integrations", []).append(record)
        self._audit(
            None,
            actor_id,
            "integration_connection",
            kind,
            "installed",
            None,
            integration_connection_audit_value(record),
        )
        self.save_state(self.state)
        return self.get_integration_connection(kind) or deepcopy(record)

    def update_integration_connection(
        self, kind: str, changes: dict, actor_id: str | None = None
    ) -> dict:
        """Persist encrypted connection settings while exposing only audit-safe metadata."""

        record = next(
            (item for item in self.state.get("integrations", []) if item.get("type") == kind),
            None,
        )
        if not record:
            raise ValueError("Integration connection not found")
        before = self.get_integration_connection(kind) or {}
        expected_revision = changes.get("expectedRevision")
        if expected_revision is not None and int(expected_revision) != int(before["revision"]):
            raise ValueError("Integration settings changed; reload before saving")
        stored_changes = deepcopy(changes)
        if "configuration" in stored_changes:
            stored_changes["configuration"] = {
                **(before.get("configuration") or {}),
                **(stored_changes.get("configuration") or {}),
            }
        record.update(stored_changes)
        record.pop("expectedRevision", None)
        if record.get("lifecycleStatus", "active") != "active":
            record["enabled"] = False
        record.update(
            mode="configured_in_application",
            revision=int(before.get("revision") or 0) + 1,
            updatedAt=utc_now(),
        )
        after = self.get_integration_connection(kind) or {}
        self._audit(
            None,
            actor_id,
            "integration_connection",
            record["id"],
            "configuration_updated",
            integration_connection_audit_value(before),
            integration_connection_audit_value(after),
        )
        self.save_state(self.state)
        return after

    def integration_lifecycle_impact(self, kind: str) -> dict:
        """Summarize retained provider evidence before disabling or removal."""

        connection = self.get_integration_connection(kind)
        if not connection:
            raise ValueError("Integration connection not found")
        companies = self.list_companies()
        provider_companies = self.list_provider_companies(kind)
        ci_mappings = [
            mapping
            for company in companies
            for mapping in self.list_provider_ci_mappings(kind, company["id"])
        ]
        policies = self.list_ci_sync_policies(kind)
        return {
            "provider": kind,
            "lifecycleStatus": connection.get("lifecycleStatus", "active"),
            "customerMappings": sum(
                bool(item.get("mappedCompanyId")) for item in provider_companies
            ),
            "companyObservations": len(provider_companies),
            "ciPolicies": len(policies),
            "enabledPolicies": sum(bool(item.get("enabled")) for item in policies),
            "pendingReviews": self.count_ci_review_items(kind, state="pending"),
            "ciMappings": len(ci_mappings),
            "importedCis": len(
                {item.get("assetId") for item in ci_mappings if item.get("assetId")}
            ),
            "syncRuns": sum(item.get("type") == kind for item in self.list_sync_runs()),
        }

    def change_integration_lifecycle(
        self,
        kind: str,
        lifecycle_status: str,
        reason: str,
        actor_id: str,
        expected_revision: int | None = None,
        *,
        remove_configuration: bool = False,
    ) -> dict:
        """Change the master provider lifecycle and optionally erase usable secrets."""

        record = next(
            (item for item in self.state.get("integrations", []) if item.get("type") == kind),
            None,
        )
        if not record:
            raise ValueError("Integration connection not found")
        before = self.get_integration_connection(kind) or {}
        if expected_revision is not None and int(expected_revision) != int(before["revision"]):
            raise ValueError("Integration settings changed; reload before continuing")
        changed_at = utc_now()
        record.update(
            lifecycleStatus=lifecycle_status,
            lifecycleReason=reason[:1000],
            lifecycleChangedAt=changed_at,
            lifecycleChangedBy=actor_id,
            enabled=lifecycle_status == "active",
            revision=int(before.get("revision") or 0) + 1,
            updatedAt=changed_at,
        )
        if remove_configuration:
            record.update(
                configuration={"scope": "msp", "mode": "removed"},
                credentialsEncrypted="",
                credentialsNonce="",
                mode="removed",
                connectionStatus="not_configured",
                lastError="",
                status="Removed",
            )
        elif lifecycle_status == "paused":
            record["status"] = "Paused"
        elif lifecycle_status == "disabled":
            record["status"] = "Disabled"
        elif lifecycle_status == "active":
            record["status"] = (
                "Healthy" if record.get("connectionStatus") == "verified" else "Ready"
            )
        after = self.get_integration_connection(kind) or {}
        self._audit(
            None,
            actor_id,
            "integration_connection",
            record["id"],
            f"integration_{lifecycle_status}",
            integration_connection_audit_value(before),
            integration_connection_audit_value(after),
            reason=reason,
        )
        self.save_state(self.state)
        return after

    def mark_integration_test(
        self, kind: str, status: str, message: str, actor_id: str | None = None
    ) -> dict:
        """Record a credential test without incrementing the editable revision."""

        record = next(
            (item for item in self.state.get("integrations", []) if item.get("type") == kind),
            None,
        )
        if not record:
            raise ValueError("Integration connection not found")
        before = integration_connection_audit_value(self.get_integration_connection(kind) or {})
        record.update(
            connectionStatus=status,
            status="Ready" if status == "verified" else "error",
            lastTestAt=utc_now(),
            lastError="" if status == "verified" else message[:1000],
        )
        after = integration_connection_audit_value(self.get_integration_connection(kind) or {})
        self._audit(
            None,
            actor_id,
            "integration_connection",
            record["id"],
            "connection_tested",
            before,
            after,
            outcome="success" if status == "verified" else "failed",
            reason=message,
        )
        self.save_state(self.state)
        return self.get_integration_connection(kind) or {}

    def record_company_discovery(
        self,
        kind: str,
        run: dict,
        companies: list[dict],
        actor_id: str | None = None,
    ) -> dict:
        """Persist a bounded discovery snapshot and its governed sync run."""

        stored_run = self.record_sync_run(kind, run, True, actor_id)
        current_ids = {item["externalId"] for item in companies}
        records = self.state.setdefault("providerCompanyObservations", [])
        for record in records:
            if record.get("provider") == kind and record.get("externalId") not in current_ids:
                record["active"] = False
        for company in companies:
            existing = next(
                (
                    item
                    for item in records
                    if item.get("provider") == kind
                    and item.get("externalId") == company["externalId"]
                ),
                None,
            )
            payload_hash = hashlib.sha256(
                json.dumps(company, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if existing:
                existing.update(
                    deepcopy(company),
                    payloadHash=payload_hash,
                    active=True,
                    lastSeenAt=utc_now(),
                    lastSyncRunId=stored_run["id"],
                )
            else:
                records.append(
                    {
                        "id": str(uuid.uuid4()),
                        "provider": kind,
                        **deepcopy(company),
                        "payloadHash": payload_hash,
                        "active": True,
                        "firstSeenAt": utc_now(),
                        "lastSeenAt": utc_now(),
                        "lastSyncRunId": stored_run["id"],
                    }
                )
        self.save_state(self.state)
        return stored_run

    def list_provider_companies(self, kind: str) -> list[dict]:
        """List sanitized provider companies with any explicit canonical mapping."""

        mappings = {
            item["externalId"]: item
            for item in self.state.get("providerCompanyMappings", [])
            if item.get("provider") == kind and item.get("active", True)
        }
        companies = {item["id"]: item for item in self.state.get("companies", [])}
        records = []
        for item in self.state.get("providerCompanyObservations", []):
            if item.get("provider") != kind:
                continue
            mapping = mappings.get(item["externalId"])
            mapped_company = companies.get(mapping.get("companyId")) if mapping else None
            records.append(
                {
                    **deepcopy(item),
                    "mappedCompanyId": mapping.get("companyId") if mapping else None,
                    "mappedCompanyName": mapped_company.get("name") if mapped_company else "",
                    "mappingId": mapping.get("id") if mapping else None,
                }
            )
        return sorted(records, key=lambda item: (not item.get("active", True), item["name"]))

    def map_provider_company(
        self, kind: str, external_id: str, company_id: str, actor_id: str | None = None
    ) -> dict:
        """Create or replace an explicit provider-company to CMDB-customer mapping."""

        observation = next(
            (
                item
                for item in self.state.get("providerCompanyObservations", [])
                if item.get("provider") == kind and item.get("externalId") == external_id
            ),
            None,
        )
        company = next(
            (item for item in self.state.get("companies", []) if item.get("id") == company_id),
            None,
        )
        if not observation or not company:
            raise ValueError("Provider company or CMDB customer not found")
        mapping = next(
            (
                item
                for item in self.state.setdefault("providerCompanyMappings", [])
                if item.get("provider") == kind and item.get("externalId") == external_id
            ),
            None,
        )
        before = deepcopy(mapping) if mapping else None
        mapped_at = utc_now()
        if mapping:
            mapping.update(
                companyId=company_id,
                externalName=observation["name"],
                active=True,
                lastSyncedAt=mapped_at,
            )
        else:
            mapping = {
                "id": str(uuid.uuid4()),
                "provider": kind,
                "externalId": external_id,
                "externalName": observation["name"],
                "companyId": company_id,
                "active": True,
                "firstSeenAt": mapped_at,
                "lastSeenAt": mapped_at,
                "lastSyncedAt": mapped_at,
            }
            self.state["providerCompanyMappings"].append(mapping)
        self._audit(
            company_id,
            actor_id,
            "external_company_mapping",
            mapping["id"],
            "mapped",
            before,
            mapping,
        )
        self.save_state(self.state)
        return next(
            item for item in self.list_provider_companies(kind) if item["externalId"] == external_id
        )

    def unmap_provider_company(
        self, kind: str, external_id: str, actor_id: str | None = None
    ) -> bool:
        """Deactivate a provider-company mapping without deleting its history."""

        mapping = next(
            (
                item
                for item in self.state.get("providerCompanyMappings", [])
                if item.get("provider") == kind
                and item.get("externalId") == external_id
                and item.get("active", True)
            ),
            None,
        )
        if not mapping:
            return False
        before = deepcopy(mapping)
        mapping.update(active=False, lastSyncedAt=utc_now())
        self._audit(
            mapping.get("companyId"),
            actor_id,
            "external_company_mapping",
            mapping["id"],
            "unmapped",
            before,
            mapping,
        )
        self.save_state(self.state)
        return True

    def list_provider_ci_mappings(self, kind: str, company_id: str) -> list[dict]:
        """Return active immutable provider-ID mappings for one customer."""

        return [
            deepcopy(item)
            for item in self.state.get("providerCiMappings", [])
            if item.get("provider") == kind
            and item.get("companyId") == company_id
            and item.get("active", True)
        ]

    def classify_provider_ci_mapping_import(
        self,
        kind: str,
        company_id: str,
        external_id: str,
        provider_parent_id: str | None = None,
    ) -> dict:
        """Preflight one provider identity before any canonical import mutation."""

        mapping = next(
            (
                item
                for item in self.state.get("providerCiMappings", [])
                if item.get("provider") == kind and item.get("externalId") == external_id
            ),
            None,
        )
        presence = next(
            (
                item
                for item in self.state.get("integrationCiPresence", [])
                if mapping is not None and item.get("mappingId") == mapping.get("id")
            ),
            None,
        )
        company_matches = not mapping or mapping.get("companyId") == company_id
        provider_parent_matches = bool(
            not mapping
            or not provider_parent_id
            or not mapping.get("providerParentId")
            or mapping.get("providerParentId") == provider_parent_id
        )
        decision, reason = _provider_ci_import_decision(
            mapping_exists=mapping is not None,
            mapping_active=bool(not mapping or mapping.get("active", True)),
            lifecycle_state=str(presence.get("state") or "") if presence else None,
            company_matches=company_matches,
            provider_parent_matches=provider_parent_matches,
        )
        return {
            "decision": decision,
            "mappingId": mapping.get("id") if mapping else None,
            "companyMatches": company_matches,
            "providerParentMatches": provider_parent_matches,
            "reason": reason,
        }

    def _apply_state_ci_presence_snapshot(
        self,
        kind: str,
        policy: dict[str, Any],
        run_id: str,
        presence_snapshot: dict[str, Any],
        expected_policy_revision: int,
    ) -> dict[str, int]:
        """Evaluate one validated presence snapshot inside a local transaction."""

        snapshot = _normalized_ci_presence_snapshot(presence_snapshot)
        company_id = str(policy.get("companyId") or "")
        provider_parent_id = str(policy.get("providerParentId") or "")
        counts = {state: 0 for state in CI_PRESENCE_STATES}
        counts["skipped"] = 0
        current_connection = self.get_integration_connection(kind)
        current_policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy.get("id")
                and item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
            ),
            None,
        )
        provider_company_mapping = next(
            (
                item
                for item in self.state.get("providerCompanyMappings", [])
                if item.get("provider") == kind
                and item.get("externalId") == provider_parent_id
                and item.get("companyId") == company_id
                and item.get("active", True)
            ),
            None,
        )
        provider_company_observation = next(
            (
                item
                for item in self.state.get("providerCompanyObservations", [])
                if item.get("provider") == kind
                and item.get("externalId") == provider_parent_id
                and item.get("active", True)
                and not item.get("deleted", False)
            ),
            None,
        )
        started = parse_timestamp(snapshot["snapshotStartedAt"])
        company_mapping_changed = parse_timestamp(
            (provider_company_mapping or {}).get("lastSyncedAt")
        )
        if (
            not snapshot["providerReadComplete"]
            or snapshot["policyRevision"] != int(expected_policy_revision)
            or not current_policy
            or snapshot["policyRevision"] != int(current_policy.get("revision") or 0)
            or not current_connection
            or not current_connection.get("enabled", False)
            or current_connection.get("lifecycleStatus", "active") != "active"
            or snapshot["connectionRevision"] != int(current_connection.get("revision") or 0)
            or not provider_company_mapping
            or not provider_company_observation
            or bool(company_mapping_changed and started and company_mapping_changed > started)
        ):
            return counts
        if any(
            item["providerParentId"] != provider_parent_id for item in snapshot["observedRecords"]
        ):
            raise ValueError("Presence snapshot includes a record outside the preview scope")

        mappings = [
            item
            for item in self.state.get("providerCiMappings", [])
            if item.get("provider") == kind and item.get("companyId") == company_id
        ]
        presence_rows = self.state.setdefault("integrationCiPresence", [])
        presence_by_mapping = {str(item.get("mappingId") or ""): item for item in presence_rows}
        observed = {item["externalId"]: item for item in snapshot["observedRecords"]}
        evaluated_at = snapshot["providerReadCompletedAt"] or utc_now()
        evaluated = parse_timestamp(evaluated_at) or datetime.now(UTC)
        for mapping in mappings:
            external_id = str(mapping.get("externalId") or "")
            observed_record = observed.get(external_id)
            current_parent = str(mapping.get("providerParentId") or "")
            row = presence_by_mapping.get(str(mapping.get("id") or ""))

            if observed_record is not None:
                if current_parent and current_parent != provider_parent_id:
                    counts["skipped"] += 1
                    continue
                mapping["providerParentId"] = provider_parent_id
                now = utc_now()
                if row is None:
                    row = {
                        "id": str(uuid.uuid4()),
                        "mappingId": mapping["id"],
                        "createdAt": now,
                        "revision": 0,
                    }
                    presence_rows.append(row)
                    presence_by_mapping[str(mapping["id"])] = row
                next_state = "restore_ready" if not mapping.get("active", True) else "observed"
                row.update(
                    policyId=policy["id"],
                    provider=kind,
                    companyId=company_id,
                    providerParentId=provider_parent_id,
                    assetId=mapping.get("assetId"),
                    externalId=external_id,
                    externalName=(
                        observed_record.get("externalName")
                        or mapping.get("externalName")
                        or external_id
                    ),
                    state=next_state,
                    absenceCount=0,
                    requiredAbsences=snapshot["requiredAbsences"],
                    minimumMissingHours=snapshot["minimumMissingHours"],
                    firstMissingAt=None,
                    lastMissingAt=None,
                    candidateSince=None,
                    lastObservedAt=evaluated_at,
                    lastEvaluatedAt=evaluated_at,
                    lastEvaluatedRunId=run_id,
                    scopeMode=snapshot["scopeMode"],
                    providerReadComplete=snapshot["providerReadComplete"],
                    evaluationReason=(
                        "Provider observation returned for a retired mapping."
                        if next_state == "restore_ready"
                        else "Observed in the provider snapshot."
                    ),
                    discoveryScopeFingerprint=snapshot["discoveryScopeFingerprint"],
                    policyDecisionFingerprint=snapshot["policyDecisionFingerprint"],
                    connectionRevision=snapshot["connectionRevision"],
                    policyRevision=snapshot["policyRevision"],
                    snapshotStartedAt=snapshot["snapshotStartedAt"],
                    providerReadCompletedAt=snapshot["providerReadCompletedAt"],
                    revision=int(row.get("revision") or 0) + 1,
                    updatedAt=now,
                )
                counts[next_state] += 1
                continue

            if current_parent != provider_parent_id:
                # Legacy rows without an immutable parent are backfilled only by
                # positive evidence; absence must never infer their scope.
                counts["skipped"] += 1
                continue
            if row is not None and row.get("lastEvaluatedRunId") == run_id:
                counts["skipped"] += 1
                continue
            first_seen = parse_timestamp(mapping.get("firstSeenAt"))
            if started is not None and first_seen is not None and first_seen > started:
                counts["skipped"] += 1
                continue
            if not mapping.get("active", True):
                if row is not None and row.get("state") == "restore_ready":
                    row.update(
                        policyId=policy["id"],
                        provider=kind,
                        companyId=company_id,
                        providerParentId=provider_parent_id,
                        assetId=mapping.get("assetId"),
                        externalId=external_id,
                        externalName=mapping.get("externalName") or external_id,
                        state="retired",
                        absenceCount=0,
                        requiredAbsences=snapshot["requiredAbsences"],
                        minimumMissingHours=snapshot["minimumMissingHours"],
                        firstMissingAt=None,
                        lastMissingAt=None,
                        candidateSince=None,
                        evaluationReason="The provider no longer reports the retired mapping.",
                        lastEvaluatedAt=evaluated_at,
                        lastEvaluatedRunId=run_id,
                        scopeMode=snapshot["scopeMode"],
                        providerReadComplete=snapshot["providerReadComplete"],
                        discoveryScopeFingerprint=snapshot["discoveryScopeFingerprint"],
                        policyDecisionFingerprint=snapshot["policyDecisionFingerprint"],
                        connectionRevision=snapshot["connectionRevision"],
                        policyRevision=snapshot["policyRevision"],
                        snapshotStartedAt=snapshot["snapshotStartedAt"],
                        providerReadCompletedAt=snapshot["providerReadCompletedAt"],
                        revision=int(row.get("revision") or 0) + 1,
                        updatedAt=evaluated_at,
                    )
                    counts["retired"] += 1
                else:
                    counts["skipped"] += 1
                continue

            if row is None:
                now = utc_now()
                row = {
                    "id": str(uuid.uuid4()),
                    "mappingId": mapping["id"],
                    "createdAt": now,
                    "revision": 0,
                }
                presence_rows.append(row)
                presence_by_mapping[str(mapping["id"])] = row

            scope_changed = (
                row.get("discoveryScopeFingerprint")
                and row.get("discoveryScopeFingerprint") != snapshot["discoveryScopeFingerprint"]
            )
            absence_count = 0 if scope_changed else int(row.get("absenceCount") or 0)
            first_missing_at = None if scope_changed else row.get("firstMissingAt")
            if not snapshot["providerReadComplete"] or snapshot["scopeMode"] != "unfiltered":
                next_state = "not_evaluated"
                absence_count = 0
                first_missing_at = None
                last_missing_at = None
                candidate_since = None
                reason = (
                    "Provider read was incomplete; absence was not evaluated."
                    if not snapshot["providerReadComplete"]
                    else "Provider-side filtering was active; absence was not evaluated."
                )
            else:
                absence_count += 1
                first_missing_at = first_missing_at or evaluated_at
                first_missing = parse_timestamp(first_missing_at) or evaluated
                elapsed_hours = max(0.0, (evaluated - first_missing).total_seconds() / 3600)
                eligible = (
                    absence_count >= snapshot["requiredAbsences"]
                    and elapsed_hours >= snapshot["minimumMissingHours"]
                )
                next_state = "eligible" if eligible else "monitoring"
                last_missing_at = evaluated_at
                candidate_since = first_missing_at if eligible else None
                reason = (
                    "Required complete missing snapshots and minimum elapsed time were reached."
                    if eligible
                    else "Waiting for additional complete missing snapshots or elapsed time."
                )
            now = utc_now()
            row.update(
                policyId=policy["id"],
                provider=kind,
                companyId=company_id,
                providerParentId=provider_parent_id,
                assetId=mapping.get("assetId"),
                externalId=external_id,
                externalName=mapping.get("externalName") or external_id,
                state=next_state,
                absenceCount=absence_count,
                requiredAbsences=snapshot["requiredAbsences"],
                minimumMissingHours=snapshot["minimumMissingHours"],
                firstMissingAt=first_missing_at,
                lastMissingAt=last_missing_at,
                candidateSince=candidate_since,
                lastEvaluatedAt=evaluated_at,
                lastEvaluatedRunId=run_id,
                scopeMode=snapshot["scopeMode"],
                providerReadComplete=snapshot["providerReadComplete"],
                evaluationReason=reason,
                discoveryScopeFingerprint=snapshot["discoveryScopeFingerprint"],
                policyDecisionFingerprint=snapshot["policyDecisionFingerprint"],
                connectionRevision=snapshot["connectionRevision"],
                policyRevision=snapshot["policyRevision"],
                snapshotStartedAt=snapshot["snapshotStartedAt"],
                providerReadCompletedAt=snapshot["providerReadCompletedAt"],
                revision=int(row.get("revision") or 0) + 1,
                updatedAt=now,
            )
            counts[next_state] += 1
        return counts

    def _state_ci_presence_stale_reason(self, stored: dict[str, Any]) -> str:
        """Compare one lifecycle row with current local policy and scope state."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == stored.get("policyId")
                and item.get("provider") == stored.get("provider")
                and item.get("companyId") == stored.get("companyId")
                and item.get("providerParentId") == stored.get("providerParentId")
            ),
            None,
        )
        integration = next(
            (
                item
                for item in self.state.get("integrations", [])
                if item.get("type", item.get("id")) == stored.get("provider")
            ),
            None,
        )
        company_mapping = next(
            (
                item
                for item in self.state.get("providerCompanyMappings", [])
                if item.get("provider") == stored.get("provider")
                and item.get("externalId") == stored.get("providerParentId")
            ),
            None,
        )
        company_observation = next(
            (
                item
                for item in self.state.get("providerCompanyObservations", [])
                if item.get("provider") == stored.get("provider")
                and item.get("externalId") == stored.get("providerParentId")
                and item.get("active", True)
                and not item.get("deleted", False)
            ),
            None,
        )
        current_policy = normalize_ci_policy(policy or {})
        return _ci_presence_stale_evidence_reason(
            {
                **stored,
                "policyExists": policy is not None,
                "currentPolicyRevision": int((policy or {}).get("revision") or 0),
                "currentPolicyEnabled": bool((policy or {}).get("enabled", False)),
                "currentPolicyFiltered": bool(current_policy.get("providerFilterId")),
                "presenceEvidenceMaxAgeHours": _ci_presence_evidence_max_age_hours(current_policy),
                "integrationExists": integration is not None,
                "currentConnectionRevision": int((integration or {}).get("revision") or 0),
                "currentIntegrationEnabled": bool((integration or {}).get("enabled", False)),
                "currentIntegrationLifecycle": (integration or {}).get("lifecycleStatus", "active"),
                "providerCompanyMappingValid": bool(
                    company_mapping
                    and company_mapping.get("active", True)
                    and company_mapping.get("companyId") == stored.get("companyId")
                    and company_observation
                ),
                "providerCompanyMappingChangedAt": (company_mapping or {}).get("lastSyncedAt"),
            }
        )

    def list_ci_presence_lifecycle(
        self,
        provider: str | None = None,
        company_id: str | None = None,
        company_ids: list[str] | None = None,
        provider_parent_id: str | None = None,
        state: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return a tenant-filtered, paged provider-presence review queue."""

        if state is not None and state not in CI_PRESENCE_STATES:
            raise ValueError("Presence lifecycle state is invalid")
        restrict_companies = company_ids is not None
        selected_companies = set(company_ids or [])
        if company_id:
            selected_companies = (
                selected_companies.intersection({company_id})
                if restrict_companies
                else {company_id}
            )
            restrict_companies = True
        query = str(search or "").strip().casefold()
        mappings = {
            str(item.get("id") or ""): item for item in self.state.get("providerCiMappings", [])
        }
        companies = {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in self.state.get("companies", [])
        }
        assets = {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in self.state.get("assets", [])
        }
        reviewers = {
            str(item.get("id") or ""): str(
                item.get("name") or item.get("displayName") or item.get("email") or ""
            )
            for item in self.state.get("users", [])
        }
        rows: list[dict[str, Any]] = []
        for stored in self.state.get("integrationCiPresence", []):
            mapping = mappings.get(str(stored.get("mappingId") or ""))
            if not mapping:
                continue
            item = _public_ci_presence(
                {
                    **stored,
                    "mappingActive": mapping.get("active", True),
                    "companyName": companies.get(str(stored.get("companyId") or ""), ""),
                    "assetName": assets.get(str(stored.get("assetId") or ""), ""),
                    "retiredByName": reviewers.get(str(stored.get("reviewedBy") or ""), ""),
                    "staleEvidenceReason": self._state_ci_presence_stale_reason(stored),
                }
            )
            if provider is not None and item["provider"] != provider:
                continue
            if restrict_companies and item["companyId"] not in selected_companies:
                continue
            if provider_parent_id is not None and item["providerParentId"] != provider_parent_id:
                continue
            if state is not None and item["state"] != state:
                continue
            if query and query not in " ".join(
                str(item.get(key) or "").casefold()
                for key in ("externalId", "externalName", "assetName", "companyName")
            ):
                continue
            rows.append(item)
        rows.sort(
            key=lambda item: (str(item.get("updatedAt") or ""), str(item["id"])), reverse=True
        )
        total = len(rows)
        summary = _ci_presence_collection(rows, total)["summary"]
        bounded_limit = max(1, min(int(limit or 50), 500))
        bounded_offset = max(0, int(offset or 0))
        return {
            "items": rows[bounded_offset : bounded_offset + bounded_limit],
            "total": total,
            "summary": summary,
        }

    def retire_ci_presence_mapping(
        self,
        presence_id: str,
        expected_revision: int,
        notes: str,
        actor_id: str | None,
    ) -> dict | None:
        """Deactivate an eligible immutable mapping without retiring its asset."""

        review_notes = str(notes or "").strip()
        if not review_notes:
            raise ValueError("Review notes are required")
        row = next(
            (
                item
                for item in self.state.get("integrationCiPresence", [])
                if item.get("id") == presence_id
            ),
            None,
        )
        mapping = next(
            (
                item
                for item in self.state.get("providerCiMappings", [])
                if row is not None and item.get("id") == row.get("mappingId")
            ),
            None,
        )
        if row is None or mapping is None:
            return None
        if int(row.get("revision") or 0) != int(expected_revision):
            raise ValueError("Presence lifecycle item changed; reload before saving")
        if row.get("state") != "eligible" or not mapping.get("active", True):
            raise ValueError("Only an eligible active mapping can be retired")
        stale_reason = self._state_ci_presence_stale_reason(row)
        if stale_reason:
            raise ValueError(f"Presence evidence is stale: {stale_reason}")
        before = deepcopy(row)
        mapping["active"] = False
        timestamp = utc_now()
        row.update(
            state="retired",
            reviewedBy=actor_id,
            reviewedAt=timestamp,
            reviewNotes=review_notes[:2000],
            evaluationReason="An administrator retired the immutable provider mapping.",
            revision=int(row.get("revision") or 0) + 1,
            updatedAt=timestamp,
        )
        retired_candidates = 0
        for candidate in self.state.get("ciRelationshipCandidates", []):
            if (
                candidate.get("sourceMappingId") == mapping.get("id")
                and candidate.get("state") == "pending"
                and not candidate.get("retiredAt")
            ):
                candidate["retiredAt"] = timestamp
                candidate["revision"] = int(candidate.get("revision") or 1) + 1
                retired_candidates += 1
        self._audit(
            row.get("companyId"),
            actor_id,
            "integration_ci_presence",
            row["id"],
            "mapping_retired",
            before,
            row,
            metadata={
                "mappingId": row.get("mappingId"),
                "assetId": row.get("assetId"),
                "retiredPendingRelationshipCandidates": retired_candidates,
            },
        )
        self.save_state(self.state)
        return _public_ci_presence({**row, "mappingActive": False})

    def restore_ci_presence_mapping(
        self,
        presence_id: str,
        expected_revision: int,
        notes: str,
        actor_id: str | None,
    ) -> dict | None:
        """Reactivate a provider mapping only after fresh provider evidence."""

        review_notes = str(notes or "").strip()
        if not review_notes:
            raise ValueError("Review notes are required")
        row = next(
            (
                item
                for item in self.state.get("integrationCiPresence", [])
                if item.get("id") == presence_id
            ),
            None,
        )
        mapping = next(
            (
                item
                for item in self.state.get("providerCiMappings", [])
                if row is not None and item.get("id") == row.get("mappingId")
            ),
            None,
        )
        if row is None or mapping is None:
            return None
        if int(row.get("revision") or 0) != int(expected_revision):
            raise ValueError("Presence lifecycle item changed; reload before saving")
        if row.get("state") != "restore_ready" or mapping.get("active", True):
            raise ValueError("Only a restore-ready retired mapping can be restored")
        stale_reason = self._state_ci_presence_stale_reason(row)
        if stale_reason:
            raise ValueError(f"Presence evidence is stale: {stale_reason}")
        before = deepcopy(row)
        mapping["active"] = True
        timestamp = utc_now()
        row.update(
            state="observed",
            absenceCount=0,
            firstMissingAt=None,
            lastMissingAt=None,
            candidateSince=None,
            reviewedBy=actor_id,
            reviewedAt=timestamp,
            reviewNotes=review_notes[:2000],
            evaluationReason="An administrator restored the observed provider mapping.",
            revision=int(row.get("revision") or 0) + 1,
            updatedAt=timestamp,
        )
        for item in self.state.get("integrationCiReviewItems", []):
            if (
                item.get("policyId") == row.get("policyId")
                and item.get("externalId") == row.get("externalId")
                and item.get("state") == "pending"
            ):
                item.update(state="resolved", reviewedBy=actor_id, reviewedAt=timestamp)
        self._audit(
            row.get("companyId"),
            actor_id,
            "integration_ci_presence",
            row["id"],
            "mapping_restored",
            before,
            row,
            metadata={"mappingId": row.get("mappingId"), "assetId": row.get("assetId")},
        )
        self.save_state(self.state)
        return _public_ci_presence({**row, "mappingActive": True})

    def get_ci_sync_policy(self, kind: str, company_id: str, provider_parent_id: str) -> dict:
        """Return one saved provider/customer CI policy or explicit safe defaults."""

        record = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
            ),
            None,
        )
        if record:
            failures = int(record.get("consecutiveFailures") or 0)
            return {
                **deepcopy(record),
                **normalize_ci_policy(record),
                "backoffActive": failures > 0,
                "retryDelayMinutes": (ci_sync_retry_delay_minutes(failures) if failures else 0),
            }
        return {
            "id": "",
            "provider": kind,
            "companyId": company_id,
            "providerParentId": provider_parent_id,
            **normalize_ci_policy(None),
            "revision": 0,
            "updatedAt": None,
            "nextRunAt": None,
            "lastRunAt": None,
            "lastSuccessAt": None,
            "lastError": "",
            "consecutiveFailures": 0,
            "backoffActive": False,
            "retryDelayMinutes": 0,
        }

    def update_ci_sync_policy(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        policy: dict,
        expected_revision: int | None = None,
        actor_id: str | None = None,
    ) -> dict:
        """Persist an audited CI filter/schedule policy with optimistic concurrency."""

        policies = self.state.setdefault("integrationCiPolicies", [])
        record = next(
            (
                item
                for item in policies
                if item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
            ),
            None,
        )
        before = deepcopy(record) if record else None
        revision = int(record.get("revision") or 0) if record else 0
        if expected_revision is not None and expected_revision != revision:
            raise ValueError("CI policy changed; reload before saving")
        normalized = normalize_ci_policy(policy)
        if not record:
            record = {
                "id": str(uuid.uuid4()),
                "provider": kind,
                "companyId": company_id,
                "providerParentId": provider_parent_id,
            }
            policies.append(record)
        record.update(
            normalized,
            revision=revision + 1,
            updatedAt=utc_now(),
            nextRunAt=(
                utc_now()
                if normalized["enabled"] and normalized["syncMode"] == "continuous_preview"
                else None
            ),
        )
        self._audit(
            company_id,
            actor_id,
            "integration_ci_policy",
            record["id"],
            "updated" if before else "created",
            before,
            record,
            metadata={"provider": kind, "providerParentId": provider_parent_id},
        )
        self.save_state(self.state)
        return deepcopy(record)

    def list_ci_sync_policies(self, kind: str | None = None) -> list[dict]:
        """Return saved CI policies with their worker scheduling state."""

        policies = [
            deepcopy(item)
            for item in self.state.get("integrationCiPolicies", [])
            if kind is None or item.get("provider") == kind
        ]
        for policy in policies:
            failures = int(policy.get("consecutiveFailures") or 0)
            policy["backoffActive"] = failures > 0
            policy["retryDelayMinutes"] = ci_sync_retry_delay_minutes(failures) if failures else 0
        return sorted(policies, key=lambda item: (item.get("companyId", ""), item.get("id", "")))

    def claim_due_ci_sync_policy(
        self, kind: str, worker_id: str, lease_seconds: int = 300
    ) -> dict | None:
        """Claim one due continuous-preview policy in the local repository."""

        connection = self.get_integration_connection(kind)
        if (
            not connection
            or not connection.get("enabled")
            or connection.get("lifecycleStatus", "active") != "active"
        ):
            return None
        now = datetime.now(UTC)
        due = []
        for policy in self.state.get("integrationCiPolicies", []):
            if policy.get("provider") != kind:
                continue
            if not policy.get("enabled") or policy.get("syncMode") != "continuous_preview":
                continue
            if any(
                run.get("policyId") == policy.get("id")
                and run.get("status") in ACTIVE_CI_PREVIEW_RUN_STATUSES
                for run in self.state.get("syncRuns", [])
            ):
                continue
            next_run = parse_timestamp(policy.get("nextRunAt"))
            lease_until = parse_timestamp(policy.get("leaseUntil"))
            if next_run and next_run > now:
                continue
            if lease_until and lease_until > now:
                continue
            due.append(policy)
        if not due:
            return None
        policy = min(due, key=lambda item: parse_timestamp(item.get("nextRunAt")) or now)
        policy["leaseOwner"] = worker_id[:120]
        policy["leaseUntil"] = (
            (now + timedelta(seconds=max(30, lease_seconds))).isoformat().replace("+00:00", "Z")
        )
        self.save_state(self.state)
        return self.get_ci_sync_policy(
            str(policy.get("provider") or ""),
            str(policy.get("companyId") or ""),
            str(policy.get("providerParentId") or ""),
        )

    def claim_ci_sync_policy_now(
        self, policy_id: str, worker_id: str, lease_seconds: int = 300
    ) -> dict | None:
        """Lease one saved policy immediately for an operator-triggered preview."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        if not policy:
            return None
        if any(
            run.get("policyId") == policy_id and run.get("status") in ACTIVE_CI_PREVIEW_RUN_STATUSES
            for run in self.state.get("syncRuns", [])
        ):
            return None
        connection = self.get_integration_connection(str(policy.get("provider") or ""))
        if (
            not connection
            or not connection.get("enabled")
            or connection.get("lifecycleStatus", "active") != "active"
        ):
            return None
        now = datetime.now(UTC)
        lease_until = parse_timestamp(policy.get("leaseUntil"))
        if lease_until and lease_until > now:
            return None
        policy["leaseOwner"] = worker_id[:120]
        policy["leaseUntil"] = (
            (now + timedelta(seconds=max(30, min(lease_seconds, 3600))))
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.save_state(self.state)
        return deepcopy(policy)

    def _finish_state_ci_sync_policy(
        self,
        policy_id: str,
        lease_owner: str,
        *,
        success: bool | None,
        error: str = "",
    ) -> dict | None:
        """Finish an owned local policy lease without persisting the outer operation."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        if not policy:
            return None
        if policy.get("leaseOwner") != str(lease_owner)[:120]:
            return None
        if success is None:
            policy["leaseOwner"] = None
            policy["leaseUntil"] = None
            return policy
        now = datetime.now(UTC)
        failures = 0 if success else int(policy.get("consecutiveFailures") or 0) + 1
        interval = max(15, min(int(policy.get("intervalMinutes") or 360), 10080))
        scheduled = policy.get("enabled") and policy.get("syncMode") == "continuous_preview"
        delay = interval if success else ci_sync_retry_delay_minutes(failures)
        policy.update(
            lastRunAt=now.isoformat().replace("+00:00", "Z"),
            lastSuccessAt=(
                now.isoformat().replace("+00:00", "Z") if success else policy.get("lastSuccessAt")
            ),
            lastError="" if success else str(error)[:1000],
            consecutiveFailures=failures,
            nextRunAt=(
                (now + timedelta(minutes=delay)).isoformat().replace("+00:00", "Z")
                if scheduled
                else None
            ),
            leaseOwner=None,
            leaseUntil=None,
        )
        return policy

    def complete_ci_sync_policy_run(
        self,
        policy_id: str,
        *,
        lease_owner: str,
        success: bool,
        error: str = "",
    ) -> dict | None:
        """Finish an owned policy lease and calculate its next scheduled execution."""

        policy = self._finish_state_ci_sync_policy(
            policy_id,
            lease_owner,
            success=success,
            error=error,
        )
        if not policy:
            return None
        self.save_state(self.state)
        return self.get_ci_sync_policy(
            str(policy.get("provider") or ""),
            str(policy.get("companyId") or ""),
            str(policy.get("providerParentId") or ""),
        )

    def renew_ci_sync_policy_run(
        self,
        policy_id: str,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> dict | None:
        """Extend an unexpired local policy lease owned by the same worker."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        now = datetime.now(UTC)
        if (
            not policy
            or policy.get("leaseOwner") != str(lease_owner)[:120]
            or (parse_timestamp(policy.get("leaseUntil")) or datetime.min.replace(tzinfo=UTC))
            <= now
        ):
            return None
        policy["leaseUntil"] = (
            (now + timedelta(seconds=max(30, min(int(lease_seconds), 3600))))
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.save_state(self.state)
        return self.get_ci_sync_policy(
            str(policy.get("provider") or ""),
            str(policy.get("companyId") or ""),
            str(policy.get("providerParentId") or ""),
        )

    @staticmethod
    def _ci_policy_preview_scope_matches(
        policy: dict,
        kind: str,
        run: dict,
    ) -> bool:
        """Validate that terminal preview evidence belongs to its leased policy."""

        attributes = run.get("attributes") or {}
        return bool(
            policy.get("provider") == kind
            and str(attributes.get("policyId") or "") == str(policy.get("id") or "")
            and str(attributes.get("companyId") or "") == str(policy.get("companyId") or "")
            and str(attributes.get("providerCompanyId") or "")
            == str(policy.get("providerParentId") or "")
        )

    def _record_state_ci_policy_preview(
        self,
        kind: str,
        policy: dict,
        run: dict,
        actor_id: str | None,
    ) -> dict:
        """Insert terminal local preview evidence without saving the outer transaction."""

        stored = deepcopy(run)
        if any(item.get("id") == stored.get("id") for item in self.state.get("syncRuns", [])):
            raise ValueError("Sync run already exists")
        self.state["syncRuns"] = [stored, *self.state.get("syncRuns", [])]
        self._retain_sync_runs()
        integration = next(
            (
                item
                for item in self.state.get("integrations", [])
                if item.get("type") == kind and item.get("companyId") is None
            ),
            None,
        )
        if integration:
            before = deepcopy(integration)
            integration.update(
                lastSync=stored.get("finishedAt"),
                status=(
                    "Healthy"
                    if stored.get("status") == "success"
                    else stored.get("status", "unknown")
                ),
            )
            self._audit(
                policy.get("companyId"),
                actor_id,
                "integration_connection",
                integration["id"],
                "sync_completed",
                before,
                integration,
            )
        return stored

    def publish_and_complete_ci_policy_preview(
        self,
        kind: str,
        policy_id: str,
        lease_owner: str,
        run: dict,
        review_items: list[dict],
        actor_id: str | None = None,
        *,
        presence_snapshot: dict[str, Any] | None = None,
    ) -> dict | None:
        """Atomically publish a directly executed preview under its policy lease."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        bounded_owner = str(lease_owner)[:120]
        now = datetime.now(UTC)
        if (
            not policy
            or policy.get("leaseOwner") != bounded_owner
            or (parse_timestamp(policy.get("leaseUntil")) or datetime.min.replace(tzinfo=UTC))
            <= now
            or not self._ci_policy_preview_scope_matches(policy, kind, run)
        ):
            return None
        snapshot = deepcopy(self.state)
        try:
            stored = self._record_state_ci_policy_preview(kind, policy, run, actor_id)
            if presence_snapshot is not None:
                stored.setdefault("attributes", {})["connectionRevision"] = int(
                    _normalized_ci_presence_snapshot(presence_snapshot)["connectionRevision"]
                )
            queue_summary = self._replace_ci_review_items_in_state(
                policy_id,
                str(policy.get("companyId") or ""),
                str(stored["id"]),
                review_items,
                actor_id,
            )
            if presence_snapshot is not None:
                attributes = stored.get("attributes") or {}
                self._apply_state_ci_presence_snapshot(
                    kind,
                    policy,
                    str(stored["id"]),
                    presence_snapshot,
                    int(attributes.get("policyRevision") or 0),
                )
            trigger = str((stored.get("attributes") or {}).get("trigger") or "")
            finished_policy = self._finish_state_ci_sync_policy(
                policy_id,
                bounded_owner,
                success=True if trigger in {"continuous_preview", "manual_sync"} else None,
            )
            if not finished_policy:
                raise RuntimeError("Preview policy lease is no longer owned")
            self.save_state(self.state)
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return {
            "run": _public_sync_run(stored),
            "queueSummary": queue_summary,
            "policy": self.get_ci_sync_policy(
                str(policy.get("provider") or ""),
                str(policy.get("companyId") or ""),
                str(policy.get("providerParentId") or ""),
            ),
        }

    def fail_and_complete_ci_policy_preview(
        self,
        kind: str,
        policy_id: str,
        lease_owner: str,
        run: dict,
        error: Any,
        actor_id: str | None = None,
    ) -> dict | None:
        """Atomically record an owned direct-preview failure and finish its policy."""

        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        bounded_owner = str(lease_owner)[:120]
        now = datetime.now(UTC)
        if (
            not policy
            or policy.get("leaseOwner") != bounded_owner
            or (parse_timestamp(policy.get("leaseUntil")) or datetime.min.replace(tzinfo=UTC))
            <= now
            or not self._ci_policy_preview_scope_matches(policy, kind, run)
        ):
            return None
        snapshot = deepcopy(self.state)
        try:
            stored = self._record_state_ci_policy_preview(kind, policy, run, actor_id)
            trigger = str((stored.get("attributes") or {}).get("trigger") or "")
            finished_policy = self._finish_state_ci_sync_policy(
                policy_id,
                bounded_owner,
                success=False if trigger in {"continuous_preview", "manual_sync"} else None,
                error=str(error)[:1000],
            )
            if not finished_policy:
                raise RuntimeError("Preview policy lease is no longer owned")
            self.save_state(self.state)
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return {
            "run": _public_sync_run(stored),
            "policy": self.get_ci_sync_policy(
                str(policy.get("provider") or ""),
                str(policy.get("companyId") or ""),
                str(policy.get("providerParentId") or ""),
            ),
        }

    def _replace_ci_review_items_in_state(
        self,
        policy_id: str,
        company_id: str,
        run_id: str,
        items: list[dict],
        actor_id: str | None = None,
    ) -> dict[str, int]:
        """Mutate the local review queue without persisting the surrounding transaction."""

        queue = self.state.setdefault("integrationCiReviewItems", [])
        reviewable = [
            deepcopy(item)
            for item in items
            if item.get("action") in {"create", "update", "link", "conflict"}
        ]
        seen = {str(item["externalId"]) for item in reviewable}
        resolved = 0
        for queued in queue:
            if (
                queued.get("policyId") == policy_id
                and queued.get("state") == "pending"
                and queued.get("externalId") not in seen
            ):
                queued["state"] = "resolved"
                queued["reviewedAt"] = utc_now()
                resolved += 1
        created = 0
        updated = 0
        now = utc_now()
        for item in reviewable:
            content = {
                "action": item.get("action"),
                "assetId": item.get("assetId"),
                "reason": item.get("reason", ""),
                "changes": item.get("changes") or {},
                "blockedFields": item.get("blockedFields") or [],
                "fieldDecisions": item.get("fieldDecisions") or [],
                "record": item.get("record") or {},
            }
            content_hash = hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            queued = next(
                (
                    row
                    for row in queue
                    if row.get("policyId") == policy_id
                    and row.get("externalId") == item.get("externalId")
                ),
                None,
            )
            if not queued:
                queued = {
                    "id": str(uuid.uuid4()),
                    "policyId": policy_id,
                    "companyId": company_id,
                    "externalId": str(item["externalId"]),
                    "firstSeenAt": now,
                    "state": "pending",
                }
                queue.append(queued)
                created += 1
            else:
                if queued.get("contentHash") != content_hash or queued.get("state") == "resolved":
                    queued.update(state="pending", reviewedBy=None, reviewedAt=None, reviewNotes="")
                updated += 1
            record = item.get("record") or {}
            queued.update(
                externalName=item.get("name") or record.get("name") or item["externalId"],
                action=item["action"],
                assetId=item.get("assetId"),
                assetName=item.get("assetName") or "",
                reason=item.get("reason") or "",
                providerTypeName=record.get("providerTypeName") or record.get("type") or "",
                providerStatusName=record.get("providerStatusName") or record.get("status") or "",
                providerRecord=deepcopy(record),
                evidence={
                    "changedFields": deepcopy(item.get("changedFields") or []),
                    "blockedFields": deepcopy(item.get("blockedFields") or []),
                    "fieldDecisions": deepcopy(item.get("fieldDecisions") or []),
                    "changes": deepcopy(item.get("changes") or {}),
                },
                contentHash=content_hash,
                lastRunId=run_id,
                lastSeenAt=now,
            )
        self._audit(
            company_id,
            actor_id,
            "integration_ci_policy",
            policy_id,
            "review_queue_refreshed",
            None,
            {
                "pending": len(reviewable),
                "created": created,
                "updated": updated,
                "resolved": resolved,
            },
            metadata={"syncRunId": run_id},
            actor_type="system" if actor_id is None else "user",
            source_system="integration_worker" if actor_id is None else "web",
        )
        return {
            "pending": len(reviewable),
            "created": created,
            "updated": updated,
            "resolved": resolved,
        }

    def replace_ci_review_items(
        self,
        policy_id: str,
        company_id: str,
        run_id: str,
        items: list[dict],
        actor_id: str | None = None,
    ) -> dict[str, int]:
        """Refresh the current review queue while preserving unchanged dismissals."""

        summary = self._replace_ci_review_items_in_state(
            policy_id,
            company_id,
            run_id,
            items,
            actor_id,
        )
        self.save_state(self.state)
        return summary

    def list_ci_review_items(
        self,
        kind: str | None = None,
        company_id: str | None = None,
        state: str | None = "pending",
        limit: int = 250,
    ) -> list[dict]:
        """Return the current provider-neutral CI review queue."""

        policies = {item["id"]: item for item in self.list_ci_sync_policies(kind)}
        records = []
        for item in self.state.get("integrationCiReviewItems", []):
            policy = policies.get(item.get("policyId"))
            if not policy:
                continue
            if company_id and item.get("companyId") != company_id:
                continue
            if state and item.get("state") != state:
                continue
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            records.append(
                {
                    **deepcopy(item),
                    "provider": policy.get("provider"),
                    "providerParentId": policy.get("providerParentId"),
                    "changedFields": deepcopy(evidence.get("changedFields") or []),
                    "blockedFields": deepcopy(evidence.get("blockedFields") or []),
                    "fieldDecisions": deepcopy(evidence.get("fieldDecisions") or []),
                }
            )
        return sorted(records, key=lambda item: item.get("lastSeenAt", ""), reverse=True)[
            : max(1, min(limit, 1000))
        ]

    def count_ci_review_items(
        self,
        kind: str | None = None,
        company_id: str | None = None,
        state: str | None = "pending",
    ) -> int:
        """Count current CI review observations without a page-size cap."""

        policies = {item["id"]: item for item in self.list_ci_sync_policies(kind)}
        return sum(
            1
            for item in self.state.get("integrationCiReviewItems", [])
            if item.get("policyId") in policies
            and (not company_id or item.get("companyId") == company_id)
            and (not state or item.get("state") == state)
        )

    def query_ci_review_items(
        self,
        *,
        kind: str | None = None,
        company_id: str | None = None,
        company_ids: Iterable[str] | None = None,
        state: str | None = "pending",
        action: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return a filtered, paged provider-neutral review workbench."""

        policies = {item["id"]: item for item in self.list_ci_sync_policies(kind)}
        permitted_companies = set(company_ids) if company_ids is not None else None
        needle = search.strip().casefold()
        records: list[dict] = []
        for raw in self.state.get("integrationCiReviewItems", []):
            policy = policies.get(raw.get("policyId"))
            if not policy:
                continue
            evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
            item = {
                **deepcopy(raw),
                "provider": policy.get("provider"),
                "providerParentId": policy.get("providerParentId"),
                "changedFields": deepcopy(evidence.get("changedFields") or []),
                "blockedFields": deepcopy(evidence.get("blockedFields") or []),
                "fieldDecisions": deepcopy(evidence.get("fieldDecisions") or []),
            }
            if company_id and item.get("companyId") != company_id:
                continue
            if permitted_companies is not None and item.get("companyId") not in permitted_companies:
                continue
            if state and item.get("state") != state:
                continue
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("externalName", "externalId", "assetName", "reason")
            ).casefold()
            if needle and needle not in haystack:
                continue
            records.append(item)
        summary = {
            decision: sum(item.get("action") == decision for item in records)
            for decision in ("create", "update", "link", "conflict")
        }
        filtered = [item for item in records if not action or item.get("action") == action]
        filtered.sort(key=lambda item: item.get("lastSeenAt", ""), reverse=True)
        page_limit = max(1, min(limit, 250))
        page_offset = max(0, offset)
        return {
            "items": filtered[page_offset : page_offset + page_limit],
            "total": len(filtered),
            "summary": summary,
        }

    def get_ci_review_item(self, item_id: str) -> dict | None:
        """Return one review item by immutable queue identifier."""

        raw = next(
            (
                item
                for item in self.state.get("integrationCiReviewItems", [])
                if item.get("id") == item_id
            ),
            None,
        )
        if not raw:
            return None
        policy = next(
            (
                item
                for item in self.list_ci_sync_policies()
                if item.get("id") == raw.get("policyId")
            ),
            None,
        )
        if not policy:
            return None
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        return {
            **deepcopy(raw),
            "provider": policy.get("provider"),
            "providerParentId": policy.get("providerParentId"),
            "changedFields": deepcopy(evidence.get("changedFields") or []),
            "blockedFields": deepcopy(evidence.get("blockedFields") or []),
            "fieldDecisions": deepcopy(evidence.get("fieldDecisions") or []),
        }

    def get_ci_review_item_by_identity(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        external_id: str,
        state: str | None = "pending",
    ) -> dict | None:
        """Return one exact provider observation without depending on queue pagination."""

        policy_ids = {
            item["id"]
            for item in self.state.get("integrationCiPolicies", [])
            if item.get("provider") == kind
            and item.get("companyId") == company_id
            and item.get("providerParentId") == provider_parent_id
        }
        raw = next(
            (
                item
                for item in self.state.get("integrationCiReviewItems", [])
                if item.get("policyId") in policy_ids
                and item.get("externalId") == external_id
                and (state is None or item.get("state") == state)
            ),
            None,
        )
        return self.get_ci_review_item(str(raw["id"])) if raw else None

    def dismiss_ci_review_item(self, item_id: str, notes: str, actor_id: str) -> dict | None:
        """Dismiss one unchanged review observation with audited notes."""

        item = next(
            (
                row
                for row in self.state.get("integrationCiReviewItems", [])
                if row.get("id") == item_id
            ),
            None,
        )
        if not item:
            return None
        before = deepcopy(item)
        item.update(state="dismissed", reviewedBy=actor_id, reviewedAt=utc_now(), reviewNotes=notes)
        self._audit(
            item.get("companyId"),
            actor_id,
            "integration_ci_review_item",
            item_id,
            "dismissed",
            before,
            item,
        )
        self.save_state(self.state)
        return deepcopy(item)

    def ignore_ci_review_items(self, item_ids: list[str], notes: str, actor_id: str) -> list[dict]:
        """Persist durable immutable-ID suppressions for one governed CI policy."""

        selected_ids = set(item_ids)
        items = [
            item
            for item in self.state.get("integrationCiReviewItems", [])
            if item.get("id") in selected_ids
        ]
        if len(items) != len(selected_ids):
            raise ValueError("One or more review items no longer exist")
        policy_ids = {str(item.get("policyId") or "") for item in items}
        if len(policy_ids) != 1:
            raise ValueError("Ignored configurations must belong to one customer policy")
        policy_id = next(iter(policy_ids))
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        if not policy:
            raise ValueError("CI policy not found")

        now = utc_now()
        suppressions = self.state.setdefault("integrationObjectSuppressions", [])
        stored: list[dict] = []
        excluded_ids = set(normalize_ci_policy(policy)["excludedExternalIds"])
        for item in items:
            external_id = str(item.get("externalId") or "")
            suppression = next(
                (
                    row
                    for row in suppressions
                    if row.get("policyId") == policy_id
                    and row.get("externalObjectType") == "configuration"
                    and row.get("externalId") == external_id
                ),
                None,
            )
            before = deepcopy(suppression) if suppression else None
            if not suppression:
                suppression = {
                    "id": str(uuid.uuid4()),
                    "policyId": policy_id,
                    "companyId": item.get("companyId"),
                    "externalObjectType": "configuration",
                    "externalId": external_id,
                    "createdAt": now,
                }
                suppressions.append(suppression)
            suppression.update(
                externalName=item.get("externalName") or external_id,
                providerRecord=deepcopy(item.get("providerRecord") or {}),
                reason=notes,
                active=True,
                ignoredBy=actor_id,
                ignoredAt=now,
                restoredBy=None,
                restoredAt=None,
                restoreReason="",
                updatedAt=now,
            )
            item.update(
                state="resolved",
                reviewedBy=actor_id,
                reviewedAt=now,
                reviewNotes=notes,
            )
            excluded_ids.add(external_id)
            self._audit(
                item.get("companyId"),
                actor_id,
                "integration_object_suppression",
                suppression["id"],
                "ignored" if before is None else "reignored",
                before,
                suppression,
                metadata={
                    "provider": policy.get("provider"),
                    "policyId": policy_id,
                    "externalId": external_id,
                },
            )
            stored.append(deepcopy(suppression))

        policy_before = deepcopy(policy)
        policy.update(
            excludedExternalIds=sorted(excluded_ids),
            revision=int(policy.get("revision") or 0) + 1,
            updatedAt=now,
        )
        self._audit(
            policy.get("companyId"),
            actor_id,
            "integration_ci_policy",
            policy_id,
            "suppression_updated",
            policy_before,
            policy,
            metadata={"ignoredExternalIds": sorted(excluded_ids)},
        )
        self.save_state(self.state)
        return stored

    def query_integration_object_suppressions(
        self,
        *,
        kind: str | None = None,
        company_id: str | None = None,
        company_ids: Iterable[str] | None = None,
        active: bool | None = True,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return filtered, tenant-safe provider object suppressions."""

        policies = {item["id"]: item for item in self.list_ci_sync_policies(kind)}
        companies = {
            item.get("id"): item.get("name", item.get("id", ""))
            for item in self.state.get("companies", [])
        }
        users = {
            item.get("id"): item.get("email", item.get("id", ""))
            for item in self.state.get("users", [])
        }
        permitted_companies = set(company_ids) if company_ids is not None else None
        needle = search.strip().casefold()
        records: list[dict] = []
        for raw in self.state.get("integrationObjectSuppressions", []):
            policy = policies.get(raw.get("policyId"))
            if not policy:
                continue
            item = {
                **deepcopy(raw),
                "provider": policy.get("provider"),
                "providerParentId": policy.get("providerParentId"),
                "companyName": companies.get(raw.get("companyId"), raw.get("companyId", "")),
                "ignoredByName": users.get(raw.get("ignoredBy"), raw.get("ignoredBy", "")),
                "restoredByName": users.get(raw.get("restoredBy"), raw.get("restoredBy", "")),
            }
            if company_id and item.get("companyId") != company_id:
                continue
            if permitted_companies is not None and item.get("companyId") not in permitted_companies:
                continue
            if active is not None and bool(item.get("active")) is not active:
                continue
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("externalName", "externalId", "reason", "restoreReason")
            ).casefold()
            if needle and needle not in haystack:
                continue
            records.append(item)
        records.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
        page_limit = max(1, min(limit, 250))
        page_offset = max(0, offset)
        return {
            "items": records[page_offset : page_offset + page_limit],
            "total": len(records),
        }

    def get_integration_object_suppression(self, suppression_id: str) -> dict | None:
        """Return one provider-object suppression by its governed identifier."""

        raw = next(
            (
                item
                for item in self.state.get("integrationObjectSuppressions", [])
                if item.get("id") == suppression_id
            ),
            None,
        )
        if not raw:
            return None
        policy = next(
            (
                item
                for item in self.list_ci_sync_policies()
                if item.get("id") == raw.get("policyId")
            ),
            None,
        )
        if not policy:
            return None
        companies = {
            item.get("id"): item.get("name", item.get("id", ""))
            for item in self.state.get("companies", [])
        }
        users = {
            item.get("id"): item.get("email", item.get("id", ""))
            for item in self.state.get("users", [])
        }
        return {
            **deepcopy(raw),
            "provider": policy.get("provider"),
            "providerParentId": policy.get("providerParentId"),
            "companyName": companies.get(raw.get("companyId"), raw.get("companyId", "")),
            "ignoredByName": users.get(raw.get("ignoredBy"), raw.get("ignoredBy", "")),
            "restoredByName": users.get(raw.get("restoredBy"), raw.get("restoredBy", "")),
        }

    def restore_integration_object_suppression(
        self, suppression_id: str, notes: str, actor_id: str
    ) -> dict | None:
        """Restore one ignored provider object without changing its canonical CI."""

        suppression = next(
            (
                item
                for item in self.state.get("integrationObjectSuppressions", [])
                if item.get("id") == suppression_id
            ),
            None,
        )
        if not suppression or not suppression.get("active"):
            return None
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == suppression.get("policyId")
            ),
            None,
        )
        if not policy:
            raise ValueError("CI policy not found")
        before = deepcopy(suppression)
        policy_before = deepcopy(policy)
        now = utc_now()
        suppression.update(
            active=False,
            restoredBy=actor_id,
            restoredAt=now,
            restoreReason=notes,
            updatedAt=now,
        )
        external_id = str(suppression.get("externalId") or "")
        policy.update(
            excludedExternalIds=[
                value
                for value in normalize_ci_policy(policy)["excludedExternalIds"]
                if value != external_id
            ],
            revision=int(policy.get("revision") or 0) + 1,
            updatedAt=now,
        )
        for item in self.state.get("integrationCiReviewItems", []):
            if item.get("policyId") == policy["id"] and item.get("externalId") == external_id:
                item.update(
                    state="pending",
                    reviewedBy=None,
                    reviewedAt=None,
                    reviewNotes="",
                )
        self._audit(
            suppression.get("companyId"),
            actor_id,
            "integration_object_suppression",
            suppression_id,
            "restored",
            before,
            suppression,
            metadata={
                "provider": policy.get("provider"),
                "policyId": policy.get("id"),
                "externalId": external_id,
            },
        )
        self._audit(
            policy.get("companyId"),
            actor_id,
            "integration_ci_policy",
            policy["id"],
            "suppression_updated",
            policy_before,
            policy,
            metadata={"restoredExternalId": external_id},
        )
        self.save_state(self.state)
        return deepcopy(suppression)

    def resolve_ci_review_items(
        self, policy_id: str, external_ids: list[str], actor_id: str | None = None
    ) -> int:
        """Resolve queue entries after an explicit import or immutable-ID link."""

        selected = set(external_ids)
        resolved = 0
        for item in self.state.get("integrationCiReviewItems", []):
            if item.get("policyId") == policy_id and item.get("externalId") in selected:
                item.update(state="resolved", reviewedBy=actor_id, reviewedAt=utc_now())
                resolved += 1
        if resolved:
            self.save_state(self.state)
        return resolved

    def _provider_import_failpoint(self, stage: str) -> None:
        """Provide a deterministic no-op seam for atomic-import rollback tests."""

    def apply_reviewed_provider_ci_import(
        self,
        kind: str,
        company_id: str,
        record: dict,
        action: str,
        *,
        asset_id: str | None = None,
        asset: dict | None = None,
        changes: dict | None = None,
        actor_id: str | None = None,
        provider_parent_id: str | None = None,
        policy_id: str | None = None,
        review_item_id: str | None = None,
        review_content_hash: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
    ) -> dict:
        """Apply one reviewed create, update or link as a single local commit."""

        if action not in {"create", "update", "link"}:
            raise ValueError("Provider import action is invalid")
        if not isinstance(record, dict) or not str(record.get("externalId") or "").strip():
            raise ValueError("Provider external identity is required")
        selected_parent_id = str(provider_parent_id or record.get("providerParentId") or "").strip()
        working = StateRepository(deepcopy(self.state), lambda _value: None)
        company_mapping = next(
            (
                item
                for item in working.state.get("providerCompanyMappings", [])
                if item.get("provider") == kind
                and item.get("externalId") == selected_parent_id
                and item.get("companyId") == company_id
                and item.get("active", True)
            ),
            None,
        )
        company_observation = next(
            (
                item
                for item in working.state.get("providerCompanyObservations", [])
                if item.get("provider") == kind
                and item.get("externalId") == selected_parent_id
                and item.get("active", True)
                and not item.get("deleted", False)
            ),
            None,
        )
        if not selected_parent_id or not company_mapping or not company_observation:
            raise ValueError(
                "The active provider customer mapping changed during review. Run a new preview."
            )
        generation_supplied = (
            expected_policy_revision is not None or expected_connection_revision is not None
        )
        if generation_supplied:
            if (
                not policy_id
                or expected_policy_revision is None
                or expected_connection_revision is None
            ):
                raise ValueError("Reviewed provider generation is incomplete. Run a new preview.")
            current_policy = next(
                (
                    item
                    for item in working.state.get("integrationCiPolicies", [])
                    if item.get("id") == policy_id
                    and item.get("provider") == kind
                    and item.get("companyId") == company_id
                    and item.get("providerParentId") == selected_parent_id
                ),
                None,
            )
            current_connection = working.get_integration_connection(kind)
            if (
                not current_policy
                or int(current_policy.get("revision") or 0) != int(expected_policy_revision)
                or not current_connection
                or not current_connection.get("enabled", False)
                or current_connection.get("lifecycleStatus", "active") != "active"
                or int(current_connection.get("revision") or 0) != int(expected_connection_revision)
            ):
                raise ValueError("Reviewed provider generation is stale. Run a new preview.")
        review_guard_supplied = review_item_id is not None or review_content_hash is not None
        review_item = None
        if review_guard_supplied:
            if not review_item_id or not review_content_hash or not policy_id:
                raise ValueError("Reviewed queue evidence is incomplete. Run a new preview.")
            review_item = next(
                (
                    item
                    for item in working.state.get("integrationCiReviewItems", [])
                    if item.get("id") == review_item_id
                    and item.get("policyId") == policy_id
                    and item.get("companyId") == company_id
                    and item.get("externalId") == str(record["externalId"])
                    and item.get("state") == "pending"
                    and item.get("contentHash") == review_content_hash
                ),
                None,
            )
            if review_item is None:
                raise ValueError("Reviewed queue item changed. Run a new preview.")
        classification = working.classify_provider_ci_mapping_import(
            kind,
            company_id,
            str(record["externalId"]),
            selected_parent_id,
        )
        if classification["decision"] != "allow":
            raise ValueError(str(classification["reason"]))
        existing_mapping = classification.get("mappingId") is not None
        if action == "create" and existing_mapping:
            raise ValueError("Provider identity is already mapped; refresh the import preview")
        existing = next(
            (
                item
                for item in working.state.get("providerCiMappings", [])
                if item.get("id") == classification.get("mappingId")
            ),
            None,
        )
        if action == "update" and (
            not existing or str(existing.get("assetId") or "") != str(asset_id or "")
        ):
            raise ValueError(
                "Provider update target no longer matches its immutable mapping. Run a new preview."
            )

        stored_asset: dict | None
        if action == "create":
            if not isinstance(asset, dict) or asset.get("companyId") != company_id:
                raise ValueError("A same-customer canonical asset is required for create")
            stored_asset = working.create_asset(deepcopy(asset), actor_id)
            self._provider_import_failpoint("after_asset_write")
            mapping = working.record_provider_ci_mapping(
                kind,
                company_id,
                deepcopy(record),
                stored_asset["id"],
                actor_id,
                provider_parent_id=selected_parent_id,
            )
        else:
            if not asset_id:
                raise ValueError("A canonical target is required for update or link")
            # Lock/record immutable identity before changing an existing CI. The
            # working copy is published only after every operation succeeds.
            mapping = working.record_provider_ci_mapping(
                kind,
                company_id,
                deepcopy(record),
                asset_id,
                actor_id,
                provider_parent_id=selected_parent_id,
            )
            if changes:
                stored_asset = working.update_asset(asset_id, deepcopy(changes), actor_id)
            else:
                stored_asset = next(
                    (
                        deepcopy(item)
                        for item in working.state.get("assets", [])
                        if item.get("id") == asset_id and item.get("companyId") == company_id
                    ),
                    None,
                )
            if stored_asset is None:
                raise ValueError("Configuration item is unavailable in this customer")
            self._provider_import_failpoint("after_asset_write")

        resolved = 0
        if review_item is not None:
            review_item.update(state="resolved", reviewedBy=actor_id, reviewedAt=utc_now())
            resolved = 1
        elif policy_id:
            policy = next(
                (
                    item
                    for item in working.state.get("integrationCiPolicies", [])
                    if item.get("id") == policy_id
                    and item.get("provider") == kind
                    and item.get("companyId") == company_id
                    and (
                        not selected_parent_id or item.get("providerParentId") == selected_parent_id
                    )
                ),
                None,
            )
            if policy:
                resolved = working.resolve_ci_review_items(
                    policy_id,
                    [str(record["externalId"])],
                    actor_id,
                )
        assert stored_asset is not None
        self.state.clear()
        self.state.update(working.state)
        self.save_state(self.state)
        return {
            "action": action,
            "asset": deepcopy(stored_asset),
            "mapping": deepcopy(mapping),
            "reviewItemsResolved": resolved,
        }

    def record_provider_ci_mapping(
        self,
        kind: str,
        company_id: str,
        record: dict,
        asset_id: str,
        actor_id: str | None = None,
        *,
        provider_parent_id: str | None = None,
    ) -> dict:
        """Upsert provider identity and retain a deduplicated source observation."""

        mappings = self.state.setdefault("providerCiMappings", [])
        mapping = next(
            (
                item
                for item in mappings
                if item.get("provider") == kind and item.get("externalId") == record["externalId"]
            ),
            None,
        )
        before = deepcopy(mapping) if mapping else None
        selected_parent_id = str(provider_parent_id or record.get("providerParentId") or "").strip()
        presence = next(
            (
                item
                for item in self.state.get("integrationCiPresence", [])
                if mapping is not None and item.get("mappingId") == mapping.get("id")
            ),
            None,
        )
        classification = self.classify_provider_ci_mapping_import(
            kind,
            company_id,
            record["externalId"],
            selected_parent_id,
        )
        if classification["decision"] != "allow":
            raise ValueError(str(classification["reason"]))
        if not mapping:
            mapping = {
                "id": str(uuid.uuid4()),
                "provider": kind,
                "companyId": company_id,
                "externalId": record["externalId"],
                "firstSeenAt": utc_now(),
            }
            mappings.append(mapping)
        observed_at = utc_now()
        mapping.update(
            companyId=company_id,
            assetId=asset_id,
            externalName=record.get("name", ""),
            externalVersion=record.get("providerVersion", ""),
            providerParentId=selected_parent_id or mapping.get("providerParentId"),
            active=True,
            lastSeenAt=observed_at,
            lastSyncedAt=observed_at,
        )
        if presence is not None:
            presence_before = deepcopy(presence)
            selected_policy = next(
                (
                    item
                    for item in self.state.get("integrationCiPolicies", [])
                    if item.get("provider") == kind
                    and item.get("companyId") == company_id
                    and item.get("providerParentId") == mapping.get("providerParentId")
                ),
                None,
            )
            if selected_policy is None:
                self.state["integrationCiPresence"].remove(presence)
            else:
                presence.update(
                    policyId=selected_policy["id"],
                    provider=kind,
                    companyId=company_id,
                    providerParentId=mapping.get("providerParentId"),
                    assetId=asset_id,
                    externalId=record["externalId"],
                    externalName=record.get("name") or record["externalId"],
                    state="observed",
                    absenceCount=0,
                    firstMissingAt=None,
                    lastMissingAt=None,
                    candidateSince=None,
                    lastObservedAt=observed_at,
                    lastEvaluatedAt=observed_at,
                    lastEvaluatedRunId=None,
                    evaluationReason="Reviewed import observed and re-scoped this provider mapping.",
                    discoveryScopeFingerprint="0" * 64,
                    policyDecisionFingerprint="0" * 64,
                    policyRevision=int(selected_policy.get("revision") or 0),
                    revision=int(presence.get("revision") or 0) + 1,
                    updatedAt=observed_at,
                )
            self._audit(
                company_id,
                actor_id,
                "integration_ci_presence",
                str(presence.get("id") or mapping["id"]),
                "mapping_rescoped",
                presence_before,
                presence if selected_policy is not None else None,
                metadata={"mappingId": mapping["id"], "assetId": asset_id},
            )
        payload_hash = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observations = self.state.setdefault("providerCiObservations", [])
        if not any(
            item.get("mappingId") == mapping["id"] and item.get("payloadHash") == payload_hash
            for item in observations
        ):
            observations.append(
                {
                    "id": str(uuid.uuid4()),
                    "mappingId": mapping["id"],
                    "assetId": asset_id,
                    "payloadHash": payload_hash,
                    "fields": deepcopy(record),
                    "observedAt": utc_now(),
                }
            )
        self._audit(
            company_id,
            actor_id,
            "external_ci_mapping",
            mapping["id"],
            (
                "remapped"
                if before and before.get("assetId") != asset_id
                else ("source_observed" if before else "mapped")
            ),
            before,
            mapping,
        )
        self.save_state(self.state)
        return deepcopy(mapping)

    def _state_provider_ci_mapping(
        self,
        kind: str,
        company_id: str,
        asset_id: str,
        mapping_id: str,
    ) -> dict:
        """Return one active provider mapping after enforcing its tenant and CI."""

        mapping = next(
            (
                item
                for item in self.state.get("providerCiMappings", [])
                if item.get("id") == mapping_id
                and item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("assetId") == asset_id
                and item.get("active", True)
            ),
            None,
        )
        asset = next(
            (
                item
                for item in self.state.get("assets", [])
                if item.get("id") == asset_id
                and item.get("companyId") == company_id
                and not item.get("retiredAt")
            ),
            None,
        )
        if not mapping or not asset:
            raise ValueError("Provider mapping is unavailable for this customer asset")
        return mapping

    def replace_ci_inventory(
        self,
        kind: str,
        company_id: str,
        asset_id: str,
        mapping_id: str,
        collections: dict[str, Any],
        *,
        observed_at: str | None = None,
        retention: int = 3,
    ) -> dict:
        """Replace current provider inventory and retain bounded content history."""

        self._state_provider_ci_mapping(kind, company_id, asset_id, mapping_id)
        source = (
            collections.get("inventoryCollections")
            if isinstance(collections, dict)
            and isinstance(collections.get("inventoryCollections"), dict)
            else collections
        )
        if not isinstance(source, dict):
            raise ValueError("Inventory collections must be an object")
        observed = observed_at or utc_now()
        if not parse_timestamp(observed):
            raise ValueError("Inventory observed timestamp is invalid")
        keep = max(1, min(int(retention), 12))
        normalized_collections: dict[str, Any] = {}
        interfaces: list[dict[str, Any]] = []
        for raw_type, raw_payload in source.items():
            collection_type = _inventory_collection_type(raw_type)
            if collection_type == "network_interfaces":
                if not isinstance(raw_payload, list):
                    raise ValueError("Network interface inventory must be an array")
                if len(raw_payload) > INVENTORY_MAX_NETWORK_INTERFACES:
                    raise ValueError("Network interface inventory contains too many items")
                interfaces = [
                    _normalized_interface(item, ordinal) for ordinal, item in enumerate(raw_payload)
                ]
                normalized_collections[collection_type] = [
                    {key: value for key, value in item.items() if key != "fingerprint"}
                    for item in interfaces
                ]
            else:
                normalized_collections[collection_type] = raw_payload

        stored_snapshots = self.state.setdefault("ciInventorySnapshots", [])
        current_snapshots: dict[str, dict] = {}
        stale_types: list[str] = []
        for collection_type, raw_payload in normalized_collections.items():
            payload, item_count, fingerprint = _bounded_inventory_payload(raw_payload)
            current = next(
                (
                    item
                    for item in stored_snapshots
                    if item.get("mappingId") == mapping_id
                    and item.get("collectionType") == collection_type
                    and not item.get("supersededAt")
                ),
                None,
            )
            current_seen = parse_timestamp(current.get("lastObservedAt")) if current else None
            incoming_seen = parse_timestamp(observed)
            is_stale = bool(current_seen and incoming_seen and incoming_seen < current_seen)
            matching = next(
                (
                    item
                    for item in stored_snapshots
                    if item.get("mappingId") == mapping_id
                    and item.get("collectionType") == collection_type
                    and item.get("fingerprint") == fingerprint
                ),
                None,
            )
            if is_stale:
                stale_types.append(collection_type)
                if not matching:
                    matching = {
                        "id": canonical_uuid(
                            "ci_inventory_snapshot",
                            f"{mapping_id}:{collection_type}:{fingerprint}",
                        ),
                        "companyId": company_id,
                        "assetId": asset_id,
                        "mappingId": mapping_id,
                        "collectionType": collection_type,
                        "fingerprint": fingerprint,
                        "payload": payload,
                        "itemCount": item_count,
                        "completeness": "unknown",
                        "firstObservedAt": observed,
                        "lastObservedAt": observed,
                        "supersededAt": current.get("firstObservedAt") if current else observed,
                    }
                    stored_snapshots.append(matching)
                current_snapshots[collection_type] = current or matching
                continue
            if current and current.get("fingerprint") != fingerprint:
                current["supersededAt"] = observed
            if matching:
                matching.update(
                    companyId=company_id,
                    assetId=asset_id,
                    payload=payload,
                    itemCount=item_count,
                    lastObservedAt=observed,
                    supersededAt=None,
                )
            else:
                matching = {
                    "id": canonical_uuid(
                        "ci_inventory_snapshot",
                        f"{mapping_id}:{collection_type}:{fingerprint}",
                    ),
                    "companyId": company_id,
                    "assetId": asset_id,
                    "mappingId": mapping_id,
                    "collectionType": collection_type,
                    "fingerprint": fingerprint,
                    "payload": payload,
                    "itemCount": item_count,
                    "completeness": "unknown",
                    "firstObservedAt": observed,
                    "lastObservedAt": observed,
                    "supersededAt": None,
                }
                stored_snapshots.append(matching)
            current_snapshots[collection_type] = matching

            history = sorted(
                (
                    item
                    for item in stored_snapshots
                    if item.get("mappingId") == mapping_id
                    and item.get("collectionType") == collection_type
                ),
                key=lambda item: (
                    item.get("supersededAt") is None,
                    item.get("lastObservedAt") or "",
                    item.get("id") or "",
                ),
                reverse=True,
            )
            remove_ids = {item["id"] for item in history[keep:]}
            if remove_ids:
                stored_snapshots[:] = [
                    item for item in stored_snapshots if item.get("id") not in remove_ids
                ]

        if (
            "network_interfaces" in normalized_collections
            and "network_interfaces" not in stale_types
        ):
            stored_interfaces = self.state.setdefault("ciNetworkInterfaces", [])
            active_keys = {item["interfaceKey"] for item in interfaces}
            for stored in stored_interfaces:
                if (
                    stored.get("mappingId") == mapping_id
                    and not stored.get("retiredAt")
                    and stored.get("interfaceKey") not in active_keys
                ):
                    stored["retiredAt"] = observed
            for interface in interfaces:
                stored = next(
                    (
                        item
                        for item in stored_interfaces
                        if item.get("mappingId") == mapping_id
                        and item.get("interfaceKey") == interface["interfaceKey"]
                    ),
                    None,
                )
                if stored:
                    first_observed = stored.get("firstObservedAt") or observed
                    stored.update(
                        deepcopy(interface),
                        companyId=company_id,
                        assetId=asset_id,
                        lastObservedAt=observed,
                        retiredAt=None,
                    )
                    stored["firstObservedAt"] = first_observed
                else:
                    stored_interfaces.append(
                        {
                            "id": canonical_uuid(
                                "ci_network_interface",
                                f"{mapping_id}:{interface['interfaceKey']}",
                            ),
                            "companyId": company_id,
                            "assetId": asset_id,
                            "mappingId": mapping_id,
                            **deepcopy(interface),
                            "firstObservedAt": observed,
                            "lastObservedAt": observed,
                            "retiredAt": None,
                        }
                    )
        self.save_state(self.state)
        return {
            "companyId": company_id,
            "assetId": asset_id,
            "mappingId": mapping_id,
            "observedAt": observed,
            "collections": {
                key: {
                    "fingerprint": value.get("fingerprint", ""),
                    "itemCount": int(value.get("itemCount") or 0),
                }
                for key, value in current_snapshots.items()
            },
            "networkInterfaceCount": len(interfaces),
            "staleCollections": stale_types,
            "retention": keep,
        }

    def get_ci_inventory(
        self,
        company_id: str,
        asset_id: str,
        *,
        collection_types: Iterable[str] | None = None,
        include_history: bool = False,
    ) -> dict:
        """Return tenant-scoped technical inventory for one active asset."""

        asset = next(
            (
                item
                for item in self.state.get("assets", [])
                if item.get("id") == asset_id
                and item.get("companyId") == company_id
                and not item.get("retiredAt")
            ),
            None,
        )
        if not asset:
            raise ValueError("Configuration item is unavailable in this customer")
        selected = (
            {_inventory_collection_type(item) for item in collection_types}
            if collection_types is not None
            else None
        )
        snapshots = [
            deepcopy(item)
            for item in self.state.get("ciInventorySnapshots", [])
            if item.get("companyId") == company_id
            and item.get("assetId") == asset_id
            and (include_history or not item.get("supersededAt"))
            and (selected is None or item.get("collectionType") in selected)
        ]
        snapshots.sort(
            key=lambda item: (
                item.get("collectionType") or "",
                item.get("lastObservedAt") or "",
            ),
            reverse=True,
        )
        grouped: dict[str, list[dict]] = {}
        for snapshot in snapshots:
            grouped.setdefault(snapshot["collectionType"], []).append(snapshot)
        interfaces = [
            deepcopy(item)
            for item in self.state.get("ciNetworkInterfaces", [])
            if item.get("companyId") == company_id
            and item.get("assetId") == asset_id
            and (include_history or not item.get("retiredAt"))
        ]
        interfaces.sort(key=lambda item: (item.get("name") or "", item.get("interfaceKey") or ""))
        return {
            "companyId": company_id,
            "assetId": asset_id,
            "collections": grouped,
            "networkInterfaces": interfaces,
        }

    def _require_state_provider_relationship_context(
        self,
        kind: str,
        company_id: str,
        *,
        policy_id: str | None,
        expected_policy_revision: int | None,
        expected_connection_revision: int | None,
        provider_parent_id: str | None,
    ) -> dict[str, Any] | None:
        """Fail closed when a supplied provider topology generation is obsolete."""

        supplied = any(
            value is not None
            for value in (
                policy_id,
                expected_policy_revision,
                expected_connection_revision,
                provider_parent_id,
            )
        )
        if not supplied:
            return None
        if (
            not policy_id
            or expected_policy_revision is None
            or expected_connection_revision is None
            or not provider_parent_id
        ):
            raise ValueError(
                "Provider relationship context is stale: the expected provider generation "
                "is incomplete. Run a new preview."
            )
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
                and item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
            ),
            None,
        )
        integration = self.get_integration_connection(kind)
        company_mapping = next(
            (
                item
                for item in self.state.get("providerCompanyMappings", [])
                if item.get("provider") == kind
                and item.get("externalId") == provider_parent_id
                and item.get("companyId") == company_id
                and item.get("active", True)
            ),
            None,
        )
        company_observation = next(
            (
                item
                for item in self.state.get("providerCompanyObservations", [])
                if item.get("provider") == kind
                and item.get("externalId") == provider_parent_id
                and item.get("active", True)
                and not item.get("deleted", False)
            ),
            None,
        )
        reason = ""
        if not policy:
            reason = "the CI policy is missing"
        elif int(policy.get("revision") or 0) != int(expected_policy_revision):
            reason = "the CI policy changed after discovery"
        elif not integration or not integration.get("enabled", False):
            reason = "the integration is missing or disabled"
        elif integration.get("lifecycleStatus", "active") != "active":
            reason = "the integration is not active"
        elif int(integration.get("revision") or 0) != int(expected_connection_revision):
            reason = "the integration settings changed after discovery"
        elif not company_mapping or not company_observation:
            reason = "the provider customer mapping changed or is inactive"
        if reason:
            raise ValueError(
                f"Provider relationship context is stale: {reason}. Run a new preview."
            )
        return {
            "policyId": policy_id,
            "policyRevision": int(expected_policy_revision),
            "connectionRevision": int(expected_connection_revision),
            "providerParentId": provider_parent_id,
        }

    def upsert_relationship_candidates(
        self,
        kind: str,
        company_id: str,
        source_mapping_id: str,
        candidates: list[dict[str, Any]],
        *,
        observed_at: str | None = None,
        policy_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
        provider_parent_id: str | None = None,
    ) -> list[dict]:
        """Replace one mapping's relationship proposal set without touching relationships."""

        provider_context = self._require_state_provider_relationship_context(
            kind,
            company_id,
            policy_id=policy_id,
            expected_policy_revision=expected_policy_revision,
            expected_connection_revision=expected_connection_revision,
            provider_parent_id=provider_parent_id,
        )
        mapping = next(
            (
                item
                for item in self.state.get("providerCiMappings", [])
                if item.get("id") == source_mapping_id
                and item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("active", True)
            ),
            None,
        )
        if not mapping:
            raise ValueError("Provider mapping is unavailable for this customer")
        if provider_context is not None and mapping.get("providerParentId") != provider_parent_id:
            raise ValueError(
                "Provider relationship context is stale: the source CI mapping belongs to "
                "a different provider customer. Run a new preview."
            )
        observed = observed_at or utc_now()
        if not parse_timestamp(observed):
            raise ValueError("Relationship candidate timestamp is invalid")
        company_assets = {
            str(item.get("id"))
            for item in self.state.get("assets", [])
            if item.get("companyId") == company_id and not item.get("retiredAt")
        }
        stored_candidates = self.state.setdefault("ciRelationshipCandidates", [])
        active_keys: set[str] = set()
        for value in candidates:
            if not isinstance(value, dict):
                raise ValueError("Relationship candidates must be objects")
            from_ci_id = str(value.get("fromCiId") or "") or None
            to_ci_id = str(value.get("toCiId") or "") or None
            from_external = deepcopy(value.get("fromExternalIdentity") or {})
            to_external = deepcopy(value.get("toExternalIdentity") or {})
            if not isinstance(from_external, dict) or not isinstance(to_external, dict):
                raise ValueError("Relationship candidate identities must be objects")
            if (from_ci_id and from_ci_id not in company_assets) or (
                to_ci_id and to_ci_id not in company_assets
            ):
                raise ValueError("Relationship candidate crosses the customer boundary")
            if not from_ci_id and not from_external:
                raise ValueError("Relationship candidate source identity is required")
            if not to_ci_id and not to_external:
                raise ValueError("Relationship candidate target identity is required")
            if from_ci_id and from_ci_id == to_ci_id:
                raise ValueError("Relationship candidate endpoints must be different")
            relationship_type = str(
                value.get("relationshipType") or value.get("type") or ""
            ).strip()
            if not relationship_type or len(relationship_type) > 80:
                raise ValueError("Relationship candidate type is invalid")
            confidence = float(value.get("confidence", 0))
            if confidence < 0 or confidence > 1:
                raise ValueError("Relationship candidate confidence must be between 0 and 1")
            evidence = deepcopy(value.get("evidence") or {})
            if not isinstance(evidence, dict):
                raise ValueError("Relationship candidate evidence must be an object")
            if len(json.dumps(evidence, separators=(",", ":")).encode("utf-8")) > 262_144:
                raise ValueError("Relationship candidate evidence is too large")
            if provider_context is not None:
                evidence["providerContext"] = deepcopy(provider_context)
            candidate_key = _relationship_candidate_key(value)
            active_keys.add(candidate_key)
            stored = next(
                (
                    item
                    for item in stored_candidates
                    if item.get("sourceMappingId") == source_mapping_id
                    and item.get("candidateKey") == candidate_key
                ),
                None,
            )
            incoming_seen = parse_timestamp(observed)
            stored_seen = parse_timestamp(stored.get("lastSeenAt")) if stored else None
            if stored and stored_seen and incoming_seen and incoming_seen < stored_seen:
                continue
            if stored:
                evidence_changed = _relationship_candidate_evidence_changed(stored, evidence)
                stored["revision"] = int(stored.get("revision") or 1) + 1
                stored["observationCount"] = int(stored.get("observationCount") or 1) + 1
                stored.update(
                    fromCiId=from_ci_id,
                    toCiId=to_ci_id,
                    fromExternalIdentity=from_external,
                    toExternalIdentity=to_external,
                    relationshipType=relationship_type,
                    confidence=confidence,
                    evidence=evidence,
                    lastSeenAt=observed,
                    retiredAt=None,
                )
                if evidence_changed:
                    stored.update(
                        state="pending",
                        decidedBy=None,
                        decidedAt=None,
                        decisionNotes="",
                        approvedRelationshipId=None,
                    )
            else:
                stored_candidates.append(
                    {
                        "id": canonical_uuid(
                            "ci_relationship_candidate",
                            f"{source_mapping_id}:{candidate_key}",
                        ),
                        "companyId": company_id,
                        "sourceMappingId": source_mapping_id,
                        "provider": kind,
                        "candidateKey": candidate_key,
                        "fromCiId": from_ci_id,
                        "toCiId": to_ci_id,
                        "fromExternalIdentity": from_external,
                        "toExternalIdentity": to_external,
                        "relationshipType": relationship_type,
                        "confidence": confidence,
                        "evidence": evidence,
                        "state": "pending",
                        "firstObservedAt": observed,
                        "lastSeenAt": observed,
                        "retiredAt": None,
                        "decidedBy": None,
                        "decidedAt": None,
                        "decisionNotes": "",
                        "approvedRelationshipId": None,
                        "revision": 1,
                        "observationCount": 1,
                    }
                )
        incoming_seen = parse_timestamp(observed)
        for stored in stored_candidates:
            stored_seen = parse_timestamp(stored.get("lastSeenAt"))
            if (
                stored.get("sourceMappingId") == source_mapping_id
                and not stored.get("retiredAt")
                and stored.get("candidateKey") not in active_keys
                and (not stored_seen or not incoming_seen or stored_seen <= incoming_seen)
            ):
                stored["retiredAt"] = observed
                stored["revision"] = int(stored.get("revision") or 1) + 1
        self.save_state(self.state)
        return self.list_relationship_candidates(
            company_id,
            provider=kind,
            include_retired=False,
        )

    def list_relationship_candidates(
        self,
        company_id: str,
        *,
        state: str | None = None,
        asset_id: str | None = None,
        provider: str | None = None,
        include_retired: bool = False,
        limit: int = 250,
    ) -> list[dict]:
        """Return a bounded tenant-scoped relationship proposal queue."""

        if state is not None and state not in RELATIONSHIP_CANDIDATE_STATES:
            raise ValueError("Relationship candidate state is invalid")
        asset_names = {
            str(item.get("id")): str(item.get("name") or "")
            for item in self.state.get("assets", [])
            if item.get("companyId") == company_id
        }
        active_mapping_ids = {
            str(item.get("id") or "")
            for item in self.state.get("providerCiMappings", [])
            if item.get("companyId") == company_id and item.get("active", True)
        }
        records = [
            {
                **deepcopy(item),
                "fromName": asset_names.get(str(item.get("fromCiId") or ""), ""),
                "toName": asset_names.get(str(item.get("toCiId") or ""), ""),
            }
            for item in self.state.get("ciRelationshipCandidates", [])
            if item.get("companyId") == company_id
            and str(item.get("sourceMappingId") or "") in active_mapping_ids
            and (state is None or item.get("state") == state)
            and (
                asset_id is None
                or item.get("fromCiId") == asset_id
                or item.get("toCiId") == asset_id
            )
            and (provider is None or item.get("provider") == provider)
            and (include_retired or not item.get("retiredAt"))
        ]
        records.sort(
            key=lambda item: (
                item.get("lastSeenAt") or "",
                item.get("id") or "",
            ),
            reverse=True,
        )
        return records[: max(1, min(int(limit), 1_000))]

    def decide_relationship_candidate(
        self,
        company_id: str,
        candidate_id: str,
        state: str,
        actor_id: str | None = None,
        notes: str = "",
        approved_relationship_id: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict:
        """Record a candidate decision without materializing a CI relationship."""

        if state not in RELATIONSHIP_CANDIDATE_STATES:
            raise ValueError("Relationship candidate state is invalid")
        candidate = next(
            (
                item
                for item in self.state.get("ciRelationshipCandidates", [])
                if item.get("id") == candidate_id and item.get("companyId") == company_id
            ),
            None,
        )
        if not candidate:
            raise ValueError("Relationship candidate not found")
        current_revision = int(candidate.get("revision") or 1)
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise ValueError("Relationship candidate changed; refresh it before deciding")
        mapping_active = any(
            item.get("id") == candidate.get("sourceMappingId")
            and item.get("companyId") == company_id
            and item.get("active", True)
            for item in self.state.get("providerCiMappings", [])
        )
        if state == "approved" and not mapping_active:
            raise ValueError("Restore the provider mapping before approving this suggestion")
        if state == "approved" and (not candidate.get("fromCiId") or not candidate.get("toCiId")):
            raise ValueError("Resolve both candidate endpoints before approval")
        if approved_relationship_id:
            relationship = next(
                (
                    item
                    for item in self.state.get("relationships", [])
                    if item.get("id") == approved_relationship_id
                    and item.get("companyId", company_id) == company_id
                    and item.get("fromId") == candidate.get("fromCiId")
                    and item.get("toId") == candidate.get("toCiId")
                    and item.get("type") == candidate.get("relationshipType")
                ),
                None,
            )
            if state != "approved" or not relationship:
                raise ValueError("Approved relationship does not match this candidate")
        before = deepcopy(candidate)
        linked_relationship_id = (
            approved_relationship_id or candidate.get("approvedRelationshipId")
            if state == "approved"
            else None
        )
        candidate.update(
            state=state,
            decidedBy=actor_id if state != "pending" else None,
            decidedAt=utc_now() if state != "pending" else None,
            decisionNotes=str(notes or "")[:2000] if state != "pending" else "",
            approvedRelationshipId=linked_relationship_id,
            revision=current_revision + 1,
        )
        self._audit(
            company_id,
            actor_id,
            "relationship_candidate",
            candidate_id,
            "reopened" if state == "pending" else state,
            before,
            candidate,
            metadata={
                "sourceMappingId": candidate.get("sourceMappingId"),
                "relationshipType": candidate.get("relationshipType"),
            },
        )
        self.save_state(self.state)
        return deepcopy(candidate)

    def approve_relationship_candidate(
        self,
        company_id: str,
        candidate_id: str,
        actor_id: str | None = None,
        notes: str = "",
        *,
        expected_revision: int | None = None,
        policy_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
        provider_parent_id: str | None = None,
    ) -> tuple[dict, dict]:
        """Atomically materialize or reuse an edge and approve its candidate."""

        candidate = next(
            (
                item
                for item in self.state.get("ciRelationshipCandidates", [])
                if item.get("id") == candidate_id and item.get("companyId") == company_id
            ),
            None,
        )
        if not candidate:
            raise ValueError("Relationship candidate not found")
        current_revision = int(candidate.get("revision") or 1)
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise ValueError("Relationship candidate changed; refresh it before deciding")
        stored_context = _required_relationship_provider_context(candidate.get("evidence"))
        supplied_context = _supplied_relationship_provider_context(
            policy_id=policy_id,
            expected_policy_revision=expected_policy_revision,
            expected_connection_revision=expected_connection_revision,
            provider_parent_id=provider_parent_id,
        )
        if supplied_context is not None and stored_context != supplied_context:
            raise ValueError(
                "Provider relationship context is stale: the candidate belongs to a "
                "different provider generation. Run a new preview."
            )
        validated_context = self._require_state_provider_relationship_context(
            str(candidate.get("provider") or ""),
            company_id,
            policy_id=str(stored_context["policyId"]),
            expected_policy_revision=int(stored_context["policyRevision"]),
            expected_connection_revision=int(stored_context["connectionRevision"]),
            provider_parent_id=str(stored_context["providerParentId"]),
        )
        if validated_context != stored_context:
            raise ValueError(
                "Provider relationship context is stale: the candidate belongs to a "
                "different provider generation. Run a new preview."
            )
        if not any(
            item.get("id") == candidate.get("sourceMappingId")
            and item.get("provider") == candidate.get("provider")
            and item.get("companyId") == company_id
            and item.get("providerParentId") == stored_context["providerParentId"]
            and item.get("active", True)
            for item in self.state.get("providerCiMappings", [])
        ):
            raise ValueError(
                "Provider relationship context is stale: restore or refresh the source "
                "provider mapping before approving this suggestion."
            )
        if candidate.get("retiredAt"):
            raise ValueError("Refresh provider evidence before approving this suggestion")
        from_ci_id = str(candidate.get("fromCiId") or "")
        to_ci_id = str(candidate.get("toCiId") or "")
        if not from_ci_id or not to_ci_id:
            raise ValueError("Resolve both candidate endpoints before approval")
        company_assets = {
            str(item.get("id")): item
            for item in self.state.get("assets", [])
            if item.get("companyId") == company_id and not item.get("retiredAt")
        }
        if from_ci_id not in company_assets or to_ci_id not in company_assets:
            raise ValueError("Both candidate endpoints must remain in this customer")
        relationship_type = str(candidate.get("relationshipType") or "")
        if relationship_type not in CANONICAL_RELATIONSHIP_TYPES:
            raise ValueError("The provider proposed an unsupported relationship type")
        raw_evidence = candidate.get("evidence")
        evidence = deepcopy(raw_evidence) if isinstance(raw_evidence, dict) else {}
        impact_policy = str(evidence.get("impactPolicy") or "required")
        if impact_policy not in RELATIONSHIP_IMPACT_POLICIES:
            impact_policy = "required"
        all_relationships = self.state.setdefault("relationships", [])
        relationships = all_relationships
        relationship = _matching_relationship(
            relationships,
            from_ci_id,
            to_ci_id,
            relationship_type,
        )
        if (
            not relationship
            and relationship_type == "depends_on"
            and _dependency_cycle(relationships, from_ci_id, to_ci_id)
        ):
            raise ValueError("That provider suggestion would create a dependency cycle")
        state_before = deepcopy(self.state)
        if not relationship:
            relationship = {
                "id": str(uuid.uuid4()),
                "fromId": from_ci_id,
                "toId": to_ci_id,
                "type": relationship_type,
                "impactPolicy": impact_policy,
                "sourceMappingId": candidate.get("sourceMappingId") or None,
                "confidence": float(candidate.get("confidence") or 0),
                "evidence": {
                    **evidence,
                    "candidateId": candidate_id,
                    "provider": candidate.get("provider"),
                },
                "provenance": "provider",
            }
            all_relationships.append(relationship)
            self._audit(
                company_id,
                actor_id,
                "relationship",
                relationship["id"],
                "created",
                None,
                relationship,
            )
        else:
            relationship.setdefault("impactPolicy", "required")
            relationship.setdefault("sourceMappingId", None)
            relationship.setdefault("confidence", 1.0)
            relationship.setdefault("evidence", {})
            relationship.setdefault("provenance", "manual")
        before = deepcopy(candidate)
        candidate.update(
            state="approved",
            decidedBy=actor_id,
            decidedAt=utc_now(),
            decisionNotes=str(notes or "")[:2000],
            approvedRelationshipId=relationship["id"],
            revision=current_revision + 1,
        )
        self._audit(
            company_id,
            actor_id,
            "relationship_candidate",
            candidate_id,
            "approved",
            before,
            candidate,
            metadata={
                "sourceMappingId": candidate.get("sourceMappingId"),
                "relationshipType": relationship_type,
            },
        )
        try:
            self.save_state(self.state)
        except Exception:
            self.state.clear()
            self.state.update(state_before)
            raise
        return deepcopy(candidate), deepcopy(relationship)

    def _retain_sync_runs(self) -> None:
        """Bound local history without ever discarding active preview work."""

        retained: list[dict] = []
        terminal_count = 0
        ordered = sorted(
            self.state.get("syncRuns", []),
            key=lambda item: (
                item.get("requestedAt") or item.get("startedAt") or "",
                item.get("id") or "",
            ),
            reverse=True,
        )
        for run in ordered:
            if run.get("status") in ACTIVE_CI_PREVIEW_RUN_STATUSES:
                retained.append(run)
            elif terminal_count < 250:
                retained.append(run)
                terminal_count += 1
        self.state["syncRuns"] = retained

    def _clear_state_policy_lease(
        self,
        policy_id: str | None,
        expected_owner: str,
    ) -> None:
        """Clear one local policy lease only when its expected owner still holds it."""

        bounded_owner = str(expected_owner)[:120]
        if not policy_id or not bounded_owner:
            return
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
            ),
            None,
        )
        if policy and policy.get("leaseOwner") == bounded_owner:
            policy["leaseOwner"] = None
            policy["leaseUntil"] = None

    def create_ci_preview_run(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        policy_id: str,
        trigger: str,
        actor_id: str | None,
        policy_snapshot: dict,
        retry_of_id: str | None = None,
        schedule_trigger: str | None = None,
    ) -> dict:
        """Queue one immutable preview request and deduplicate its active scope."""

        dedupe_key = _ci_preview_dedupe_key(
            kind,
            company_id,
            provider_parent_id,
            policy_id,
        )
        active = next(
            (
                item
                for item in self.state.get("syncRuns", [])
                if item.get("dedupeKey") == dedupe_key
                and item.get("status") in ACTIVE_CI_PREVIEW_RUN_STATUSES
            ),
            None,
        )
        if active:
            return _public_sync_run(active)
        timestamp = utc_now()
        snapshot = deepcopy(policy_snapshot or {})
        selected_trigger = str(trigger or "manual_preview")[:80]
        selected_schedule_trigger = str(schedule_trigger or selected_trigger)[:80]
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
                and item.get("provider") == kind
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
            ),
            None,
        )
        if not policy:
            raise ValueError("CI preview policy not found")
        lease_until = parse_timestamp(policy.get("leaseUntil"))
        if lease_until and lease_until > datetime.now(UTC):
            raise ValueError("This policy is already running; refresh and retry")
        run_id = str(uuid.uuid4())
        run: dict[str, Any] = {
            "id": run_id,
            "type": kind,
            "status": "queued",
            "message": "Preview queued for background processing.",
            "startedAt": None,
            "finishedAt": None,
            "discovered": 0,
            "imported": 0,
            "updated": 0,
            "review": 0,
            "companyId": company_id,
            "providerCompanyId": provider_parent_id,
            "policyId": policy_id or None,
            "trigger": selected_trigger,
            "requestedBy": actor_id,
            "requestedAt": timestamp,
            "availableAt": timestamp,
            "attemptCount": 0,
            "maxAttempts": 3,
            "leaseOwner": None,
            "leaseUntil": None,
            "heartbeatAt": None,
            "cancelRequestedAt": None,
            "cancelledAt": None,
            "retryOfId": retry_of_id,
            "dedupeKey": dedupe_key,
            "progress": {"phase": "queued"},
            "updatedAt": timestamp,
            "attributes": {
                "operation": _ci_preview_operation(kind),
                "trigger": selected_trigger,
                "scheduleTrigger": selected_schedule_trigger,
                "companyId": company_id,
                "providerCompanyId": provider_parent_id,
                "policyId": policy_id or None,
                "policyRevision": int(snapshot.get("revision") or 0),
                "policySnapshot": snapshot,
                "readOnly": True,
            },
        }
        policy["leaseOwner"] = f"queued:{run_id}"[:120]
        policy["leaseUntil"] = (
            (datetime.now(UTC) + timedelta(seconds=3600)).isoformat().replace("+00:00", "Z")
        )
        self.state.setdefault("syncRuns", []).insert(0, run)
        self._retain_sync_runs()
        self._audit(
            company_id,
            actor_id,
            "sync_run",
            run["id"],
            "queued",
            None,
            _public_sync_run(run),
            metadata={
                "provider": kind,
                "policyId": policy_id or None,
                "retryOfId": retry_of_id,
            },
        )
        self.save_state(self.state)
        return _public_sync_run(run)

    def claim_ci_preview_run(
        self,
        worker_id: str,
        provider: str | None = None,
        lease_seconds: int = 180,
    ) -> dict | None:
        """Claim queued or stale preview work in the local development repository."""

        now = datetime.now(UTC)
        lease_duration = max(30, min(int(lease_seconds), 3600))
        eligible: list[dict] = []
        changed = False
        for run in self.state.get("syncRuns", []):
            if provider and run.get("type") != provider:
                continue
            status = run.get("status")
            available_at = parse_timestamp(run.get("availableAt"))
            lease_until = parse_timestamp(run.get("leaseUntil"))
            due = status == "queued" and (available_at is None or available_at <= now)
            stale = status == "running" and (lease_until is None or lease_until <= now)
            if not (due or stale):
                continue
            if int(run.get("attemptCount") or 0) >= int(run.get("maxAttempts") or 3):
                cancelled = bool(run.get("cancelRequestedAt"))
                timestamp = utc_now()
                run.update(
                    status="cancelled" if cancelled else "failed",
                    message=(
                        "Preview cancelled."
                        if cancelled
                        else "Preview failed after the maximum number of attempts."
                    ),
                    finishedAt=timestamp,
                    cancelledAt=timestamp if cancelled else None,
                    leaseOwner=None,
                    leaseUntil=None,
                    heartbeatAt=timestamp,
                    progress={"phase": "cancelled" if cancelled else "failed"},
                    updatedAt=timestamp,
                )
                expected_owner = (
                    f"queued:{run.get('id')}"[:120]
                    if status == "queued"
                    else str(run.get("leaseOwner") or "")[:120]
                )
                self._clear_state_policy_lease(run.get("policyId"), expected_owner)
                changed = True
                continue
            eligible.append(run)
        if not eligible:
            if changed:
                self._retain_sync_runs()
                self.save_state(self.state)
            return None
        run = min(
            eligible,
            key=lambda item: (
                parse_timestamp(item.get("availableAt"))
                or parse_timestamp(item.get("requestedAt"))
                or now,
                str(item.get("id") or ""),
            ),
        )
        timestamp = utc_now()
        claimed_lease_until = (
            (now + timedelta(seconds=lease_duration)).isoformat().replace("+00:00", "Z")
        )
        run.update(
            status="running",
            startedAt=run.get("startedAt") or timestamp,
            attemptCount=int(run.get("attemptCount") or 0) + 1,
            leaseOwner=str(worker_id)[:160],
            leaseUntil=claimed_lease_until,
            heartbeatAt=timestamp,
            progress={
                **_bounded_preview_progress(run.get("progress")),
                "phase": "cancelling" if run.get("cancelRequestedAt") else "starting",
            },
            updatedAt=timestamp,
        )
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == run.get("policyId")
            ),
            None,
        )
        if policy:
            policy["leaseOwner"] = str(worker_id)[:120]
            policy["leaseUntil"] = claimed_lease_until
        self._audit(
            run.get("companyId"),
            None,
            "sync_run",
            run["id"],
            "claimed",
            None,
            {"status": "running", "attemptCount": run["attemptCount"]},
            metadata={"provider": run.get("type"), "worker": True},
            actor_type="system",
            source_system="integration_worker",
        )
        self.save_state(self.state)
        return _public_sync_run(run, include_internal=True)

    def renew_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        progress: dict,
        message: str = "",
        lease_seconds: int = 180,
    ) -> dict | None:
        """Renew an owned local job lease and publish bounded progress."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if (
            not run
            or run.get("status") != "running"
            or run.get("leaseOwner") != str(worker_id)[:160]
        ):
            return None
        now = datetime.now(UTC)
        timestamp = now.isoformat().replace("+00:00", "Z")
        run.update(
            heartbeatAt=timestamp,
            leaseUntil=(now + timedelta(seconds=max(30, min(int(lease_seconds), 3600))))
            .isoformat()
            .replace("+00:00", "Z"),
            progress={
                **_bounded_preview_progress(run.get("progress")),
                **_bounded_preview_progress(progress),
            },
            updatedAt=timestamp,
        )
        if message:
            run["message"] = str(message)[:1000]
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == run.get("policyId")
            ),
            None,
        )
        if policy:
            policy["leaseOwner"] = str(worker_id)[:120]
            policy["leaseUntil"] = run["leaseUntil"]
        self.save_state(self.state)
        return _public_sync_run(run)

    def get_sync_run(
        self,
        run_id: str,
        company_ids: Iterable[str] | None = None,
    ) -> dict | None:
        """Return one scoped sync run."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if not run:
            return None
        permitted = set(company_ids) if company_ids is not None else None
        company_id = run.get("companyId") or (run.get("attributes") or {}).get("companyId")
        if permitted is not None and company_id is not None and company_id not in permitted:
            return None
        return _public_sync_run(run)

    def get_latest_ci_preview_run(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
    ) -> dict | None:
        """Return the newest preview job for one provider/customer scope."""

        matches = [
            item
            for item in self.state.get("syncRuns", [])
            if item.get("type") == kind
            and (item.get("companyId") or (item.get("attributes") or {}).get("companyId"))
            == company_id
            and (
                item.get("providerCompanyId")
                or (item.get("attributes") or {}).get("providerCompanyId")
            )
            == provider_parent_id
            and (item.get("attributes") or {}).get("operation") == _ci_preview_operation(kind)
        ]
        if not matches:
            return None
        return _public_sync_run(
            max(
                matches,
                key=lambda item: (
                    item.get("requestedAt") or item.get("startedAt") or "",
                    item.get("id") or "",
                ),
            )
        )

    def is_sync_run_cancel_requested(
        self,
        run_id: str,
        worker_id: str | None = None,
    ) -> bool:
        """Return whether the current owner should cooperatively stop a run."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if not run:
            return False
        if worker_id is not None and run.get("leaseOwner") != str(worker_id)[:160]:
            return False
        return bool(run.get("cancelRequestedAt") or run.get("status") == "cancelled")

    def request_sync_run_cancel(
        self,
        run_id: str,
        actor_id: str | None,
    ) -> dict | None:
        """Cancel queued work immediately or flag owned work for cooperative stop."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if not run:
            return None
        before = _public_sync_run(run)
        timestamp = utc_now()
        if run.get("status") == "queued":
            run.update(
                status="cancelled",
                message="Preview cancelled before execution.",
                finishedAt=timestamp,
                cancelRequestedAt=timestamp,
                cancelledAt=timestamp,
                progress={"phase": "cancelled"},
                leaseOwner=None,
                leaseUntil=None,
                updatedAt=timestamp,
            )
            self._clear_state_policy_lease(
                run.get("policyId"),
                f"queued:{run.get('id')}"[:120],
            )
        elif run.get("status") == "running" and not run.get("cancelRequestedAt"):
            run.update(
                cancelRequestedAt=timestamp,
                progress={
                    **_bounded_preview_progress(run.get("progress")),
                    "phase": "cancelling",
                },
                message="Cancellation requested; waiting for the current provider request.",
                updatedAt=timestamp,
            )
        self._audit(
            run.get("companyId"),
            actor_id,
            "sync_run",
            run["id"],
            "cancel_requested",
            before,
            _public_sync_run(run),
        )
        self._retain_sync_runs()
        self.save_state(self.state)
        return _public_sync_run(run)

    def complete_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        preview_summary: dict,
        actor_id: str | None = None,
    ) -> dict | None:
        """Complete an owned preview while retaining only aggregate result evidence."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if (
            not run
            or run.get("status") != "running"
            or run.get("leaseOwner") != str(worker_id)[:160]
            or (parse_timestamp(run.get("leaseUntil")) or datetime.min.replace(tzinfo=UTC))
            <= datetime.now(UTC)
        ):
            return None
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == run.get("policyId")
            ),
            None,
        )
        if not policy or policy.get("leaseOwner") != str(worker_id)[:120]:
            return None
        if run.get("cancelRequestedAt"):
            return self.fail_ci_preview_run(
                run_id,
                worker_id,
                "Preview cancelled.",
                actor_id,
                cancelled=True,
            )
        summary = _sanitized_preview_summary(preview_summary)
        timestamp = utc_now()
        message = str(preview_summary.get("message") or "Preview completed.")[:1000]
        review_count = sum(
            summary["counts"].get(key, 0) for key in ("create", "update", "link", "conflict")
        )
        previous_progress = _bounded_preview_progress(run.get("progress"))
        run.update(
            status="success",
            message=message,
            finishedAt=timestamp,
            discovered=summary["discovered"],
            review=review_count,
            leaseOwner=None,
            leaseUntil=None,
            heartbeatAt=timestamp,
            progress={
                "phase": "completed",
                "discovered": summary["discovered"],
                "enriched": int(previous_progress.get("enriched") or 0),
                "included": summary["included"],
                "excluded": summary["excluded"],
                "reviewed": review_count,
                "current": summary["discovered"],
                "total": summary["discovered"],
                "percent": 100,
                "counts": summary["counts"],
            },
            updatedAt=timestamp,
        )
        run.setdefault("attributes", {})["resultSummary"] = summary
        trigger = _ci_preview_schedule_trigger(run)
        self._finish_state_ci_sync_policy(
            str(run.get("policyId") or ""),
            worker_id,
            success=True if trigger in {"continuous_preview", "manual_sync"} else None,
        )
        self._audit(
            run.get("companyId"),
            actor_id,
            "sync_run",
            run["id"],
            "completed",
            None,
            _public_sync_run(run),
            metadata={"provider": run.get("type")},
            actor_type="system" if actor_id is None else "user",
            source_system="integration_worker" if actor_id is None else "web",
        )
        self._retain_sync_runs()
        self.save_state(self.state)
        return _public_sync_run(run)

    def publish_and_complete_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        review_items: list[dict],
        preview_summary: dict,
        actor_id: str | None = None,
        *,
        presence_snapshot: dict[str, Any] | None = None,
    ) -> dict | None:
        """Publish review observations and complete their owned run as one state change."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        bounded_owner = str(worker_id)[:160]
        now = datetime.now(UTC)
        lease_until = parse_timestamp(run.get("leaseUntil")) if run else None
        if (
            not run
            or run.get("status") != "running"
            or run.get("leaseOwner") != bounded_owner
            or lease_until is None
            or lease_until <= now
        ):
            return None
        policy_id = str(run.get("policyId") or "")
        company_id = str(run.get("companyId") or "")
        provider_parent_id = str(run.get("providerCompanyId") or "")
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == policy_id
                and item.get("companyId") == company_id
                and item.get("providerParentId") == provider_parent_id
                and item.get("provider") == run.get("type")
            ),
            None,
        )
        policy_lease_until = parse_timestamp(policy.get("leaseUntil")) if policy else None
        if (
            not policy
            or policy.get("leaseOwner") != bounded_owner[:120]
            or policy_lease_until is None
            or policy_lease_until <= now
        ):
            return None

        snapshot = deepcopy(self.state)
        try:
            timestamp = utc_now()
            if run.get("cancelRequestedAt"):
                run.update(
                    status="cancelled",
                    message="Preview cancelled.",
                    finishedAt=timestamp,
                    cancelledAt=timestamp,
                    leaseOwner=None,
                    leaseUntil=None,
                    heartbeatAt=timestamp,
                    progress={"phase": "cancelled"},
                    updatedAt=timestamp,
                )
                finished_policy = self._finish_state_ci_sync_policy(
                    policy_id,
                    bounded_owner,
                    success=None,
                )
                if not finished_policy:
                    raise RuntimeError("Preview policy lease is no longer owned")
                self._audit(
                    company_id,
                    actor_id,
                    "sync_run",
                    run["id"],
                    "cancelled",
                    None,
                    _public_sync_run(run),
                    outcome="success",
                    severity="informational",
                    metadata={"provider": run.get("type")},
                    actor_type="system" if actor_id is None else "user",
                    source_system="integration_worker" if actor_id is None else "web",
                )
                self._retain_sync_runs()
                self.save_state(self.state)
                return _public_sync_run(run)

            if presence_snapshot is not None:
                attributes = run.get("attributes") or {}
                attributes["connectionRevision"] = int(
                    _normalized_ci_presence_snapshot(presence_snapshot)["connectionRevision"]
                )
                run["attributes"] = attributes
            queue_summary = self._replace_ci_review_items_in_state(
                policy_id,
                company_id,
                run_id,
                review_items,
                actor_id,
            )
            if presence_snapshot is not None:
                attributes = run.get("attributes") or {}
                self._apply_state_ci_presence_snapshot(
                    str(run.get("type") or ""),
                    policy,
                    run_id,
                    presence_snapshot,
                    int(attributes.get("policyRevision") or 0),
                )
            aggregate = deepcopy(preview_summary)
            aggregate["queueSummary"] = queue_summary
            summary = _sanitized_preview_summary(aggregate)
            counts = summary["counts"]
            review_count = sum(
                counts.get(key, 0) for key in ("create", "update", "link", "conflict")
            )
            message = str(preview_summary.get("message") or "Preview completed.")[:1000]
            previous_progress = _bounded_preview_progress(run.get("progress"))
            run.update(
                status="success",
                message=message,
                finishedAt=timestamp,
                discovered=summary["discovered"],
                review=review_count,
                leaseOwner=None,
                leaseUntil=None,
                heartbeatAt=timestamp,
                progress={
                    "phase": "completed",
                    "discovered": summary["discovered"],
                    "enriched": int(previous_progress.get("enriched") or 0),
                    "included": summary["included"],
                    "excluded": summary["excluded"],
                    "reviewed": review_count,
                    "current": summary["discovered"],
                    "total": summary["discovered"],
                    "percent": 100,
                    "counts": counts,
                },
                updatedAt=timestamp,
            )
            run.setdefault("attributes", {})["resultSummary"] = summary
            trigger = _ci_preview_schedule_trigger(run)
            policy_success = trigger in {"continuous_preview", "manual_sync"}
            finished_policy = self._finish_state_ci_sync_policy(
                policy_id,
                bounded_owner,
                success=True if policy_success else None,
            )
            if not finished_policy:
                raise RuntimeError("Preview policy lease is no longer owned")
            self._audit(
                company_id,
                actor_id,
                "sync_run",
                run["id"],
                "completed",
                None,
                _public_sync_run(run),
                metadata={"provider": run.get("type")},
                actor_type="system" if actor_id is None else "user",
                source_system="integration_worker" if actor_id is None else "web",
            )
            self._retain_sync_runs()
            self.save_state(self.state)
        except Exception:
            self.state.clear()
            self.state.update(snapshot)
            raise
        return _public_sync_run(run)

    def fail_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        error: Any,
        actor_id: str | None = None,
        cancelled: bool = False,
    ) -> dict | None:
        """Fail or cooperatively cancel an owned preview run."""

        run = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if (
            not run
            or run.get("status") != "running"
            or run.get("leaseOwner") != str(worker_id)[:160]
            or (parse_timestamp(run.get("leaseUntil")) or datetime.min.replace(tzinfo=UTC))
            <= datetime.now(UTC)
        ):
            return None
        policy = next(
            (
                item
                for item in self.state.get("integrationCiPolicies", [])
                if item.get("id") == run.get("policyId")
            ),
            None,
        )
        if not policy or policy.get("leaseOwner") != str(worker_id)[:120]:
            return None
        was_cancelled = cancelled or bool(run.get("cancelRequestedAt"))
        timestamp = utc_now()
        detail = "Preview cancelled." if was_cancelled else str(error)[:1000]
        run.update(
            status="cancelled" if was_cancelled else "failed",
            message=detail,
            finishedAt=timestamp,
            cancelledAt=timestamp if was_cancelled else None,
            leaseOwner=None,
            leaseUntil=None,
            heartbeatAt=timestamp,
            progress={"phase": "cancelled" if was_cancelled else "failed"},
            updatedAt=timestamp,
        )
        trigger = _ci_preview_schedule_trigger(run)
        tracks_schedule = trigger in {"continuous_preview", "manual_sync"} and not was_cancelled
        self._finish_state_ci_sync_policy(
            str(run.get("policyId") or ""),
            worker_id,
            success=False if tracks_schedule else None,
            error=detail,
        )
        self._audit(
            run.get("companyId"),
            actor_id,
            "sync_run",
            run["id"],
            "cancelled" if was_cancelled else "failed",
            None,
            _public_sync_run(run),
            outcome="success" if was_cancelled else "failed",
            severity="informational" if was_cancelled else "warning",
            reason="" if was_cancelled else detail,
            metadata={"provider": run.get("type")},
            actor_type="system" if actor_id is None else "user",
            source_system="integration_worker" if actor_id is None else "web",
        )
        self._retain_sync_runs()
        self.save_state(self.state)
        return _public_sync_run(run)

    def retry_ci_preview_run(self, run_id: str, actor_id: str | None) -> dict:
        """Queue a new immutable attempt for a retryable terminal preview."""

        source = next(
            (item for item in self.state.get("syncRuns", []) if item.get("id") == run_id),
            None,
        )
        if not source:
            raise ValueError("Sync run not found")
        if source.get("status") not in RETRYABLE_CI_PREVIEW_RUN_STATUSES:
            raise ValueError("Only failed or cancelled preview runs can be retried")
        attributes = source.get("attributes") or {}
        schedule_trigger = _ci_preview_schedule_trigger(source)
        return self.create_ci_preview_run(
            str(source.get("type") or ""),
            str(source.get("companyId") or attributes.get("companyId") or ""),
            str(source.get("providerCompanyId") or attributes.get("providerCompanyId") or ""),
            str(source.get("policyId") or attributes.get("policyId") or ""),
            "retry",
            actor_id,
            deepcopy(attributes.get("policySnapshot") or {}),
            retry_of_id=source["id"],
            schedule_trigger=schedule_trigger,
        )

    def release_ci_sync_policy_lease(
        self,
        policy_id: str,
        lease_owner: str,
    ) -> dict | None:
        """Release one owned local policy lease without changing its schedule."""

        policy = self._finish_state_ci_sync_policy(
            policy_id,
            lease_owner,
            success=None,
        )
        if not policy:
            return None
        self.save_state(self.state)
        return self.get_ci_sync_policy(
            str(policy.get("provider") or ""),
            str(policy.get("companyId") or ""),
            str(policy.get("providerParentId") or ""),
        )

    def list_ci_review_items_for_run(
        self,
        kind: str,
        run_id: str,
        company_id: str,
    ) -> list[dict]:
        """Return reviewable observations produced by one completed local run."""

        return [
            item
            for item in self.list_ci_review_items(kind, company_id, None, 1000)
            if item.get("lastRunId") == run_id
        ]

    def list_sync_runs(
        self,
        kind: str | None = None,
        status: str | None = None,
        operation: str | None = None,
        company_id: str | None = None,
        limit: int = 50,
        *,
        company_ids: Iterable[str] | None = None,
    ) -> list[dict]:
        """Return recent sync evidence using provider-neutral filters."""

        permitted_companies = set(company_ids) if company_ids is not None else None
        records = []
        for raw in self.state.get("syncRuns", []):
            item = deepcopy(raw)
            attributes = item.get("attributes") or {}
            if kind and item.get("type") != kind:
                continue
            if status and item.get("status") != status:
                continue
            if operation and attributes.get("operation") != operation:
                continue
            if company_id and attributes.get("companyId") != company_id:
                continue
            if (
                permitted_companies is not None
                and attributes.get("companyId") is not None
                and attributes.get("companyId") not in permitted_companies
            ):
                continue
            item["attributes"] = deepcopy(attributes)
            records.append(_public_sync_run(item))
        return records[: max(1, min(limit, 250))]

    def list_audit_events(
        self,
        company_id: str | None = None,
        *,
        actor_id: str | None = None,
        category: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
        limit: int = 250,
    ) -> list[dict]:
        users = {item["id"]: item for item in self.state.get("users", [])}
        records = []
        for raw in self.state.get("auditEvents", []):
            event = deepcopy(raw)
            event.setdefault("actorUserId", event.get("actorId"))
            event.setdefault(
                "actorLabel",
                (users.get(event.get("actorUserId")) or {}).get("email", "System"),
            )
            event.setdefault("actorType", "user" if event.get("actorUserId") else "system")
            event.setdefault("sourceSystem", "web")
            event.setdefault(
                "category",
                event_category(event.get("entityType", "data"), event.get("action", "viewed")),
            )
            event.setdefault("outcome", "success")
            event.setdefault("severity", "informational")
            event.setdefault("requestId", "")
            event.setdefault("correlationId", event.get("requestId", ""))
            event.setdefault(
                "entityName",
                entity_name(event.get("before"), event.get("after"), event.get("entityId", "")),
            )
            event.setdefault("changes", field_changes(event.get("before"), event.get("after")))
            event.setdefault("metadata", {})
            haystack = " ".join(
                str(event.get(key, ""))
                for key in (
                    "actorLabel",
                    "entityName",
                    "entityType",
                    "action",
                    "correlationId",
                )
            ).casefold()
            if company_id is not None and event.get("companyId") != company_id:
                continue
            if actor_id and event.get("actorUserId") != actor_id:
                continue
            if category and event.get("category") != category:
                continue
            if action and event.get("action") != action:
                continue
            if entity_type and event.get("entityType") != entity_type:
                continue
            if entity_id and event.get("entityId") != entity_id:
                continue
            if outcome and event.get("outcome") != outcome:
                continue
            if search and search.casefold() not in haystack:
                continue
            records.append(event)
        return sorted(records, key=lambda item: item.get("createdAt", ""), reverse=True)[
            : max(1, min(limit, 1000))
        ]

    def record_audit_event(
        self,
        company_id: str | None,
        actor_id: str | None,
        entity_type: str,
        entity_id: str,
        action: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        outcome: str = "success",
        severity: str = "informational",
        reason: str = "",
        metadata: dict | None = None,
        actor_type: str = "user",
        source_system: str | None = None,
    ) -> dict:
        self._audit(
            company_id,
            actor_id,
            entity_type,
            entity_id,
            action,
            before,
            after,
            outcome=outcome,
            severity=severity,
            reason=reason,
            metadata=metadata,
            actor_type=actor_type,
            source_system=source_system,
        )
        self.save_state(self.state)
        return deepcopy(self.state["auditEvents"][0])

    def record_sync_run(
        self,
        kind: str,
        run: dict,
        configured: bool,
        actor_id: str | None = None,
    ) -> dict:
        stored = deepcopy(run)
        self.state["syncRuns"] = [stored, *self.state.get("syncRuns", [])]
        self._retain_sync_runs()
        integration = next(
            (item for item in self.state.get("integrations", []) if item["type"] == kind),
            None,
        )
        if integration:
            before = deepcopy(integration)
            integration.update(
                lastSync=stored.get("finishedAt"),
                status="Healthy"
                if stored.get("status") == "success"
                else stored.get("status", "unknown"),
            )
            self._audit(
                integration.get("companyId"),
                actor_id,
                "integration_connection",
                integration["id"],
                "sync_completed",
                before,
                integration,
            )
        self.save_state(self.state)
        return _public_sync_run(stored)

    def get_msp_branding(self) -> dict:
        return {**DEFAULT_MSP_BRANDING, **deepcopy(self.state.get("mspBranding") or {})}

    def update_msp_branding(self, brand: dict, actor_id: str | None = None) -> dict:
        before = self.get_msp_branding()
        stored = {**DEFAULT_MSP_BRANDING, **deepcopy(brand)}
        self.state["mspBranding"] = stored
        self._audit(
            None,
            actor_id,
            "msp_branding",
            "msp",
            "updated",
            branding_audit_value(before),
            branding_audit_value(stored),
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def get_company_branding(self, company_id: str) -> dict:
        company = next((item for item in self.state["companies"] if item["id"] == company_id), None)
        if not company:
            raise ValueError("Customer not found")
        return {
            **default_company_branding(company["name"]),
            **deepcopy(self.state.setdefault("branding", {}).get(company_id) or {}),
        }

    def list_company_branding(self) -> dict[str, dict]:
        return {
            company["id"]: self.get_company_branding(company["id"])
            for company in self.state["companies"]
        }

    def update_company_branding(
        self, company_id: str, brand: dict, actor_id: str | None = None
    ) -> dict:
        before = self.get_company_branding(company_id)
        stored = {**before, **deepcopy(brand)}
        self.state.setdefault("branding", {})[company_id] = stored
        self._audit(
            company_id,
            actor_id,
            "company_branding",
            company_id,
            "updated",
            branding_audit_value(before),
            branding_audit_value(stored),
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def get_email_connection(self) -> dict:
        """Return the MSP-wide outbound email connection including encrypted material."""

        return {
            **deepcopy(DEFAULT_EMAIL_CONNECTION),
            **deepcopy(self.state.get("emailConnection") or {}),
        }

    def update_email_connection(self, connection: dict, actor_id: str | None = None) -> dict:
        """Persist and audit an MSP-wide outbound email connection."""

        before = self.get_email_connection()
        stored = {
            **before,
            **deepcopy(connection),
            "id": "msp-email",
            "scope": "msp",
            "provider": "microsoft_graph",
            "updatedAt": utc_now(),
            "revision": int(before.get("revision") or 0) + 1,
        }
        self.state["emailConnection"] = stored
        self._audit(
            None,
            actor_id,
            "email_connection",
            "msp-email",
            "updated",
            email_connection_audit_value(before),
            email_connection_audit_value(stored),
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def create_email_outbox(self, message: dict, actor_id: str | None = None) -> dict:
        """Queue a provider-neutral outbound message with an idempotency key."""

        existing = next(
            (
                item
                for item in self.state.get("emailOutbox", [])
                if item.get("idempotencyKey") == message.get("idempotencyKey")
            ),
            None,
        )
        if existing:
            return deepcopy(existing)
        stored = {
            "id": str(uuid.uuid4()),
            "companyId": None,
            "connectionId": "msp-email",
            "to": [],
            "cc": [],
            "bcc": [],
            "subject": "",
            "bodyHtml": "",
            "bodyText": "",
            "templateKey": "manual",
            "templateVersion": 1,
            "status": "queued",
            "attempts": 0,
            "maxAttempts": 5,
            "nextAttemptAt": utc_now(),
            "acceptedAt": None,
            "lastError": "",
            "providerRequestId": "",
            "createdBy": actor_id,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            **deepcopy(message),
        }
        self.state["emailOutbox"] = [stored, *self.state.get("emailOutbox", [])[:999]]
        self._audit(
            stored.get("companyId"),
            actor_id,
            "email_message",
            stored["id"],
            "queued",
            None,
            {key: value for key, value in stored.items() if key not in {"bodyHtml", "bodyText"}},
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def list_email_outbox(self, limit: int = 100) -> list[dict]:
        """List recent delivery records without message bodies."""

        records = sorted(
            self.state.get("emailOutbox", []),
            key=lambda item: item.get("createdAt", ""),
            reverse=True,
        )[: max(1, min(limit, 500))]
        return [
            {
                key: deepcopy(value)
                for key, value in item.items()
                if key not in {"bodyHtml", "bodyText"}
            }
            for item in records
        ]

    def get_email_outbox(self, message_id: str) -> dict | None:
        """Return one complete outbox item for an authorized delivery attempt."""

        item = next(
            (item for item in self.state.get("emailOutbox", []) if item["id"] == message_id),
            None,
        )
        return deepcopy(item) if item else None

    def update_email_outbox(
        self, message_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Record a delivery attempt, acceptance, or sanitized failure."""

        item = next(
            (item for item in self.state.get("emailOutbox", []) if item["id"] == message_id),
            None,
        )
        if not item:
            return None
        before_status = item.get("status")
        item.update(deepcopy(changes))
        item["updatedAt"] = utc_now()
        self._audit(
            item.get("companyId"),
            actor_id,
            "email_message",
            message_id,
            f"delivery_{item.get('status', 'updated')}",
            {"status": before_status},
            {
                "status": item.get("status"),
                "attempts": item.get("attempts"),
                "providerRequestId": item.get("providerRequestId"),
            },
            outcome="failed" if item.get("status") in {"failed", "dead_letter"} else "success",
            severity="warning"
            if item.get("status") in {"failed", "dead_letter"}
            else "informational",
            reason=str(item.get("lastError") or ""),
        )
        self.save_state(self.state)
        return deepcopy(item)

    def claim_email_outbox(self, message_id: str | None = None) -> dict | None:
        """Atomically claim one due message for a delivery worker."""

        now = datetime.now(UTC)
        stale_before = now - timedelta(minutes=15)
        for item in self.state.get("emailOutbox", []):
            if message_id and item.get("id") != message_id:
                continue
            status = item.get("status")
            next_attempt = parse_timestamp(item.get("nextAttemptAt"))
            updated_at = parse_timestamp(item.get("updatedAt"))
            eligible = status in {"queued", "failed"} and (
                next_attempt is None or next_attempt <= now
            )
            eligible = eligible or (
                status == "sending" and updated_at is not None and updated_at <= stale_before
            )
            if not eligible or int(item.get("attempts") or 0) >= int(item.get("maxAttempts") or 5):
                continue
            before = {"status": status, "attempts": item.get("attempts", 0)}
            item["status"] = "sending"
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["updatedAt"] = utc_now()
            self._audit(
                item.get("companyId"),
                None,
                "email_message",
                item["id"],
                "delivery_claimed",
                before,
                {"status": "sending", "attempts": item["attempts"]},
                metadata={"worker": True},
            )
            self.save_state(self.state)
            return deepcopy(item)
        return None

    def list_notification_rules(self) -> list[dict]:
        """Return root and customer notification rules."""

        return deepcopy(self.state.get("notificationRules", []))

    def update_notification_rule(
        self, rule_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Update one notification rule with audit evidence."""

        rule = next(
            (item for item in self.state.get("notificationRules", []) if item["id"] == rule_id),
            None,
        )
        if not rule:
            return None
        before = deepcopy(rule)
        rule.update(deepcopy(changes))
        rule["revision"] = int(before.get("revision") or 0) + 1
        rule["updatedAt"] = utc_now()
        self._audit(
            rule.get("companyId"),
            actor_id,
            "notification_rule",
            rule_id,
            "updated",
            before,
            rule,
        )
        self.save_state(self.state)
        return deepcopy(rule)

    def mark_notification_rule_run(self, rule_id: str, run_at: str) -> None:
        """Record scheduler progress without creating administrative audit noise."""

        rule = next(
            (item for item in self.state.get("notificationRules", []) if item["id"] == rule_id),
            None,
        )
        if rule:
            rule["lastRunAt"] = run_at
            rule["updatedAt"] = run_at
            self.save_state(self.state)

    def list_notification_templates(self) -> list[dict]:
        """Return editable notification templates."""

        return deepcopy(self.state.get("notificationTemplates", []))

    def update_notification_template(
        self, template_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Update one versioned notification template."""

        template = next(
            (
                item
                for item in self.state.get("notificationTemplates", [])
                if item["id"] == template_id
            ),
            None,
        )
        if not template:
            return None
        before = deepcopy(template)
        template.update(deepcopy(changes))
        template["version"] = int(before.get("version") or 0) + 1
        template["updatedAt"] = utc_now()
        self._audit(
            template.get("companyId"),
            actor_id,
            "notification_template",
            template_id,
            "updated",
            before,
            template,
        )
        self.save_state(self.state)
        return deepcopy(template)

    def list_notification_preferences(self, company_id: str | None = None) -> list[dict]:
        """Return contact and portal-user notification preferences."""

        return [
            deepcopy(item)
            for item in self.state.get("notificationPreferences", [])
            if not company_id or item.get("companyId") == company_id
        ]

    def upsert_notification_preference(self, preference: dict, actor_id: str | None = None) -> dict:
        """Create or update one recipient's channel and event choices."""

        existing = next(
            (
                item
                for item in self.state.get("notificationPreferences", [])
                if (
                    preference.get("contactId") and item.get("contactId") == preference["contactId"]
                )
                or (preference.get("userId") and item.get("userId") == preference["userId"])
            ),
            None,
        )
        before = deepcopy(existing) if existing else None
        if existing:
            existing.update(deepcopy(preference))
            stored = existing
        else:
            stored = {
                "id": str(uuid.uuid4()),
                "emailEnabled": True,
                "eventTypes": ["*"],
                "digestMode": "instant",
                **deepcopy(preference),
            }
            self.state.setdefault("notificationPreferences", []).append(stored)
        stored["updatedAt"] = utc_now()
        self._audit(
            stored.get("companyId"),
            actor_id,
            "notification_preference",
            stored["id"],
            "updated" if before else "created",
            before,
            stored,
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def create_notification_event(self, event: dict, actor_id: str | None = None) -> dict:
        """Persist one deduplicated notification event."""

        existing = next(
            (
                item
                for item in self.state.get("notificationEvents", [])
                if item.get("dedupeKey") == event.get("dedupeKey")
            ),
            None,
        )
        if existing:
            return deepcopy(existing)
        stored = {
            "id": str(uuid.uuid4()),
            "status": "pending",
            "recipients": [],
            "missingRoles": [],
            "context": {},
            "emailOutboxId": None,
            "scheduledFor": utc_now(),
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            **deepcopy(event),
        }
        self.state["notificationEvents"] = [
            stored,
            *self.state.get("notificationEvents", [])[:4999],
        ]
        self._audit(
            stored.get("companyId"),
            actor_id,
            "notification_event",
            stored["id"],
            "created",
            None,
            {key: value for key, value in stored.items() if key != "context"},
        )
        self.save_state(self.state)
        return deepcopy(stored)

    def update_notification_event(
        self, event_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Update event status, recipient evidence, or linked outbox record."""

        event = next(
            (item for item in self.state.get("notificationEvents", []) if item["id"] == event_id),
            None,
        )
        if not event:
            return None
        before = deepcopy(event)
        event.update(deepcopy(changes))
        event["updatedAt"] = utc_now()
        self._audit(
            event.get("companyId"),
            actor_id,
            "notification_event",
            event_id,
            "updated",
            {"status": before.get("status")},
            {"status": event.get("status"), "recipients": event.get("recipients")},
        )
        self.save_state(self.state)
        return deepcopy(event)

    def update_notification_event_for_outbox(
        self, outbox_id: str, status: str, actor_id: str | None = None
    ) -> dict | None:
        """Mirror a terminal email state onto its notification event."""

        event = next(
            (
                item
                for item in self.state.get("notificationEvents", [])
                if item.get("emailOutboxId") == outbox_id
            ),
            None,
        )
        return (
            self.update_notification_event(event["id"], {"status": status}, actor_id)
            if event
            else None
        )

    def list_notification_events(
        self, company_id: str | None = None, limit: int = 200
    ) -> list[dict]:
        """List recent notification events with tenant filtering."""

        return [
            deepcopy(item)
            for item in self.state.get("notificationEvents", [])
            if not company_id or item.get("companyId") == company_id
        ][: max(1, min(limit, 500))]

    def export_state(self) -> dict:
        state = deepcopy(self.state)
        # Credentials and message bodies are installation-bound and never portable.
        state.pop("apiTokens", None)
        state.pop("emailConnection", None)
        state.pop("emailOutbox", None)
        state.pop("notificationEvents", None)
        state.pop("passwordResets", None)
        state.pop("changeApprovalRequests", None)
        state.pop("providerCompanyObservations", None)
        for integration in state.get("integrations", []):
            integration.pop("credentialsEncrypted", None)
            integration.pop("credentialsNonce", None)
        return state

    def import_state(self, state: dict, actor_id: str | None = None) -> dict[str, int]:
        self.state.clear()
        self.state.update(deepcopy(state))
        self.state.setdefault("auditEvents", [])
        self.state.setdefault("contacts", [])
        self.state.setdefault("contactResponsibilities", [])
        self.state.setdefault("integrationObjectSuppressions", [])
        self.state.setdefault("emailConnection", deepcopy(DEFAULT_EMAIL_CONNECTION))
        self.state.setdefault("emailOutbox", [])
        self.state.setdefault(
            "notificationRules",
            [
                {
                    **deepcopy(rule),
                    "id": canonical_uuid("notification_rule", str(rule["key"])),
                    "companyId": None,
                    "fallbackAddresses": [],
                    "maxAttempts": 5,
                    "lastRunAt": None,
                    "revision": 1,
                }
                for rule in DEFAULT_NOTIFICATION_RULES
            ],
        )
        self.state.setdefault(
            "notificationTemplates",
            [
                {
                    **deepcopy(template),
                    "id": canonical_uuid("notification_template", str(template["key"])),
                    "companyId": None,
                    "enabled": True,
                    "version": 1,
                }
                for template in DEFAULT_NOTIFICATION_TEMPLATES
            ],
        )
        self.state.setdefault("notificationPreferences", [])
        self.state.setdefault("notificationEvents", [])
        self.state.setdefault("changeApprovalRequests", [])
        self.state.setdefault("changeTemplates", default_change_template_records())
        self.state.setdefault("passwordResets", [])
        self.state.setdefault("providerCompanyObservations", [])
        self.state.setdefault("providerCompanyMappings", [])
        self.state.setdefault("integrationCiPresence", [])
        self.state["apiTokens"] = []
        self.save_state(self.state)
        return {
            "companies": len(self.state.get("companies", [])),
            "assets": len(self.state.get("assets", [])),
            "relationships": len(self.state.get("relationships", [])),
        }

    def _audit(
        self,
        company_id: str | None,
        actor_id: str | None,
        entity_type: str,
        entity_id: str,
        action: str,
        before: dict | None,
        after: dict | None,
        *,
        outcome: str = "success",
        severity: str = "informational",
        reason: str = "",
        metadata: dict | None = None,
        actor_type: str = "user",
        source_system: str | None = None,
    ) -> None:
        context = current_audit_context()
        safe_before = sanitize_audit_value(deepcopy(before)) if before is not None else None
        safe_after = sanitize_audit_value(deepcopy(after)) if after is not None else None
        actor = next(
            (item for item in self.state.get("users", []) if item.get("id") == actor_id),
            None,
        )
        event = {
            "id": str(uuid.uuid4()),
            "companyId": company_id,
            "actorUserId": actor_id,
            "actorLabel": actor.get("email") if actor else (actor_id if actor_id else "System"),
            "actorType": actor_type if actor_id or actor_type != "user" else "system",
            "sourceSystem": source_system or context.source_system,
            "category": event_category(entity_type, action),
            "entityType": entity_type,
            "entityId": entity_id,
            "entityName": entity_name(before, after, entity_id),
            "action": action,
            "outcome": outcome,
            "severity": severity,
            "requestId": context.request_id,
            "correlationId": context.correlation_id or context.request_id,
            "before": safe_before,
            "after": safe_after,
            "changes": field_changes(before, after),
            "reason": reason[:1000],
            "metadata": sanitize_audit_value(
                {
                    **(metadata or {}),
                    "clientAddress": context.client_address,
                    "userAgent": context.user_agent,
                }
            ),
            "createdAt": utc_now(),
        }
        self.state["auditEvents"] = [
            event,
            *self.state["auditEvents"][:9999],
        ]


class PostgresCmdbRepository(StateRepository):
    """Canonical PostgreSQL repository for tenant-scoped CMDB resources."""

    mode = "canonical_postgresql"

    # Resource-specific implementations live alongside the shared transition
    # helpers until the JSON fallback is retired.
    list_changes = StateRepository._postgres_list_changes
    get_change = StateRepository._postgres_get_change
    next_change_number = StateRepository._postgres_next_change_number
    create_change = StateRepository._postgres_create_change
    update_change = StateRepository._postgres_update_change
    create_change_approval_request = StateRepository._postgres_create_change_approval_request
    list_change_approval_requests = StateRepository._postgres_list_change_approval_requests
    get_change_approval_request_by_token = (
        StateRepository._postgres_get_change_approval_request_by_token
    )
    update_change_approval_request = StateRepository._postgres_update_change_approval_request
    decide_change_approval_request = StateRepository._postgres_decide_change_approval_request
    revoke_change_approval_requests = StateRepository._postgres_revoke_change_approval_requests

    def __init__(
        self,
        state: dict,
        save_state: Callable[[dict], None],
        connection_factory: Callable[[], Any],
    ):
        super().__init__(state, save_state)
        self.connection_factory = connection_factory
        self._ensure_default_change_templates()

    @staticmethod
    def _worker_runtime_from_row(row: tuple) -> dict:
        """Convert a canonical worker status row to the public runtime shape."""

        return {
            "workerName": row[0],
            "workerId": row[1],
            "deploymentMode": row[2],
            "status": row[3],
            "intervalSeconds": int(row[4]),
            "lastStartedAt": PostgresCmdbRepository._timestamp(row[5]) or None,
            "lastHeartbeatAt": PostgresCmdbRepository._timestamp(row[6]),
            "lastCycleStartedAt": PostgresCmdbRepository._timestamp(row[7]) or None,
            "lastCycleFinishedAt": PostgresCmdbRepository._timestamp(row[8]) or None,
            "lastSuccessAt": PostgresCmdbRepository._timestamp(row[9]) or None,
            "lastErrorAt": PostgresCmdbRepository._timestamp(row[10]) or None,
            "lastError": row[11] or "",
            "cyclesCompleted": int(row[12]),
            "itemsProcessed": int(row[13]),
            "metadata": row[14] or {},
        }

    def record_worker_runtime(
        self,
        worker_name: str,
        worker_id: str,
        deployment_mode: str,
        interval_seconds: int,
        event: str,
        *,
        processed: int = 0,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """Upsert a cross-replica worker heartbeat in canonical PostgreSQL."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT worker_name, worker_id, deployment_mode, status, interval_seconds,
                       last_started_at, last_heartbeat_at, last_cycle_started_at,
                       last_cycle_finished_at, last_success_at, last_error_at, last_error,
                       cycles_completed, items_processed, metadata
                FROM worker_runtime_status
                WHERE worker_name = %s
                FOR UPDATE
                """,
                (worker_name,),
            )
            row = cursor.fetchone()
            current = self._worker_runtime_from_row(row) if row else None
            stored = updated_worker_runtime(
                current,
                worker_name,
                worker_id,
                deployment_mode,
                interval_seconds,
                event,
                processed=processed,
                error=error,
                metadata=metadata,
            )
            cursor.execute(
                """
                INSERT INTO worker_runtime_status (
                    worker_name, worker_id, deployment_mode, status, interval_seconds,
                    last_started_at, last_heartbeat_at, last_cycle_started_at,
                    last_cycle_finished_at, last_success_at, last_error_at, last_error,
                    cycles_completed, items_processed, metadata, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s::timestamptz, %s::timestamptz, %s::timestamptz,
                    %s::timestamptz, %s::timestamptz, %s::timestamptz, %s,
                    %s, %s, %s::jsonb, now()
                )
                ON CONFLICT (worker_name) DO UPDATE SET
                    worker_id = EXCLUDED.worker_id,
                    deployment_mode = EXCLUDED.deployment_mode,
                    status = EXCLUDED.status,
                    interval_seconds = EXCLUDED.interval_seconds,
                    last_started_at = EXCLUDED.last_started_at,
                    last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                    last_cycle_started_at = EXCLUDED.last_cycle_started_at,
                    last_cycle_finished_at = EXCLUDED.last_cycle_finished_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error_at = EXCLUDED.last_error_at,
                    last_error = EXCLUDED.last_error,
                    cycles_completed = EXCLUDED.cycles_completed,
                    items_processed = EXCLUDED.items_processed,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    stored["workerName"],
                    stored["workerId"],
                    stored["deploymentMode"],
                    stored["status"],
                    stored["intervalSeconds"],
                    stored["lastStartedAt"],
                    stored["lastHeartbeatAt"],
                    stored["lastCycleStartedAt"],
                    stored["lastCycleFinishedAt"],
                    stored["lastSuccessAt"],
                    stored["lastErrorAt"],
                    stored["lastError"] or None,
                    stored["cyclesCompleted"],
                    stored["itemsProcessed"],
                    json.dumps(stored["metadata"]),
                ),
            )
        return self.get_worker_runtime(worker_name) or stored

    def get_worker_runtime(self, worker_name: str) -> dict | None:
        """Return one canonical worker heartbeat."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT worker_name, worker_id, deployment_mode, status, interval_seconds,
                       last_started_at, last_heartbeat_at, last_cycle_started_at,
                       last_cycle_finished_at, last_success_at, last_error_at, last_error,
                       cycles_completed, items_processed, metadata
                FROM worker_runtime_status
                WHERE worker_name = %s
                """,
                (worker_name,),
            )
            row = cursor.fetchone()
        return self._worker_runtime_from_row(row) if row else None

    def record_provider_rate_limit(self, provider: str, observation: dict[str, Any]) -> dict:
        """Upsert sanitized provider throttle metadata without retaining payloads."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO provider_rate_limit_status (
                    provider, observed_at, http_status, limit_value, remaining_value,
                    reset_at, retry_after_seconds, limited, request_path, metadata, updated_at
                ) VALUES (
                    %s, now(), %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now()
                )
                ON CONFLICT (provider) DO UPDATE SET
                    observed_at = EXCLUDED.observed_at,
                    http_status = EXCLUDED.http_status,
                    limit_value = EXCLUDED.limit_value,
                    remaining_value = EXCLUDED.remaining_value,
                    reset_at = EXCLUDED.reset_at,
                    retry_after_seconds = EXCLUDED.retry_after_seconds,
                    limited = EXCLUDED.limited,
                    request_path = EXCLUDED.request_path,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    provider[:80],
                    observation.get("httpStatus"),
                    observation.get("limit"),
                    observation.get("remaining"),
                    str(observation.get("resetAt") or "")[:160] or None,
                    observation.get("retryAfterSeconds"),
                    bool(observation.get("limited")),
                    str(observation.get("requestPath") or "")[:500] or None,
                    json.dumps(observation.get("metadata") or {}),
                ),
            )
        return self.get_provider_rate_limit(provider) or {}

    def get_provider_rate_limit(self, provider: str) -> dict | None:
        """Return the latest canonical throttle observation for one provider."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT provider, observed_at, http_status, limit_value, remaining_value,
                       reset_at, retry_after_seconds, limited, request_path, metadata
                FROM provider_rate_limit_status
                WHERE provider = %s
                """,
                (provider,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "provider": row[0],
            "observedAt": self._timestamp(row[1]),
            "httpStatus": row[2],
            "limit": row[3],
            "remaining": row[4],
            "resetAt": row[5] or None,
            "retryAfterSeconds": row[6],
            "limited": bool(row[7]),
            "requestPath": row[8] or "",
            "metadata": row[9] or {},
        }

    @staticmethod
    def _change_template_from_row(row: tuple) -> dict:
        (
            template_id,
            company_slug,
            key,
            name,
            description,
            tags,
            status,
            system,
            current_version,
            owner_user_id,
            review_due_date,
            content,
            created_at,
            updated_at,
        ) = row
        return {
            "id": str(template_id),
            "companyId": company_slug,
            "key": key,
            "name": name,
            "description": description or "",
            "tags": list(tags or []),
            "status": status,
            "system": bool(system),
            "version": int(current_version),
            "ownerUserId": str(owner_user_id) if owner_user_id else None,
            "reviewDueDate": review_due_date.isoformat() if review_due_date else None,
            "content": content or {},
            "createdAt": PostgresCmdbRepository._timestamp(created_at),
            "updatedAt": PostgresCmdbRepository._timestamp(updated_at),
        }

    def _ensure_default_change_templates(self) -> None:
        """Idempotently seed the maintained MSP standard-template catalogue."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            for template in DEFAULT_CHANGE_TEMPLATES:
                template_id = canonical_uuid("change_template", template["key"])
                content = normalize_template_content(template["content"])
                cursor.execute(
                    """
                    INSERT INTO change_templates (
                        id, company_id, template_key, name, description, tags,
                        status, system, current_version
                    ) VALUES (
                        %s::uuid, NULL, %s, %s, %s, %s::jsonb,
                        'published', true, 1
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        template_id,
                        template["key"],
                        template["name"],
                        template["description"],
                        json.dumps(template["tags"]),
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO change_template_versions (
                        id, template_id, version, content
                    ) VALUES (%s::uuid, %s::uuid, 1, %s::jsonb)
                    ON CONFLICT (template_id, version) DO NOTHING
                    """,
                    (
                        change_template_version_uuid(template_id, 1),
                        template_id,
                        json.dumps(content),
                    ),
                )
                cursor.execute(
                    """
                    SELECT template.current_version, version.content
                    FROM change_templates template
                    JOIN change_template_versions version
                      ON version.template_id = template.id
                     AND version.version = template.current_version
                    WHERE template.id = %s::uuid AND template.system = true
                    """,
                    (template_id,),
                )
                current = cursor.fetchone()
                if current and not (current[1] or {}).get("closureTests"):
                    next_version = int(current[0]) + 1
                    upgraded_content = normalize_template_content(
                        {
                            **(current[1] or {}),
                            "closureTests": content["closureTests"],
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO change_template_versions (
                            id, template_id, version, content
                        ) VALUES (%s::uuid, %s::uuid, %s, %s::jsonb)
                        ON CONFLICT (template_id, version) DO NOTHING
                        """,
                        (
                            change_template_version_uuid(template_id, next_version),
                            template_id,
                            next_version,
                            json.dumps(upgraded_content),
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE change_templates
                        SET current_version = %s, updated_at = now()
                        WHERE id = %s::uuid AND current_version < %s
                        """,
                        (next_version, template_id, next_version),
                    )

    def list_change_templates(self, company_id: str | None = None) -> list[dict]:
        """Load current global and customer template versions."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT template.id, company.slug, template.template_key::text,
                       template.name, template.description, template.tags,
                       template.status, template.system, template.current_version,
                       template.owner_user_id, template.review_due_date,
                       version.content, template.created_at, template.updated_at
                FROM change_templates template
                LEFT JOIN companies company ON company.id = template.company_id
                JOIN change_template_versions version
                  ON version.template_id = template.id
                 AND version.version = template.current_version
                WHERE %s::text IS NULL OR template.company_id IS NULL OR company.slug = %s
                ORDER BY company.name NULLS FIRST, template.name
                """,
                (company_id, company_id),
            )
            return [self._change_template_from_row(row) for row in cursor.fetchall()]

    def get_change_template(self, template_id: str, version: int | None = None) -> dict | None:
        """Load one identity at its current or requested immutable version."""

        try:
            parsed_id = str(uuid.UUID(template_id))
        except (ValueError, TypeError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT template.id, company.slug, template.template_key::text,
                       template.name, template.description, template.tags,
                       template.status, template.system, version.version,
                       template.owner_user_id, template.review_due_date,
                       version.content, template.created_at, template.updated_at
                FROM change_templates template
                LEFT JOIN companies company ON company.id = template.company_id
                JOIN change_template_versions version
                  ON version.template_id = template.id
                 AND version.version = COALESCE(%s::integer, template.current_version)
                WHERE template.id = %s::uuid
                """,
                (version, parsed_id),
            )
            row = cursor.fetchone()
            return self._change_template_from_row(row) if row else None

    def create_change_template(self, template: dict, actor_id: str | None = None) -> dict:
        """Create a scoped template and immutable version one."""

        template_id = canonical_uuid("change_template", template.get("id") or str(uuid.uuid4()))
        actor_uuid = canonical_uuid("user", actor_id) if actor_id else None
        company_uuid = None
        content = normalize_template_content(template["content"])
        try:
            with self.connection_factory() as connection, connection.cursor() as cursor:
                if template.get("companyId"):
                    cursor.execute(
                        "SELECT id FROM companies WHERE slug = %s",
                        (template["companyId"],),
                    )
                    row = cursor.fetchone()
                    if not row:
                        raise ValueError("Customer not found")
                    company_uuid = str(row[0])
                cursor.execute(
                    """
                    INSERT INTO change_templates (
                        id, company_id, template_key, name, description, tags,
                        status, system, current_version, owner_user_id,
                        review_due_date, created_by
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb,
                        %s, false, 1, %s::uuid, %s::date, %s::uuid
                    )
                    """,
                    (
                        template_id,
                        company_uuid,
                        template["key"],
                        template["name"],
                        template.get("description", ""),
                        json.dumps(template.get("tags") or []),
                        template.get("status", "draft"),
                        canonical_uuid("user", template["ownerUserId"])
                        if template.get("ownerUserId")
                        else None,
                        template.get("reviewDueDate") or None,
                        actor_uuid,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO change_template_versions (
                        id, template_id, version, content, created_by
                    ) VALUES (%s::uuid, %s::uuid, 1, %s::jsonb, %s::uuid)
                    """,
                    (
                        change_template_version_uuid(template_id, 1),
                        template_id,
                        json.dumps(content),
                        actor_uuid,
                    ),
                )
                stored = {
                    **deepcopy(template),
                    "id": template_id,
                    "version": 1,
                    "system": False,
                    "content": content,
                }
                self._insert_audit(
                    cursor,
                    template.get("companyId"),
                    actor_id,
                    "change_template",
                    template_id,
                    "created",
                    None,
                    stored,
                )
        except UniqueViolation as error:
            raise ValueError("A template with that key already exists in this scope") from error
        self._refresh_state_mirror()
        self.save_state(self.state)
        reloaded = self.get_change_template(template_id)
        if not reloaded:
            raise RuntimeError("Created change template could not be reloaded")
        return reloaded

    def update_change_template(
        self,
        template_id: str,
        changes: dict,
        expected_version: int,
        actor_id: str | None = None,
    ) -> dict:
        """Write a new content version using optimistic concurrency."""

        before = self.get_change_template(template_id)
        if not before:
            raise ValueError("Change template not found")
        content = normalize_template_content(changes.get("content") or before["content"])
        actor_uuid = canonical_uuid("user", actor_id) if actor_id else None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_version FROM change_templates
                WHERE id = %s::uuid FOR UPDATE
                """,
                (template_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Change template not found")
            if int(row[0]) != expected_version:
                raise ValueError("Change template changed. Reload it before saving.")
            new_version = expected_version + 1
            cursor.execute(
                """
                INSERT INTO change_template_versions (
                    id, template_id, version, content, created_by
                ) VALUES (%s::uuid, %s::uuid, %s, %s::jsonb, %s::uuid)
                """,
                (
                    change_template_version_uuid(template_id, new_version),
                    template_id,
                    new_version,
                    json.dumps(content),
                    actor_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE change_templates SET
                    name = %s, description = %s, tags = %s::jsonb,
                    status = %s, current_version = %s,
                    owner_user_id = %s::uuid, review_due_date = %s::date,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    changes.get("name", before["name"]),
                    changes.get("description", before["description"]),
                    json.dumps(changes.get("tags", before["tags"])),
                    changes.get("status", before["status"]),
                    new_version,
                    canonical_uuid("user", changes["ownerUserId"])
                    if changes.get("ownerUserId")
                    else None,
                    changes.get("reviewDueDate") or None,
                    template_id,
                ),
            )
            after = {
                **before,
                **{key: deepcopy(value) for key, value in changes.items() if key != "content"},
                "version": new_version,
                "content": content,
            }
            self._insert_audit(
                cursor,
                before.get("companyId"),
                actor_id,
                "change_template",
                template_id,
                "version_created",
                before,
                after,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        stored = self.get_change_template(template_id)
        if not stored:
            raise RuntimeError("Updated change template could not be reloaded")
        return stored

    def is_initialized(self) -> bool:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM users WHERE status <> 'disabled')")
            return bool(cursor.fetchone()[0])

    def migrate_legacy_company_branding(self) -> int:
        """Import only the last non-canonical field, then retire the old document."""
        imported = 0
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT slug, id, name FROM companies WHERE status <> 'inactive'")
            companies = {
                slug: (str(company_id), name) for slug, company_id, name in cursor.fetchall()
            }
            for company_slug, configured_brand in self.state.get("branding", {}).items():
                company = companies.get(company_slug)
                if not company:
                    continue
                company_uuid, company_name = company
                brand = {
                    **default_company_branding(company_name),
                    **(configured_brand or {}),
                }
                cursor.execute(
                    """
                    INSERT INTO company_branding (
                        company_id, display_name, logo_text, accent_color,
                        secondary_color, logo_data_url, logo_file_name, updated_at
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (company_id) DO NOTHING
                    """,
                    (
                        company_uuid,
                        brand["name"],
                        brand["logoText"],
                        brand["accent"],
                        brand["secondaryAccent"],
                        brand.get("logoDataUrl") or None,
                        brand.get("logoFileName") or None,
                    ),
                )
                imported += max(0, cursor.rowcount)
            cursor.execute("SELECT to_regclass('public.legacy_application_state')")
            if cursor.fetchone()[0]:
                cursor.execute("DELETE FROM legacy_application_state WHERE state_key = 'cmdb_api'")
        return imported

    def bootstrap(self) -> dict[str, int]:
        """Idempotently import transition-state CMDB records and refresh the mirror."""
        asset_ids: dict[str, str] = {}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            for company in self.state["companies"]:
                company_uuid = canonical_uuid("company", company["id"])
                cursor.execute(
                    """
                    INSERT INTO companies (id, slug, name, attributes, updated_at)
                    VALUES (%s::uuid, %s, %s, %s::jsonb, now())
                    ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name,
                        attributes = companies.attributes || EXCLUDED.attributes,
                        updated_at = now()
                    """,
                    (
                        company_uuid,
                        company["id"],
                        company["name"],
                        json.dumps({"externalIds": company.get("externalIds", {})}),
                    ),
                )

            cursor.execute("SELECT slug, id FROM companies")
            company_ids = {slug: str(company_id) for slug, company_id in cursor.fetchall()}

            for company_slug, configured_brand in self.state.get("branding", {}).items():
                brand_company_uuid = company_ids.get(company_slug)
                company = next(
                    (item for item in self.state["companies"] if item["id"] == company_slug),
                    None,
                )
                if not brand_company_uuid or not company:
                    continue
                brand = {
                    **default_company_branding(company["name"]),
                    **(configured_brand or {}),
                }
                cursor.execute(
                    """
                    INSERT INTO company_branding (
                        company_id, display_name, logo_text, accent_color,
                        secondary_color, logo_data_url, logo_file_name, updated_at
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (company_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        logo_text = EXCLUDED.logo_text,
                        accent_color = EXCLUDED.accent_color,
                        secondary_color = EXCLUDED.secondary_color,
                        logo_data_url = EXCLUDED.logo_data_url,
                        logo_file_name = EXCLUDED.logo_file_name,
                        updated_at = now()
                    """,
                    (
                        brand_company_uuid,
                        brand["name"],
                        brand["logoText"],
                        brand["accent"],
                        brand["secondaryAccent"],
                        brand.get("logoDataUrl") or None,
                        brand.get("logoFileName") or None,
                    ),
                )

            for group in self.state.get("accessGroups", []):
                group_uuid = canonical_uuid("access_group", group["id"])
                cursor.execute(
                    """
                    INSERT INTO access_groups (
                        id, slug, name, system, description, membership_mode,
                        membership_rules, revision, updated_at
                    )
                    VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s, now())
                    ON CONFLICT (slug) DO UPDATE SET
                        name = EXCLUDED.name,
                        system = EXCLUDED.system,
                        description = EXCLUDED.description,
                        membership_mode = EXCLUDED.membership_mode,
                        membership_rules = EXCLUDED.membership_rules,
                        revision = EXCLUDED.revision,
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        group_uuid,
                        group["id"],
                        group["name"],
                        bool(group.get("system")),
                        group.get("description", ""),
                        group.get("membershipMode", "dynamic" if group.get("system") else "manual"),
                        json.dumps(
                            group.get("membershipRules")
                            or ({"rule": "all_managed_customers"} if group.get("system") else {})
                        ),
                        max(1, int(group.get("revision", 1))),
                    ),
                )
                group_uuid = str(cursor.fetchone()[0])
                cursor.execute(
                    "DELETE FROM access_group_companies WHERE access_group_id = %s::uuid",
                    (group_uuid,),
                )
                selected_companies = (
                    company_ids.values()
                    if "*" in group.get("companyIds", [])
                    else (
                        company_ids[item]
                        for item in group.get("companyIds", [])
                        if item in company_ids
                    )
                )
                for company_uuid in selected_companies:
                    cursor.execute(
                        """
                        INSERT INTO access_group_companies (access_group_id, company_id)
                        VALUES (%s::uuid, %s::uuid) ON CONFLICT DO NOTHING
                        """,
                        (group_uuid, company_uuid),
                    )

            cursor.execute("SELECT slug, id FROM access_groups")
            group_ids = {slug: str(group_id) for slug, group_id in cursor.fetchall()}
            user_ids: dict[str, str] = {}
            for user in self.state.get("users", []):
                user_uuid = canonical_uuid("user", user["id"])
                attributes = {
                    "legacyId": user.get("id"),
                    "role": user.get("role"),
                    "accountType": user.get(
                        "accountType",
                        "root"
                        if user.get("role") in {"platform_admin", "msp_operator"}
                        else "customer",
                    ),
                }
                cursor.execute(
                    """
                    INSERT INTO users (id, email, display_name, status, attributes)
                    VALUES (%s::uuid, %s, %s, 'active', %s::jsonb)
                    ON CONFLICT (email) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        status = 'active',
                        attributes = users.attributes || EXCLUDED.attributes
                    RETURNING id
                    """,
                    (
                        user_uuid,
                        user["email"],
                        user.get("displayName") or user["email"].split("@", 1)[0],
                        json.dumps(attributes),
                    ),
                )
                user_uuid = str(cursor.fetchone()[0])
                user_ids[user["id"]] = user_uuid
                password_hash = user.get("passwordHash")
                if not password_hash and user.get("password"):
                    password_hash = hash_password(user["password"])
                if password_hash:
                    cursor.execute(
                        """
                        INSERT INTO local_auth_credentials (user_id, password_hash, updated_at)
                        VALUES (%s::uuid, %s, now())
                        ON CONFLICT (user_id) DO UPDATE SET password_hash = EXCLUDED.password_hash, updated_at = now()
                        """,
                        (user_uuid, password_hash),
                    )
                cursor.execute(
                    "DELETE FROM user_platform_roles WHERE user_id = %s::uuid",
                    (user_uuid,),
                )
                cursor.execute(
                    "DELETE FROM user_company_roles WHERE user_id = %s::uuid",
                    (user_uuid,),
                )
                cursor.execute(
                    "DELETE FROM user_access_groups WHERE user_id = %s::uuid",
                    (user_uuid,),
                )
                if user.get("role") == "platform_admin":
                    cursor.execute(
                        "INSERT INTO user_platform_roles (user_id, role) VALUES (%s::uuid, 'platform_admin') ON CONFLICT DO NOTHING",
                        (user_uuid,),
                    )
                else:
                    database_role = (
                        "msp_operator" if user.get("role") == "msp_operator" else "customer_reader"
                    )
                    for company_slug in user.get("directCompanyIds", user.get("companyIds", [])):
                        selected_role_companies: tuple[str, ...]
                        if company_slug == "*":
                            selected_role_companies = tuple(company_ids.values())
                        elif company_slug in company_ids:
                            selected_role_companies = (company_ids[company_slug],)
                        else:
                            selected_role_companies = ()
                        for company_uuid in selected_role_companies:
                            cursor.execute(
                                """
                                INSERT INTO user_company_roles (user_id, company_id, role)
                                VALUES (%s::uuid, %s::uuid, %s) ON CONFLICT DO NOTHING
                                """,
                                (user_uuid, company_uuid, database_role),
                            )
                for group_slug in user.get("groupIds", []):
                    if group_slug in group_ids:
                        cursor.execute(
                            """
                            INSERT INTO user_access_groups (user_id, access_group_id)
                            VALUES (%s::uuid, %s::uuid) ON CONFLICT DO NOTHING
                            """,
                            (user_uuid, group_ids[group_slug]),
                        )

            for group in self.state.get("accessGroups", []):
                owner_uuid = user_ids.get(group.get("ownerUserId"))
                if owner_uuid and group.get("id") in group_ids:
                    cursor.execute(
                        "UPDATE access_groups SET owner_user_id = %s::uuid WHERE id = %s::uuid",
                        (owner_uuid, group_ids[group["id"]]),
                    )

            for template in self.state.get("changeTemplates", []):
                template_id = canonical_uuid("change_template", template["id"])
                template_company_uuid = company_ids.get(template.get("companyId"))
                owner_id = template.get("ownerUserId")
                owner_uuid = user_ids.get(owner_id) if owner_id else None
                current_version = max(1, int(template.get("version") or 1))
                cursor.execute(
                    """
                    INSERT INTO change_templates (
                        id, company_id, template_key, name, description, tags,
                        status, system, current_version, owner_user_id,
                        review_due_date, created_by, created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s,
                        %s::uuid, %s::date, %s::uuid,
                        COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now())
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        template_key = EXCLUDED.template_key,
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        tags = EXCLUDED.tags,
                        status = EXCLUDED.status,
                        system = EXCLUDED.system,
                        current_version = EXCLUDED.current_version,
                        owner_user_id = EXCLUDED.owner_user_id,
                        review_due_date = EXCLUDED.review_due_date,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        template_id,
                        template_company_uuid,
                        template["key"],
                        template["name"],
                        template.get("description", ""),
                        json.dumps(template.get("tags") or []),
                        template.get("status", "draft"),
                        bool(template.get("system")),
                        current_version,
                        owner_uuid,
                        template.get("reviewDueDate") or None,
                        user_ids.get(template.get("createdBy")),
                        template.get("createdAt") or None,
                        template.get("updatedAt") or None,
                    ),
                )
                versions = template.get("versions") or [
                    {
                        "version": current_version,
                        "content": template.get("content") or {},
                        "createdBy": template.get("createdBy"),
                        "createdAt": template.get("updatedAt") or template.get("createdAt"),
                    }
                ]
                for version_record in versions:
                    version_number = max(1, int(version_record.get("version") or 1))
                    cursor.execute(
                        """
                        INSERT INTO change_template_versions (
                            id, template_id, version, content, created_by, created_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s, %s::jsonb, %s::uuid,
                            COALESCE(%s::timestamptz, now())
                        )
                        ON CONFLICT (template_id, version) DO UPDATE SET
                            content = EXCLUDED.content,
                            created_by = EXCLUDED.created_by
                        """,
                        (
                            change_template_version_uuid(template_id, version_number),
                            template_id,
                            version_number,
                            json.dumps(
                                normalize_template_content(
                                    version_record.get("content") or template["content"]
                                )
                            ),
                            user_ids.get(version_record.get("createdBy")),
                            version_record.get("createdAt") or None,
                        ),
                    )

            contact_ids: dict[str, str] = {}
            for contact in self.state.get("contacts", []):
                contact_company_uuid = company_ids.get(contact.get("companyId"))
                if not contact_company_uuid:
                    continue
                contact_uuid = canonical_uuid("contact", contact["id"])
                contact_ids[contact["id"]] = contact_uuid
                linked_user_id = contact.get("linkedUserId")
                cursor.execute(
                    """
                    INSERT INTO contacts (
                        id, company_id, linked_user_id, display_name, normalized_name,
                        first_name, last_name, primary_email, phone, mobile, job_title,
                        department, location, timezone, manager_contact_id, status,
                        source, sync_status, last_seen_at, last_synced_at, attributes,
                        created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s::uuid, %s, %s, %s, %s::timestamptz,
                        %s::timestamptz, %s::jsonb, COALESCE(%s::timestamptz, now()),
                        COALESCE(%s::timestamptz, now())
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        linked_user_id = EXCLUDED.linked_user_id,
                        display_name = EXCLUDED.display_name,
                        normalized_name = EXCLUDED.normalized_name,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        primary_email = EXCLUDED.primary_email,
                        phone = EXCLUDED.phone,
                        mobile = EXCLUDED.mobile,
                        job_title = EXCLUDED.job_title,
                        department = EXCLUDED.department,
                        location = EXCLUDED.location,
                        timezone = EXCLUDED.timezone,
                        manager_contact_id = EXCLUDED.manager_contact_id,
                        status = EXCLUDED.status,
                        source = EXCLUDED.source,
                        sync_status = EXCLUDED.sync_status,
                        last_seen_at = EXCLUDED.last_seen_at,
                        last_synced_at = EXCLUDED.last_synced_at,
                        attributes = EXCLUDED.attributes,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        contact_uuid,
                        contact_company_uuid,
                        canonical_uuid("user", linked_user_id) if linked_user_id else None,
                        contact["displayName"],
                        normalized_name(contact["displayName"]),
                        contact.get("firstName") or None,
                        contact.get("lastName") or None,
                        contact.get("email") or None,
                        contact.get("phone") or None,
                        contact.get("mobile") or None,
                        contact.get("jobTitle") or None,
                        contact.get("department") or None,
                        contact.get("location") or None,
                        contact.get("timezone") or None,
                        None,
                        contact.get("status", "active"),
                        contact.get("source", "manual"),
                        contact.get("syncStatus", "not_synced"),
                        contact.get("lastSeen") or None,
                        contact.get("lastSynced") or None,
                        json.dumps(contact.get("attributes") or {}),
                        contact.get("createdAt") or None,
                        contact.get("updatedAt") or None,
                    ),
                )

            for contact in self.state.get("contacts", []):
                if contact.get("managerContactId") and contact.get("id") in contact_ids:
                    cursor.execute(
                        "UPDATE contacts SET manager_contact_id = %s::uuid WHERE id = %s::uuid",
                        (
                            contact_ids.get(
                                contact["managerContactId"],
                                canonical_uuid("contact", contact["managerContactId"]),
                            ),
                            contact_ids[contact["id"]],
                        ),
                    )

            integration_ids: dict[str, str] = {}
            for integration in self.state.get("integrations", []):
                kind = integration.get("type", integration.get("id", "future"))
                provider = PROVIDER_TO_DB.get(kind, "future")
                integration_uuid = canonical_uuid("integration_connection", integration["id"])
                company_slug = integration.get("companyId")
                configuration = {
                    "legacyId": integration.get("id"),
                    "mode": integration.get("mode", "configured_by_environment"),
                    "scope": integration.get("scope", "customer" if company_slug else "msp"),
                }
                cursor.execute(
                    """
                    INSERT INTO integration_connections (
                        id, slug, company_id, provider, name, credential_reference,
                        configuration, enabled, updated_at
                    ) VALUES (
                        %s::uuid, %s, (SELECT id FROM companies WHERE slug = %s),
                        %s, %s, %s, %s::jsonb, %s, now()
                    )
                    ON CONFLICT (slug) DO UPDATE SET
                        company_id = EXCLUDED.company_id, provider = EXCLUDED.provider,
                        name = EXCLUDED.name, credential_reference = EXCLUDED.credential_reference,
                        configuration = integration_connections.configuration || EXCLUDED.configuration,
                        enabled = EXCLUDED.enabled, updated_at = now()
                    RETURNING id
                    """,
                    (
                        integration_uuid,
                        integration["id"],
                        company_slug,
                        provider,
                        integration["name"],
                        CREDENTIAL_REFERENCES.get(kind, "keyvault://future"),
                        json.dumps(configuration),
                        bool(integration.get("enabled")),
                    ),
                )
                integration_ids[kind] = str(cursor.fetchone()[0])

            status_to_db = {
                "success": "succeeded",
                "blocked": "blocked",
                "failed": "failed",
            }
            for run in self.state.get("syncRuns", []):
                kind = run.get("type")
                run_integration_uuid = integration_ids.get(kind) if isinstance(kind, str) else None
                if not run_integration_uuid:
                    continue
                run_uuid = canonical_uuid("sync_run", run["id"])
                status = status_to_db.get(run.get("status"), "review_required")
                cursor.execute(
                    """
                    INSERT INTO sync_runs (
                        id, integration_connection_id, status, started_at, finished_at,
                        discovered_count, created_count, updated_count, review_count,
                        error_summary, message, attributes
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, COALESCE(%s::timestamptz, now()),
                        %s::timestamptz, %s, %s, %s, %s, %s, %s, %s::jsonb
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status, finished_at = EXCLUDED.finished_at,
                        discovered_count = EXCLUDED.discovered_count,
                        created_count = EXCLUDED.created_count,
                        updated_count = EXCLUDED.updated_count,
                        review_count = EXCLUDED.review_count,
                        error_summary = EXCLUDED.error_summary, message = EXCLUDED.message,
                        attributes = EXCLUDED.attributes
                    """,
                    (
                        run_uuid,
                        run_integration_uuid,
                        status,
                        run.get("startedAt") or None,
                        run.get("finishedAt") or None,
                        int(run.get("discovered") or 0),
                        int(run.get("imported") or 0),
                        int(run.get("updated") or 0),
                        int(run.get("review") or 0),
                        run.get("message") if status == "failed" else None,
                        run.get("message") or "",
                        json.dumps({"legacyStatus": run.get("status")}),
                    ),
                )

            brand = {**DEFAULT_MSP_BRANDING, **(self.state.get("mspBranding") or {})}
            cursor.execute(
                """
                INSERT INTO msp_branding (
                    id, display_name, logo_text, accent_color, secondary_color,
                    logo_data_url, logo_file_name, support_email, support_url,
                    support_phone, welcome_message, report_footer,
                    confidentiality_label, updated_at
                ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    logo_text = EXCLUDED.logo_text,
                    accent_color = EXCLUDED.accent_color,
                    secondary_color = EXCLUDED.secondary_color,
                    logo_data_url = EXCLUDED.logo_data_url,
                    logo_file_name = EXCLUDED.logo_file_name,
                    support_email = EXCLUDED.support_email,
                    support_url = EXCLUDED.support_url,
                    support_phone = EXCLUDED.support_phone,
                    welcome_message = EXCLUDED.welcome_message,
                    report_footer = EXCLUDED.report_footer,
                    confidentiality_label = EXCLUDED.confidentiality_label,
                    updated_at = now()
                """,
                (
                    brand["name"],
                    brand["logoText"],
                    brand["accent"],
                    brand["secondaryAccent"],
                    brand["logoDataUrl"] or None,
                    brand["logoFileName"] or None,
                    brand["supportEmail"] or None,
                    brand["supportUrl"] or None,
                    brand["supportPhone"] or None,
                    brand["welcomeMessage"] or None,
                    brand["reportFooter"] or None,
                    brand["confidentialityLabel"] or None,
                ),
            )

            for asset in self.state["assets"]:
                asset_company_uuid = company_ids.get(asset["companyId"])
                if not asset_company_uuid:
                    continue
                asset_uuid = canonical_uuid("configuration_item", asset["id"])
                asset_ids[asset["id"]] = asset_uuid
                metadata = deepcopy(asset.get("metadata") or {})
                lifecycle = metadata.get("lifecycle", "in_service")
                operational = metadata.get("operationalStatus", "unknown")
                if asset.get("status") == "Retired":
                    lifecycle = "retired"
                elif asset.get("status") == "Planned":
                    lifecycle = "planned"
                elif asset.get("status") == "Active" and operational == "unknown":
                    # Preserve the prototype's visible default for active records
                    # that did not carry an explicit monitoring state.
                    operational = "healthy"
                metadata["lifecycle"] = lifecycle
                metadata["operationalStatus"] = operational
                attributes = {
                    "legacyId": asset.get("id"),
                    "status": asset.get("status", "Active"),
                    "source": asset.get("source", "manual"),
                    "externalId": asset.get("externalId"),
                    "lastSeen": asset.get("lastSeen"),
                    "fields": asset.get("fields", {}),
                    "metadata": metadata,
                }
                cursor.execute(
                    """
                    INSERT INTO configuration_items (
                        id, company_id, ci_type, display_name, normalized_name,
                        lifecycle_status, operational_status, attributes, updated_at
                    ) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (id) DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        ci_type = EXCLUDED.ci_type,
                        display_name = EXCLUDED.display_name,
                        normalized_name = EXCLUDED.normalized_name,
                        lifecycle_status = EXCLUDED.lifecycle_status,
                        operational_status = EXCLUDED.operational_status,
                        attributes = configuration_items.attributes || EXCLUDED.attributes,
                        updated_at = now()
                    """,
                    (
                        asset_uuid,
                        asset_company_uuid,
                        asset["type"],
                        asset["name"],
                        normalized_name(asset["name"]),
                        lifecycle,
                        operational,
                        json.dumps(attributes),
                    ),
                )

            for responsibility in self.state.get("contactResponsibilities", []):
                responsibility_company_uuid = company_ids.get(responsibility.get("companyId"))
                source_asset_id = responsibility.get("assetId")
                source_contact_id = responsibility.get("contactId")
                if not isinstance(source_asset_id, str) or not isinstance(source_contact_id, str):
                    continue
                asset_uuid = asset_ids.get(
                    source_asset_id,
                    canonical_uuid("configuration_item", source_asset_id),
                )
                contact_uuid = contact_ids.get(
                    source_contact_id,
                    canonical_uuid("contact", source_contact_id),
                )
                if not responsibility_company_uuid:
                    continue
                cursor.execute(
                    """
                    INSERT INTO contact_responsibilities (
                        id, company_id, ci_id, contact_id, responsibility_role,
                        is_primary, effective_from, effective_until, escalation_order,
                        notes, source, created_by, ended_by, created_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s,
                        COALESCE(%s::timestamptz, now()), %s::timestamptz, %s,
                        %s, %s, %s::uuid, %s::uuid, COALESCE(%s::timestamptz, now())
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        contact_id = EXCLUDED.contact_id,
                        responsibility_role = EXCLUDED.responsibility_role,
                        is_primary = EXCLUDED.is_primary,
                        effective_from = EXCLUDED.effective_from,
                        effective_until = EXCLUDED.effective_until,
                        escalation_order = EXCLUDED.escalation_order,
                        notes = EXCLUDED.notes,
                        source = EXCLUDED.source,
                        ended_by = EXCLUDED.ended_by
                    """,
                    (
                        canonical_uuid("contact_responsibility", responsibility["id"]),
                        responsibility_company_uuid,
                        asset_uuid,
                        contact_uuid,
                        responsibility["role"],
                        bool(responsibility.get("isPrimary", True)),
                        responsibility.get("effectiveFrom") or None,
                        responsibility.get("effectiveUntil") or None,
                        int(responsibility.get("escalationOrder", 1)),
                        responsibility.get("notes") or None,
                        responsibility.get("source", "manual"),
                        canonical_uuid("user", responsibility["createdBy"])
                        if responsibility.get("createdBy")
                        else None,
                        canonical_uuid("user", responsibility["endedBy"])
                        if responsibility.get("endedBy")
                        else None,
                        responsibility.get("effectiveFrom") or None,
                    ),
                )

            for relationship in self.state["relationships"]:
                from_id = asset_ids.get(
                    relationship["fromId"],
                    canonical_uuid("configuration_item", relationship["fromId"]),
                )
                to_id = asset_ids.get(
                    relationship["toId"],
                    canonical_uuid("configuration_item", relationship["toId"]),
                )
                cursor.execute(
                    "SELECT company_id FROM configuration_items WHERE id = %s::uuid",
                    (from_id,),
                )
                company_row = cursor.fetchone()
                if not company_row:
                    continue
                cursor.execute(
                    """
                    INSERT INTO ci_relationships (
                        id, company_id, from_ci_id, to_ci_id, relationship_type, impact_policy
                    ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        from_ci_id = EXCLUDED.from_ci_id,
                        to_ci_id = EXCLUDED.to_ci_id,
                        relationship_type = EXCLUDED.relationship_type,
                        impact_policy = EXCLUDED.impact_policy,
                        retired_at = NULL
                    """,
                    (
                        canonical_uuid("relationship", relationship["id"]),
                        str(company_row[0]),
                        from_id,
                        to_id,
                        relationship["type"],
                        relationship.get("impactPolicy", "required"),
                    ),
                )

            self._rewrite_change_ids(asset_ids)
            for change in self.state.get("changes", []):
                self._write_change(cursor, change)

            cursor.execute("SELECT to_regclass('public.legacy_application_state')")
            if cursor.fetchone()[0]:
                cursor.execute("DELETE FROM legacy_application_state WHERE state_key = 'cmdb_api'")

        self._refresh_state_mirror()
        self.save_state(self.state)
        return {
            "companies": len(self.state["companies"]),
            "assets": len(self.state["assets"]),
            "relationships": len(self.state["relationships"]),
            "changes": len(self.state.get("changes", [])),
            "integrations": len(self.state.get("integrations", [])),
            "syncRuns": len(self.state.get("syncRuns", [])),
        }

    def list_companies(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT slug, name, attributes FROM companies WHERE status <> 'inactive' ORDER BY name"
            )
            return [
                {
                    "id": slug,
                    "name": name,
                    "externalIds": (attributes or {}).get("externalIds", {}),
                }
                for slug, name, attributes in cursor.fetchall()
            ]

    def create_company(self, company: dict, actor_id: str | None = None) -> dict:
        company_uuid = canonical_uuid("company", company["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO companies (id, slug, name, attributes)
                VALUES (%s::uuid, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    company_uuid,
                    company["id"],
                    company["name"],
                    json.dumps({"externalIds": company.get("externalIds", {})}),
                ),
            )
            cursor.execute(
                """
                INSERT INTO access_group_companies (access_group_id, company_id)
                SELECT id, %s::uuid FROM access_groups WHERE system = true
                ON CONFLICT DO NOTHING
                """,
                (company_uuid,),
            )
            self._insert_audit(
                cursor,
                company["id"],
                actor_id,
                "company",
                company_uuid,
                "created",
                None,
                company,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return company

    def list_users(
        self, include_inactive: bool = False, include_credentials: bool = False
    ) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, u.email::text, u.display_name, u.status,
                       u.api_access_enabled, u.last_login_at, u.archived_at,
                       u.identity_provider_subject, u.attributes, lac.password_hash,
                       COALESCE(lac.mfa_required, false),
                       COALESCE(mfa.status = 'enabled', false),
                       (SELECT count(*) FROM user_mfa_recovery_codes recovery
                        WHERE recovery.user_id = u.id AND recovery.used_at IS NULL),
                       EXISTS (SELECT 1 FROM user_platform_roles upr WHERE upr.user_id = u.id AND upr.role = 'platform_admin'),
                       ARRAY(
                           SELECT DISTINCT c.slug
                           FROM (
                               SELECT ucr.company_id FROM user_company_roles ucr WHERE ucr.user_id = u.id
                               UNION
                               SELECT agc.company_id
                               FROM user_access_groups uag
                               JOIN access_group_companies agc ON agc.access_group_id = uag.access_group_id
                               WHERE uag.user_id = u.id
                           ) permitted
                           JOIN companies c ON c.id = permitted.company_id
                           WHERE c.status <> 'inactive'
                           ORDER BY c.slug
                       ),
                       ARRAY(
                           SELECT ag.slug
                           FROM user_access_groups uag
                           JOIN access_groups ag ON ag.id = uag.access_group_id
                           WHERE uag.user_id = u.id
                           ORDER BY ag.name
                       ),
                       ARRAY(
                           SELECT c.slug
                           FROM user_company_roles ucr
                           JOIN companies c ON c.id = ucr.company_id
                           WHERE ucr.user_id = u.id AND c.status <> 'inactive'
                           ORDER BY c.slug
                       ),
                       EXISTS (SELECT 1 FROM user_company_roles ucr WHERE ucr.user_id = u.id AND ucr.role = 'msp_operator'),
                       (SELECT count(*) FROM user_api_tokens token
                        WHERE token.user_id = u.id AND token.revoked_at IS NULL
                          AND token.expires_at > now()),
                       (SELECT max(token.last_used_at) FROM user_api_tokens token
                        WHERE token.user_id = u.id)
                FROM users u
                LEFT JOIN local_auth_credentials lac ON lac.user_id = u.id
                LEFT JOIN user_mfa_credentials mfa ON mfa.user_id = u.id
                WHERE %s OR u.status NOT IN ('disabled', 'archived')
                ORDER BY u.email
                """,
                (include_inactive,),
            )
            records = []
            for (
                user_id,
                email,
                display_name,
                status,
                api_access_enabled,
                last_login_at,
                archived_at,
                identity_provider_subject,
                attributes,
                password_hash,
                mfa_required,
                mfa_enabled,
                recovery_codes_remaining,
                is_admin,
                company_ids,
                group_ids,
                direct_company_ids,
                is_msp,
                api_token_count,
                last_api_used_at,
            ) in cursor.fetchall():
                role = (
                    "platform_admin"
                    if is_admin
                    else "msp_operator"
                    if is_msp or (attributes or {}).get("role") == "msp_operator"
                    else "client_reader"
                )
                record = {
                    "id": str(user_id),
                    "email": email,
                    "displayName": display_name or email.split("@", 1)[0],
                    "status": status,
                    "role": role,
                    "companyIds": ["*"] if is_admin else list(company_ids or []),
                    "directCompanyIds": (["*"] if is_admin else list(direct_company_ids or [])),
                    "groupIds": list(group_ids or []),
                    "apiAccessEnabled": bool(api_access_enabled),
                    "mfaRequired": bool(mfa_required),
                    "mfaEnabled": bool(mfa_enabled),
                    "mfaRecoveryCodesRemaining": int(recovery_codes_remaining or 0),
                    "apiTokenCount": int(api_token_count or 0),
                    "lastLoginAt": self._timestamp(last_login_at) or None,
                    "lastApiUsedAt": self._timestamp(last_api_used_at) or None,
                    "archivedAt": self._timestamp(archived_at) or None,
                    "authSource": (
                        "entra"
                        if identity_provider_subject
                        else "local"
                        if password_hash
                        else "none"
                    ),
                    "accountType": (attributes or {}).get(
                        "accountType", "root" if role != "client_reader" else "customer"
                    ),
                }
                if include_credentials and password_hash:
                    record["passwordHash"] = password_hash
                records.append(record)
            return records

    def authenticate(self, email: str, password: str) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, lac.password_hash
                FROM users u
                JOIN local_auth_credentials lac ON lac.user_id = u.id
                WHERE u.email = %s AND u.status = 'active'
                """,
                (email,),
            )
            row = cursor.fetchone()
        password_hash = row[1] if row else _DUMMY_PASSWORD_HASH
        password_matches = verify_password(password, password_hash)
        if not row or not password_matches:
            return None
        user_id = str(row[0])
        return next((item for item in self.list_users() if item["id"] == user_id), None)

    def reserve_local_login_attempt(
        self,
        identifier_hash: str,
        requester_hash: str,
        *,
        source_limit: int = 20,
        source_window_seconds: int = 900,
        identifier_limit: int = 5,
        identifier_window_seconds: int = 900,
        pending_ttl_seconds: int = 120,
        audit_window_seconds: int = 300,
    ) -> dict:
        """Reserve local-login capacity across every PostgreSQL-backed replica."""

        source_limit = max(1, source_limit)
        identifier_limit = max(1, identifier_limit)
        source_window_seconds = max(1, source_window_seconds)
        identifier_window_seconds = max(1, identifier_window_seconds)
        pending_ttl_seconds = max(1, pending_ttl_seconds)
        audit_window_seconds = max(1, audit_window_seconds)
        attempt_id = str(uuid.uuid4())
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM auth_login_attempts
                WHERE id IN (
                    SELECT id
                    FROM auth_login_attempts
                    WHERE created_at < now() - interval '30 days'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 500
                )
                """
            )
            lock_keys = sorted(
                (
                    f"auth-login:identifier:{identifier_hash}",
                    f"auth-login:requester:{requester_hash}",
                )
            )
            for lock_key in lock_keys:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_key,),
                )
            cursor.execute(
                """
                SELECT outcome, created_at, identifier_hash, requester_hash,
                       identifier_cleared_at, now()
                FROM auth_login_attempts
                WHERE outcome IN ('pending', 'password_failed')
                  AND (
                    (
                        identifier_hash = %s
                        AND created_at >= now() - make_interval(secs => %s)
                    )
                    OR
                    (
                        requester_hash = %s
                        AND created_at >= now() - make_interval(secs => %s)
                    )
                  )
                """,
                (
                    identifier_hash,
                    max(identifier_window_seconds, pending_ttl_seconds),
                    requester_hash,
                    max(source_window_seconds, pending_ttl_seconds),
                ),
            )
            rows = cursor.fetchall()
            now = rows[0][5] if rows else None
            if now is None:
                cursor.execute("SELECT now()")
                now = cursor.fetchone()[0]

            source_expirations: list[datetime] = []
            identifier_expirations: list[datetime] = []
            for outcome, created_at, stored_identifier, stored_requester, cleared_at, _ in rows:
                pending = outcome == "pending"
                if stored_requester == requester_hash:
                    source_expiration = created_at + timedelta(
                        seconds=pending_ttl_seconds if pending else source_window_seconds
                    )
                    if source_expiration > now:
                        source_expirations.append(source_expiration)
                if stored_identifier == identifier_hash and (pending or not cleared_at):
                    identifier_expiration = created_at + timedelta(
                        seconds=(pending_ttl_seconds if pending else identifier_window_seconds)
                    )
                    if identifier_expiration > now:
                        identifier_expirations.append(identifier_expiration)
            source_expirations.sort()
            identifier_expirations.sort()
            source_blocked = len(source_expirations) >= source_limit
            identifier_blocked = len(identifier_expirations) >= identifier_limit
            if source_blocked or identifier_blocked:
                limited_by = "source" if source_blocked else "identifier"
                expirations = source_expirations if source_blocked else identifier_expirations
                limit = source_limit if source_blocked else identifier_limit
                release_at = expirations[max(0, len(expirations) - limit)]
                retry_after = max(1, int((release_at - now).total_seconds() + 0.999))
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM auth_login_attempts
                        WHERE outcome = 'throttled'
                          AND throttle_reason = %s
                          AND created_at >= now() - make_interval(secs => %s)
                          AND (
                            (%s = 'source' AND requester_hash = %s)
                            OR
                            (%s = 'identifier' AND identifier_hash = %s)
                          )
                    )
                    """,
                    (
                        limited_by,
                        audit_window_seconds,
                        limited_by,
                        requester_hash,
                        limited_by,
                        identifier_hash,
                    ),
                )
                recent_audit = bool(cursor.fetchone()[0])
                if not recent_audit:
                    cursor.execute(
                        """
                        INSERT INTO auth_login_attempts (
                            id, identifier_hash, requester_hash, outcome,
                            throttle_reason, completed_at
                        ) VALUES (%s::uuid, %s, %s, 'throttled', %s, now())
                        """,
                        (
                            attempt_id,
                            identifier_hash,
                            requester_hash,
                            limited_by,
                        ),
                    )
                return {
                    "allowed": False,
                    "attemptId": None,
                    "retryAfterSeconds": retry_after,
                    "auditRequired": not recent_audit,
                }

            cursor.execute(
                """
                INSERT INTO auth_login_attempts (
                    id, identifier_hash, requester_hash, outcome
                ) VALUES (%s::uuid, %s, %s, 'pending')
                """,
                (attempt_id, identifier_hash, requester_hash),
            )
            return {
                "allowed": True,
                "attemptId": attempt_id,
                "retryAfterSeconds": 0,
                "auditRequired": False,
            }

    def finish_local_login_attempt(self, attempt_id: str, outcome: str) -> bool:
        """Finalize a PostgreSQL login reservation exactly once."""

        if outcome not in {"password_failed", "password_verified"}:
            raise ValueError("Unsupported local-login attempt outcome")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auth_login_attempts
                SET outcome = %s, completed_at = now()
                WHERE id = %s::uuid AND outcome = 'pending'
                """,
                (outcome, attempt_id),
            )
            return cursor.rowcount == 1

    def clear_local_login_failures(self, identifier_hash: str) -> int:
        """Clear identifier failures after a governed recovery or account update."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auth_login_attempts
                SET identifier_cleared_at = COALESCE(identifier_cleared_at, now())
                WHERE identifier_hash = %s
                  AND outcome = 'password_failed'
                  AND identifier_cleared_at IS NULL
                """,
                (identifier_hash,),
            )
            return cursor.rowcount

    def complete_local_login(self, identifier_hash: str) -> int:
        """Record full authentication while preserving the broader source bucket."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auth_login_attempts
                SET identifier_cleared_at = COALESCE(identifier_cleared_at, now())
                WHERE identifier_hash = %s
                  AND outcome = 'password_failed'
                  AND identifier_cleared_at IS NULL
                """,
                (identifier_hash,),
            )
            cleared = cursor.rowcount
            cursor.execute(
                """
                UPDATE auth_login_attempts
                SET outcome = 'succeeded'
                WHERE identifier_hash = %s
                  AND outcome = 'password_verified'
                  AND created_at >= now() - interval '1 day'
                """,
                (identifier_hash,),
            )
            return cleared

    def create_user(self, user: dict, password: str, actor_id: str | None = None) -> dict:
        user_uuid = canonical_uuid("user", user["id"])
        attributes = {
            "legacyId": user.get("id"),
            "role": user.get("role"),
            "accountType": user.get("accountType", "customer"),
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (
                    id, email, display_name, status, api_access_enabled, attributes, updated_at
                )
                VALUES (%s::uuid, %s, %s, 'active', false, %s::jsonb, now())
                """,
                (
                    user_uuid,
                    user["email"],
                    user.get("displayName") or user["email"].split("@", 1)[0],
                    json.dumps(attributes),
                ),
            )
            cursor.execute(
                "INSERT INTO local_auth_credentials (user_id, password_hash) VALUES (%s::uuid, %s)",
                (user_uuid, hash_password(password)),
            )
            if user["role"] == "platform_admin":
                cursor.execute(
                    "INSERT INTO user_platform_roles (user_id, role) VALUES (%s::uuid, 'platform_admin')",
                    (user_uuid,),
                )
            else:
                database_role = (
                    "msp_operator" if user["role"] == "msp_operator" else "customer_reader"
                )
                for company_slug in user.get("companyIds", []):
                    cursor.execute(
                        """
                        INSERT INTO user_company_roles (user_id, company_id, role)
                        SELECT %s::uuid, id, %s FROM companies WHERE slug = %s
                        ON CONFLICT DO NOTHING
                        """,
                        (user_uuid, database_role, company_slug),
                    )
            for group_slug in user.get("groupIds", []):
                cursor.execute(
                    """
                    INSERT INTO user_access_groups (user_id, access_group_id)
                    SELECT %s::uuid, id FROM access_groups WHERE slug = %s
                    ON CONFLICT DO NOTHING
                    """,
                    (user_uuid, group_slug),
                )
            stored = {**deepcopy(user), "id": user_uuid}
            self._insert_audit(cursor, None, actor_id, "user", user_uuid, "created", None, stored)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return next(item for item in self.list_users() if item["id"] == user_uuid)

    def update_user(
        self, user_id: str, changes: dict, actor_id: str | None = None, *, reason: str = ""
    ) -> dict | None:
        before = next(
            (item for item in self.list_users(include_inactive=True) if item["id"] == user_id),
            None,
        )
        if not before:
            return None
        role = changes.get("role", before["role"])
        direct_company_ids = changes.get(
            "directCompanyIds", before.get("directCompanyIds", before["companyIds"])
        )
        group_ids = changes.get("groupIds", before.get("groupIds", []))
        account_type = changes.get("accountType", before.get("accountType", "customer"))
        after = {
            **before,
            **deepcopy(changes),
            "directCompanyIds": list(direct_company_ids),
            "groupIds": list(group_ids),
            "accountType": account_type,
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET email = %s, display_name = %s, api_access_enabled = %s,
                    attributes = attributes || %s::jsonb, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    after["email"],
                    after.get("displayName") or after["email"].split("@", 1)[0],
                    bool(after.get("apiAccessEnabled")),
                    json.dumps({"role": role, "accountType": account_type}),
                    user_id,
                ),
            )
            cursor.execute("DELETE FROM user_platform_roles WHERE user_id = %s::uuid", (user_id,))
            cursor.execute("DELETE FROM user_company_roles WHERE user_id = %s::uuid", (user_id,))
            cursor.execute("DELETE FROM user_access_groups WHERE user_id = %s::uuid", (user_id,))
            if "mfaRequired" in changes:
                cursor.execute(
                    "UPDATE local_auth_credentials SET mfa_required = %s, updated_at = now() WHERE user_id = %s::uuid",
                    (bool(changes["mfaRequired"]), user_id),
                )
            if role == "platform_admin":
                cursor.execute(
                    """
                    INSERT INTO user_platform_roles (user_id, role)
                    VALUES (%s::uuid, 'platform_admin')
                    """,
                    (user_id,),
                )
            else:
                database_role = "msp_operator" if role == "msp_operator" else "customer_reader"
                for company_slug in direct_company_ids:
                    cursor.execute(
                        """
                        INSERT INTO user_company_roles (user_id, company_id, role)
                        SELECT %s::uuid, id, %s FROM companies WHERE slug = %s
                        ON CONFLICT DO NOTHING
                        """,
                        (user_id, database_role, company_slug),
                    )
                for group_slug in group_ids:
                    cursor.execute(
                        """
                        INSERT INTO user_access_groups (user_id, access_group_id)
                        SELECT %s::uuid, id FROM access_groups WHERE slug = %s
                        ON CONFLICT DO NOTHING
                        """,
                        (user_id, group_slug),
                    )
            company_id = next((item for item in after.get("companyIds", []) if item != "*"), None)
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "user",
                user_id,
                "updated",
                before,
                after,
                reason=reason,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return next(
            (item for item in self.list_users(include_inactive=True) if item["id"] == user_id),
            None,
        )

    def set_user_password(
        self,
        user_id: str,
        password: str,
        actor_id: str | None = None,
        *,
        action: str = "password_reset",
    ) -> bool:
        """Replace a local credential and record the supplied audit action."""

        before = next(
            (item for item in self.list_users(include_inactive=True) if item["id"] == user_id),
            None,
        )
        if not before:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO local_auth_credentials (user_id, password_hash, updated_at)
                VALUES (%s::uuid, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET password_hash = EXCLUDED.password_hash, updated_at = now()
                """,
                (user_id, hash_password(password)),
            )
            cursor.execute("UPDATE users SET updated_at = now() WHERE id = %s::uuid", (user_id,))
            self._insert_audit(
                cursor,
                next((item for item in before.get("companyIds", []) if item != "*"), None),
                actor_id,
                "user",
                user_id,
                action,
                None,
                {"passwordChanged": True},
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    def record_user_login(self, user_id: str) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET last_login_at = now(), updated_at = now() WHERE id = %s::uuid",
                (user_id,),
            )

    def get_mfa_credential(self, user_id: str) -> dict | None:
        """Return encrypted MFA material for an internal authentication flow."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, method, status, encrypted_secret, secret_nonce,
                       key_version, last_accepted_counter, enabled_at, updated_at
                FROM user_mfa_credentials WHERE user_id = %s::uuid
                """,
                (user_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "userId": str(row[0]),
            "method": row[1],
            "status": row[2],
            "encryptedSecret": row[3],
            "secretNonce": row[4],
            "keyVersion": row[5],
            "lastAcceptedCounter": row[6],
            "enabledAt": self._timestamp(row[7]) or None,
            "updatedAt": self._timestamp(row[8]),
        }

    def save_mfa_enrollment(
        self,
        user_id: str,
        encrypted_secret: str,
        nonce: str,
        actor_id: str | None = None,
    ) -> dict:
        """Create or replace a pending TOTP enrollment."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_mfa_credentials (
                    user_id, status, encrypted_secret, secret_nonce, key_version, updated_at
                ) VALUES (%s::uuid, 'pending', %s, %s, 1, now())
                ON CONFLICT (user_id) DO UPDATE
                SET status = 'pending', encrypted_secret = EXCLUDED.encrypted_secret,
                    secret_nonce = EXCLUDED.secret_nonce, key_version = 1,
                    last_accepted_counter = NULL, enabled_at = NULL, updated_at = now()
                """,
                (user_id, encrypted_secret, nonce),
            )
            cursor.execute(
                "DELETE FROM user_mfa_recovery_codes WHERE user_id = %s::uuid", (user_id,)
            )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "authentication",
                user_id,
                "mfa_enrollment_started",
                None,
                {"method": "totp", "status": "pending"},
            )
        credential = self.get_mfa_credential(user_id)
        if not credential:
            raise RuntimeError("MFA enrollment could not be persisted")
        return credential

    def enable_mfa(
        self,
        user_id: str,
        counter: int,
        recovery_hashes: list[str],
        actor_id: str | None = None,
    ) -> bool:
        """Activate a verified TOTP credential and replace its recovery codes."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE user_mfa_credentials
                SET status = 'enabled', last_accepted_counter = %s,
                    enabled_at = now(), updated_at = now()
                WHERE user_id = %s::uuid AND status = 'pending'
                """,
                (counter, user_id),
            )
            if cursor.rowcount != 1:
                return False
            cursor.execute(
                "DELETE FROM user_mfa_recovery_codes WHERE user_id = %s::uuid", (user_id,)
            )
            cursor.executemany(
                "INSERT INTO user_mfa_recovery_codes (user_id, code_hash) VALUES (%s::uuid, %s)",
                [(user_id, code_hash) for code_hash in recovery_hashes],
            )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "authentication",
                user_id,
                "mfa_enabled",
                None,
                {"method": "totp", "recoveryCodeCount": len(recovery_hashes)},
            )
        return True

    def accept_mfa_counter(self, user_id: str, counter: int) -> bool:
        """Atomically record a newer accepted TOTP time step."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE user_mfa_credentials
                SET last_accepted_counter = %s, updated_at = now()
                WHERE user_id = %s::uuid AND status = 'enabled'
                  AND (last_accepted_counter IS NULL OR last_accepted_counter < %s)
                """,
                (counter, user_id, counter),
            )
            return cursor.rowcount == 1

    def consume_recovery_code(self, user_id: str, code: str) -> bool:
        """Redeem one matching recovery code and make it unusable thereafter."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, code_hash FROM user_mfa_recovery_codes
                WHERE user_id = %s::uuid AND used_at IS NULL FOR UPDATE
                """,
                (user_id,),
            )
            for code_id, code_hash in cursor.fetchall():
                if verify_password(code.upper(), code_hash):
                    cursor.execute(
                        "UPDATE user_mfa_recovery_codes SET used_at = now() WHERE id = %s::uuid AND used_at IS NULL",
                        (code_id,),
                    )
                    return cursor.rowcount == 1
        return False

    def replace_recovery_codes(
        self, user_id: str, recovery_hashes: list[str], actor_id: str | None = None
    ) -> bool:
        """Replace recovery codes after a freshly verified MFA challenge."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM user_mfa_credentials WHERE user_id = %s::uuid AND status = 'enabled'",
                (user_id,),
            )
            if not cursor.fetchone():
                return False
            cursor.execute(
                "DELETE FROM user_mfa_recovery_codes WHERE user_id = %s::uuid", (user_id,)
            )
            cursor.executemany(
                "INSERT INTO user_mfa_recovery_codes (user_id, code_hash) VALUES (%s::uuid, %s)",
                [(user_id, code_hash) for code_hash in recovery_hashes],
            )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "authentication",
                user_id,
                "mfa_recovery_codes_regenerated",
                None,
                {"recoveryCodeCount": len(recovery_hashes)},
            )
        return True

    def disable_mfa(
        self,
        user_id: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
        action: str = "mfa_disabled",
        metadata: dict | None = None,
    ) -> bool:
        """Remove TOTP and recovery material while preserving its audit trail."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM user_mfa_credentials WHERE user_id = %s::uuid", (user_id,))
            if cursor.rowcount != 1:
                return False
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "authentication",
                user_id,
                action,
                {"status": "enabled"},
                {"status": "disabled"},
                reason=reason,
                metadata=metadata,
            )
        return True

    def create_login_challenge(self, challenge: dict) -> None:
        """Persist a password-verified, short-lived MFA login transaction."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"auth-login-challenge:{challenge['userId']}",),
            )
            cursor.execute(
                """
                DELETE FROM auth_login_challenges
                WHERE expires_at < now() - interval '1 day'
                   OR consumed_at < now() - interval '1 day'
                """
            )
            cursor.execute(
                """
                UPDATE auth_login_challenges
                SET consumed_at = now()
                WHERE token_hash IN (
                    SELECT token_hash
                    FROM auth_login_challenges
                    WHERE user_id = %s::uuid
                      AND consumed_at IS NULL
                      AND expires_at > now()
                    ORDER BY created_at DESC
                    OFFSET 4
                )
                """,
                (challenge["userId"],),
            )
            cursor.execute(
                """
                INSERT INTO auth_login_challenges (
                    token_hash, user_id, purpose, attempts, max_attempts, expires_at
                ) VALUES (%s, %s::uuid, %s, 0, %s, %s::timestamptz)
                """,
                (
                    challenge["tokenHash"],
                    challenge["userId"],
                    challenge["purpose"],
                    challenge.get("maxAttempts", 5),
                    challenge["expiresAt"],
                ),
            )

    def get_login_challenge(self, token_hash: str) -> dict | None:
        """Return a live, unconsumed login challenge."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT token_hash, user_id, purpose, attempts, max_attempts, expires_at
                FROM auth_login_challenges
                WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()
                  AND attempts < max_attempts
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "tokenHash": row[0],
            "userId": str(row[1]),
            "purpose": row[2],
            "attempts": row[3],
            "maxAttempts": row[4],
            "expiresAt": self._timestamp(row[5]),
        }

    def record_login_challenge_attempt(self, token_hash: str) -> int:
        """Increment a live challenge, or return the token's current attempt count."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auth_login_challenges
                SET attempts = attempts + 1
                WHERE token_hash = %s
                  AND consumed_at IS NULL
                  AND expires_at > now()
                  AND attempts < max_attempts
                RETURNING attempts
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            if row:
                return int(row[0])
            cursor.execute(
                "SELECT attempts FROM auth_login_challenges WHERE token_hash = %s",
                (token_hash,),
            )
            existing = cursor.fetchone()
            return int(existing[0]) if existing else 0

    def consume_login_challenge(self, token_hash: str) -> bool:
        """Mark a successful login transaction as single-use."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE auth_login_challenges
                SET consumed_at = now()
                WHERE token_hash = %s
                  AND consumed_at IS NULL
                  AND expires_at > now()
                  AND attempts < max_attempts
                """,
                (token_hash,),
            )
            return cursor.rowcount == 1

    def create_session(self, token_hash: str, user_id: str, expires_at: str) -> None:
        """Persist a hashed browser session token."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (%s, %s::uuid, %s::timestamptz)",
                (token_hash, user_id, expires_at),
            )

    def authenticate_session(self, token_hash: str) -> str | None:
        """Resolve a live hashed browser session to its user identifier."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE user_sessions session
                SET last_seen_at = now()
                FROM users owner
                WHERE session.token_hash = %s AND session.user_id = owner.id
                  AND session.revoked_at IS NULL AND session.expires_at > now()
                  AND owner.status = 'active'
                RETURNING session.user_id
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None

    def revoke_session(self, token_hash: str) -> bool:
        """Revoke one browser session by its stored hash."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE user_sessions SET revoked_at = COALESCE(revoked_at, now()) WHERE token_hash = %s",
                (token_hash,),
            )
            return cursor.rowcount == 1

    def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke every live browser session belonging to one user."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s::uuid AND revoked_at IS NULL",
                (user_id,),
            )
            return cursor.rowcount

    def create_password_reset(
        self,
        reset: dict,
        *,
        identifier_limit: int = 3,
        requester_limit: int = 20,
    ) -> dict | None:
        """Rate-limit and persist one recovery request across all API replicas."""

        reset_id = str(uuid.uuid4())
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                "DELETE FROM auth_password_resets WHERE created_at < now() - interval '30 days'"
            )
            lock_keys = sorted(
                (
                    f"identifier:{reset['identifierHash']}",
                    f"requester:{reset['requesterHash']}",
                )
            )
            for lock_key in lock_keys:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_key,),
                )
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE identifier_hash = %s AND created_at >= now() - interval '15 minutes'
                    ),
                    COUNT(*) FILTER (
                        WHERE requester_hash = %s AND created_at >= now() - interval '1 hour'
                    )
                FROM auth_password_resets
                WHERE (identifier_hash = %s AND created_at >= now() - interval '15 minutes')
                   OR (requester_hash = %s AND created_at >= now() - interval '1 hour')
                """,
                (
                    reset["identifierHash"],
                    reset["requesterHash"],
                    reset["identifierHash"],
                    reset["requesterHash"],
                ),
            )
            identifier_count, requester_count = cursor.fetchone()
            if identifier_count >= identifier_limit or requester_count >= requester_limit:
                return None
            cursor.execute(
                """
                INSERT INTO auth_password_resets (
                    id, user_id, token_hash, identifier_hash, requester_hash, expires_at
                ) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s::timestamptz)
                """,
                (
                    reset_id,
                    reset.get("userId"),
                    reset["tokenHash"],
                    reset["identifierHash"],
                    reset["requesterHash"],
                    reset["expiresAt"],
                ),
            )
            if reset.get("userId"):
                self._insert_audit(
                    cursor,
                    None,
                    reset["userId"],
                    "authentication",
                    reset["userId"],
                    "password_reset_requested",
                    None,
                    {"expiresAt": reset["expiresAt"]},
                    metadata={"delivery": "email"},
                )
        return {"id": reset_id, "createdAt": utc_now(), **deepcopy(reset)}

    def get_password_reset(self, token_hash: str) -> dict | None:
        """Load one live reset token for validation without exposing credentials."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reset.id, reset.user_id, owner.email::text, reset.expires_at,
                       reset.created_at
                FROM auth_password_resets reset
                JOIN users owner ON owner.id = reset.user_id
                JOIN local_auth_credentials credential ON credential.user_id = owner.id
                WHERE reset.token_hash = %s AND reset.consumed_at IS NULL
                  AND reset.invalidated_at IS NULL AND reset.expires_at > now()
                  AND owner.status = 'active'
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": str(row[0]),
            "userId": str(row[1]),
            "email": row[2],
            "expiresAt": self._timestamp(row[3]),
            "createdAt": self._timestamp(row[4]),
        }

    def complete_password_reset(
        self,
        token_hash: str,
        password: str,
        *,
        revoke_api_tokens: bool = True,
    ) -> dict | None:
        """Atomically consume recovery, rotate password, and revoke credentials."""

        result: dict | None = None
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                SELECT reset.id, owner.id, owner.email::text, credential.password_hash
                FROM auth_password_resets reset
                JOIN users owner ON owner.id = reset.user_id
                JOIN local_auth_credentials credential ON credential.user_id = owner.id
                WHERE reset.token_hash = %s AND reset.consumed_at IS NULL
                  AND reset.invalidated_at IS NULL AND reset.expires_at > now()
                  AND owner.status = 'active'
                FOR UPDATE OF reset, credential
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            reset_id, user_id, email, current_hash = row
            if verify_password(password, current_hash):
                raise ValueError("New password must be different from the current password")
            cursor.execute(
                """
                UPDATE local_auth_credentials
                SET password_hash = %s, updated_at = now()
                WHERE user_id = %s::uuid
                """,
                (hash_password(password), user_id),
            )
            cursor.execute(
                "UPDATE users SET updated_at = now() WHERE id = %s::uuid",
                (user_id,),
            )
            cursor.execute(
                "UPDATE auth_password_resets SET consumed_at = now() WHERE id = %s::uuid",
                (reset_id,),
            )
            cursor.execute(
                """
                UPDATE auth_password_resets SET invalidated_at = now()
                WHERE user_id = %s::uuid AND id <> %s::uuid
                  AND consumed_at IS NULL AND invalidated_at IS NULL
                """,
                (user_id, reset_id),
            )
            cursor.execute(
                """
                UPDATE user_sessions SET revoked_at = now()
                WHERE user_id = %s::uuid AND revoked_at IS NULL
                """,
                (user_id,),
            )
            revoked_sessions = cursor.rowcount
            revoked_tokens = 0
            if revoke_api_tokens:
                cursor.execute(
                    """
                    UPDATE user_api_tokens
                    SET revoked_at = now(), revoked_by_user_id = %s::uuid
                    WHERE user_id = %s::uuid AND revoked_at IS NULL
                    """,
                    (user_id, user_id),
                )
                revoked_tokens = cursor.rowcount
            self._insert_audit(
                cursor,
                None,
                str(user_id),
                "authentication",
                str(user_id),
                "password_reset_completed",
                None,
                {"passwordChanged": True},
                metadata={
                    "revokedSessions": revoked_sessions,
                    "revokedApiTokens": revoked_tokens,
                    "mfaPreserved": True,
                },
            )
            result = {
                "userId": str(user_id),
                "email": email,
                "revokedSessions": revoked_sessions,
                "revokedApiTokens": revoked_tokens,
            }
        self._refresh_state_mirror()
        self.save_state(self.state)
        return result

    def list_api_tokens(self, user_id: str) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, user_id, name, token_prefix, scopes, company_ids,
                       expires_at, last_used_at, revoked_at, created_at, created_by_user_id,
                       revoked_by_user_id
                FROM user_api_tokens
                WHERE user_id = %s::uuid
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            return [
                {
                    "id": str(token_id),
                    "userId": str(owner_id),
                    "name": name,
                    "tokenPrefix": prefix,
                    "scopes": list(scopes or []),
                    "companyIds": list(company_ids or []),
                    "expiresAt": self._timestamp(expires_at),
                    "lastUsedAt": self._timestamp(last_used_at) or None,
                    "revokedAt": self._timestamp(revoked_at) or None,
                    "createdAt": self._timestamp(created_at),
                    "createdByUserId": str(created_by) if created_by else None,
                    "revokedByUserId": str(revoked_by) if revoked_by else None,
                }
                for (
                    token_id,
                    owner_id,
                    name,
                    prefix,
                    scopes,
                    company_ids,
                    expires_at,
                    last_used_at,
                    revoked_at,
                    created_at,
                    created_by,
                    revoked_by,
                ) in cursor.fetchall()
            ]

    def create_api_token(self, token: dict, actor_id: str | None = None) -> dict:
        token_id = canonical_uuid("api_token", token["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_api_tokens (
                    id, user_id, name, token_prefix, token_hash, scopes, company_ids,
                    expires_at, created_by_user_id
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s, %s::text[], %s::text[],
                    %s::timestamptz, %s::uuid
                )
                """,
                (
                    token_id,
                    token["userId"],
                    token["name"],
                    token["tokenPrefix"],
                    token["tokenHash"],
                    token["scopes"],
                    token.get("companyIds", []),
                    token["expiresAt"],
                    actor_id,
                ),
            )
            public = {
                **{key: value for key, value in token.items() if key != "tokenHash"},
                "id": token_id,
                "createdAt": utc_now(),
                "lastUsedAt": None,
                "revokedAt": None,
            }
            self._insert_audit(
                cursor,
                next(iter(token.get("companyIds", [])), None),
                actor_id,
                "api_token",
                token_id,
                "created",
                None,
                public,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return public

    def authenticate_api_token(self, token_hash: str) -> dict | None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT token.id, token.user_id, token.name, token.token_prefix,
                       token.scopes, token.company_ids, token.expires_at,
                       token.last_used_at, token.created_at
                FROM user_api_tokens token
                JOIN users owner ON owner.id = token.user_id
                WHERE token.token_hash = %s
                  AND token.revoked_at IS NULL
                  AND token.expires_at > now()
                  AND owner.status = 'active'
                  AND owner.api_access_enabled = true
                FOR UPDATE OF token
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE user_api_tokens SET last_used_at = now() WHERE id = %s::uuid",
                (row[0],),
            )
        user = next((item for item in self.list_users() if item["id"] == str(row[1])), None)
        if not user:
            return None
        return {
            "user": user,
            "token": {
                "id": str(row[0]),
                "userId": str(row[1]),
                "name": row[2],
                "tokenPrefix": row[3],
                "scopes": list(row[4] or []),
                "companyIds": list(row[5] or []),
                "expiresAt": self._timestamp(row[6]),
                "lastUsedAt": utc_now(),
                "revokedAt": None,
                "createdAt": self._timestamp(row[8]),
            },
        }

    def revoke_api_token(
        self, token_id: str, actor_id: str | None = None, *, reason: str = ""
    ) -> dict | None:
        current = next(
            (item for item in self.list_api_tokens_for_all_users() if item["id"] == token_id),
            None,
        )
        if not current:
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE user_api_tokens
                SET revoked_at = COALESCE(revoked_at, now()), revoked_by_user_id = %s::uuid
                WHERE id = %s::uuid
                """,
                (actor_id, token_id),
            )
            after = {**current, "revokedAt": current.get("revokedAt") or utc_now()}
            self._insert_audit(
                cursor,
                next(iter(current.get("companyIds", [])), None),
                actor_id,
                "api_token",
                token_id,
                "revoked",
                current,
                after,
                reason=reason,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return after

    def list_api_tokens_for_all_users(self) -> list[dict]:
        return [
            token
            for user in self.list_users(include_inactive=True)
            for token in self.list_api_tokens(user["id"])
        ]

    def revoke_user_api_tokens(self, user_id: str, actor_id: str | None = None) -> int:
        tokens = [item for item in self.list_api_tokens(user_id) if not item.get("revokedAt")]
        for token in tokens:
            self.revoke_api_token(token["id"], actor_id, reason="User access disabled")
        return len(tokens)

    def set_user_status(
        self,
        user_id: str,
        status: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> bool:
        active = next(
            (item for item in self.list_users(include_inactive=True) if item["id"] == user_id),
            None,
        )
        before = active or {"id": user_id, "status": "disabled"}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET status = %s,
                    archived_at = CASE WHEN %s = 'archived' THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (status, status, user_id),
            )
            if cursor.rowcount != 1:
                return False
            after = {**before, "status": status}
            company_id = before.get("companyIds", [None])[0] if before.get("companyIds") else None
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "user",
                user_id,
                "status_changed",
                before,
                after,
                reason=reason,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    def list_contacts(self, company_id: str | None = None) -> list[dict]:
        parameters: list[Any] = []
        where = ""
        if company_id:
            where = "WHERE c.slug = %s"
            parameters.append(company_id)
        users = {item["id"]: item for item in self.list_users()}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, email::text, status FROM users")
            portal_accounts = {
                str(user_id): {
                    "id": str(user_id),
                    "email": email,
                    "role": users.get(str(user_id), {}).get("role", "client_reader"),
                    "status": status,
                }
                for user_id, email, status in cursor.fetchall()
            }
            query = sql.SQL(
                """
                SELECT contact.id, c.slug, contact.linked_user_id, contact.display_name,
                       contact.first_name, contact.last_name, contact.primary_email::text,
                       contact.phone, contact.mobile, contact.job_title, contact.department,
                       contact.location, contact.timezone, contact.manager_contact_id,
                       contact.status, contact.source, contact.sync_status,
                       contact.last_seen_at, contact.last_synced_at, contact.attributes,
                       contact.created_at, contact.updated_at,
                       COUNT(responsibility.id) FILTER (WHERE responsibility.effective_until IS NULL)
                FROM contacts contact
                JOIN companies c ON c.id = contact.company_id
                LEFT JOIN contact_responsibilities responsibility ON responsibility.contact_id = contact.id
                {where_clause}
                GROUP BY contact.id, c.slug
                ORDER BY contact.normalized_name, contact.id
                """
            ).format(where_clause=sql.SQL(where))
            cursor.execute(
                query,
                tuple(parameters),
            )
            records = []
            for row in cursor.fetchall():
                linked_user_id = str(row[2]) if row[2] else None
                linked_user = portal_accounts.get(linked_user_id) if linked_user_id else None
                records.append(
                    {
                        "id": str(row[0]),
                        "companyId": row[1],
                        "linkedUserId": linked_user_id,
                        "displayName": row[3],
                        "firstName": row[4] or "",
                        "lastName": row[5] or "",
                        "email": row[6] or "",
                        "phone": row[7] or "",
                        "mobile": row[8] or "",
                        "jobTitle": row[9] or "",
                        "department": row[10] or "",
                        "location": row[11] or "",
                        "timezone": row[12] or "",
                        "managerContactId": str(row[13]) if row[13] else None,
                        "status": row[14],
                        "source": row[15],
                        "syncStatus": row[16],
                        "lastSeen": self._timestamp(row[17]) or None,
                        "lastSynced": self._timestamp(row[18]) or None,
                        "attributes": row[19] or {},
                        "createdAt": self._timestamp(row[20]),
                        "updatedAt": self._timestamp(row[21]),
                        "responsibilityCount": int(row[22] or 0),
                        "portalUser": (
                            {
                                "id": linked_user["id"],
                                "email": linked_user["email"],
                                "role": linked_user["role"],
                                "status": linked_user["status"],
                            }
                            if linked_user
                            else None
                        ),
                    }
                )
            return records

    def get_contact(self, contact_id: str) -> dict | None:
        try:
            parsed = str(uuid.UUID(contact_id))
        except (ValueError, TypeError):
            return None
        return next((item for item in self.list_contacts() if item["id"] == parsed), None)

    def create_contact(self, contact: dict, actor_id: str | None = None) -> dict:
        contact_uuid = canonical_uuid("contact", contact["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM companies WHERE slug = %s AND status <> 'inactive'",
                (contact["companyId"],),
            )
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            cursor.execute(
                """
                INSERT INTO contacts (
                    id, company_id, linked_user_id, display_name, normalized_name,
                    first_name, last_name, primary_email, phone, mobile, job_title,
                    department, location, timezone, manager_contact_id, status,
                    source, sync_status, last_seen_at, last_synced_at, attributes,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::uuid, %s, %s, %s, %s::timestamptz,
                    %s::timestamptz, %s::jsonb, %s::uuid, %s::uuid, now(), now()
                )
                """,
                (
                    contact_uuid,
                    str(company_row[0]),
                    contact.get("linkedUserId"),
                    contact["displayName"],
                    normalized_name(contact["displayName"]),
                    contact.get("firstName") or None,
                    contact.get("lastName") or None,
                    contact.get("email") or None,
                    contact.get("phone") or None,
                    contact.get("mobile") or None,
                    contact.get("jobTitle") or None,
                    contact.get("department") or None,
                    contact.get("location") or None,
                    contact.get("timezone") or None,
                    contact.get("managerContactId") or None,
                    contact.get("status", "active"),
                    contact.get("source", "manual"),
                    contact.get("syncStatus", "not_synced"),
                    contact.get("lastSeen") or None,
                    contact.get("lastSynced") or None,
                    json.dumps(contact.get("attributes") or {}),
                    actor_id,
                    actor_id,
                ),
            )
            stored = {**deepcopy(contact), "id": contact_uuid}
            self._insert_audit(
                cursor,
                contact["companyId"],
                actor_id,
                "contact",
                contact_uuid,
                "created",
                None,
                stored,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        created = self.get_contact(contact_uuid)
        if created is None:
            raise RuntimeError("Created contact could not be reloaded")
        return created

    def update_contact(
        self,
        contact_id: str,
        changes: dict,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> dict | None:
        before = self.get_contact(contact_id)
        if not before:
            return None
        after = {**before, **deepcopy(changes)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contacts SET
                    linked_user_id = %s::uuid, display_name = %s, normalized_name = %s,
                    first_name = %s, last_name = %s, primary_email = %s,
                    phone = %s, mobile = %s, job_title = %s, department = %s,
                    location = %s, timezone = %s, manager_contact_id = %s::uuid,
                    status = %s, source = %s, sync_status = %s,
                    last_seen_at = %s::timestamptz, last_synced_at = %s::timestamptz,
                    attributes = %s::jsonb, updated_by = %s::uuid, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    after.get("linkedUserId") or None,
                    after["displayName"],
                    normalized_name(after["displayName"]),
                    after.get("firstName") or None,
                    after.get("lastName") or None,
                    after.get("email") or None,
                    after.get("phone") or None,
                    after.get("mobile") or None,
                    after.get("jobTitle") or None,
                    after.get("department") or None,
                    after.get("location") or None,
                    after.get("timezone") or None,
                    after.get("managerContactId") or None,
                    after.get("status", "active"),
                    after.get("source", "manual"),
                    after.get("syncStatus", "not_synced"),
                    after.get("lastSeen") or None,
                    after.get("lastSynced") or None,
                    json.dumps(after.get("attributes") or {}),
                    actor_id,
                    contact_id,
                ),
            )
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "contact",
                contact_id,
                "updated",
                before,
                after,
                reason=reason,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_contact(contact_id)

    def list_contact_responsibilities(
        self,
        company_id: str | None = None,
        *,
        contact_id: str | None = None,
        asset_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict]:
        clauses = []
        parameters: list[Any] = []
        if company_id:
            clauses.append("company.slug = %s")
            parameters.append(company_id)
        if contact_id:
            clauses.append("responsibility.contact_id = %s::uuid")
            parameters.append(contact_id)
        if asset_id:
            clauses.append("responsibility.ci_id = %s::uuid")
            parameters.append(asset_id)
        if not include_inactive:
            clauses.append("responsibility.effective_until IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection_factory() as connection, connection.cursor() as cursor:
            query = sql.SQL(
                """
                SELECT responsibility.id, company.slug, responsibility.ci_id,
                       ci.display_name, ci.ci_type, responsibility.contact_id,
                       contact.display_name, contact.primary_email::text,
                       responsibility.responsibility_role, responsibility.is_primary,
                       responsibility.effective_from, responsibility.effective_until,
                       responsibility.escalation_order, responsibility.notes,
                       responsibility.source, responsibility.created_by, responsibility.ended_by
                FROM contact_responsibilities responsibility
                JOIN companies company ON company.id = responsibility.company_id
                JOIN configuration_items ci ON ci.id = responsibility.ci_id
                JOIN contacts contact ON contact.id = responsibility.contact_id
                {where_clause}
                ORDER BY responsibility.effective_until NULLS FIRST,
                         responsibility.responsibility_role, responsibility.escalation_order,
                         contact.display_name
                """
            ).format(where_clause=sql.SQL(where))
            cursor.execute(
                query,
                tuple(parameters),
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "assetId": str(row[2]),
                    "assetName": row[3],
                    "assetType": row[4],
                    "contactId": str(row[5]),
                    "contactName": row[6],
                    "contactEmail": row[7] or "",
                    "role": row[8],
                    "isPrimary": bool(row[9]),
                    "effectiveFrom": self._timestamp(row[10]),
                    "effectiveUntil": self._timestamp(row[11]) or None,
                    "escalationOrder": int(row[12]),
                    "notes": row[13] or "",
                    "source": row[14],
                    "createdBy": str(row[15]) if row[15] else None,
                    "endedBy": str(row[16]) if row[16] else None,
                }
                for row in cursor.fetchall()
            ]

    def replace_asset_responsibilities(
        self,
        asset_id: str,
        assignments: list[dict],
        company_id: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> list[dict]:
        before = self.list_contact_responsibilities(company_id, asset_id=asset_id)
        normalized_before = sorted(
            (
                item["role"],
                item["contactId"],
                bool(item["isPrimary"]),
                int(item["escalationOrder"]),
            )
            for item in before
        )
        normalized_after = sorted(
            (
                item["role"],
                item["contactId"],
                bool(item.get("isPrimary", True)),
                int(item.get("escalationOrder", 1)),
            )
            for item in assignments
        )
        if normalized_before == normalized_after:
            return before
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contact_responsibilities
                SET effective_until = now(), ended_by = %s::uuid
                WHERE ci_id = %s::uuid AND effective_until IS NULL
                """,
                (actor_id, asset_id),
            )
            for item in assignments:
                cursor.execute(
                    """
                    INSERT INTO contact_responsibilities (
                        id, company_id, ci_id, contact_id, responsibility_role,
                        is_primary, effective_from, escalation_order, notes,
                        source, created_by, created_at
                    ) SELECT gen_random_uuid(), company.id, %s::uuid, contact.id, %s,
                             %s, now(), %s, %s, %s, %s::uuid, now()
                      FROM companies company
                      JOIN contacts contact ON contact.company_id = company.id AND contact.id = %s::uuid
                      WHERE company.slug = %s
                    """,
                    (
                        asset_id,
                        item["role"],
                        bool(item.get("isPrimary", True)),
                        int(item.get("escalationOrder", 1)),
                        item.get("notes") or None,
                        item.get("source", "manual"),
                        actor_id,
                        item["contactId"],
                        company_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Choose contacts belonging to this customer")
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "contact_responsibility",
                asset_id,
                "reassigned",
                {"assignments": before},
                {"assignments": assignments},
                reason=reason,
            )
            before_by_contact = {(item["contactId"], item["role"]): item for item in before}
            after_by_contact = {(item["contactId"], item["role"]): item for item in assignments}
            for key in sorted(set(before_by_contact) | set(after_by_contact)):
                if key in before_by_contact and key in after_by_contact:
                    continue
                contact_id, role = key
                assigned = key in after_by_contact
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "contact",
                    contact_id,
                    "responsibility_assigned" if assigned else "responsibility_ended",
                    before_by_contact.get(key),
                    after_by_contact.get(key),
                    reason=reason,
                    metadata={"assetId": asset_id, "responsibilityRole": role},
                )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.list_contact_responsibilities(company_id, asset_id=asset_id)

    def list_access_groups(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ag.slug, ag.name, ag.system, ag.description,
                       ag.membership_mode, ag.membership_rules,
                       ag.owner_user_id,
                       COALESCE(owner.display_name, owner.email::text),
                       ag.revision, ag.updated_at,
                       ARRAY(SELECT c.slug FROM access_group_companies agc
                             JOIN companies c ON c.id = agc.company_id
                             WHERE agc.access_group_id = ag.id AND c.status <> 'inactive'
                             ORDER BY c.name),
                       (SELECT count(*)
                        FROM user_access_groups uag
                        JOIN users assigned ON assigned.id = uag.user_id
                        WHERE uag.access_group_id = ag.id
                          AND assigned.status <> 'archived')
                FROM access_groups ag
                LEFT JOIN users owner ON owner.id = ag.owner_user_id
                ORDER BY ag.system DESC, ag.name
                """
            )
            return [
                {
                    "id": row[0],
                    "name": row[1],
                    "system": bool(row[2]),
                    "description": row[3] or "",
                    "membershipMode": row[4],
                    "membershipRules": deepcopy(row[5] or {}),
                    "ownerUserId": str(row[6]) if row[6] else None,
                    "ownerLabel": "System" if row[2] else row[7] or "Unassigned",
                    "revision": int(row[8]),
                    "updatedAt": self._timestamp(row[9]),
                    "companyIds": list(row[10] or []),
                    "assignedUserCount": int(row[11]),
                }
                for row in cursor.fetchall()
            ]

    def create_access_group(self, group: dict, actor_id: str | None = None) -> dict:
        group_uuid = canonical_uuid("access_group", group["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO access_groups (
                    id, slug, name, system, description, membership_mode,
                    membership_rules, owner_user_id, revision, updated_at
                )
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s::uuid, 1, now())
                """,
                (
                    group_uuid,
                    group["id"],
                    group["name"],
                    bool(group.get("system")),
                    group.get("description", ""),
                    group.get("membershipMode", "manual"),
                    json.dumps(group.get("membershipRules") or {}),
                    group.get("ownerUserId") or actor_id,
                ),
            )
            self._replace_group_companies(cursor, group_uuid, group["companyIds"])
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "access_group",
                group_uuid,
                "created",
                None,
                group,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return next(item for item in self.list_access_groups() if item["id"] == group["id"])

    def update_access_group(
        self, group_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        before = next((item for item in self.list_access_groups() if item["id"] == group_id), None)
        if not before:
            return None
        values = deepcopy(changes)
        expected_revision = values.pop("expectedRevision", before.get("revision", 1))
        if expected_revision != before.get("revision", 1):
            return None
        after = {
            **before,
            **values,
            "revision": before.get("revision", 1) + 1,
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM access_groups WHERE slug = %s", (group_id,))
            group_uuid = str(cursor.fetchone()[0])
            cursor.execute(
                """
                UPDATE access_groups
                SET name = %s, description = %s, membership_mode = %s,
                    membership_rules = %s::jsonb, owner_user_id = %s::uuid,
                    revision = revision + 1, updated_at = now()
                WHERE id = %s::uuid AND revision = %s
                """,
                (
                    after["name"],
                    after.get("description", ""),
                    after.get("membershipMode", "manual"),
                    json.dumps(after.get("membershipRules") or {}),
                    after.get("ownerUserId"),
                    group_uuid,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._replace_group_companies(cursor, group_uuid, after["companyIds"])
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "access_group",
                group_uuid,
                "updated",
                before,
                after,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return next(
            (item for item in self.list_access_groups() if item["id"] == group_id),
            None,
        )

    def delete_access_group(self, group_id: str, actor_id: str | None = None) -> bool:
        before = next((item for item in self.list_access_groups() if item["id"] == group_id), None)
        if not before:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM access_groups WHERE slug = %s", (group_id,))
            group_uuid = str(cursor.fetchone()[0])
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "access_group",
                group_uuid,
                "deleted",
                before,
                None,
            )
            cursor.execute("DELETE FROM access_groups WHERE id = %s::uuid", (group_uuid,))
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    @staticmethod
    def _replace_group_companies(cursor: Any, group_uuid: str, company_ids: list[str]) -> None:
        cursor.execute(
            "DELETE FROM access_group_companies WHERE access_group_id = %s::uuid",
            (group_uuid,),
        )
        for company_slug in company_ids:
            cursor.execute(
                """
                INSERT INTO access_group_companies (access_group_id, company_id)
                SELECT %s::uuid, id FROM companies WHERE slug = %s
                ON CONFLICT DO NOTHING
                """,
                (group_uuid, company_slug),
            )

    def list_assets(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ci.id, c.slug, ci.display_name, ci.ci_type,
                       ci.lifecycle_status, ci.operational_status, ci.attributes,
                       ci.updated_at
                FROM configuration_items ci
                JOIN companies c ON c.id = ci.company_id
                WHERE ci.retired_at IS NULL
                ORDER BY ci.display_name
                """
            )
            assets = [self._asset_from_row(row) for row in cursor.fetchall()]
        assignments = self.list_contact_responsibilities()
        by_asset: dict[str, list[dict]] = {}
        for item in assignments:
            by_asset.setdefault(item["assetId"], []).append(item)
        return [{**asset, "responsibilities": by_asset.get(asset["id"], [])} for asset in assets]

    def get_asset(self, asset_id: str) -> dict | None:
        try:
            parsed_id = str(uuid.UUID(asset_id))
        except (ValueError, TypeError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ci.id, c.slug, ci.display_name, ci.ci_type,
                       ci.lifecycle_status, ci.operational_status, ci.attributes,
                       ci.updated_at
                FROM configuration_items ci
                JOIN companies c ON c.id = ci.company_id
                WHERE ci.id = %s::uuid AND ci.retired_at IS NULL
                """,
                (parsed_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        asset = self._asset_from_row(row)
        asset["responsibilities"] = self.list_contact_responsibilities(
            asset["companyId"], asset_id=asset["id"]
        )
        return asset

    def create_asset(self, asset: dict, actor_id: str | None = None) -> dict:
        asset_uuid = canonical_uuid("configuration_item", asset["id"])
        metadata = deepcopy(asset.get("metadata") or {})
        attributes = self._asset_attributes(asset)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (asset["companyId"],))
            row = cursor.fetchone()
            if not row:
                raise ValueError("Customer not found")
            company_uuid = str(row[0])
            cursor.execute(
                """
                INSERT INTO configuration_items (
                    id, company_id, ci_type, display_name, normalized_name,
                    lifecycle_status, operational_status, attributes
                ) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    asset_uuid,
                    company_uuid,
                    asset["type"],
                    asset["name"],
                    normalized_name(asset["name"]),
                    metadata.get("lifecycle", "in_service"),
                    metadata.get("operationalStatus", "unknown"),
                    json.dumps(attributes),
                ),
            )
            stored = {**asset, "id": asset_uuid}
            self._insert_audit(
                cursor,
                asset["companyId"],
                actor_id,
                "configuration_item",
                asset_uuid,
                "created",
                None,
                stored,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return stored

    def update_asset(
        self, asset_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        before = self.get_asset(asset_id)
        if not before:
            return None
        updated = {**before, **deepcopy(changes), "updatedAt": utc_now()}
        metadata = updated.get("metadata") or {}
        attributes = self._asset_attributes(updated)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE configuration_items
                SET ci_type = %s,
                    display_name = %s,
                    normalized_name = %s,
                    lifecycle_status = %s,
                    operational_status = %s,
                    attributes = %s::jsonb,
                    updated_at = now()
                WHERE id = %s::uuid AND retired_at IS NULL
                """,
                (
                    updated["type"],
                    updated["name"],
                    normalized_name(updated["name"]),
                    metadata.get("lifecycle", "in_service"),
                    metadata.get("operationalStatus", "unknown"),
                    json.dumps(attributes),
                    asset_id,
                ),
            )
            self._insert_audit(
                cursor,
                updated["companyId"],
                actor_id,
                "configuration_item",
                asset_id,
                "updated",
                before,
                updated,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_asset(asset_id)

    def list_relationships(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, from_ci_id, to_ci_id, relationship_type, impact_policy,
                       source_mapping_id, confidence, evidence, provenance
                FROM ci_relationships
                WHERE retired_at IS NULL
                ORDER BY created_at, id
                """
            )
            return [
                {
                    "id": str(row[0]),
                    "fromId": str(row[1]),
                    "toId": str(row[2]),
                    "type": row[3],
                    "impactPolicy": row[4],
                    "sourceMappingId": str(row[5]) if row[5] else None,
                    "confidence": float(row[6]),
                    "evidence": row[7] or {},
                    "provenance": row[8],
                }
                for row in cursor.fetchall()
            ]

    def create_relationship(
        self, relationship: dict, company_id: str, actor_id: str | None = None
    ) -> dict:
        relationship_uuid = canonical_uuid("relationship", relationship["id"])
        evidence = deepcopy(relationship.get("evidence") or {})
        if not isinstance(evidence, dict):
            raise ValueError("Relationship evidence must be an object")
        if len(json.dumps(evidence, separators=(",", ":")).encode("utf-8")) > 262_144:
            raise ValueError("Relationship evidence is too large")
        confidence = float(relationship.get("confidence", 1))
        if confidence < 0 or confidence > 1:
            raise ValueError("Relationship confidence must be between 0 and 1")
        source_mapping_id = relationship.get("sourceMappingId") or None
        provenance = str(
            relationship.get("provenance") or ("provider" if source_mapping_id else "manual")
        )
        if provenance not in {"manual", "provider"}:
            raise ValueError("Relationship provenance is invalid")
        try:
            from_ci_id = str(uuid.UUID(str(relationship["fromId"])))
            to_ci_id = str(uuid.UUID(str(relationship["toId"])))
            mapping_uuid = str(uuid.UUID(str(source_mapping_id))) if source_mapping_id else None
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Relationship identity is invalid") from error
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            company_uuid = str(company_row[0])
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ci_relationships:{company_uuid}",),
            )
            cursor.execute(
                """
                SELECT count(*)
                FROM configuration_items
                WHERE company_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND retired_at IS NULL
                """,
                (company_uuid, [from_ci_id, to_ci_id]),
            )
            if int(cursor.fetchone()[0]) != 2:
                raise ValueError("Relationship crosses the customer boundary")
            if mapping_uuid:
                cursor.execute(
                    """
                    SELECT 1
                    FROM external_object_mappings mapping
                    JOIN configuration_items source_ci
                      ON source_ci.id = mapping.canonical_entity_id
                     AND mapping.canonical_entity_type = 'configuration_item'
                    WHERE mapping.id = %s::uuid
                      AND source_ci.company_id = %s::uuid
                      AND mapping.active = true
                    """,
                    (mapping_uuid, company_uuid),
                )
                if not cursor.fetchone():
                    raise ValueError("Relationship source mapping is unavailable")
            cursor.execute(
                """
                INSERT INTO ci_relationships (
                    id, company_id, from_ci_id, to_ci_id, relationship_type,
                    impact_policy, source_mapping_id, confidence, evidence,
                    provenance
                )
                VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s,
                    %s::uuid, %s, %s::jsonb, %s
                )
                ON CONFLICT (from_ci_id, to_ci_id, relationship_type) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    impact_policy = CASE
                        WHEN EXCLUDED.provenance = 'manual'
                            OR ci_relationships.provenance <> 'manual'
                        THEN EXCLUDED.impact_policy
                        ELSE ci_relationships.impact_policy
                    END,
                    source_mapping_id = CASE
                        WHEN EXCLUDED.provenance = 'manual' THEN NULL
                        WHEN ci_relationships.provenance = 'manual'
                            THEN ci_relationships.source_mapping_id
                        ELSE EXCLUDED.source_mapping_id
                    END,
                    confidence = CASE
                        WHEN EXCLUDED.provenance = 'manual'
                            OR ci_relationships.provenance <> 'manual'
                        THEN EXCLUDED.confidence
                        ELSE ci_relationships.confidence
                    END,
                    evidence = CASE
                        WHEN EXCLUDED.provenance = 'manual'
                            OR ci_relationships.provenance <> 'manual'
                        THEN EXCLUDED.evidence
                        ELSE ci_relationships.evidence
                    END,
                    provenance = CASE
                        WHEN EXCLUDED.provenance = 'manual' THEN 'manual'
                        ELSE ci_relationships.provenance
                    END,
                    retired_at = NULL
                RETURNING id, source_mapping_id, confidence, evidence,
                          provenance, impact_policy
                """,
                (
                    relationship_uuid,
                    company_uuid,
                    from_ci_id,
                    to_ci_id,
                    relationship["type"],
                    relationship.get("impactPolicy", "required"),
                    mapping_uuid,
                    confidence,
                    json.dumps(evidence),
                    provenance,
                ),
            )
            row = cursor.fetchone()
            stored_id = str(row[0])
            reactivated = stored_id != relationship_uuid
            stored = {
                **relationship,
                "id": stored_id,
                "sourceMappingId": str(row[1]) if row[1] else None,
                "confidence": float(row[2]),
                "evidence": row[3] or {},
                "provenance": row[4],
                "impactPolicy": row[5],
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "relationship",
                stored_id,
                "reactivated" if reactivated else "created",
                {**stored, "retired": True} if reactivated else None,
                stored,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return stored

    def delete_relationship(
        self, relationship_id: str, company_id: str, actor_id: str | None = None
    ) -> bool:
        relationship = next(
            (item for item in self.list_relationships() if item["id"] == relationship_id),
            None,
        )
        if not relationship:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ci_relationships SET retired_at = now() WHERE id = %s::uuid AND retired_at IS NULL",
                (relationship_id,),
            )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "relationship",
                relationship_id,
                "retired",
                relationship,
                None,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    def list_data_quality_exceptions(self, company_id: str | None = None) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.id, c.slug, d.rule_key, d.entity_type, d.entity_id, d.reason,
                       d.state, d.expires_at, d.created_by, d.created_at, d.resolved_by, d.resolved_at
                FROM data_quality_exceptions d JOIN companies c ON c.id = d.company_id
                WHERE (%s::text IS NULL OR c.slug = %s) ORDER BY d.created_at DESC
                """,
                (company_id, company_id),
            )
            return [
                {
                    "id": str(r[0]),
                    "companyId": r[1],
                    "ruleKey": r[2],
                    "entityType": r[3],
                    "entityId": str(r[4]),
                    "reason": r[5],
                    "state": r[6],
                    "expiresAt": self._timestamp(r[7]) or None,
                    "createdBy": str(r[8]) if r[8] else None,
                    "createdAt": self._timestamp(r[9]),
                    "resolvedBy": str(r[10]) if r[10] else None,
                    "resolvedAt": self._timestamp(r[11]) or None,
                }
                for r in cursor.fetchall()
            ]

    def create_data_quality_exception(self, exception: dict, actor_id: str | None = None) -> dict:
        exception_uuid = canonical_uuid("data_quality_exception", exception["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (exception["companyId"],))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            cursor.execute(
                """INSERT INTO data_quality_exceptions
                       (id, company_id, rule_key, entity_type, entity_id, reason, state, expires_at, created_by)
                   VALUES (%s::uuid, %s::uuid, %s, %s, %s::uuid, %s, 'active', %s::timestamptz, %s::uuid)
                   ON CONFLICT (company_id, rule_key, entity_type, entity_id) WHERE state = 'active'
                   DO UPDATE SET reason = EXCLUDED.reason, expires_at = EXCLUDED.expires_at,
                                 created_by = EXCLUDED.created_by, created_at = now()
                   RETURNING id""",
                (
                    exception_uuid,
                    str(company_row[0]),
                    exception["ruleKey"],
                    exception.get("entityType", "configuration_item"),
                    exception["entityId"],
                    exception["reason"],
                    exception.get("expiresAt") or None,
                    actor_id,
                ),
            )
            stored_id = str(cursor.fetchone()[0])
            stored = {**exception, "id": stored_id, "state": "active"}
            self._insert_audit(
                cursor,
                exception["companyId"],
                actor_id,
                "data_quality_exception",
                stored_id,
                "created",
                None,
                stored,
                reason=exception["reason"],
            )
        return next(
            item
            for item in self.list_data_quality_exceptions(exception["companyId"])
            if item["id"] == stored_id
        )

    def resolve_data_quality_exception(
        self, exception_id: str, actor_id: str | None = None
    ) -> dict | None:
        before = next(
            (item for item in self.list_data_quality_exceptions() if item["id"] == exception_id),
            None,
        )
        if not before:
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE data_quality_exceptions SET state = 'resolved', resolved_by = %s::uuid, resolved_at = now() WHERE id = %s::uuid AND state = 'active'",
                (actor_id, exception_id),
            )
            if not cursor.rowcount:
                return None
            after = {
                **before,
                "state": "resolved",
                "resolvedBy": actor_id,
                "resolvedAt": utc_now(),
            }
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "data_quality_exception",
                exception_id,
                "resolved",
                before,
                after,
            )
        return next(
            (
                item
                for item in self.list_data_quality_exceptions(before["companyId"])
                if item["id"] == exception_id
            ),
            after,
        )

    def list_reconciliation_candidates(
        self, company_id: str | None = None, state: str | None = None
    ) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT rc.id, c.slug, ic.provider, e.external_object_type, e.external_id, e.external_name,
                          rc.candidate_ci_id, ci.display_name, rc.reason, rc.confidence, rc.state,
                          rc.external_record, rc.conflict_details, rc.decision, rc.decision_notes,
                          rc.target_ci_id, target.display_name, rc.created_at, rc.reviewed_by, rc.reviewed_at
                   FROM reconciliation_candidates rc
                   JOIN sync_runs sr ON sr.id = rc.sync_run_id
                   JOIN integration_connections ic ON ic.id = sr.integration_connection_id
                   LEFT JOIN external_object_mappings e ON e.id = rc.mapping_id
                   LEFT JOIN configuration_items ci ON ci.id = rc.candidate_ci_id
                   LEFT JOIN configuration_items target ON target.id = rc.target_ci_id
                   LEFT JOIN companies c ON c.id = COALESCE(rc.company_id, ci.company_id, target.company_id, ic.company_id)
                   WHERE (%s::text IS NULL OR c.slug = %s) AND (%s::text IS NULL OR rc.state = %s)
                   ORDER BY rc.created_at DESC, rc.confidence DESC""",
                (company_id, company_id, state, state),
            )
            return [
                {
                    "id": str(r[0]),
                    "companyId": r[1],
                    "provider": PROVIDER_FROM_DB.get(r[2], r[2]),
                    "externalObjectType": r[3] or "configuration_item",
                    "externalId": r[4] or "",
                    "externalName": r[5] or (r[11] or {}).get("name", ""),
                    "candidateAssetId": str(r[6]) if r[6] else None,
                    "candidateAssetName": r[7] or "",
                    "reason": r[8],
                    "confidence": float(r[9]),
                    "state": r[10],
                    "externalRecord": r[11] or {},
                    "conflictDetails": r[12] or {},
                    "decision": r[13] or "",
                    "decisionNotes": r[14] or "",
                    "targetAssetId": str(r[15]) if r[15] else None,
                    "targetAssetName": r[16] or "",
                    "createdAt": self._timestamp(r[17]),
                    "reviewedBy": str(r[18]) if r[18] else None,
                    "reviewedAt": self._timestamp(r[19]) or None,
                }
                for r in cursor.fetchall()
            ]

    def resolve_reconciliation_candidate(
        self,
        candidate_id: str,
        decision: str,
        notes: str,
        target_ci_id: str | None,
        actor_id: str | None = None,
    ) -> dict | None:
        before = next(
            (item for item in self.list_reconciliation_candidates() if item["id"] == candidate_id),
            None,
        )
        if not before:
            return None
        new_state = "approved" if decision in {"use_existing", "create_new"} else "rejected"
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE reconciliation_candidates SET state = %s, decision = %s, decision_notes = %s,
                          target_ci_id = %s::uuid, reviewed_by = %s::uuid, reviewed_at = now()
                   WHERE id = %s::uuid AND state = 'pending'""",
                (new_state, decision, notes, target_ci_id, actor_id, candidate_id),
            )
            if not cursor.rowcount:
                return None
            after = {
                **before,
                "state": new_state,
                "decision": decision,
                "decisionNotes": notes,
                "targetAssetId": target_ci_id,
            }
            self._insert_audit(
                cursor,
                before.get("companyId"),
                actor_id,
                "reconciliation_candidate",
                candidate_id,
                "decision_recorded",
                before,
                after,
                reason=notes,
            )
        return next(
            (
                item
                for item in self.list_reconciliation_candidates(before.get("companyId"))
                if item["id"] == candidate_id
            ),
            after,
        )

    def list_field_authority(self, company_id: str | None = None) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT c.slug, f.ci_type, f.field_name, f.provider, f.priority
                   FROM ci_field_authority f JOIN companies c ON c.id = f.company_id
                   WHERE (%s::text IS NULL OR c.slug = %s)
                   ORDER BY c.name, f.ci_type, f.field_name, f.priority, f.provider""",
                (company_id, company_id),
            )
            return [
                {
                    "companyId": r[0],
                    "ciType": r[1],
                    "fieldName": r[2],
                    "provider": PROVIDER_FROM_DB.get(r[3], r[3]),
                    "priority": r[4],
                }
                for r in cursor.fetchall()
            ]

    def upsert_field_authority(self, rule: dict, actor_id: str | None = None) -> dict:
        before = next(
            (
                item
                for item in self.list_field_authority(rule["companyId"])
                if all(
                    item.get(key) == rule.get(key)
                    for key in ("companyId", "ciType", "fieldName", "provider")
                )
            ),
            None,
        )
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (rule["companyId"],))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            cursor.execute(
                """INSERT INTO ci_field_authority (company_id, ci_type, field_name, provider, priority)
                   VALUES (%s::uuid, %s, %s, %s, %s)
                   ON CONFLICT (company_id, ci_type, field_name, provider) DO UPDATE SET priority = EXCLUDED.priority""",
                (
                    str(company_row[0]),
                    rule["ciType"],
                    rule["fieldName"],
                    PROVIDER_TO_DB.get(rule["provider"], rule["provider"]),
                    rule["priority"],
                ),
            )
            rule_id = canonical_uuid(
                "field_authority",
                f"{rule['companyId']}:{rule['ciType']}:{rule['fieldName']}:{rule['provider']}",
            )
            self._insert_audit(
                cursor,
                rule["companyId"],
                actor_id,
                "field_authority",
                rule_id,
                "updated" if before else "created",
                before,
                rule,
            )
        return rule

    def delete_field_authority(
        self,
        company_id: str,
        ci_type: str,
        field_name: str,
        provider: str,
        actor_id: str | None = None,
    ) -> bool:
        before = next(
            (
                item
                for item in self.list_field_authority(company_id)
                if item["ciType"] == ci_type
                and item["fieldName"] == field_name
                and item["provider"] == provider
            ),
            None,
        )
        if not before:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_uuid = str(cursor.fetchone()[0])
            cursor.execute(
                "DELETE FROM ci_field_authority WHERE company_id = %s::uuid AND ci_type = %s AND field_name = %s AND provider = %s",
                (
                    company_uuid,
                    ci_type,
                    field_name,
                    PROVIDER_TO_DB.get(provider, provider),
                ),
            )
            rule_id = canonical_uuid(
                "field_authority", f"{company_id}:{ci_type}:{field_name}:{provider}"
            )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "field_authority",
                rule_id,
                "deleted",
                before,
                None,
            )
        return True

    def list_integrations(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ic.id, ic.slug, c.slug, ic.provider, ic.name, ic.configuration,
                       ic.enabled, latest.status, latest.finished_at,
                       ic.connection_status, ic.last_test_at, ic.last_error, ic.revision,
                       (ic.credentials_encrypted IS NOT NULL), ic.lifecycle_status,
                       ic.lifecycle_reason, ic.lifecycle_changed_at, ic.lifecycle_changed_by
                FROM integration_connections ic
                LEFT JOIN companies c ON c.id = ic.company_id
                LEFT JOIN LATERAL (
                    SELECT status, finished_at
                    FROM sync_runs sr
                    WHERE sr.integration_connection_id = ic.id
                    ORDER BY sr.started_at DESC, sr.id DESC
                    LIMIT 1
                ) latest ON true
                ORDER BY ic.company_id NULLS FIRST, ic.name
                """
            )
            records = []
            status_labels = {
                "succeeded": "Healthy",
                "blocked": "blocked",
                "failed": "failed",
                "review_required": "Review required",
                "queued": "Queued",
                "running": "Running",
            }
            lifecycle_labels = {
                "paused": "Paused",
                "disabled": "Disabled",
                "removed": "Removed",
            }
            for (
                _item_id,
                slug,
                company_slug,
                provider,
                name,
                configuration,
                enabled,
                latest_status,
                finished_at,
                connection_status,
                last_test_at,
                last_error,
                revision,
                has_credentials,
                lifecycle_status,
                lifecycle_reason,
                lifecycle_changed_at,
                lifecycle_changed_by,
            ) in cursor.fetchall():
                configuration = configuration or {}
                records.append(
                    {
                        "id": slug,
                        "name": name,
                        "type": PROVIDER_FROM_DB.get(provider, provider),
                        "enabled": bool(enabled),
                        "mode": configuration.get("mode", "configured_by_environment"),
                        "lastSync": self._timestamp(finished_at) or None,
                        "status": lifecycle_labels.get(lifecycle_status)
                        or status_labels.get(
                            latest_status, "Ready" if enabled else "Not configured"
                        ),
                        "scope": configuration.get("scope", "customer" if company_slug else "msp"),
                        "companyId": company_slug,
                        "connectionStatus": connection_status,
                        "lastTestAt": self._timestamp(last_test_at) or None,
                        "lastError": last_error or "",
                        "revision": revision,
                        "hasCredentials": bool(has_credentials),
                        "lifecycleStatus": lifecycle_status,
                        "lifecycleReason": lifecycle_reason or "",
                        "lifecycleChangedAt": self._timestamp(lifecycle_changed_at) or None,
                        "lifecycleChangedBy": (
                            str(lifecycle_changed_by) if lifecycle_changed_by else None
                        ),
                    }
                )
            return records

    def get_integration_connection(self, kind: str) -> dict | None:
        """Load one root integration including encrypted server-side material."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ic.id, ic.slug, ic.provider, ic.name, ic.configuration, ic.enabled,
                       ic.credentials_encrypted, ic.credentials_nonce,
                       ic.connection_status, ic.last_test_at, ic.last_error, ic.revision,
                       ic.created_at, ic.updated_at, ic.lifecycle_status,
                       ic.lifecycle_reason, ic.lifecycle_changed_at, ic.lifecycle_changed_by
                FROM integration_connections ic
                WHERE ic.provider = %s AND ic.company_id IS NULL
                ORDER BY ic.created_at LIMIT 1
                """,
                (PROVIDER_TO_DB.get(kind, kind),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return {
            "uuid": str(row[0]),
            "id": row[1],
            "type": PROVIDER_FROM_DB.get(row[2], row[2]),
            "name": row[3],
            "configuration": row[4] or {},
            "enabled": bool(row[5]),
            "credentialsEncrypted": row[6] or "",
            "credentialsNonce": row[7] or "",
            "connectionStatus": row[8],
            "lastTestAt": self._timestamp(row[9]) or None,
            "lastError": row[10] or "",
            "revision": int(row[11]),
            "createdAt": self._timestamp(row[12]),
            "updatedAt": self._timestamp(row[13]),
            "lifecycleStatus": row[14],
            "lifecycleReason": row[15] or "",
            "lifecycleChangedAt": self._timestamp(row[16]) or None,
            "lifecycleChangedBy": str(row[17]) if row[17] else None,
        }

    @staticmethod
    def _integration_capability_snapshot_from_row(row: tuple) -> dict:
        """Return one public, credential-free provider capability cache entry."""

        return {
            "id": str(row[0]),
            "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
            "capabilityKey": row[2],
            "status": row[3],
            "summary": row[4] or {},
            "schemaFingerprint": row[5],
            "errorCategory": row[6] or "",
            "checkedAt": PostgresCmdbRepository._timestamp(row[7]),
            "expiresAt": PostgresCmdbRepository._timestamp(row[8]),
            "stale": bool(row[9]),
        }

    def upsert_integration_capability_snapshot(
        self,
        kind: str,
        capability_key: str,
        snapshot: dict,
        *,
        ttl_seconds: int = 900,
    ) -> dict:
        """Cache one bounded provider capability result without raw API envelopes."""

        if not isinstance(snapshot, dict):
            raise ValueError("Integration capability snapshot must be an object")
        key = str(capability_key or "").strip().casefold()
        if not INTEGRATION_CACHE_KEY.fullmatch(key):
            raise ValueError("Integration capability key is invalid")
        status = str(snapshot.get("status") or "").strip().casefold()
        if status not in INTEGRATION_CAPABILITY_STATUSES:
            raise ValueError("Integration capability status is invalid")
        summary, summary_fingerprint = _bounded_integration_cache_summary(
            snapshot.get("summary") or {},
            maximum_bytes=INTEGRATION_CAPABILITY_CACHE_MAX_BYTES,
        )
        schema_fingerprint = (
            str(snapshot.get("schemaFingerprint") or summary_fingerprint).strip().casefold()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", schema_fingerprint):
            raise ValueError("Integration capability schema fingerprint is invalid")
        error_category = str(snapshot.get("errorCategory") or "").strip()[:80]
        ttl = _bounded_integration_cache_ttl(ttl_seconds)
        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_capability_snapshots (
                    integration_connection_id, capability_key, status, summary,
                    schema_fingerprint, error_category, checked_at, expires_at,
                    created_at, updated_at
                )
                SELECT integration.id, %s, %s, %s::jsonb, %s, %s,
                       now(), now() + make_interval(secs => %s), now(), now()
                FROM integration_connections integration
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                ORDER BY integration.created_at
                LIMIT 1
                ON CONFLICT (integration_connection_id, capability_key)
                DO UPDATE SET status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    schema_fingerprint = EXCLUDED.schema_fingerprint,
                    error_category = EXCLUDED.error_category,
                    checked_at = EXCLUDED.checked_at,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                RETURNING id
                """,
                (
                    key,
                    status,
                    json.dumps(summary),
                    schema_fingerprint,
                    error_category or None,
                    ttl,
                    provider,
                ),
            )
            row = cursor.fetchone()
        if not row:
            raise ValueError("Integration connection not found")
        stored = self.get_integration_capability_snapshot(kind, key, include_expired=True)
        if not stored:
            raise ValueError("Integration capability snapshot could not be loaded")
        return stored

    def get_integration_capability_snapshot(
        self,
        kind: str,
        capability_key: str,
        *,
        include_expired: bool = False,
    ) -> dict | None:
        """Return one unexpired capability result for a root provider connection."""

        key = str(capability_key or "").strip().casefold()
        if not INTEGRATION_CACHE_KEY.fullmatch(key):
            raise ValueError("Integration capability key is invalid")
        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot.id, integration.provider, snapshot.capability_key,
                       snapshot.status, snapshot.summary, snapshot.schema_fingerprint,
                       snapshot.error_category, snapshot.checked_at, snapshot.expires_at,
                       snapshot.expires_at <= now()
                FROM integration_capability_snapshots snapshot
                JOIN integration_connections integration
                  ON integration.id = snapshot.integration_connection_id
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND snapshot.capability_key = %s
                  AND (%s OR snapshot.expires_at > now())
                ORDER BY integration.created_at
                LIMIT 1
                """,
                (provider, key, include_expired),
            )
            row = cursor.fetchone()
        return self._integration_capability_snapshot_from_row(row) if row else None

    def list_integration_capability_snapshots(
        self,
        kind: str,
        *,
        include_expired: bool = False,
    ) -> list[dict]:
        """Return bounded capability state for one root provider connection."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot.id, integration.provider, snapshot.capability_key,
                       snapshot.status, snapshot.summary, snapshot.schema_fingerprint,
                       snapshot.error_category, snapshot.checked_at, snapshot.expires_at,
                       snapshot.expires_at <= now()
                FROM integration_capability_snapshots snapshot
                JOIN integration_connections integration
                  ON integration.id = snapshot.integration_connection_id
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND (%s OR snapshot.expires_at > now())
                ORDER BY snapshot.capability_key
                """,
                (provider, include_expired),
            )
            rows = cursor.fetchall()
        return [self._integration_capability_snapshot_from_row(row) for row in rows]

    @staticmethod
    def _integration_enrichment_preview_from_row(row: tuple) -> dict:
        """Return one public tenant-scoped enrichment preview cache entry."""

        return {
            "id": str(row[0]),
            "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
            "companyId": row[2],
            "providerParentId": row[3],
            "sourceNamespace": row[4],
            "sourceServerId": row[5],
            "sourceDeviceId": row[6],
            "policyId": str(row[7]) if row[7] else None,
            "syncRunId": str(row[8]) if row[8] else None,
            "assetId": str(row[9]) if row[9] else None,
            "sourceMappingId": str(row[10]) if row[10] else None,
            "status": row[11],
            "summary": row[12] or {},
            "sourceFingerprint": row[13],
            "observedAt": PostgresCmdbRepository._timestamp(row[14]),
            "expiresAt": PostgresCmdbRepository._timestamp(row[15]),
            "stale": bool(row[16]),
        }

    @staticmethod
    def _integration_enrichment_preview_select() -> str:
        """Return the fixed projection shared by cache lookups."""

        return """
            SELECT preview.id, integration.provider, company.slug,
                   preview.provider_parent_id, preview.source_namespace,
                   preview.source_server_id, preview.source_device_id,
                   preview.policy_id, preview.sync_run_id, preview.canonical_ci_id,
                   preview.source_mapping_id, preview.status, preview.summary,
                   preview.source_fingerprint, preview.observed_at, preview.expires_at,
                   preview.expires_at <= now()
            FROM integration_enrichment_previews preview
            JOIN integration_connections integration
              ON integration.id = preview.integration_connection_id
            JOIN companies company ON company.id = preview.company_id
        """

    @contextmanager
    def integration_enrichment_scope_lock(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
    ) -> Iterator[None]:
        """Serialize one cache publication scope across PostgreSQL application replicas."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        parent_id = _integration_cache_identifier(provider_parent_id, "Provider parent ID")
        server_id = _integration_cache_identifier(source_server_id, "Source server ID")
        scope_key = ":".join((provider, company_id, parent_id, server_id))
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (scope_key,),
            )
            yield

    def upsert_integration_enrichment_preview(
        self,
        kind: str,
        company_id: str,
        preview: dict,
        *,
        ttl_seconds: int = 900,
    ) -> dict:
        """Cache one normalized GraphQL enrichment under collision-safe identity."""

        if not isinstance(preview, dict):
            raise ValueError("Integration enrichment preview must be an object")
        namespace = str(preview.get("sourceNamespace") or "").strip().casefold()
        if not INTEGRATION_SOURCE_NAMESPACE.fullmatch(namespace):
            raise ValueError("Integration source namespace is invalid")
        source_server_id = _integration_cache_identifier(
            preview.get("sourceServerId"), "Source server ID"
        )
        source_device_id = _integration_cache_identifier(
            preview.get("sourceDeviceId"), "Source device ID"
        )
        provider_parent_id = _integration_cache_identifier(
            preview.get("providerParentId"), "Provider parent ID"
        )
        status = str(preview.get("status") or "").strip().casefold()
        if status not in INTEGRATION_ENRICHMENT_STATUSES:
            raise ValueError("Integration enrichment status is invalid")
        summary, summary_fingerprint = _bounded_integration_cache_summary(
            preview.get("summary") or {},
            maximum_bytes=INTEGRATION_ENRICHMENT_CACHE_MAX_BYTES,
        )
        source_fingerprint = (
            str(preview.get("sourceFingerprint") or summary_fingerprint).strip().casefold()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
            raise ValueError("Integration enrichment source fingerprint is invalid")
        policy_id = _integration_cache_optional_uuid(preview.get("policyId"), "Policy ID")
        sync_run_id = _integration_cache_optional_uuid(preview.get("syncRunId"), "Sync run ID")
        asset_id = _integration_cache_optional_uuid(
            preview.get("assetId") or preview.get("canonicalCiId"),
            "Configuration item ID",
        )
        source_mapping_id = _integration_cache_optional_uuid(
            preview.get("sourceMappingId"), "Source mapping ID"
        )
        ttl = _bounded_integration_cache_ttl(ttl_seconds)
        provider = PROVIDER_TO_DB.get(kind, kind)

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT integration.id, company.id
                FROM integration_connections integration
                CROSS JOIN companies company
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND company.status <> 'inactive'
                ORDER BY integration.created_at
                LIMIT 1
                """,
                (provider, company_id),
            )
            scope = cursor.fetchone()
            if not scope:
                raise ValueError("Integration connection or customer not found")
            connection_uuid, company_uuid = scope
            if policy_id:
                cursor.execute(
                    """
                    SELECT 1 FROM integration_ci_policies
                    WHERE id = %s::uuid
                      AND integration_connection_id = %s::uuid
                      AND company_id = %s::uuid
                    """,
                    (policy_id, connection_uuid, company_uuid),
                )
                if not cursor.fetchone():
                    raise ValueError("Integration policy is unavailable for this customer")
            if sync_run_id:
                cursor.execute(
                    """
                    SELECT 1 FROM sync_runs
                    WHERE id = %s::uuid
                      AND integration_connection_id = %s::uuid
                      AND (company_id IS NULL OR company_id = %s::uuid)
                    """,
                    (sync_run_id, connection_uuid, company_uuid),
                )
                if not cursor.fetchone():
                    raise ValueError("Sync run is unavailable for this customer")
            if asset_id:
                cursor.execute(
                    """
                    SELECT 1 FROM configuration_items
                    WHERE id = %s::uuid AND company_id = %s::uuid
                      AND retired_at IS NULL
                    """,
                    (asset_id, company_uuid),
                )
                if not cursor.fetchone():
                    raise ValueError("Configuration item is unavailable for this customer")
            if source_mapping_id:
                cursor.execute(
                    """
                    SELECT mapping.canonical_entity_id
                    FROM external_object_mappings mapping
                    JOIN configuration_items ci
                      ON ci.id = mapping.canonical_entity_id
                     AND mapping.canonical_entity_type = 'configuration_item'
                    WHERE mapping.id = %s::uuid
                      AND mapping.integration_connection_id = %s::uuid
                      AND mapping.active = true
                      AND ci.company_id = %s::uuid
                      AND ci.retired_at IS NULL
                    """,
                    (source_mapping_id, connection_uuid, company_uuid),
                )
                mapping = cursor.fetchone()
                if not mapping or (asset_id and str(mapping[0]) != asset_id):
                    raise ValueError("Source mapping is unavailable for this customer asset")

            cursor.execute(
                """
                INSERT INTO integration_enrichment_previews (
                    integration_connection_id, company_id, policy_id, sync_run_id,
                    source_namespace, source_server_id, source_device_id,
                    provider_parent_id, canonical_ci_id, source_mapping_id, status,
                    summary, source_fingerprint, observed_at, expires_at,
                    created_at, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s, %s, %s, %s, %s::uuid, %s::uuid, %s,
                    %s::jsonb, %s, now(), now() + make_interval(secs => %s),
                    now(), now()
                )
                ON CONFLICT (
                    integration_connection_id, company_id, source_namespace,
                    source_server_id, source_device_id
                ) DO UPDATE SET policy_id = EXCLUDED.policy_id,
                    sync_run_id = EXCLUDED.sync_run_id,
                    provider_parent_id = EXCLUDED.provider_parent_id,
                    canonical_ci_id = EXCLUDED.canonical_ci_id,
                    source_mapping_id = EXCLUDED.source_mapping_id,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    source_fingerprint = EXCLUDED.source_fingerprint,
                    observed_at = EXCLUDED.observed_at,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                RETURNING id
                """,
                (
                    connection_uuid,
                    company_uuid,
                    policy_id,
                    sync_run_id,
                    namespace,
                    source_server_id,
                    source_device_id,
                    provider_parent_id,
                    asset_id,
                    source_mapping_id,
                    status,
                    json.dumps(summary),
                    source_fingerprint,
                    ttl,
                ),
            )
            stored_id = str(cursor.fetchone()[0])

        stored = self.get_integration_enrichment_preview(
            kind,
            company_id,
            namespace,
            source_server_id,
            source_device_id,
            include_expired=True,
        )
        if not stored or stored["id"] != stored_id:
            raise ValueError("Integration enrichment preview could not be loaded")
        return stored

    def get_integration_enrichment_preview(
        self,
        kind: str,
        company_id: str,
        source_namespace: str,
        source_server_id: str,
        source_device_id: str,
        *,
        include_expired: bool = False,
    ) -> dict | None:
        """Return one cached enrichment constrained to a provider customer."""

        namespace = str(source_namespace or "").strip().casefold()
        if not INTEGRATION_SOURCE_NAMESPACE.fullmatch(namespace):
            raise ValueError("Integration source namespace is invalid")
        server_id = _integration_cache_identifier(source_server_id, "Source server ID")
        device_id = _integration_cache_identifier(source_device_id, "Source device ID")
        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._integration_enrichment_preview_select()
                + """
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND preview.source_namespace = %s
                  AND preview.source_server_id = %s
                  AND preview.source_device_id = %s
                  AND (%s OR preview.expires_at > now())
                ORDER BY integration.created_at
                LIMIT 1
                """,
                (provider, company_id, namespace, server_id, device_id, include_expired),
            )
            row = cursor.fetchone()
        return self._integration_enrichment_preview_from_row(row) if row else None

    def list_integration_enrichment_previews(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str | None = None,
        *,
        source_server_id: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """List a bounded customer cache without crossing integration identities."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        parent_id = (
            _integration_cache_identifier(provider_parent_id, "Provider parent ID")
            if provider_parent_id is not None
            else None
        )
        server_id = (
            _integration_cache_identifier(source_server_id, "Source server ID")
            if source_server_id is not None
            else None
        )
        bounded_limit = max(1, min(int(limit), 25_000))
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                self._integration_enrichment_preview_select()
                + """
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND (%s::text IS NULL OR preview.provider_parent_id = %s)
                  AND (%s::text IS NULL OR preview.source_server_id = %s)
                  AND (%s OR preview.expires_at > now())
                ORDER BY preview.observed_at DESC, preview.id
                LIMIT %s
                """,
                (
                    provider,
                    company_id,
                    parent_id,
                    parent_id,
                    server_id,
                    server_id,
                    include_expired,
                    bounded_limit,
                ),
            )
            rows = cursor.fetchall()
        return [self._integration_enrichment_preview_from_row(row) for row in rows]

    def replace_integration_enrichment_generation(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
        generation_id: str | None,
    ) -> int:
        """Remove superseded rows after a complete tenant-scoped cache refresh."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        parent_id = _integration_cache_identifier(provider_parent_id, "Provider parent ID")
        server_id = _integration_cache_identifier(source_server_id, "Source server ID")
        retained_generation = str(generation_id or "") or None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM integration_enrichment_previews preview
                USING integration_connections integration, companies company
                WHERE preview.integration_connection_id = integration.id
                  AND preview.company_id = company.id
                  AND integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND preview.provider_parent_id = %s
                  AND preview.source_server_id = %s
                  AND (
                    %s::text IS NULL
                    OR COALESCE(preview.summary->>'cacheGenerationId', '') <> %s
                  )
                """,
                (
                    provider,
                    company_id,
                    parent_id,
                    server_id,
                    retained_generation,
                    retained_generation,
                ),
            )
            return int(cursor.rowcount or 0)

    def delete_integration_enrichment_generation(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        source_server_id: str,
        generation_id: str,
    ) -> int:
        """Delete one unpublished tenant-scoped cache generation."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        parent_id = _integration_cache_identifier(provider_parent_id, "Provider parent ID")
        server_id = _integration_cache_identifier(source_server_id, "Source server ID")
        selected_generation = _integration_cache_identifier(generation_id, "Cache generation ID")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM integration_enrichment_previews preview
                USING integration_connections integration, companies company
                WHERE preview.integration_connection_id = integration.id
                  AND preview.company_id = company.id
                  AND integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND preview.provider_parent_id = %s
                  AND preview.source_server_id = %s
                  AND preview.summary->>'cacheGenerationId' = %s
                """,
                (
                    provider,
                    company_id,
                    parent_id,
                    server_id,
                    selected_generation,
                ),
            )
            return int(cursor.rowcount or 0)

    def prune_integration_enrichment_previews(
        self,
        kind: str,
        *,
        max_entries: int = 5_000,
    ) -> int:
        """Delete expired rows without splitting a published cache generation."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        del max_entries
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH scoped AS (
                    SELECT preview.id, preview.expires_at,
                           preview.summary->>'cacheGenerationId' AS generation_id
                    FROM integration_enrichment_previews preview
                    JOIN integration_connections integration
                      ON integration.id = preview.integration_connection_id
                    WHERE integration.provider = %s
                      AND integration.company_id IS NULL
                ), expired_generations AS (
                    SELECT DISTINCT generation_id
                    FROM scoped
                    WHERE generation_id IS NOT NULL
                      AND generation_id <> ''
                      AND expires_at <= now()
                ), doomed AS (
                    SELECT scoped.id
                    FROM scoped
                    WHERE (
                        (scoped.generation_id IS NULL OR scoped.generation_id = '')
                        AND scoped.expires_at <= now()
                    ) OR scoped.generation_id IN (
                        SELECT generation_id FROM expired_generations
                    )
                )
                DELETE FROM integration_enrichment_previews preview
                USING doomed
                WHERE preview.id = doomed.id
                """,
                (provider,),
            )
            return cursor.rowcount

    def ensure_integration_connection(
        self, kind: str, name: str, actor_id: str | None = None
    ) -> dict:
        """Install a supported root provider row before its first configuration save."""

        existing = self.get_integration_connection(kind)
        if existing:
            return existing
        if kind not in CREDENTIAL_REFERENCES:
            raise ValueError("Unsupported integration provider")
        connection_id = canonical_uuid("integration_connection", kind)
        provider = PROVIDER_TO_DB.get(kind, kind)
        configuration = {"scope": "msp", "mode": "configured_in_application"}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_connections (
                    id, slug, company_id, provider, name, credential_reference,
                    configuration, enabled
                ) VALUES (
                    %s::uuid, %s, NULL, %s, %s, %s, %s::jsonb, false
                )
                ON CONFLICT (slug) DO NOTHING
                RETURNING id
                """,
                (
                    connection_id,
                    kind,
                    provider,
                    name[:160],
                    CREDENTIAL_REFERENCES[kind],
                    json.dumps(configuration),
                ),
            )
            inserted = cursor.fetchone()
            if inserted:
                self._insert_audit(
                    cursor,
                    None,
                    actor_id,
                    "integration_connection",
                    str(inserted[0]),
                    "installed",
                    None,
                    {
                        "id": kind,
                        "name": name[:160],
                        "type": kind,
                        "enabled": False,
                        "scope": "msp",
                        "configuration": configuration,
                        "revision": 1,
                        "lifecycleStatus": "active",
                        "hasCredentials": False,
                    },
                )
        installed = self.get_integration_connection(kind)
        if not installed:
            raise ValueError("Integration connection could not be installed")
        return installed

    def update_integration_connection(
        self, kind: str, changes: dict, actor_id: str | None = None
    ) -> dict:
        """Update encrypted root connection settings with optimistic concurrency."""

        before = self.get_integration_connection(kind)
        if not before:
            raise ValueError("Integration connection not found")
        expected_revision = changes.get("expectedRevision")
        if expected_revision is not None and int(expected_revision) != int(before["revision"]):
            raise ValueError("Integration settings changed; reload before saving")
        stored_configuration = {
            **(before.get("configuration") or {}),
            **deepcopy(changes.get("configuration") or {}),
            "mode": "configured_in_application",
            "scope": "msp",
        }
        lifecycle_status = changes.get("lifecycleStatus", before.get("lifecycleStatus", "active"))
        lifecycle_changed = lifecycle_status != before.get("lifecycleStatus", "active")
        enabled = bool(changes.get("enabled", before.get("enabled"))) and (
            lifecycle_status == "active"
        )
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections SET
                    configuration = %s::jsonb,
                    credentials_encrypted = %s,
                    credentials_nonce = %s,
                    credential_reference = %s,
                    enabled = %s,
                    lifecycle_status = %s,
                    lifecycle_reason = CASE WHEN %s THEN %s ELSE lifecycle_reason END,
                    lifecycle_changed_at = CASE WHEN %s THEN now() ELSE lifecycle_changed_at END,
                    lifecycle_changed_by = CASE WHEN %s THEN %s::uuid
                        ELSE lifecycle_changed_by END,
                    connection_status = %s,
                    last_error = %s,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid AND revision = %s
                """,
                (
                    json.dumps(stored_configuration),
                    changes.get("credentialsEncrypted")
                    or before.get("credentialsEncrypted")
                    or None,
                    changes.get("credentialsNonce") or before.get("credentialsNonce") or None,
                    f"encrypted://integration/{kind}",
                    enabled,
                    lifecycle_status,
                    lifecycle_changed,
                    changes.get("lifecycleReason") or "",
                    lifecycle_changed,
                    lifecycle_changed,
                    actor_id,
                    changes.get("connectionStatus", "configured"),
                    changes.get("lastError") or None,
                    before["uuid"],
                    before["revision"],
                ),
            )
            if not cursor.rowcount:
                raise ValueError("Integration settings changed; reload before saving")
            after = {
                **before,
                "configuration": stored_configuration,
                "credentialsEncrypted": changes.get("credentialsEncrypted")
                or before.get("credentialsEncrypted"),
                "credentialsNonce": changes.get("credentialsNonce")
                or before.get("credentialsNonce"),
                "enabled": enabled,
                "lifecycleStatus": lifecycle_status,
                "lifecycleReason": (
                    changes.get("lifecycleReason") or ""
                    if lifecycle_changed
                    else before.get("lifecycleReason", "")
                ),
                "connectionStatus": changes.get("connectionStatus", "configured"),
                "lastError": changes.get("lastError") or "",
                "revision": int(before["revision"]) + 1,
            }
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "integration_connection",
                before["uuid"],
                "configuration_updated",
                integration_connection_audit_value(before),
                integration_connection_audit_value(after),
            )
        return self.get_integration_connection(kind) or after

    def integration_lifecycle_impact(self, kind: str) -> dict:
        """Return exact retained-data counts for an integration lifecycle decision."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        connection_record = self.get_integration_connection(kind)
        if not connection_record:
            raise ValueError("Integration connection not found")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM provider_company_observations observation
                     WHERE observation.integration_connection_id = integration.id),
                    (SELECT count(*) FROM external_object_mappings mapping
                     WHERE mapping.integration_connection_id = integration.id
                       AND mapping.external_object_type = 'company'
                       AND mapping.active = true),
                    (SELECT count(*) FROM integration_ci_policies policy
                     WHERE policy.integration_connection_id = integration.id),
                    (SELECT count(*) FROM integration_ci_policies policy
                     WHERE policy.integration_connection_id = integration.id
                       AND policy.enabled = true),
                    (SELECT count(*) FROM integration_ci_review_items item
                     JOIN integration_ci_policies policy ON policy.id = item.policy_id
                     WHERE policy.integration_connection_id = integration.id
                       AND item.state = 'pending'),
                    (SELECT count(*) FROM external_object_mappings mapping
                     WHERE mapping.integration_connection_id = integration.id
                       AND mapping.external_object_type = 'configuration'
                       AND mapping.active = true),
                    (SELECT count(DISTINCT mapping.canonical_entity_id)
                     FROM external_object_mappings mapping
                     WHERE mapping.integration_connection_id = integration.id
                       AND mapping.external_object_type = 'configuration'
                       AND mapping.canonical_entity_type = 'configuration_item'
                       AND mapping.active = true),
                    (SELECT count(*) FROM sync_runs run
                     WHERE run.integration_connection_id = integration.id)
                FROM integration_connections integration
                WHERE integration.provider = %s AND integration.company_id IS NULL
                ORDER BY integration.created_at
                LIMIT 1
                """,
                (provider,),
            )
            row = cursor.fetchone()
        if not row:
            raise ValueError("Integration connection not found")
        return {
            "provider": kind,
            "lifecycleStatus": connection_record.get("lifecycleStatus", "active"),
            "companyObservations": int(row[0]),
            "customerMappings": int(row[1]),
            "ciPolicies": int(row[2]),
            "enabledPolicies": int(row[3]),
            "pendingReviews": int(row[4]),
            "ciMappings": int(row[5]),
            "importedCis": int(row[6]),
            "syncRuns": int(row[7]),
        }

    def change_integration_lifecycle(
        self,
        kind: str,
        lifecycle_status: str,
        reason: str,
        actor_id: str,
        expected_revision: int | None = None,
        *,
        remove_configuration: bool = False,
    ) -> dict:
        """Apply an audited lifecycle transition and optionally destroy stored secrets."""

        before = self.get_integration_connection(kind)
        if not before:
            raise ValueError("Integration connection not found")
        if expected_revision is not None and int(expected_revision) != int(before["revision"]):
            raise ValueError("Integration settings changed; reload before continuing")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections
                SET lifecycle_status = %s,
                    lifecycle_reason = %s,
                    lifecycle_changed_at = now(),
                    lifecycle_changed_by = %s::uuid,
                    enabled = %s,
                    configuration = CASE WHEN %s
                        THEN jsonb_build_object('scope', 'msp', 'mode', 'removed')
                        ELSE configuration END,
                    credentials_encrypted = CASE WHEN %s THEN NULL
                        ELSE credentials_encrypted END,
                    credentials_nonce = CASE WHEN %s THEN NULL ELSE credentials_nonce END,
                    credential_reference = CASE WHEN %s
                        THEN 'removed://integration/' || slug ELSE credential_reference END,
                    connection_status = CASE WHEN %s THEN 'not_configured'
                        ELSE connection_status END,
                    last_error = CASE WHEN %s THEN NULL ELSE last_error END,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid AND revision = %s
                """,
                (
                    lifecycle_status,
                    reason[:1000],
                    actor_id,
                    lifecycle_status == "active",
                    remove_configuration,
                    remove_configuration,
                    remove_configuration,
                    remove_configuration,
                    remove_configuration,
                    remove_configuration,
                    before["uuid"],
                    before["revision"],
                ),
            )
            if not cursor.rowcount:
                raise ValueError("Integration settings changed; reload before continuing")
            after = {
                **before,
                "enabled": lifecycle_status == "active",
                "lifecycleStatus": lifecycle_status,
                "lifecycleReason": reason[:1000],
                "lifecycleChangedAt": utc_now(),
                "lifecycleChangedBy": actor_id,
                "revision": int(before["revision"]) + 1,
            }
            if remove_configuration:
                after.update(
                    configuration={"scope": "msp", "mode": "removed"},
                    credentialsEncrypted="",
                    credentialsNonce="",
                    connectionStatus="not_configured",
                    lastError="",
                )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "integration_connection",
                before["uuid"],
                f"integration_{lifecycle_status}",
                integration_connection_audit_value(before),
                integration_connection_audit_value(after),
                reason=reason,
            )
        return self.get_integration_connection(kind) or after

    def mark_integration_test(
        self, kind: str, status: str, message: str, actor_id: str | None = None
    ) -> dict:
        """Record a connection test and its audit outcome without revision churn."""

        before = self.get_integration_connection(kind)
        if not before:
            raise ValueError("Integration connection not found")
        tested_at = utc_now()
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections SET connection_status = %s,
                    last_test_at = %s::timestamptz, last_error = %s, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    status,
                    tested_at,
                    None if status == "verified" else message[:1000],
                    before["uuid"],
                ),
            )
            after = {
                **before,
                "connectionStatus": status,
                "lastTestAt": tested_at,
                "lastError": "" if status == "verified" else message[:1000],
            }
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "integration_connection",
                before["uuid"],
                "connection_tested",
                integration_connection_audit_value(before),
                integration_connection_audit_value(after),
                outcome="success" if status == "verified" else "failed",
                reason=message,
            )
        return self.get_integration_connection(kind) or after

    def record_company_discovery(
        self,
        kind: str,
        run: dict,
        companies: list[dict],
        actor_id: str | None = None,
    ) -> dict:
        """Atomically store a company snapshot, sync run and audit evidence."""

        connection_record = self.get_integration_connection(kind)
        if not connection_record:
            raise ValueError("Integration connection not found")
        run_uuid = canonical_uuid("sync_run", run["id"])
        status_to_db = {"success": "succeeded", "review_required": "review_required"}
        database_status = status_to_db.get(str(run.get("status")), "failed")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_runs (
                    id, integration_connection_id, status, started_at, finished_at,
                    discovered_count, created_count, updated_count, review_count,
                    error_summary, message, attributes
                ) VALUES (
                    %s::uuid, %s::uuid, %s, %s::timestamptz, %s::timestamptz,
                    %s, 0, 0, %s, NULL, %s, %s::jsonb
                )
                """,
                (
                    run_uuid,
                    connection_record["uuid"],
                    database_status,
                    run.get("startedAt"),
                    run.get("finishedAt"),
                    len(companies),
                    int(run.get("review") or 0),
                    run.get("message") or "",
                    json.dumps(
                        {
                            **deepcopy(run.get("attributes") or {}),
                            "operation": "company_discovery",
                            "readOnly": True,
                        }
                    ),
                ),
            )
            for company in companies:
                payload_hash = hashlib.sha256(
                    json.dumps(company, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                observation_id = canonical_uuid(
                    "provider_company_observation",
                    f"{connection_record['uuid']}:{company['externalId']}",
                )
                cursor.execute(
                    """
                    INSERT INTO provider_company_observations (
                        id, integration_connection_id, last_sync_run_id, external_id,
                        identifier, display_name, status_name, type_name, site_name,
                        deleted, provider_updated_at, observed_fields, payload_hash,
                        active, first_seen_at, last_seen_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb, %s, true, now(), now()
                    )
                    ON CONFLICT (integration_connection_id, external_id) DO UPDATE SET
                        last_sync_run_id = EXCLUDED.last_sync_run_id,
                        identifier = EXCLUDED.identifier,
                        display_name = EXCLUDED.display_name,
                        status_name = EXCLUDED.status_name,
                        type_name = EXCLUDED.type_name,
                        site_name = EXCLUDED.site_name,
                        deleted = EXCLUDED.deleted,
                        provider_updated_at = EXCLUDED.provider_updated_at,
                        observed_fields = EXCLUDED.observed_fields,
                        payload_hash = EXCLUDED.payload_hash,
                        active = true,
                        last_seen_at = now()
                    """,
                    (
                        observation_id,
                        connection_record["uuid"],
                        run_uuid,
                        company["externalId"],
                        company.get("identifier") or None,
                        company["name"],
                        company.get("status") or None,
                        company.get("type") or None,
                        company.get("site") or None,
                        bool(company.get("deleted")),
                        company.get("lastUpdated") or None,
                        json.dumps(company),
                        payload_hash,
                    ),
                )
            cursor.execute(
                """
                UPDATE provider_company_observations SET active = false
                WHERE integration_connection_id = %s::uuid
                  AND last_sync_run_id IS DISTINCT FROM %s::uuid
                """,
                (connection_record["uuid"], run_uuid),
            )
            cursor.execute(
                """
                UPDATE integration_connections SET
                    connection_status = 'verified', last_test_at = now(),
                    last_error = NULL, updated_at = now()
                WHERE id = %s::uuid
                """,
                (connection_record["uuid"],),
            )
            stored = {**deepcopy(run), "id": run_uuid, "discovered": len(companies)}
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "integration_connection",
                connection_record["uuid"],
                "company_discovery_completed",
                None,
                stored,
                metadata={"readOnly": True, "reviewCount": int(run.get("review") or 0)},
            )
        return next(item for item in self.list_sync_runs() if item["id"] == run_uuid)

    def list_provider_companies(self, kind: str) -> list[dict]:
        """Load sanitized company observations and active explicit mappings."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT observation.id, observation.external_id, observation.identifier,
                       observation.display_name, observation.status_name,
                       observation.type_name, observation.site_name, observation.deleted,
                       observation.provider_updated_at, observation.active,
                       observation.first_seen_at, observation.last_seen_at,
                       mapping.id, company.slug, company.name
                FROM provider_company_observations observation
                JOIN integration_connections integration
                  ON integration.id = observation.integration_connection_id
                LEFT JOIN external_object_mappings mapping
                  ON mapping.integration_connection_id = integration.id
                 AND mapping.external_object_type = 'company'
                 AND mapping.external_id = observation.external_id
                 AND mapping.active = true
                LEFT JOIN companies company
                  ON company.id = mapping.canonical_entity_id
                 AND mapping.canonical_entity_type = 'company'
                WHERE integration.provider = %s AND integration.company_id IS NULL
                ORDER BY observation.active DESC, observation.display_name, observation.external_id
                """,
                (PROVIDER_TO_DB.get(kind, kind),),
            )
            return [
                {
                    "id": str(row[0]),
                    "provider": kind,
                    "externalId": row[1],
                    "identifier": row[2] or "",
                    "name": row[3],
                    "status": row[4] or "",
                    "type": row[5] or "",
                    "site": row[6] or "",
                    "deleted": bool(row[7]),
                    "lastUpdated": row[8] or "",
                    "active": bool(row[9]),
                    "firstSeenAt": self._timestamp(row[10]),
                    "lastSeenAt": self._timestamp(row[11]),
                    "mappingId": str(row[12]) if row[12] else None,
                    "mappedCompanyId": row[13],
                    "mappedCompanyName": row[14] or "",
                }
                for row in cursor.fetchall()
            ]

    def map_provider_company(
        self, kind: str, external_id: str, company_id: str, actor_id: str | None = None
    ) -> dict:
        """Persist an explicit provider-company mapping without name-based auto-merge."""

        before = next(
            (
                item
                for item in self.list_provider_companies(kind)
                if item["externalId"] == external_id
            ),
            None,
        )
        if not before:
            raise ValueError("Provider company not found")
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM companies WHERE slug = %s AND status <> 'inactive'",
                (company_id,),
            )
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("CMDB customer not found")
            cursor.execute(
                """
                SELECT id FROM integration_connections
                WHERE provider = %s AND company_id IS NULL ORDER BY created_at LIMIT 1
                """,
                (PROVIDER_TO_DB.get(kind, kind),),
            )
            connection_uuid = str(cursor.fetchone()[0])
            mapping_uuid = canonical_uuid(
                "external_company_mapping", f"{connection_uuid}:{external_id}"
            )
            cursor.execute(
                """
                INSERT INTO external_object_mappings (
                    id, integration_connection_id, external_object_type, external_id,
                    canonical_entity_type, canonical_entity_id, external_name,
                    active, first_seen_at, last_seen_at, last_synced_at
                ) VALUES (
                    %s::uuid, %s::uuid, 'company', %s, 'company', %s::uuid, %s,
                    true, now(), now(), now()
                )
                ON CONFLICT (integration_connection_id, external_object_type, external_id)
                DO UPDATE SET canonical_entity_type = 'company',
                    canonical_entity_id = EXCLUDED.canonical_entity_id,
                    external_name = EXCLUDED.external_name,
                    active = true, last_seen_at = now(), last_synced_at = now()
                """,
                (mapping_uuid, connection_uuid, external_id, str(company_row[0]), before["name"]),
            )
            after = {
                **before,
                "mappingId": mapping_uuid,
                "mappedCompanyId": company_id,
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "external_company_mapping",
                mapping_uuid,
                "mapped",
                before if before.get("mappingId") else None,
                after,
            )
        return next(
            item for item in self.list_provider_companies(kind) if item["externalId"] == external_id
        )

    def unmap_provider_company(
        self, kind: str, external_id: str, actor_id: str | None = None
    ) -> bool:
        """Deactivate an explicit mapping while retaining its audit history."""

        before = next(
            (
                item
                for item in self.list_provider_companies(kind)
                if item["externalId"] == external_id and item.get("mappingId")
            ),
            None,
        )
        if not before:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE external_object_mappings SET active = false, last_synced_at = now() WHERE id = %s::uuid AND active = true",
                (before["mappingId"],),
            )
            if not cursor.rowcount:
                return False
            after = {**before, "mappingId": None, "mappedCompanyId": None, "mappedCompanyName": ""}
            self._insert_audit(
                cursor,
                before.get("mappedCompanyId"),
                actor_id,
                "external_company_mapping",
                before["mappingId"],
                "unmapped",
                before,
                after,
            )
        return True

    def list_provider_ci_mappings(self, kind: str, company_id: str) -> list[dict]:
        """Return active provider mappings joined to their canonical customer."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mapping.id, mapping.external_id, mapping.external_name,
                       mapping.external_version, mapping.canonical_entity_id,
                       mapping.first_seen_at, mapping.last_seen_at, mapping.last_synced_at,
                       mapping.external_parent_id
                FROM external_object_mappings mapping
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                JOIN configuration_items ci
                  ON ci.id = mapping.canonical_entity_id
                 AND mapping.canonical_entity_type = 'configuration_item'
                JOIN companies company ON company.id = ci.company_id
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND mapping.external_object_type = 'configuration'
                  AND mapping.active = true
                  AND company.slug = %s
                ORDER BY mapping.external_name, mapping.external_id
                """,
                (PROVIDER_TO_DB.get(kind, kind), company_id),
            )
            return [
                {
                    "id": str(row[0]),
                    "provider": kind,
                    "companyId": company_id,
                    "externalId": row[1],
                    "externalName": row[2] or "",
                    "externalVersion": row[3] or "",
                    "assetId": str(row[4]),
                    "firstSeenAt": self._timestamp(row[5]),
                    "lastSeenAt": self._timestamp(row[6]),
                    "lastSyncedAt": self._timestamp(row[7]) or None,
                    "providerParentId": row[8] or None,
                    "active": True,
                }
                for row in cursor.fetchall()
            ]

    def classify_provider_ci_mapping_import(
        self,
        kind: str,
        company_id: str,
        external_id: str,
        provider_parent_id: str | None = None,
    ) -> dict:
        """Preflight one provider identity before any canonical import mutation."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mapping.id, mapping.active, presence.state, company.slug,
                       mapping.external_parent_id
                FROM external_object_mappings mapping
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                JOIN configuration_items ci
                  ON ci.id = mapping.canonical_entity_id
                 AND mapping.canonical_entity_type = 'configuration_item'
                JOIN companies company ON company.id = ci.company_id
                LEFT JOIN integration_ci_presence presence
                  ON presence.mapping_id = mapping.id
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND mapping.external_object_type = 'configuration'
                  AND mapping.external_id = %s
                """,
                (PROVIDER_TO_DB.get(kind, kind), external_id),
            )
            row = cursor.fetchone()
        company_matches = bool(not row or row[3] == company_id)
        provider_parent_matches = bool(
            not row or not provider_parent_id or not row[4] or row[4] == provider_parent_id
        )
        decision, reason = _provider_ci_import_decision(
            mapping_exists=row is not None,
            mapping_active=bool(not row or row[1]),
            lifecycle_state=str(row[2] or "") if row else None,
            company_matches=company_matches,
            provider_parent_matches=provider_parent_matches,
        )
        return {
            "decision": decision,
            "mappingId": str(row[0]) if row else None,
            "companyMatches": company_matches,
            "providerParentMatches": provider_parent_matches,
            "reason": reason,
        }

    def _upsert_ci_presence_with_cursor(
        self,
        cursor: Any,
        *,
        policy_id: str,
        company_uuid: str,
        mapping: dict[str, Any],
        provider_parent_id: str,
        run_id: str,
        snapshot: dict[str, Any],
        state: str,
        absence_count: int,
        first_missing_at: str | None,
        last_missing_at: str | None,
        candidate_since: str | None,
        last_observed_at: str | None,
        evaluation_reason: str,
    ) -> None:
        """Upsert one mapping-keyed lifecycle row inside the caller's transaction."""

        cursor.execute(
            """
            INSERT INTO integration_ci_presence (
                id, policy_id, company_id, mapping_id, ci_id, external_id,
                external_name, provider_parent_id, state, absence_count,
                required_absences, minimum_missing_hours, first_missing_at,
                last_missing_at, candidate_since, last_observed_at,
                last_evaluated_at, last_evaluated_run_id, scope_mode,
                provider_read_complete, evaluation_reason,
                discovery_scope_fingerprint, policy_decision_fingerprint,
                connection_revision, policy_revision, snapshot_started_at,
                provider_read_completed_at, revision, created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s,
                %s, %s, %s, %s, %s, %s, %s::timestamptz,
                %s::timestamptz, %s::timestamptz, %s::timestamptz,
                %s::timestamptz, %s::uuid, %s, %s, %s, %s, %s,
                %s, %s, %s::timestamptz, %s::timestamptz, 1, now(), now()
            )
            ON CONFLICT (mapping_id) DO UPDATE SET
                policy_id = EXCLUDED.policy_id,
                company_id = EXCLUDED.company_id,
                ci_id = EXCLUDED.ci_id,
                external_id = EXCLUDED.external_id,
                external_name = EXCLUDED.external_name,
                provider_parent_id = EXCLUDED.provider_parent_id,
                state = EXCLUDED.state,
                absence_count = EXCLUDED.absence_count,
                required_absences = EXCLUDED.required_absences,
                minimum_missing_hours = EXCLUDED.minimum_missing_hours,
                first_missing_at = EXCLUDED.first_missing_at,
                last_missing_at = EXCLUDED.last_missing_at,
                candidate_since = EXCLUDED.candidate_since,
                last_observed_at = EXCLUDED.last_observed_at,
                last_evaluated_at = EXCLUDED.last_evaluated_at,
                last_evaluated_run_id = EXCLUDED.last_evaluated_run_id,
                scope_mode = EXCLUDED.scope_mode,
                provider_read_complete = EXCLUDED.provider_read_complete,
                evaluation_reason = EXCLUDED.evaluation_reason,
                discovery_scope_fingerprint = EXCLUDED.discovery_scope_fingerprint,
                policy_decision_fingerprint = EXCLUDED.policy_decision_fingerprint,
                connection_revision = EXCLUDED.connection_revision,
                policy_revision = EXCLUDED.policy_revision,
                snapshot_started_at = EXCLUDED.snapshot_started_at,
                provider_read_completed_at = EXCLUDED.provider_read_completed_at,
                revision = integration_ci_presence.revision + 1,
                updated_at = now()
            """,
            (
                canonical_uuid("integration_ci_presence", str(mapping["id"])),
                policy_id,
                company_uuid,
                mapping["id"],
                mapping["assetId"],
                mapping["externalId"],
                mapping.get("externalName") or mapping["externalId"],
                provider_parent_id,
                state,
                max(0, int(absence_count)),
                snapshot["requiredAbsences"],
                snapshot["minimumMissingHours"],
                first_missing_at,
                last_missing_at,
                candidate_since,
                last_observed_at,
                snapshot["providerReadCompletedAt"] or utc_now(),
                run_id,
                snapshot["scopeMode"],
                snapshot["providerReadComplete"],
                evaluation_reason[:1000],
                snapshot["discoveryScopeFingerprint"],
                snapshot["policyDecisionFingerprint"],
                snapshot["connectionRevision"],
                snapshot["policyRevision"],
                snapshot["snapshotStartedAt"],
                snapshot["providerReadCompletedAt"],
            ),
        )

    def _apply_postgres_ci_presence_snapshot(
        self,
        cursor: Any,
        *,
        kind: str,
        policy_id: str,
        company_uuid: str,
        company_id: str,
        provider_parent_id: str,
        run_id: str,
        presence_snapshot: dict[str, Any],
        expected_policy_revision: int,
    ) -> dict[str, int]:
        """Evaluate one presence snapshot under existing preview row locks."""

        snapshot = _normalized_ci_presence_snapshot(presence_snapshot)
        counts = {state: 0 for state in CI_PRESENCE_STATES}
        counts["skipped"] = 0
        cursor.execute(
            """
            SELECT policy.revision, integration.revision, integration.id,
                   policy.enabled, integration.enabled, integration.lifecycle_status
            FROM integration_ci_policies policy
            JOIN integration_connections integration
              ON integration.id = policy.integration_connection_id
            JOIN companies company ON company.id = policy.company_id
            WHERE policy.id = %s::uuid
              AND integration.provider = %s
              AND company.id = %s::uuid
              AND company.slug = %s
              AND policy.external_parent_id = %s
            FOR SHARE OF policy, integration
            """,
            (
                policy_id,
                PROVIDER_TO_DB.get(kind, kind),
                company_uuid,
                company_id,
                provider_parent_id,
            ),
        )
        revisions = cursor.fetchone()
        if (
            not snapshot["providerReadComplete"]
            or not revisions
            or snapshot["policyRevision"] != int(expected_policy_revision)
            or snapshot["policyRevision"] != int(revisions[0] or 0)
            or snapshot["connectionRevision"] != int(revisions[1] or 0)
            or not bool(revisions[4])
            or str(revisions[5] or "active") != "active"
        ):
            return counts
        integration_uuid = str(revisions[2])
        cursor.execute(
            """
            SELECT mapping.canonical_entity_id, mapping.last_synced_at
            FROM external_object_mappings mapping
            JOIN provider_company_observations observation
              ON observation.integration_connection_id = mapping.integration_connection_id
             AND observation.external_id = mapping.external_id
            WHERE mapping.integration_connection_id = %s::uuid
              AND mapping.external_object_type = 'company'
              AND mapping.external_id = %s
              AND mapping.canonical_entity_type = 'company'
              AND mapping.active = true
              AND observation.active = true
              AND observation.deleted = false
            FOR SHARE OF mapping, observation
            """,
            (integration_uuid, provider_parent_id),
        )
        company_mapping = cursor.fetchone()
        snapshot_started = parse_timestamp(snapshot["snapshotStartedAt"])
        company_mapping_changed = company_mapping[1] if company_mapping is not None else None
        if (
            not company_mapping
            or str(company_mapping[0]) != company_uuid
            or (
                company_mapping_changed is not None
                and snapshot_started is not None
                and company_mapping_changed > snapshot_started
            )
        ):
            return counts
        if any(
            item["providerParentId"] != provider_parent_id for item in snapshot["observedRecords"]
        ):
            raise ValueError("Presence snapshot includes a record outside the preview scope")
        cursor.execute(
            """
            SELECT mapping.id, mapping.external_id, mapping.external_name,
                   mapping.canonical_entity_id, mapping.external_parent_id,
                   mapping.active, mapping.first_seen_at
            FROM external_object_mappings mapping
            JOIN integration_connections integration
              ON integration.id = mapping.integration_connection_id
            JOIN configuration_items ci
              ON ci.id = mapping.canonical_entity_id
             AND mapping.canonical_entity_type = 'configuration_item'
            WHERE integration.provider = %s
              AND integration.company_id IS NULL
              AND mapping.external_object_type = 'configuration'
              AND ci.company_id = %s::uuid
            FOR UPDATE OF mapping
            """,
            (PROVIDER_TO_DB.get(kind, kind), company_uuid),
        )
        mappings = [
            {
                "id": str(row[0]),
                "externalId": row[1],
                "externalName": row[2] or "",
                "assetId": str(row[3]),
                "providerParentId": row[4] or "",
                "active": bool(row[5]),
                "firstSeenAt": self._timestamp(row[6]),
            }
            for row in cursor.fetchall()
        ]
        mapping_ids = [item["id"] for item in mappings]
        presence_by_mapping: dict[str, dict[str, Any]] = {}
        if mapping_ids:
            cursor.execute(
                """
                SELECT id, mapping_id, policy_id, provider_parent_id, state,
                       absence_count, first_missing_at, last_missing_at,
                       candidate_since, last_observed_at, last_evaluated_at,
                       last_evaluated_run_id, discovery_scope_fingerprint,
                       revision
                FROM integration_ci_presence
                WHERE mapping_id = ANY(%s::uuid[])
                FOR UPDATE
                """,
                (mapping_ids,),
            )
            presence_by_mapping = {
                str(row[1]): {
                    "id": str(row[0]),
                    "mappingId": str(row[1]),
                    "policyId": str(row[2]),
                    "providerParentId": row[3],
                    "state": row[4],
                    "absenceCount": int(row[5] or 0),
                    "firstMissingAt": self._timestamp(row[6]) or None,
                    "lastMissingAt": self._timestamp(row[7]) or None,
                    "candidateSince": self._timestamp(row[8]) or None,
                    "lastObservedAt": self._timestamp(row[9]) or None,
                    "lastEvaluatedAt": self._timestamp(row[10]) or None,
                    "lastEvaluatedRunId": str(row[11]) if row[11] else None,
                    "discoveryScopeFingerprint": str(row[12] or ""),
                    "revision": int(row[13] or 1),
                }
                for row in cursor.fetchall()
            }

        observed = {item["externalId"]: item for item in snapshot["observedRecords"]}
        started = parse_timestamp(snapshot["snapshotStartedAt"])
        evaluated_at = snapshot["providerReadCompletedAt"] or utc_now()
        evaluated = parse_timestamp(evaluated_at) or datetime.now(UTC)
        for mapping in mappings:
            row = presence_by_mapping.get(mapping["id"])
            observed_record = observed.get(str(mapping["externalId"]))
            if observed_record is not None:
                if mapping["providerParentId"] and (
                    mapping["providerParentId"] != provider_parent_id
                ):
                    counts["skipped"] += 1
                    continue
                cursor.execute(
                    """
                    UPDATE external_object_mappings
                    SET external_parent_id = %s
                    WHERE id = %s::uuid
                      AND (external_parent_id IS NULL OR external_parent_id = '')
                    """,
                    (provider_parent_id, mapping["id"]),
                )
                mapping["externalName"] = (
                    observed_record.get("externalName")
                    or mapping.get("externalName")
                    or mapping["externalId"]
                )
                next_state = "restore_ready" if not mapping["active"] else "observed"
                self._upsert_ci_presence_with_cursor(
                    cursor,
                    policy_id=policy_id,
                    company_uuid=company_uuid,
                    mapping=mapping,
                    provider_parent_id=provider_parent_id,
                    run_id=run_id,
                    snapshot=snapshot,
                    state=next_state,
                    absence_count=0,
                    first_missing_at=None,
                    last_missing_at=None,
                    candidate_since=None,
                    last_observed_at=evaluated_at,
                    evaluation_reason=(
                        "Provider observation returned for a retired mapping."
                        if next_state == "restore_ready"
                        else "Observed in the provider snapshot."
                    ),
                )
                counts[next_state] += 1
                continue

            # An absent legacy mapping has no trustworthy parent scope.
            if mapping["providerParentId"] != provider_parent_id:
                counts["skipped"] += 1
                continue
            if row is not None and row.get("lastEvaluatedRunId") == run_id:
                counts["skipped"] += 1
                continue
            first_seen = parse_timestamp(mapping.get("firstSeenAt"))
            if started is not None and first_seen is not None and first_seen > started:
                counts["skipped"] += 1
                continue
            if not mapping["active"]:
                if row is None or row.get("state") != "restore_ready":
                    counts["skipped"] += 1
                    continue
                self._upsert_ci_presence_with_cursor(
                    cursor,
                    policy_id=policy_id,
                    company_uuid=company_uuid,
                    mapping=mapping,
                    provider_parent_id=provider_parent_id,
                    run_id=run_id,
                    snapshot=snapshot,
                    state="retired",
                    absence_count=0,
                    first_missing_at=None,
                    last_missing_at=None,
                    candidate_since=None,
                    last_observed_at=row.get("lastObservedAt"),
                    evaluation_reason="The provider no longer reports the retired mapping.",
                )
                counts["retired"] += 1
                continue

            scope_changed = bool(
                row
                and row.get("discoveryScopeFingerprint")
                and row.get("discoveryScopeFingerprint") != snapshot["discoveryScopeFingerprint"]
            )
            absence_count = 0 if scope_changed else int((row or {}).get("absenceCount") or 0)
            first_missing_at = None if scope_changed else (row or {}).get("firstMissingAt")
            last_observed_at = (row or {}).get("lastObservedAt")
            if not snapshot["providerReadComplete"] or snapshot["scopeMode"] != "unfiltered":
                next_state = "not_evaluated"
                absence_count = 0
                first_missing_at = None
                last_missing_at = None
                candidate_since = None
                reason = (
                    "Provider read was incomplete; absence was not evaluated."
                    if not snapshot["providerReadComplete"]
                    else "Provider-side filtering was active; absence was not evaluated."
                )
            else:
                absence_count += 1
                first_missing_at = first_missing_at or evaluated_at
                first_missing = parse_timestamp(first_missing_at) or evaluated
                elapsed_hours = max(0.0, (evaluated - first_missing).total_seconds() / 3600)
                eligible = (
                    absence_count >= snapshot["requiredAbsences"]
                    and elapsed_hours >= snapshot["minimumMissingHours"]
                )
                next_state = "eligible" if eligible else "monitoring"
                last_missing_at = evaluated_at
                candidate_since = first_missing_at if eligible else None
                reason = (
                    "Required complete missing snapshots and minimum elapsed time were reached."
                    if eligible
                    else "Waiting for additional complete missing snapshots or elapsed time."
                )
            self._upsert_ci_presence_with_cursor(
                cursor,
                policy_id=policy_id,
                company_uuid=company_uuid,
                mapping=mapping,
                provider_parent_id=provider_parent_id,
                run_id=run_id,
                snapshot=snapshot,
                state=next_state,
                absence_count=absence_count,
                first_missing_at=first_missing_at,
                last_missing_at=last_missing_at,
                candidate_since=candidate_since,
                last_observed_at=last_observed_at,
                evaluation_reason=reason,
            )
            counts[next_state] += 1
        return counts

    def _ci_presence_from_row(self, row: tuple) -> dict[str, Any]:
        """Normalize one joined lifecycle row to the frontend contract."""

        current_policy = normalize_ci_policy(row[39] or {})
        return _public_ci_presence(
            {
                "id": str(row[0]),
                "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
                "companyId": row[2],
                "companyName": row[3] or "",
                "providerParentId": row[4],
                "mappingId": str(row[5]),
                "assetId": str(row[6]),
                "assetName": row[7] or "",
                "externalId": row[8],
                "externalName": row[9] or "",
                "state": row[10],
                "mappingActive": bool(row[11]),
                "absenceCount": int(row[12] or 0),
                "requiredAbsences": int(row[13] or 3),
                "minimumMissingHours": int(row[14] or 24),
                "firstMissingAt": self._timestamp(row[15]) or None,
                "lastMissingAt": self._timestamp(row[16]) or None,
                "candidateSince": self._timestamp(row[17]) or None,
                "lastObservedAt": self._timestamp(row[18]) or None,
                "lastEvaluatedAt": self._timestamp(row[19]) or None,
                "lastEvaluatedRunId": str(row[20]) if row[20] else None,
                "scopeMode": row[21],
                "providerReadComplete": bool(row[22]),
                "evaluationReason": row[23] or "",
                "discoveryScopeFingerprint": str(row[24] or ""),
                "policyDecisionFingerprint": str(row[25] or ""),
                "connectionRevision": int(row[26] or 0),
                "policyRevision": int(row[27] or 0),
                "snapshotStartedAt": self._timestamp(row[28]) or None,
                "providerReadCompletedAt": self._timestamp(row[29]) or None,
                "revision": int(row[30] or 1),
                "reviewedBy": str(row[31]) if row[31] else None,
                "retiredByName": row[32] or "",
                "reviewedAt": self._timestamp(row[33]) or None,
                "reviewNotes": row[34] or "",
                "createdAt": self._timestamp(row[35]),
                "updatedAt": self._timestamp(row[36]),
                "policyExists": True,
                "currentPolicyRevision": int(row[37] or 0),
                "currentPolicyEnabled": bool(row[38]),
                "currentPolicyFiltered": bool(current_policy.get("providerFilterId")),
                "presenceEvidenceMaxAgeHours": _ci_presence_evidence_max_age_hours(current_policy),
                "integrationExists": True,
                "currentConnectionRevision": int(row[40] or 0),
                "currentIntegrationEnabled": bool(row[41]),
                "currentIntegrationLifecycle": row[42] or "active",
                "providerCompanyMappingValid": bool(
                    row[43] and row[44] and row[45] and row[47] and row[48] and not row[49]
                ),
                "providerCompanyMappingChangedAt": self._timestamp(row[46]) or None,
            }
        )

    @staticmethod
    def _ci_presence_select_sql() -> str:
        """Return the fixed joined lifecycle projection used by list and actions."""

        return """
            SELECT presence.id, integration.provider, company.slug, company.name,
                   presence.provider_parent_id, presence.mapping_id, presence.ci_id,
                   ci.display_name, presence.external_id, presence.external_name,
                   presence.state, mapping.active, presence.absence_count,
                   presence.required_absences, presence.minimum_missing_hours,
                   presence.first_missing_at, presence.last_missing_at,
                   presence.candidate_since, presence.last_observed_at,
                   presence.last_evaluated_at, presence.last_evaluated_run_id,
                   presence.scope_mode, presence.provider_read_complete,
                   presence.evaluation_reason,
                   presence.discovery_scope_fingerprint,
                   presence.policy_decision_fingerprint,
                   presence.connection_revision, presence.policy_revision,
                   presence.snapshot_started_at, presence.provider_read_completed_at,
                   presence.revision, presence.reviewed_by, reviewer.email,
                   presence.reviewed_at, presence.review_notes,
                   presence.created_at, presence.updated_at,
                   policy.revision, policy.enabled, policy.filter_policy,
                   integration.revision, integration.enabled,
                   integration.lifecycle_status, company_mapping.id,
                   company_mapping.active,
                   (
                       company_mapping.canonical_entity_type = 'company'
                       AND company_mapping.canonical_entity_id = presence.company_id
                   ),
                   company_mapping.last_synced_at, company_observation.id,
                   company_observation.active, company_observation.deleted
            FROM integration_ci_presence presence
            JOIN integration_ci_policies policy ON policy.id = presence.policy_id
            JOIN integration_connections integration
              ON integration.id = policy.integration_connection_id
            JOIN companies company ON company.id = presence.company_id
            JOIN external_object_mappings mapping ON mapping.id = presence.mapping_id
            JOIN configuration_items ci ON ci.id = presence.ci_id
            LEFT JOIN users reviewer ON reviewer.id = presence.reviewed_by
            LEFT JOIN external_object_mappings company_mapping
              ON company_mapping.integration_connection_id = integration.id
             AND company_mapping.external_object_type = 'company'
             AND company_mapping.external_id = presence.provider_parent_id
            LEFT JOIN provider_company_observations company_observation
              ON company_observation.integration_connection_id = integration.id
             AND company_observation.external_id = presence.provider_parent_id
        """

    def list_ci_presence_lifecycle(
        self,
        provider: str | None = None,
        company_id: str | None = None,
        company_ids: list[str] | None = None,
        provider_parent_id: str | None = None,
        state: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return a server-paged and tenant-filtered mapping lifecycle queue."""

        if state is not None and state not in CI_PRESENCE_STATES:
            raise ValueError("Presence lifecycle state is invalid")
        database_provider = PROVIDER_TO_DB.get(provider, provider) if provider else None
        restrict_companies = company_ids is not None
        permitted_companies = sorted(set(company_ids or []))
        needle = str(search or "").strip()
        filters = (
            database_provider,
            database_provider,
            company_id,
            company_id,
            restrict_companies,
            permitted_companies,
            provider_parent_id,
            provider_parent_id,
            state,
            state,
            needle,
            needle,
        )
        where = """
            WHERE (%s::text IS NULL OR integration.provider = %s)
              AND (%s::text IS NULL OR company.slug = %s)
              AND (%s = false OR company.slug = ANY(%s::text[]))
              AND (%s::text IS NULL OR presence.provider_parent_id = %s)
              AND (%s::text IS NULL OR presence.state = %s)
              AND (%s = '' OR strpos(lower(concat_ws(' ', presence.external_name,
                  presence.external_id, ci.display_name, company.name)), lower(%s)) > 0)
        """
        joins = """
            FROM integration_ci_presence presence
            JOIN integration_ci_policies policy ON policy.id = presence.policy_id
            JOIN integration_connections integration
              ON integration.id = policy.integration_connection_id
            JOIN companies company ON company.id = presence.company_id
            JOIN external_object_mappings mapping ON mapping.id = presence.mapping_id
            JOIN configuration_items ci ON ci.id = presence.ci_id
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT count(*),
                       count(*) FILTER (WHERE presence.state = 'observed'),
                       count(*) FILTER (WHERE presence.state = 'monitoring'),
                       count(*) FILTER (WHERE presence.state = 'eligible'),
                       count(*) FILTER (WHERE presence.state = 'not_evaluated'),
                       count(*) FILTER (WHERE presence.state = 'retired'),
                       count(*) FILTER (WHERE presence.state = 'restore_ready')
                {joins}
                {where}
                """,  # nosec B608 -- only fixed internal SQL fragments are composed.
                filters,
            )
            aggregate = cursor.fetchone() or (0, 0, 0, 0, 0, 0, 0)
            cursor.execute(
                f"""
                {self._ci_presence_select_sql()}
                {where}
                ORDER BY presence.updated_at DESC, presence.id
                LIMIT %s OFFSET %s
                """,  # nosec B608 -- only fixed internal SQL fragments are composed.
                (*filters, max(1, min(int(limit or 50), 500)), max(0, int(offset or 0))),
            )
            items = [self._ci_presence_from_row(row) for row in cursor.fetchall()]
        return {
            "items": items,
            "total": int(aggregate[0]),
            "summary": {
                "total": int(aggregate[0]),
                "observed": int(aggregate[1]),
                "monitoring": int(aggregate[2]),
                "eligible": int(aggregate[3]),
                "notEvaluated": int(aggregate[4]),
                "retired": int(aggregate[5]),
                "restoreReady": int(aggregate[6]),
            },
        }

    def _change_ci_presence_mapping(
        self,
        presence_id: str,
        expected_revision: int,
        notes: str,
        actor_id: str | None,
        *,
        action: str,
    ) -> dict | None:
        """Apply one optimistic retire or restore transition atomically."""

        try:
            parsed_id = str(uuid.UUID(presence_id))
        except (TypeError, ValueError):
            return None
        review_notes = str(notes or "").strip()
        if not review_notes:
            raise ValueError("Review notes are required")
        expected_state = "eligible" if action == "retire" else "restore_ready"
        next_state = "retired" if action == "retire" else "observed"
        expected_active = action == "retire"
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT policy.revision, policy.enabled, policy.filter_policy,
                       integration.id, integration.revision, integration.enabled,
                       integration.lifecycle_status, presence.company_id,
                       presence.provider_parent_id, presence.policy_revision,
                       presence.connection_revision, presence.provider_read_complete,
                       presence.scope_mode, presence.snapshot_started_at,
                        presence.state, presence.last_observed_at,
                        presence.provider_read_completed_at
                FROM integration_ci_presence presence
                JOIN integration_ci_policies policy ON policy.id = presence.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                WHERE presence.id = %s::uuid
                FOR SHARE OF policy, integration
                """,
                (parsed_id,),
            )
            current = cursor.fetchone()
            if not current:
                return None
            cursor.execute(
                """
                SELECT mapping.id, mapping.active,
                       mapping.canonical_entity_type = 'company'
                           AND mapping.canonical_entity_id = %s::uuid,
                       mapping.last_synced_at, observation.id,
                       observation.active, observation.deleted
                FROM external_object_mappings mapping
                JOIN provider_company_observations observation
                  ON observation.integration_connection_id = mapping.integration_connection_id
                 AND observation.external_id = mapping.external_id
                WHERE mapping.integration_connection_id = %s::uuid
                  AND mapping.external_object_type = 'company'
                  AND mapping.external_id = %s
                FOR SHARE OF mapping, observation
                """,
                (str(current[7]), str(current[3]), str(current[8])),
            )
            company_mapping = cursor.fetchone()
            normalized_policy = normalize_ci_policy(current[2] or {})
            stale_reason = _ci_presence_stale_evidence_reason(
                {
                    "providerReadComplete": bool(current[11]),
                    "scopeMode": current[12],
                    "policyRevision": int(current[9] or 0),
                    "connectionRevision": int(current[10] or 0),
                    "snapshotStartedAt": self._timestamp(current[13]) or None,
                    "state": current[14],
                    "lastObservedAt": self._timestamp(current[15]) or None,
                    "providerReadCompletedAt": self._timestamp(current[16]) or None,
                    "policyExists": True,
                    "currentPolicyRevision": int(current[0] or 0),
                    "currentPolicyEnabled": bool(current[1]),
                    "currentPolicyFiltered": bool(normalized_policy.get("providerFilterId")),
                    "presenceEvidenceMaxAgeHours": _ci_presence_evidence_max_age_hours(
                        normalized_policy
                    ),
                    "integrationExists": True,
                    "currentConnectionRevision": int(current[4] or 0),
                    "currentIntegrationEnabled": bool(current[5]),
                    "currentIntegrationLifecycle": current[6] or "active",
                    "providerCompanyMappingValid": bool(
                        company_mapping
                        and company_mapping[1]
                        and company_mapping[2]
                        and company_mapping[4]
                        and company_mapping[5]
                        and not company_mapping[6]
                    ),
                    "providerCompanyMappingChangedAt": (
                        self._timestamp(company_mapping[3]) if company_mapping is not None else None
                    ),
                }
            )
            if stale_reason:
                raise ValueError(f"Presence evidence is stale: {stale_reason}")
            cursor.execute(
                f"""
                {self._ci_presence_select_sql()}
                WHERE presence.id = %s::uuid
                FOR UPDATE OF presence, mapping
                """,  # nosec B608 -- fixed internal projection only.
                (parsed_id,),
            )
            selected = cursor.fetchone()
            if not selected:
                return None
            before = self._ci_presence_from_row(selected)
            if int(before["revision"]) != int(expected_revision):
                raise ValueError("Presence lifecycle item changed; reload before saving")
            if before.get("staleEvidenceReason"):
                raise ValueError(f"Presence evidence is stale: {before['staleEvidenceReason']}")
            if before["state"] != expected_state or before["mappingActive"] != expected_active:
                raise ValueError(
                    "Only an eligible active mapping can be retired"
                    if action == "retire"
                    else "Only a restore-ready retired mapping can be restored"
                )
            cursor.execute(
                "UPDATE external_object_mappings SET active = %s WHERE id = %s::uuid",
                (not expected_active, before["mappingId"]),
            )
            retired_candidates = 0
            if action == "retire":
                cursor.execute(
                    """
                    UPDATE ci_relationship_candidates
                    SET retired_at = now(),
                        revision = revision + 1,
                        updated_at = now()
                    WHERE source_mapping_id = %s::uuid
                      AND state = 'pending'
                      AND retired_at IS NULL
                    """,
                    (before["mappingId"],),
                )
                retired_candidates = int(cursor.rowcount or 0)
            cursor.execute(
                """
                UPDATE integration_ci_presence
                SET state = %s,
                    absence_count = CASE WHEN %s = 'observed' THEN 0 ELSE absence_count END,
                    first_missing_at = CASE WHEN %s = 'observed' THEN NULL ELSE first_missing_at END,
                    last_missing_at = CASE WHEN %s = 'observed' THEN NULL ELSE last_missing_at END,
                    candidate_since = CASE WHEN %s = 'observed' THEN NULL ELSE candidate_since END,
                    evaluation_reason = %s,
                    reviewed_by = %s::uuid,
                    reviewed_at = now(),
                    review_notes = %s,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    next_state,
                    next_state,
                    next_state,
                    next_state,
                    next_state,
                    (
                        "An administrator retired the immutable provider mapping."
                        if action == "retire"
                        else "An administrator restored the observed provider mapping."
                    ),
                    actor_id,
                    review_notes[:2000],
                    parsed_id,
                ),
            )
            if action == "restore":
                cursor.execute(
                    """
                    UPDATE integration_ci_review_items item
                    SET state = 'resolved', reviewed_by = %s::uuid, reviewed_at = now()
                    FROM integration_ci_presence presence
                    WHERE presence.id = %s::uuid
                      AND item.policy_id = presence.policy_id
                      AND item.external_id = presence.external_id
                      AND item.state = 'pending'
                    """,
                    (actor_id, parsed_id),
                )
            cursor.execute(
                f"""
                {self._ci_presence_select_sql()}
                WHERE presence.id = %s::uuid
                """,  # nosec B608 -- fixed internal projection only.
                (parsed_id,),
            )
            after = self._ci_presence_from_row(cursor.fetchone())
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "integration_ci_presence",
                parsed_id,
                "mapping_retired" if action == "retire" else "mapping_restored",
                before,
                after,
                reason=review_notes[:2000],
                metadata={
                    "mappingId": before["mappingId"],
                    "assetId": before["assetId"],
                    "retiredPendingRelationshipCandidates": retired_candidates,
                },
            )
        return after

    def retire_ci_presence_mapping(
        self,
        presence_id: str,
        expected_revision: int,
        notes: str,
        actor_id: str | None,
    ) -> dict | None:
        """Deactivate an eligible mapping without touching its canonical CI."""

        return self._change_ci_presence_mapping(
            presence_id,
            expected_revision,
            notes,
            actor_id,
            action="retire",
        )

    def restore_ci_presence_mapping(
        self,
        presence_id: str,
        expected_revision: int,
        notes: str,
        actor_id: str | None,
    ) -> dict | None:
        """Reactivate an observed mapping after explicit administrator review."""

        return self._change_ci_presence_mapping(
            presence_id,
            expected_revision,
            notes,
            actor_id,
            action="restore",
        )

    def get_ci_sync_policy(self, kind: str, company_id: str, provider_parent_id: str) -> dict:
        """Return one provider/customer CI policy from canonical PostgreSQL."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT policy.id, policy.filter_policy, policy.sync_mode,
                       policy.interval_minutes, policy.enabled, policy.revision,
                       policy.updated_at, policy.next_run_at, policy.last_run_at,
                       policy.last_success_at, policy.last_error,
                       policy.consecutive_failures
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND policy.external_parent_id = %s
                  AND policy.external_object_type = 'configuration'
                """,
                (PROVIDER_TO_DB.get(kind, kind), company_id, provider_parent_id),
            )
            row = cursor.fetchone()
        if not row:
            return {
                "id": "",
                "provider": kind,
                "companyId": company_id,
                "providerParentId": provider_parent_id,
                **normalize_ci_policy(None),
                "revision": 0,
                "updatedAt": None,
                "nextRunAt": None,
                "lastRunAt": None,
                "lastSuccessAt": None,
                "lastError": "",
                "consecutiveFailures": 0,
                "backoffActive": False,
                "retryDelayMinutes": 0,
            }
        normalized = normalize_ci_policy(
            {
                **(row[1] or {}),
                "syncMode": row[2],
                "intervalMinutes": row[3],
                "enabled": row[4],
            }
        )
        failures = int(row[11] or 0)
        return {
            "id": str(row[0]),
            "provider": kind,
            "companyId": company_id,
            "providerParentId": provider_parent_id,
            **normalized,
            "revision": int(row[5]),
            "updatedAt": self._timestamp(row[6]),
            "nextRunAt": self._timestamp(row[7]) or None,
            "lastRunAt": self._timestamp(row[8]) or None,
            "lastSuccessAt": self._timestamp(row[9]) or None,
            "lastError": row[10] or "",
            "consecutiveFailures": failures,
            "backoffActive": failures > 0,
            "retryDelayMinutes": ci_sync_retry_delay_minutes(failures) if failures else 0,
        }

    def update_ci_sync_policy(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        policy: dict,
        expected_revision: int | None = None,
        actor_id: str | None = None,
    ) -> dict:
        """Persist an audited CI filter/schedule policy with optimistic concurrency."""

        before = self.get_ci_sync_policy(kind, company_id, provider_parent_id)
        current_revision = int(before.get("revision") or 0)
        if expected_revision is not None and expected_revision != current_revision:
            raise ValueError("CI policy changed; reload before saving")
        normalized = normalize_ci_policy(policy)
        filter_policy = {
            key: normalized[key]
            for key in (
                "providerFilterId",
                "typeMode",
                "includedTypeIds",
                "typeMappings",
                "blockUnmappedTypes",
                "statusMode",
                "includedStatusIds",
                "excludedExternalIds",
                "graphqlOrganizationIds",
                "relationshipAutomationMode",
                "relationshipAutoApproveTypes",
                "relationshipMinConfidence",
                "relationshipMinObservations",
                "relationshipMaxEvidenceAgeHours",
                "missingDeviceRequiredSnapshots",
                "missingDeviceMinimumHours",
            )
        }
        filter_policy["enrichmentMode"] = str(normalized.get("enrichmentMode") or "balanced")
        policy_uuid = canonical_uuid(
            "integration_ci_policy",
            f"{kind}:{company_id}:{provider_parent_id}:configuration",
        )
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM integration_connections WHERE provider = %s AND company_id IS NULL ORDER BY created_at LIMIT 1",
                (PROVIDER_TO_DB.get(kind, kind),),
            )
            connection_row = cursor.fetchone()
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not connection_row or not company_row:
                raise ValueError("Integration connection or customer not found")
            cursor.execute(
                """
                INSERT INTO integration_ci_policies (
                    id, integration_connection_id, company_id, external_parent_id,
                    external_object_type, filter_policy, sync_mode,
                    interval_minutes, enabled, revision, updated_at, next_run_at,
                    lease_owner, lease_until
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, 'configuration', %s::jsonb,
                    %s, %s, %s, 1, now(), CASE WHEN %s THEN now() ELSE NULL END,
                    NULL, NULL
                )
                ON CONFLICT (
                    integration_connection_id, company_id, external_parent_id, external_object_type
                ) DO UPDATE SET filter_policy = EXCLUDED.filter_policy,
                    sync_mode = EXCLUDED.sync_mode,
                    interval_minutes = EXCLUDED.interval_minutes,
                    enabled = EXCLUDED.enabled,
                    revision = integration_ci_policies.revision + 1,
                    updated_at = now(),
                    next_run_at = CASE WHEN EXCLUDED.enabled
                                           AND EXCLUDED.sync_mode = 'continuous_preview'
                                       THEN now() ELSE NULL END
                WHERE %s::integer IS NULL
                   OR integration_ci_policies.revision = %s
                RETURNING id, revision
                """,
                (
                    policy_uuid,
                    connection_row[0],
                    company_row[0],
                    provider_parent_id,
                    json.dumps(filter_policy),
                    normalized["syncMode"],
                    normalized["intervalMinutes"],
                    normalized["enabled"],
                    normalized["enabled"] and normalized["syncMode"] == "continuous_preview",
                    expected_revision,
                    expected_revision,
                ),
            )
            stored_row = cursor.fetchone()
            if not stored_row:
                raise ValueError("CI policy changed; reload before saving")
            stored_id = str(stored_row[0])
            stored_revision = int(stored_row[1])
            after = {
                "id": stored_id,
                "provider": kind,
                "companyId": company_id,
                "providerParentId": provider_parent_id,
                **normalized,
                "revision": stored_revision,
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "integration_ci_policy",
                stored_id,
                "updated" if current_revision else "created",
                before if current_revision else None,
                after,
                metadata={"provider": kind, "providerParentId": provider_parent_id},
            )
        return self.get_ci_sync_policy(kind, company_id, provider_parent_id)

    def _ci_sync_policy_from_row(self, row: tuple) -> dict:
        """Build the public CI policy representation from a canonical query row."""

        normalized = normalize_ci_policy(
            {
                **(row[5] or {}),
                "syncMode": row[6],
                "intervalMinutes": row[7],
                "enabled": row[8],
            }
        )
        failures = int(row[15] or 0)
        return {
            "id": str(row[0]),
            "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
            "companyId": row[2],
            "companyName": row[3],
            "providerParentId": row[4],
            **normalized,
            "revision": int(row[9]),
            "updatedAt": self._timestamp(row[10]),
            "nextRunAt": self._timestamp(row[11]) or None,
            "lastRunAt": self._timestamp(row[12]) or None,
            "lastSuccessAt": self._timestamp(row[13]) or None,
            "lastError": row[14] or "",
            "consecutiveFailures": failures,
            "backoffActive": failures > 0,
            "retryDelayMinutes": ci_sync_retry_delay_minutes(failures) if failures else 0,
            "leaseOwner": row[16] or None,
            "leaseUntil": self._timestamp(row[17]) or None,
        }

    def _get_ci_sync_policy_by_id(self, policy_id: str) -> dict | None:
        """Return one canonical CI policy without scanning the complete policy set."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT policy.id, integration.provider, company.slug, company.name,
                       policy.external_parent_id, policy.filter_policy, policy.sync_mode,
                       policy.interval_minutes, policy.enabled, policy.revision,
                       policy.updated_at, policy.next_run_at, policy.last_run_at,
                       policy.last_success_at, policy.last_error,
                       policy.consecutive_failures, policy.lease_owner, policy.lease_until
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE policy.id = %s::uuid
                  AND policy.external_object_type = 'configuration'
                """,
                (policy_id,),
            )
            row = cursor.fetchone()
        return self._ci_sync_policy_from_row(row) if row else None

    def list_ci_sync_policies(self, kind: str | None = None) -> list[dict]:
        """Return canonical CI policies and worker scheduling health."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT policy.id, integration.provider, company.slug, company.name,
                       policy.external_parent_id, policy.filter_policy, policy.sync_mode,
                       policy.interval_minutes, policy.enabled, policy.revision,
                       policy.updated_at, policy.next_run_at, policy.last_run_at,
                       policy.last_success_at, policy.last_error,
                       policy.consecutive_failures, policy.lease_owner, policy.lease_until
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE (%s::text IS NULL OR integration.provider = %s)
                ORDER BY company.name, policy.external_parent_id
                """,
                (provider, provider),
            )
            rows = cursor.fetchall()
        return [self._ci_sync_policy_from_row(row) for row in rows]

    def claim_due_ci_sync_policy(
        self, kind: str, worker_id: str, lease_seconds: int = 300
    ) -> dict | None:
        """Atomically lease one due policy so multiple replicas cannot duplicate work."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH due AS (
                    SELECT policy.id, integration.provider, company.slug AS company_slug,
                           company.name AS company_name
                    FROM integration_ci_policies policy
                    JOIN integration_connections integration
                      ON integration.id = policy.integration_connection_id
                    JOIN companies company ON company.id = policy.company_id
                    WHERE integration.provider = %s
                      AND integration.enabled = true
                      AND integration.lifecycle_status = 'active'
                      AND policy.enabled = true
                      AND policy.sync_mode = 'continuous_preview'
                      AND (policy.next_run_at IS NULL OR policy.next_run_at <= now())
                      AND (policy.lease_until IS NULL OR policy.lease_until < now())
                      AND NOT EXISTS (
                          SELECT 1
                          FROM sync_runs run
                          WHERE run.policy_id = policy.id
                            AND run.status IN ('queued', 'running')
                      )
                    ORDER BY policy.next_run_at NULLS FIRST, policy.updated_at
                    FOR UPDATE OF policy SKIP LOCKED
                    LIMIT 1
                )
                UPDATE integration_ci_policies policy
                SET lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second')
                FROM due
                WHERE policy.id = due.id
                RETURNING policy.id, due.provider, due.company_slug, due.company_name,
                          policy.external_parent_id, policy.filter_policy, policy.sync_mode,
                          policy.interval_minutes, policy.enabled, policy.revision,
                          policy.updated_at, policy.next_run_at, policy.last_run_at,
                          policy.last_success_at, policy.last_error,
                          policy.consecutive_failures, policy.lease_owner, policy.lease_until
                """,
                (provider, worker_id[:120], max(30, min(lease_seconds, 3600))),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self._ci_sync_policy_from_row(row)

    def claim_ci_sync_policy_now(
        self, policy_id: str, worker_id: str, lease_seconds: int = 300
    ) -> dict | None:
        """Atomically lease one saved policy for an operator-triggered preview."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies policy
                SET lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second')
                FROM integration_connections integration, companies company
                WHERE policy.id = %s::uuid
                  AND integration.id = policy.integration_connection_id
                  AND company.id = policy.company_id
                  AND integration.enabled = true
                  AND integration.lifecycle_status = 'active'
                  AND (policy.lease_until IS NULL OR policy.lease_until < now())
                  AND NOT EXISTS (
                      SELECT 1
                      FROM sync_runs run
                      WHERE run.policy_id = policy.id
                        AND run.status IN ('queued', 'running')
                  )
                RETURNING policy.id, integration.provider, company.slug, company.name,
                          policy.external_parent_id, policy.filter_policy, policy.sync_mode,
                          policy.interval_minutes, policy.enabled, policy.revision,
                          policy.updated_at, policy.next_run_at, policy.last_run_at,
                          policy.last_success_at, policy.last_error,
                          policy.consecutive_failures, policy.lease_owner, policy.lease_until
                """,
                (
                    worker_id[:120],
                    max(30, min(lease_seconds, 3600)),
                    policy_id,
                ),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self._ci_sync_policy_from_row(row)

    def complete_ci_sync_policy_run(
        self,
        policy_id: str,
        *,
        lease_owner: str,
        success: bool,
        error: str = "",
    ) -> dict | None:
        """Finish an owned policy lease and schedule normal or backoff execution."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET last_run_at = now(),
                    last_success_at = CASE WHEN %s THEN now() ELSE last_success_at END,
                    last_error = CASE WHEN %s THEN NULL ELSE %s END,
                    consecutive_failures = CASE WHEN %s THEN 0 ELSE consecutive_failures + 1 END,
                    next_run_at = CASE WHEN enabled AND sync_mode = 'continuous_preview'
                        THEN now() + (
                            CASE WHEN %s THEN interval_minutes
                                 ELSE LEAST(
                                     1440,
                                     15 * power(2, LEAST(consecutive_failures, 7))
                                 )
                            END * interval '1 minute'
                        ) ELSE NULL END,
                    lease_owner = NULL,
                    lease_until = NULL
                WHERE id = %s::uuid
                  AND lease_owner = %s
                RETURNING id
                """,
                (
                    success,
                    success,
                    str(error)[:1000],
                    success,
                    success,
                    policy_id,
                    str(lease_owner)[:120],
                ),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self._get_ci_sync_policy_by_id(policy_id)

    def renew_ci_sync_policy_run(
        self,
        policy_id: str,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> dict | None:
        """Extend an unexpired canonical policy lease without reacquiring it."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_until = now() + (%s * interval '1 second')
                WHERE id = %s::uuid
                  AND lease_owner = %s
                  AND lease_until > now()
                RETURNING id
                """,
                (
                    max(30, min(int(lease_seconds), 3600)),
                    policy_id,
                    str(lease_owner)[:120],
                ),
            )
            renewed = cursor.fetchone()
        if not renewed:
            return None
        return self._get_ci_sync_policy_by_id(policy_id)

    def _insert_ci_policy_preview_run_with_cursor(
        self,
        cursor: Any,
        *,
        kind: str,
        integration_id: str,
        company_uuid: str,
        policy_id: str,
        company_id: str,
        run: dict,
        actor_id: str | None,
    ) -> dict:
        """Insert terminal direct-preview evidence inside the caller's transaction."""

        status_to_db = {
            "success": "succeeded",
            "review_required": "review_required",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        run_status = str(run.get("status") or "failed")
        database_status = status_to_db.get(run_status, "failed")
        run_uuid = canonical_uuid("sync_run", str(run["id"]))
        attributes = {
            "apiStatus": run_status,
            **deepcopy(run.get("attributes") or {}),
        }
        cursor.execute(
            """
            INSERT INTO sync_runs (
                id, integration_connection_id, company_id, policy_id,
                status, started_at, finished_at, discovered_count,
                created_count, updated_count, review_count, error_summary,
                message, attributes, requested_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                %s, COALESCE(%s::timestamptz, now()),
                COALESCE(%s::timestamptz, now()), %s, %s, %s, %s,
                %s, %s, %s::jsonb, now(), now()
            )
            """,
            (
                run_uuid,
                integration_id,
                company_uuid,
                policy_id,
                database_status,
                run.get("startedAt") or None,
                run.get("finishedAt") or None,
                int(run.get("discovered") or 0),
                int(run.get("imported") or 0),
                int(run.get("updated") or 0),
                int(run.get("review") or 0),
                run.get("message") if database_status == "failed" else None,
                run.get("message") or "",
                json.dumps(attributes),
            ),
        )
        stored = {**deepcopy(run), "id": run_uuid}
        self._insert_audit(
            cursor,
            company_id,
            actor_id,
            "integration_connection",
            integration_id,
            "sync_completed",
            None,
            stored,
            metadata={"provider": kind, "policyId": policy_id},
        )
        return stored

    @staticmethod
    def _ci_policy_preview_attributes_match(
        attributes: dict,
        *,
        policy_id: str,
        company_id: str,
        provider_parent_id: str,
    ) -> bool:
        """Check direct-preview evidence against canonical policy scope."""

        return bool(
            str(attributes.get("policyId") or "") == policy_id
            and str(attributes.get("companyId") or "") == company_id
            and str(attributes.get("providerCompanyId") or "") == provider_parent_id
        )

    def _finish_ci_policy_preview_with_cursor(
        self,
        cursor: Any,
        *,
        policy_id: str,
        lease_owner: str,
        trigger: str,
        success: bool,
        error: str = "",
    ) -> None:
        """Finish an owned direct-preview policy inside the caller's transaction."""

        scheduled = trigger in {"continuous_preview", "manual_sync"}
        if scheduled:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET last_run_at = now(),
                    last_success_at = CASE WHEN %s THEN now() ELSE last_success_at END,
                    last_error = CASE WHEN %s THEN NULL ELSE %s END,
                    consecutive_failures = CASE
                        WHEN %s THEN 0
                        ELSE consecutive_failures + 1
                    END,
                    next_run_at = CASE
                        WHEN enabled AND sync_mode = 'continuous_preview'
                            THEN now() + (
                                CASE
                                    WHEN %s THEN interval_minutes
                                    ELSE LEAST(
                                        1440,
                                        15 * power(2, LEAST(consecutive_failures, 7))
                                    )
                                END * interval '1 minute'
                            )
                        ELSE NULL
                    END,
                    lease_owner = NULL,
                    lease_until = NULL
                WHERE id = %s::uuid
                  AND lease_owner = %s
                  AND lease_until > now()
                """,
                (
                    success,
                    success,
                    str(error)[:1000],
                    success,
                    success,
                    policy_id,
                    str(lease_owner)[:120],
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = NULL, lease_until = NULL
                WHERE id = %s::uuid
                  AND lease_owner = %s
                  AND lease_until > now()
                """,
                (policy_id, str(lease_owner)[:120]),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Preview policy lease is no longer owned")

    def publish_and_complete_ci_policy_preview(
        self,
        kind: str,
        policy_id: str,
        lease_owner: str,
        run: dict,
        review_items: list[dict],
        actor_id: str | None = None,
        *,
        presence_snapshot: dict[str, Any] | None = None,
    ) -> dict | None:
        """Atomically publish direct-preview evidence under its current policy lease."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        bounded_owner = str(lease_owner)[:120]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT integration.id, company.id, company.slug,
                       policy.external_parent_id
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE policy.id = %s::uuid
                  AND integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND policy.external_parent_id = %s
                  AND policy.lease_owner = %s
                  AND policy.lease_until > now()
                FOR UPDATE OF policy
                """,
                (
                    policy_id,
                    provider,
                    str((run.get("attributes") or {}).get("companyId") or ""),
                    str((run.get("attributes") or {}).get("providerCompanyId") or ""),
                    bounded_owner,
                ),
            )
            owned = cursor.fetchone()
            attributes = deepcopy(run.get("attributes") or {})
            if presence_snapshot is not None:
                attributes["connectionRevision"] = int(
                    _normalized_ci_presence_snapshot(presence_snapshot)["connectionRevision"]
                )
                run = {**deepcopy(run), "attributes": attributes}
            if not owned or not self._ci_policy_preview_attributes_match(
                attributes,
                policy_id=policy_id,
                company_id=str(owned[2]),
                provider_parent_id=str(owned[3]),
            ):
                return None
            integration_uuid = str(owned[0])
            company_uuid = str(owned[1])
            company_id = str(owned[2])
            stored = self._insert_ci_policy_preview_run_with_cursor(
                cursor,
                kind=kind,
                integration_id=integration_uuid,
                company_uuid=company_uuid,
                policy_id=policy_id,
                company_id=company_id,
                run=run,
                actor_id=actor_id,
            )
            queue_summary = self._replace_ci_review_items_with_cursor(
                cursor,
                policy_id,
                company_id,
                str(stored["id"]),
                review_items,
                actor_id,
            )
            if presence_snapshot is not None:
                self._apply_postgres_ci_presence_snapshot(
                    cursor,
                    kind=kind,
                    policy_id=policy_id,
                    company_uuid=company_uuid,
                    company_id=company_id,
                    provider_parent_id=str(owned[3]),
                    run_id=str(stored["id"]),
                    presence_snapshot=presence_snapshot,
                    expected_policy_revision=int(attributes.get("policyRevision") or 0),
                )
            self._finish_ci_policy_preview_with_cursor(
                cursor,
                policy_id=policy_id,
                lease_owner=bounded_owner,
                trigger=str(attributes.get("trigger") or ""),
                success=True,
            )
        policy = self._get_ci_sync_policy_by_id(policy_id)
        return {
            "run": _public_sync_run(stored),
            "queueSummary": queue_summary,
            "policy": policy,
        }

    def fail_and_complete_ci_policy_preview(
        self,
        kind: str,
        policy_id: str,
        lease_owner: str,
        run: dict,
        error: Any,
        actor_id: str | None = None,
    ) -> dict | None:
        """Atomically persist an owned direct-preview failure and policy outcome."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        bounded_owner = str(lease_owner)[:120]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT integration.id, company.id, company.slug,
                       policy.external_parent_id
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE policy.id = %s::uuid
                  AND integration.provider = %s
                  AND integration.company_id IS NULL
                  AND company.slug = %s
                  AND policy.external_parent_id = %s
                  AND policy.lease_owner = %s
                  AND policy.lease_until > now()
                FOR UPDATE OF policy
                """,
                (
                    policy_id,
                    provider,
                    str((run.get("attributes") or {}).get("companyId") or ""),
                    str((run.get("attributes") or {}).get("providerCompanyId") or ""),
                    bounded_owner,
                ),
            )
            owned = cursor.fetchone()
            attributes = run.get("attributes") or {}
            if not owned or not self._ci_policy_preview_attributes_match(
                attributes,
                policy_id=policy_id,
                company_id=str(owned[2]),
                provider_parent_id=str(owned[3]),
            ):
                return None
            stored = self._insert_ci_policy_preview_run_with_cursor(
                cursor,
                kind=kind,
                integration_id=str(owned[0]),
                company_uuid=str(owned[1]),
                policy_id=policy_id,
                company_id=str(owned[2]),
                run=run,
                actor_id=actor_id,
            )
            self._finish_ci_policy_preview_with_cursor(
                cursor,
                policy_id=policy_id,
                lease_owner=bounded_owner,
                trigger=str(attributes.get("trigger") or ""),
                success=False,
                error=str(error)[:1000],
            )
        policy = self._get_ci_sync_policy_by_id(policy_id)
        return {"run": _public_sync_run(stored), "policy": policy}

    def _replace_ci_review_items_with_cursor(
        self,
        cursor: Any,
        policy_id: str,
        company_id: str,
        run_id: str,
        items: list[dict],
        actor_id: str | None = None,
    ) -> dict[str, int]:
        """Mutate canonical review observations within the caller's transaction."""

        reviewable = [
            deepcopy(item)
            for item in items
            if item.get("action") in {"create", "update", "link", "conflict"}
        ]
        seen = [str(item["externalId"]) for item in reviewable]
        cursor.execute(
            """
            SELECT company.id
            FROM companies company
            JOIN integration_ci_policies policy ON policy.company_id = company.id
            WHERE company.slug = %s
              AND policy.id = %s::uuid
            """,
            (company_id, policy_id),
        )
        company_row = cursor.fetchone()
        if not company_row:
            raise ValueError("CI policy customer scope does not match")
        company_uuid = str(company_row[0])
        cursor.execute(
            """
            SELECT external_id FROM integration_ci_review_items
            WHERE policy_id = %s::uuid
            """,
            (policy_id,),
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
        cursor.execute(
            """
            UPDATE integration_ci_review_items
            SET state = 'resolved', reviewed_at = now()
            WHERE policy_id = %s::uuid
              AND state = 'pending'
              AND NOT (external_id = ANY(%s::text[]))
            """,
            (policy_id, seen),
        )
        resolved = cursor.rowcount
        for item in reviewable:
            record = deepcopy(item.get("record") or {})
            content = {
                "action": item.get("action"),
                "assetId": item.get("assetId"),
                "reason": item.get("reason", ""),
                "changes": item.get("changes") or {},
                "blockedFields": item.get("blockedFields") or [],
                "fieldDecisions": item.get("fieldDecisions") or [],
                "record": record,
            }
            content_hash = hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            item_uuid = canonical_uuid(
                "integration_ci_review_item", f"{policy_id}:{item['externalId']}"
            )
            cursor.execute(
                """
                INSERT INTO integration_ci_review_items AS current (
                    id, policy_id, company_id, last_sync_run_id, external_id,
                    external_name, decision, candidate_ci_id, reason,
                    provider_record, evidence, content_hash, state,
                    first_seen_at, last_seen_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s,
                    %s::uuid, %s, %s::jsonb, %s::jsonb, %s, 'pending', now(), now()
                )
                ON CONFLICT (policy_id, external_id) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    last_sync_run_id = EXCLUDED.last_sync_run_id,
                    external_name = EXCLUDED.external_name,
                    decision = EXCLUDED.decision,
                    candidate_ci_id = EXCLUDED.candidate_ci_id,
                    reason = EXCLUDED.reason,
                    provider_record = EXCLUDED.provider_record,
                    evidence = EXCLUDED.evidence,
                    content_hash = EXCLUDED.content_hash,
                    state = CASE
                        WHEN current.content_hash <> EXCLUDED.content_hash
                          OR current.state = 'resolved' THEN 'pending'
                        ELSE current.state END,
                    reviewed_by = CASE
                        WHEN current.content_hash <> EXCLUDED.content_hash
                          OR current.state = 'resolved' THEN NULL
                        ELSE current.reviewed_by END,
                    reviewed_at = CASE
                        WHEN current.content_hash <> EXCLUDED.content_hash
                          OR current.state = 'resolved' THEN NULL
                        ELSE current.reviewed_at END,
                    review_notes = CASE
                        WHEN current.content_hash <> EXCLUDED.content_hash
                          OR current.state = 'resolved' THEN NULL
                        ELSE current.review_notes END,
                    last_seen_at = now()
                """,
                (
                    item_uuid,
                    policy_id,
                    company_uuid,
                    run_id,
                    str(item["externalId"]),
                    item.get("name") or record.get("name") or str(item["externalId"]),
                    item["action"],
                    item.get("assetId"),
                    item.get("reason") or "",
                    json.dumps(record),
                    json.dumps(
                        {
                            "assetName": item.get("assetName") or "",
                            "changedFields": item.get("changedFields") or [],
                            "blockedFields": item.get("blockedFields") or [],
                            "fieldDecisions": item.get("fieldDecisions") or [],
                            "changes": item.get("changes") or {},
                        }
                    ),
                    content_hash,
                ),
            )
        summary = {
            "pending": len(reviewable),
            "created": sum(external_id not in existing for external_id in seen),
            "updated": sum(external_id in existing for external_id in seen),
            "resolved": resolved,
        }
        self._insert_audit(
            cursor,
            company_id,
            actor_id,
            "integration_ci_policy",
            policy_id,
            "review_queue_refreshed",
            None,
            summary,
            metadata={"syncRunId": run_id},
            actor_type="system" if actor_id is None else "user",
            source_system="integration_worker" if actor_id is None else "web",
        )
        return summary

    def replace_ci_review_items(
        self,
        policy_id: str,
        company_id: str,
        run_id: str,
        items: list[dict],
        actor_id: str | None = None,
    ) -> dict[str, int]:
        """Upsert current review observations and resolve items absent from the new snapshot."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            return self._replace_ci_review_items_with_cursor(
                cursor,
                policy_id,
                company_id,
                run_id,
                items,
                actor_id,
            )

    def list_ci_review_items(
        self,
        kind: str | None = None,
        company_id: str | None = None,
        state: str | None = "pending",
        limit: int = 250,
    ) -> list[dict]:
        """Return sanitized current review observations for MSP operators."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item.id, integration.provider, policy.id, company.slug, company.name,
                       policy.external_parent_id, item.external_id, item.external_name,
                       item.decision, item.candidate_ci_id, ci.display_name, item.reason,
                       item.provider_record, item.evidence, item.state,
                       item.first_seen_at, item.last_seen_at, item.reviewed_at,
                       item.review_notes
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE (%s::text IS NULL OR integration.provider = %s)
                  AND (%s::text IS NULL OR company.slug = %s)
                  AND (%s::text IS NULL OR item.state = %s)
                ORDER BY item.last_seen_at DESC, item.external_name
                LIMIT %s
                """,
                (
                    provider,
                    provider,
                    company_id,
                    company_id,
                    state,
                    state,
                    max(1, min(limit, 1000)),
                ),
            )
            rows = cursor.fetchall()
        return [self._ci_review_from_row(row) for row in rows]

    def _ci_review_from_row(self, row: tuple) -> dict:
        """Normalize one PostgreSQL review-queue row for API consumers."""

        record = row[12] or {}
        evidence = row[13] or {}
        return {
            "id": str(row[0]),
            "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
            "policyId": str(row[2]),
            "companyId": row[3],
            "companyName": row[4],
            "providerParentId": row[5],
            "externalId": row[6],
            "externalName": row[7],
            "action": row[8],
            "assetId": str(row[9]) if row[9] else None,
            "assetName": row[10] or evidence.get("assetName", ""),
            "reason": row[11] or "",
            "providerTypeName": record.get("providerTypeName") or record.get("type", ""),
            "providerStatusName": record.get("providerStatusName") or record.get("status", ""),
            "providerRecord": record,
            "evidence": evidence,
            "changedFields": evidence.get("changedFields", []),
            "blockedFields": evidence.get("blockedFields", []),
            "fieldDecisions": evidence.get("fieldDecisions", []),
            "state": row[14],
            "firstSeenAt": self._timestamp(row[15]),
            "lastSeenAt": self._timestamp(row[16]),
            "reviewedAt": self._timestamp(row[17]) or None,
            "reviewNotes": row[18] or "",
        }

    def query_ci_review_items(
        self,
        *,
        kind: str | None = None,
        company_id: str | None = None,
        company_ids: Iterable[str] | None = None,
        state: str | None = "pending",
        action: str | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return an exact, server-paged provider-neutral review workbench."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        needle = search.strip()
        restrict_companies = company_ids is not None
        permitted_companies = sorted(set(company_ids or []))
        filters = (
            provider,
            provider,
            company_id,
            company_id,
            restrict_companies,
            permitted_companies,
            state,
            state,
            needle,
            needle,
        )
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item.decision, count(*)
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE (%s::text IS NULL OR integration.provider = %s)
                  AND (%s::text IS NULL OR company.slug = %s)
                  AND (%s = false OR company.slug = ANY(%s::text[]))
                  AND (%s::text IS NULL OR item.state = %s)
                  AND (%s = '' OR strpos(lower(concat_ws(' ', item.external_name,
                      item.external_id, ci.display_name, item.reason)), lower(%s)) > 0)
                GROUP BY item.decision
                """,
                filters,
            )
            summary = {decision: 0 for decision in ("create", "update", "link", "conflict")}
            for decision, count in cursor.fetchall():
                if decision in summary:
                    summary[decision] = int(count)
            cursor.execute(
                """
                SELECT item.id, integration.provider, policy.id, company.slug, company.name,
                       policy.external_parent_id, item.external_id, item.external_name,
                       item.decision, item.candidate_ci_id, ci.display_name, item.reason,
                       item.provider_record, item.evidence, item.state,
                       item.first_seen_at, item.last_seen_at, item.reviewed_at,
                       item.review_notes, count(*) OVER()
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE (%s::text IS NULL OR integration.provider = %s)
                  AND (%s::text IS NULL OR company.slug = %s)
                  AND (%s = false OR company.slug = ANY(%s::text[]))
                  AND (%s::text IS NULL OR item.state = %s)
                  AND (%s = '' OR strpos(lower(concat_ws(' ', item.external_name,
                      item.external_id, ci.display_name, item.reason)), lower(%s)) > 0)
                  AND (%s::text IS NULL OR item.decision = %s)
                ORDER BY item.last_seen_at DESC, item.external_name
                LIMIT %s OFFSET %s
                """,
                (
                    *filters,
                    action,
                    action,
                    max(1, min(limit, 250)),
                    max(0, offset),
                ),
            )
            rows = cursor.fetchall()
        return {
            "items": [self._ci_review_from_row(row) for row in rows],
            "total": int(rows[0][19]) if rows else 0,
            "summary": summary,
        }

    def get_ci_review_item(self, item_id: str) -> dict | None:
        """Return one review item without depending on queue pagination."""

        try:
            parsed_id = str(uuid.UUID(item_id))
        except (TypeError, ValueError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item.id, integration.provider, policy.id, company.slug, company.name,
                       policy.external_parent_id, item.external_id, item.external_name,
                       item.decision, item.candidate_ci_id, ci.display_name, item.reason,
                       item.provider_record, item.evidence, item.state,
                       item.first_seen_at, item.last_seen_at, item.reviewed_at,
                       item.review_notes
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE item.id = %s::uuid
                """,
                (parsed_id,),
            )
            row = cursor.fetchone()
        return self._ci_review_from_row(row) if row else None

    def get_ci_review_item_by_identity(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        external_id: str,
        state: str | None = "pending",
    ) -> dict | None:
        """Return one exact provider observation without loading the review queue."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item.id, integration.provider, policy.id, company.slug, company.name,
                       policy.external_parent_id, item.external_id, item.external_name,
                       item.decision, item.candidate_ci_id, ci.display_name, item.reason,
                       item.provider_record, item.evidence, item.state,
                       item.first_seen_at, item.last_seen_at, item.reviewed_at,
                       item.review_notes
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE integration.provider = %s
                  AND company.slug = %s
                  AND policy.external_parent_id = %s
                  AND item.external_id = %s
                  AND (%s::text IS NULL OR item.state = %s)
                ORDER BY item.last_seen_at DESC
                LIMIT 1
                """,
                (
                    provider,
                    company_id,
                    provider_parent_id,
                    external_id,
                    state,
                    state,
                ),
            )
            row = cursor.fetchone()
        return self._ci_review_from_row(row) if row else None

    def count_ci_review_items(
        self,
        kind: str | None = None,
        company_id: str | None = None,
        state: str | None = "pending",
    ) -> int:
        """Count current CI review observations without loading queue payloads."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                WHERE (%s::text IS NULL OR integration.provider = %s)
                  AND (%s::text IS NULL OR company.slug = %s)
                  AND (%s::text IS NULL OR item.state = %s)
                """,
                (provider, provider, company_id, company_id, state, state),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def dismiss_ci_review_item(self, item_id: str, notes: str, actor_id: str) -> dict | None:
        """Dismiss one queue item; changed provider evidence reopens it later."""

        before = self.get_ci_review_item(item_id)
        if not before:
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_review_items
                SET state = 'dismissed', reviewed_by = %s::uuid,
                    reviewed_at = now(), review_notes = %s
                WHERE id = %s::uuid
                """,
                (actor_id, notes, item_id),
            )
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "integration_ci_review_item",
                item_id,
                "dismissed",
                before,
                {**before, "state": "dismissed", "reviewNotes": notes},
                reason=notes,
            )
        return self.get_ci_review_item(item_id)

    def ignore_ci_review_items(self, item_ids: list[str], notes: str, actor_id: str) -> list[dict]:
        """Atomically suppress selected immutable provider IDs for one CI policy."""

        items = [self.get_ci_review_item(item_id) for item_id in item_ids]
        if any(item is None for item in items):
            raise ValueError("One or more review items no longer exist")
        current = [item for item in items if item is not None]
        policy_ids = {item["policyId"] for item in current}
        if len(policy_ids) != 1:
            raise ValueError("Ignored configurations must belong to one customer policy")
        policy_id = next(iter(policy_ids))
        provider = current[0]["provider"]
        company_id = current[0]["companyId"]
        provider_parent_id = current[0]["providerParentId"]
        before_policy = self.get_ci_sync_policy(provider, company_id, provider_parent_id)
        stored: list[dict] = []
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT filter_policy, revision
                FROM integration_ci_policies
                WHERE id = %s::uuid
                FOR UPDATE
                """,
                (policy_id,),
            )
            policy_row = cursor.fetchone()
            if not policy_row:
                raise ValueError("CI policy not found")
            before_policy = {
                **before_policy,
                **normalize_ci_policy(policy_row[0] or {}),
                "revision": int(policy_row[1]),
            }
            excluded_ids = set(before_policy["excludedExternalIds"])
            excluded_ids.update(item["externalId"] for item in current)
            filter_policy = {
                key: before_policy[key]
                for key in (
                    "typeMode",
                    "includedTypeIds",
                    "typeMappings",
                    "blockUnmappedTypes",
                    "statusMode",
                    "includedStatusIds",
                    "providerFilterId",
                    "enrichmentMode",
                    "graphqlOrganizationIds",
                    "relationshipAutomationMode",
                    "relationshipAutoApproveTypes",
                    "relationshipMinConfidence",
                    "relationshipMinObservations",
                    "relationshipMaxEvidenceAgeHours",
                    "missingDeviceRequiredSnapshots",
                    "missingDeviceMinimumHours",
                )
            }
            filter_policy["excludedExternalIds"] = sorted(excluded_ids)
            for item in current:
                cursor.execute(
                    """
                    SELECT id, active, reason, ignored_at
                    FROM integration_object_suppressions
                    WHERE policy_id = %s::uuid
                      AND external_object_type = 'configuration'
                      AND external_id = %s
                    """,
                    (policy_id, item["externalId"]),
                )
                previous = cursor.fetchone()
                suppression_id = (
                    str(previous[0])
                    if previous
                    else canonical_uuid(
                        "integration_object_suppression",
                        f"{policy_id}:configuration:{item['externalId']}",
                    )
                )
                cursor.execute(
                    """
                    INSERT INTO integration_object_suppressions AS suppression (
                        id, policy_id, company_id, external_object_type, external_id,
                        external_name, provider_record, reason, active, ignored_by,
                        ignored_at, restored_by, restored_at, restore_reason,
                        created_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid,
                        (SELECT id FROM companies WHERE slug = %s),
                        'configuration', %s, %s, %s::jsonb, %s, true, %s::uuid,
                        now(), NULL, NULL, NULL, now(), now()
                    )
                    ON CONFLICT (policy_id, external_object_type, external_id)
                    DO UPDATE SET
                        external_name = EXCLUDED.external_name,
                        provider_record = EXCLUDED.provider_record,
                        reason = EXCLUDED.reason,
                        active = true,
                        ignored_by = EXCLUDED.ignored_by,
                        ignored_at = now(),
                        restored_by = NULL,
                        restored_at = NULL,
                        restore_reason = NULL,
                        updated_at = now()
                    RETURNING id, ignored_at, updated_at
                    """,
                    (
                        suppression_id,
                        policy_id,
                        company_id,
                        item["externalId"],
                        item["externalName"],
                        json.dumps(item.get("providerRecord") or {}),
                        notes,
                        actor_id,
                    ),
                )
                stored_row = cursor.fetchone()
                stored_item = {
                    "id": str(stored_row[0]),
                    "policyId": policy_id,
                    "companyId": company_id,
                    "provider": provider,
                    "providerParentId": provider_parent_id,
                    "externalObjectType": "configuration",
                    "externalId": item["externalId"],
                    "externalName": item["externalName"],
                    "providerRecord": item.get("providerRecord") or {},
                    "reason": notes,
                    "active": True,
                    "ignoredBy": actor_id,
                    "ignoredAt": self._timestamp(stored_row[1]),
                    "restoredBy": None,
                    "restoredAt": None,
                    "restoreReason": "",
                    "updatedAt": self._timestamp(stored_row[2]),
                }
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "integration_object_suppression",
                    stored_item["id"],
                    "reignored" if previous else "ignored",
                    (
                        {
                            "id": str(previous[0]),
                            "active": bool(previous[1]),
                            "reason": previous[2],
                            "ignoredAt": self._timestamp(previous[3]),
                        }
                        if previous
                        else None
                    ),
                    stored_item,
                    reason=notes,
                    metadata={
                        "provider": provider,
                        "policyId": policy_id,
                        "externalId": item["externalId"],
                    },
                )
                stored.append(stored_item)
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET filter_policy = %s::jsonb,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (json.dumps(filter_policy), policy_id),
            )
            cursor.execute(
                """
                UPDATE integration_ci_review_items
                SET state = 'resolved', reviewed_by = %s::uuid,
                    reviewed_at = now(), review_notes = %s
                WHERE id = ANY(%s::uuid[])
                """,
                (actor_id, notes, item_ids),
            )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "integration_ci_policy",
                policy_id,
                "suppression_updated",
                before_policy,
                {
                    **before_policy,
                    "excludedExternalIds": sorted(excluded_ids),
                    "revision": int(before_policy.get("revision") or 0) + 1,
                },
                reason=notes,
                metadata={"ignoredExternalIds": sorted(excluded_ids)},
            )
        return stored

    def _integration_object_suppression_from_row(self, row: tuple) -> dict:
        """Normalize one provider-object suppression query row."""

        return {
            "id": str(row[0]),
            "provider": PROVIDER_FROM_DB.get(row[1], row[1]),
            "policyId": str(row[2]),
            "companyId": row[3],
            "companyName": row[4],
            "providerParentId": row[5],
            "externalObjectType": row[6],
            "externalId": row[7],
            "externalName": row[8],
            "providerRecord": row[9] or {},
            "reason": row[10],
            "active": bool(row[11]),
            "ignoredBy": str(row[12]) if row[12] else None,
            "ignoredByName": row[13] or "",
            "ignoredAt": self._timestamp(row[14]),
            "restoredBy": str(row[15]) if row[15] else None,
            "restoredByName": row[16] or "",
            "restoredAt": self._timestamp(row[17]) or None,
            "restoreReason": row[18] or "",
            "createdAt": self._timestamp(row[19]),
            "updatedAt": self._timestamp(row[20]),
        }

    def query_integration_object_suppressions(
        self,
        *,
        kind: str | None = None,
        company_id: str | None = None,
        company_ids: Iterable[str] | None = None,
        active: bool | None = True,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Return exact, server-paged provider-object suppression history."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        restrict_companies = company_ids is not None
        permitted_companies = sorted(set(company_ids or []))
        needle = search.strip()
        filters = (
            provider,
            provider,
            company_id,
            company_id,
            restrict_companies,
            permitted_companies,
            active,
            active,
            needle,
            needle,
        )
        joins = """
            FROM integration_object_suppressions suppression
            JOIN integration_ci_policies policy ON policy.id = suppression.policy_id
            JOIN integration_connections integration
              ON integration.id = policy.integration_connection_id
            JOIN companies company ON company.id = suppression.company_id
            LEFT JOIN users ignored_user ON ignored_user.id = suppression.ignored_by
            LEFT JOIN users restored_user ON restored_user.id = suppression.restored_by
        """
        where = """
            WHERE (%s::text IS NULL OR integration.provider = %s)
              AND (%s::text IS NULL OR company.slug = %s)
              AND (%s = false OR company.slug = ANY(%s::text[]))
              AND (%s::boolean IS NULL OR suppression.active = %s)
              AND (%s = '' OR strpos(lower(concat_ws(' ', suppression.external_name,
                  suppression.external_id, suppression.reason,
                  suppression.restore_reason)), lower(%s)) > 0)
        """
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) {joins} {where}", filters)  # nosec B608
            count_row = cursor.fetchone()
            cursor.execute(
                f"""
                SELECT suppression.id, integration.provider, policy.id, company.slug,
                       company.name, policy.external_parent_id,
                       suppression.external_object_type, suppression.external_id,
                       suppression.external_name, suppression.provider_record,
                       suppression.reason, suppression.active, suppression.ignored_by,
                       ignored_user.email, suppression.ignored_at,
                       suppression.restored_by, restored_user.email,
                       suppression.restored_at, suppression.restore_reason,
                       suppression.created_at, suppression.updated_at
                {joins}
                {where}
                ORDER BY suppression.updated_at DESC, suppression.external_name
                LIMIT %s OFFSET %s
                """,  # nosec B608
                (*filters, max(1, min(limit, 250)), max(0, offset)),
            )
            rows = cursor.fetchall()
        return {
            "items": [self._integration_object_suppression_from_row(row) for row in rows],
            "total": int(count_row[0]) if count_row else 0,
        }

    def get_integration_object_suppression(self, suppression_id: str) -> dict | None:
        """Return one exact provider-object suppression for authorization checks."""

        try:
            parsed_id = str(uuid.UUID(suppression_id))
        except (TypeError, ValueError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT suppression.id, integration.provider, policy.id, company.slug,
                       company.name, policy.external_parent_id,
                       suppression.external_object_type, suppression.external_id,
                       suppression.external_name, suppression.provider_record,
                       suppression.reason, suppression.active, suppression.ignored_by,
                       ignored_user.email, suppression.ignored_at,
                       suppression.restored_by, restored_user.email,
                       suppression.restored_at, suppression.restore_reason,
                       suppression.created_at, suppression.updated_at
                FROM integration_object_suppressions suppression
                JOIN integration_ci_policies policy ON policy.id = suppression.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = suppression.company_id
                LEFT JOIN users ignored_user ON ignored_user.id = suppression.ignored_by
                LEFT JOIN users restored_user ON restored_user.id = suppression.restored_by
                WHERE suppression.id = %s::uuid
                """,
                (parsed_id,),
            )
            row = cursor.fetchone()
        return self._integration_object_suppression_from_row(row) if row else None

    def restore_integration_object_suppression(
        self, suppression_id: str, notes: str, actor_id: str
    ) -> dict | None:
        """Deactivate one durable exclusion and schedule a fresh preview."""

        try:
            parsed_id = str(uuid.UUID(suppression_id))
        except (TypeError, ValueError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT suppression.id, integration.provider, policy.id, company.slug,
                       company.name, policy.external_parent_id,
                       suppression.external_object_type, suppression.external_id,
                       suppression.external_name, suppression.provider_record,
                       suppression.reason, suppression.active, suppression.ignored_by,
                       ignored_user.email, suppression.ignored_at,
                       suppression.restored_by, restored_user.email,
                       suppression.restored_at, suppression.restore_reason,
                       suppression.created_at, suppression.updated_at,
                       policy.filter_policy, policy.revision
                FROM integration_object_suppressions suppression
                JOIN integration_ci_policies policy ON policy.id = suppression.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = suppression.company_id
                LEFT JOIN users ignored_user ON ignored_user.id = suppression.ignored_by
                LEFT JOIN users restored_user ON restored_user.id = suppression.restored_by
                WHERE suppression.id = %s::uuid
                FOR UPDATE OF suppression, policy
                """,
                (parsed_id,),
            )
            row = cursor.fetchone()
            if not row or not row[11]:
                return None
            before = self._integration_object_suppression_from_row(row[:21])
            filter_policy = row[21] or {}
            filter_policy["excludedExternalIds"] = [
                value
                for value in normalize_ci_policy(filter_policy)["excludedExternalIds"]
                if value != before["externalId"]
            ]
            cursor.execute(
                """
                UPDATE integration_object_suppressions
                SET active = false, restored_by = %s::uuid, restored_at = now(),
                    restore_reason = %s, updated_at = now()
                WHERE id = %s::uuid
                RETURNING restored_at, updated_at
                """,
                (actor_id, notes, parsed_id),
            )
            restored_at, updated_at = cursor.fetchone()
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET filter_policy = %s::jsonb,
                    revision = revision + 1,
                    updated_at = now(),
                    next_run_at = CASE
                        WHEN enabled AND sync_mode = 'continuous_preview' THEN now()
                        ELSE next_run_at END
                WHERE id = %s::uuid
                """,
                (json.dumps(filter_policy), before["policyId"]),
            )
            cursor.execute(
                """
                UPDATE integration_ci_review_items
                SET state = 'pending', reviewed_by = NULL,
                    reviewed_at = NULL, review_notes = NULL
                WHERE policy_id = %s::uuid
                  AND external_id = %s
                """,
                (before["policyId"], before["externalId"]),
            )
            after = {
                **before,
                "active": False,
                "restoredBy": actor_id,
                "restoredAt": self._timestamp(restored_at),
                "restoreReason": notes,
                "updatedAt": self._timestamp(updated_at),
            }
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "integration_object_suppression",
                parsed_id,
                "restored",
                before,
                after,
                reason=notes,
                metadata={
                    "provider": before["provider"],
                    "policyId": before["policyId"],
                    "externalId": before["externalId"],
                },
            )
            self._insert_audit(
                cursor,
                before["companyId"],
                actor_id,
                "integration_ci_policy",
                before["policyId"],
                "suppression_updated",
                {"revision": int(row[22]), "excludedExternalIds": [before["externalId"]]},
                {
                    "revision": int(row[22]) + 1,
                    "excludedExternalIds": filter_policy["excludedExternalIds"],
                },
                reason=notes,
                metadata={"restoredExternalId": before["externalId"]},
            )
        return after

    def resolve_ci_review_items(
        self, policy_id: str, external_ids: list[str], actor_id: str | None = None
    ) -> int:
        """Resolve reviewed items after an explicit canonical import or link."""

        if not external_ids:
            return 0
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_review_items
                SET state = 'resolved', reviewed_by = %s::uuid, reviewed_at = now()
                WHERE policy_id = %s::uuid
                  AND external_id = ANY(%s::text[])
                  AND state <> 'resolved'
                """,
                (actor_id, policy_id, external_ids),
            )
            return cursor.rowcount

    def _record_provider_ci_mapping_with_cursor(
        self,
        cursor: Any,
        *,
        kind: str,
        company_id: str,
        company_uuid: str,
        connection_uuid: str,
        record: dict,
        asset_id: str,
        actor_id: str | None,
        selected_parent_id: str,
        lifecycle_mapping: tuple | None,
        before: dict | None,
    ) -> dict:
        """Persist a provider mapping using the caller's locked transaction."""

        mapping_uuid = canonical_uuid(
            "external_ci_mapping",
            f"{connection_uuid}:{record['externalId']}",
        )
        payload_hash = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observation_uuid = canonical_uuid("ci_source_observation", f"{mapping_uuid}:{payload_hash}")
        effective_parent_id = selected_parent_id or str(
            (before or {}).get("providerParentId") or ""
        )
        cursor.execute(
            """
            INSERT INTO external_object_mappings (
                id, integration_connection_id, external_object_type, external_id,
                canonical_entity_type, canonical_entity_id, external_name,
                external_version, external_parent_id, active,
                first_seen_at, last_seen_at, last_synced_at
            ) VALUES (
                %s::uuid, %s::uuid, 'configuration', %s,
                'configuration_item', %s::uuid, %s, %s, %s,
                true, now(), now(), now()
            )
            ON CONFLICT (integration_connection_id, external_object_type, external_id)
            DO UPDATE SET canonical_entity_type = 'configuration_item',
                canonical_entity_id = EXCLUDED.canonical_entity_id,
                external_name = EXCLUDED.external_name,
                external_version = EXCLUDED.external_version,
                external_parent_id = COALESCE(
                    NULLIF(EXCLUDED.external_parent_id, ''),
                    external_object_mappings.external_parent_id
                ),
                active = true, last_seen_at = now(), last_synced_at = now()
            """,
            (
                mapping_uuid,
                connection_uuid,
                record["externalId"],
                asset_id,
                record.get("name") or None,
                record.get("providerVersion") or None,
                effective_parent_id or None,
            ),
        )
        if lifecycle_mapping and lifecycle_mapping[2]:
            cursor.execute(
                """
                SELECT policy.id, policy.revision, integration.revision
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                WHERE policy.integration_connection_id = %s::uuid
                  AND policy.company_id = %s::uuid
                  AND policy.external_parent_id = %s
                  AND policy.external_object_type = 'configuration'
                """,
                (connection_uuid, company_uuid, effective_parent_id),
            )
            selected_policy = cursor.fetchone()
            if selected_policy:
                cursor.execute(
                    """
                    UPDATE integration_ci_presence
                    SET policy_id = %s::uuid,
                        company_id = %s::uuid,
                        ci_id = %s::uuid,
                        external_id = %s,
                        external_name = %s,
                        provider_parent_id = %s,
                        state = 'observed',
                        absence_count = 0,
                        first_missing_at = NULL,
                        last_missing_at = NULL,
                        candidate_since = NULL,
                        last_observed_at = now(),
                        last_evaluated_at = now(),
                        last_evaluated_run_id = NULL,
                        evaluation_reason =
                            'Reviewed import observed and re-scoped this provider mapping.',
                        discovery_scope_fingerprint = repeat('0', 64),
                        policy_decision_fingerprint = repeat('0', 64),
                        connection_revision = %s,
                        policy_revision = %s,
                        revision = revision + 1,
                        updated_at = now()
                    WHERE id = %s::uuid
                    """,
                    (
                        str(selected_policy[0]),
                        company_uuid,
                        asset_id,
                        record["externalId"],
                        record.get("name") or record["externalId"],
                        effective_parent_id,
                        int(selected_policy[2] or 0),
                        int(selected_policy[1] or 0),
                        str(lifecycle_mapping[2]),
                    ),
                )
                presence_after: dict[str, Any] | None = {
                    "id": str(lifecycle_mapping[2]),
                    "mappingId": mapping_uuid,
                    "companyId": company_id,
                    "assetId": asset_id,
                    "providerParentId": effective_parent_id,
                    "state": "observed",
                    "revision": int(lifecycle_mapping[3] or 0) + 1,
                }
            else:
                cursor.execute(
                    "DELETE FROM integration_ci_presence WHERE id = %s::uuid",
                    (str(lifecycle_mapping[2]),),
                )
                presence_after = None
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "integration_ci_presence",
                str(lifecycle_mapping[2]),
                "mapping_rescoped",
                {
                    "id": str(lifecycle_mapping[2]),
                    "state": lifecycle_mapping[1],
                    "revision": int(lifecycle_mapping[3] or 0),
                },
                presence_after,
                metadata={"mappingId": mapping_uuid, "assetId": asset_id},
            )
        cursor.execute(
            """
            DELETE FROM ci_identifiers
            WHERE source_mapping_id = %s::uuid AND ci_id <> %s::uuid
            """,
            (mapping_uuid, asset_id),
        )
        cursor.execute(
            """
            INSERT INTO ci_identifiers (
                id, ci_id, company_id, ci_type, identifier_type,
                identifier_value, verified_at, source_mapping_id
            )
            SELECT %s::uuid, ci.id, ci.company_id, ci.ci_type,
                   'provider_native', %s, now(), %s::uuid
            FROM configuration_items ci WHERE ci.id = %s::uuid
            ON CONFLICT (ci_id, identifier_type, identifier_value)
            DO UPDATE SET verified_at = now(), source_mapping_id = EXCLUDED.source_mapping_id
            """,
            (
                canonical_uuid(
                    "ci_identifier", f"{asset_id}:provider_native:{record['externalId']}"
                ),
                record["externalId"],
                mapping_uuid,
                asset_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO ci_source_observations (
                id, ci_id, mapping_id, observed_at, payload_hash, fields
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, now(), %s, %s::jsonb)
            ON CONFLICT (mapping_id, payload_hash)
            DO UPDATE SET observed_at = now()
            """,
            (observation_uuid, asset_id, mapping_uuid, payload_hash, json.dumps(record)),
        )
        after = {
            "id": mapping_uuid,
            "provider": kind,
            "companyId": company_id,
            "externalId": record["externalId"],
            "externalName": record.get("name", ""),
            "externalVersion": record.get("providerVersion", ""),
            "providerParentId": effective_parent_id or None,
            "assetId": asset_id,
            "active": True,
        }
        self._insert_audit(
            cursor,
            company_id,
            actor_id,
            "external_ci_mapping",
            mapping_uuid,
            (
                "remapped"
                if before and before.get("assetId") != asset_id
                else ("source_observed" if before else "mapped")
            ),
            before,
            after,
            metadata={"provider": kind, "payloadHash": payload_hash},
        )
        return after

    def apply_reviewed_provider_ci_import(
        self,
        kind: str,
        company_id: str,
        record: dict,
        action: str,
        *,
        asset_id: str | None = None,
        asset: dict | None = None,
        changes: dict | None = None,
        actor_id: str | None = None,
        provider_parent_id: str | None = None,
        policy_id: str | None = None,
        review_item_id: str | None = None,
        review_content_hash: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
    ) -> dict:
        """Commit reviewed canonical and immutable-identity writes atomically."""

        if action not in {"create", "update", "link"}:
            raise ValueError("Provider import action is invalid")
        if not isinstance(record, dict) or not str(record.get("externalId") or "").strip():
            raise ValueError("Provider external identity is required")
        selected_parent_id = str(provider_parent_id or record.get("providerParentId") or "").strip()
        database_provider = PROVIDER_TO_DB.get(kind, kind)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, enabled, lifecycle_status, revision
                FROM integration_connections
                WHERE provider = %s AND company_id IS NULL
                ORDER BY created_at
                LIMIT 1
                FOR SHARE
                """,
                (database_provider,),
            )
            integration_row = cursor.fetchone()
            if not integration_row:
                raise ValueError("Integration connection not found")
            if not bool(integration_row[1]) or str(integration_row[2] or "active") != "active":
                raise ValueError("Integration connection is not active. Run a new preview.")
            connection_uuid = str(integration_row[0])
            cursor.execute("SELECT id FROM companies WHERE slug = %s FOR SHARE", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            company_uuid = str(company_row[0])
            generation_supplied = (
                expected_policy_revision is not None or expected_connection_revision is not None
            )
            if generation_supplied:
                if (
                    not policy_id
                    or expected_policy_revision is None
                    or expected_connection_revision is None
                ):
                    raise ValueError(
                        "Reviewed provider generation is incomplete. Run a new preview."
                    )
                try:
                    generation_policy_uuid = str(uuid.UUID(str(policy_id)))
                except (TypeError, ValueError) as error:
                    raise ValueError("CI policy identity is invalid") from error
                cursor.execute(
                    """
                    SELECT revision
                    FROM integration_ci_policies
                    WHERE id = %s::uuid
                      AND integration_connection_id = %s::uuid
                      AND company_id = %s::uuid
                      AND external_parent_id = %s
                    FOR SHARE
                    """,
                    (
                        generation_policy_uuid,
                        connection_uuid,
                        company_uuid,
                        selected_parent_id,
                    ),
                )
                generation_policy = cursor.fetchone()
                if (
                    not generation_policy
                    or int(generation_policy[0] or 0) != int(expected_policy_revision)
                    or int(integration_row[3] or 0) != int(expected_connection_revision)
                ):
                    raise ValueError("Reviewed provider generation is stale. Run a new preview.")
            cursor.execute(
                """
                SELECT mapping.id
                FROM external_object_mappings mapping
                JOIN provider_company_observations observation
                  ON observation.integration_connection_id = mapping.integration_connection_id
                 AND observation.external_id = mapping.external_id
                WHERE mapping.integration_connection_id = %s::uuid
                  AND mapping.external_object_type = 'company'
                  AND mapping.external_id = %s
                  AND mapping.canonical_entity_type = 'company'
                  AND mapping.canonical_entity_id = %s::uuid
                  AND mapping.active = true
                  AND observation.active = true
                  AND observation.deleted = false
                FOR SHARE OF mapping, observation
                """,
                (connection_uuid, selected_parent_id, company_uuid),
            )
            if not selected_parent_id or not cursor.fetchone():
                raise ValueError(
                    "The active provider customer mapping changed during review. Run a new preview."
                )
            review_guard_supplied = review_item_id is not None or review_content_hash is not None
            parsed_review_item_id: str | None = None
            parsed_policy_id: str | None = None
            if review_guard_supplied:
                if not review_item_id or not review_content_hash or not policy_id:
                    raise ValueError("Reviewed queue evidence is incomplete. Run a new preview.")
                try:
                    parsed_review_item_id = str(uuid.UUID(str(review_item_id)))
                    parsed_policy_id = str(uuid.UUID(str(policy_id)))
                except (TypeError, ValueError) as error:
                    raise ValueError("Reviewed queue evidence is invalid.") from error
                cursor.execute(
                    """
                    SELECT item.id
                    FROM integration_ci_review_items item
                    JOIN integration_ci_policies policy ON policy.id = item.policy_id
                    WHERE item.id = %s::uuid
                      AND item.policy_id = %s::uuid
                      AND policy.integration_connection_id = %s::uuid
                      AND policy.company_id = %s::uuid
                      AND policy.external_parent_id = %s
                      AND item.external_id = %s
                      AND item.state = 'pending'
                      AND item.content_hash = %s
                    FOR UPDATE OF item
                    """,
                    (
                        parsed_review_item_id,
                        parsed_policy_id,
                        connection_uuid,
                        company_uuid,
                        selected_parent_id,
                        record["externalId"],
                        review_content_hash,
                    ),
                )
                if not cursor.fetchone():
                    raise ValueError("Reviewed queue item changed. Run a new preview.")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{connection_uuid}:configuration:{record['externalId']}",),
            )
            cursor.execute(
                """
                SELECT mapping.id, mapping.active, presence.state, presence.id,
                       presence.revision, mapped_company.slug,
                       mapping.external_parent_id, mapping.canonical_entity_id,
                       mapping.external_name, mapping.external_version
                FROM external_object_mappings mapping
                LEFT JOIN configuration_items mapped_ci
                  ON mapped_ci.id = mapping.canonical_entity_id
                 AND mapping.canonical_entity_type = 'configuration_item'
                LEFT JOIN companies mapped_company ON mapped_company.id = mapped_ci.company_id
                LEFT JOIN integration_ci_presence presence ON presence.mapping_id = mapping.id
                WHERE mapping.integration_connection_id = %s::uuid
                  AND mapping.external_object_type = 'configuration'
                  AND mapping.external_id = %s
                FOR UPDATE OF mapping
                """,
                (connection_uuid, record["externalId"]),
            )
            mapping_row = cursor.fetchone()
            lifecycle_mapping = (
                (
                    bool(mapping_row[1]),
                    mapping_row[2],
                    mapping_row[3],
                    mapping_row[4],
                    mapping_row[5],
                    mapping_row[6],
                )
                if mapping_row
                else None
            )
            before_mapping = (
                {
                    "id": str(mapping_row[0]),
                    "provider": kind,
                    "companyId": mapping_row[5],
                    "externalId": record["externalId"],
                    "externalName": mapping_row[8] or "",
                    "externalVersion": mapping_row[9] or "",
                    "providerParentId": mapping_row[6] or None,
                    "assetId": str(mapping_row[7]) if mapping_row[7] else None,
                    "active": bool(mapping_row[1]),
                }
                if mapping_row
                else None
            )
            company_matches = bool(not mapping_row or mapping_row[5] == company_id)
            parent_matches = bool(
                not mapping_row
                or not selected_parent_id
                or not mapping_row[6]
                or mapping_row[6] == selected_parent_id
            )
            decision, reason = _provider_ci_import_decision(
                mapping_exists=mapping_row is not None,
                mapping_active=bool(not mapping_row or mapping_row[1]),
                lifecycle_state=str(mapping_row[2] or "") if mapping_row else None,
                company_matches=company_matches,
                provider_parent_matches=parent_matches,
            )
            if decision != "allow":
                raise ValueError(reason)
            if action == "create" and mapping_row:
                raise ValueError("Provider identity is already mapped; refresh the import preview")

            before_asset: dict | None = None
            if action == "create":
                if not isinstance(asset, dict) or asset.get("companyId") != company_id:
                    raise ValueError("A same-customer canonical asset is required for create")
                asset_uuid = canonical_uuid("configuration_item", asset["id"])
                metadata = deepcopy(asset.get("metadata") or {})
                attributes = self._asset_attributes(asset)
                cursor.execute(
                    """
                    INSERT INTO configuration_items (
                        id, company_id, ci_type, display_name, normalized_name,
                        lifecycle_status, operational_status, attributes
                    ) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        asset_uuid,
                        company_uuid,
                        asset["type"],
                        asset["name"],
                        normalized_name(asset["name"]),
                        metadata.get("lifecycle", "in_service"),
                        metadata.get("operationalStatus", "unknown"),
                        json.dumps(attributes),
                    ),
                )
                stored_asset = {**deepcopy(asset), "id": asset_uuid}
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "configuration_item",
                    asset_uuid,
                    "created",
                    None,
                    stored_asset,
                )
                self._provider_import_failpoint("after_asset_write")
                mapping = self._record_provider_ci_mapping_with_cursor(
                    cursor,
                    kind=kind,
                    company_id=company_id,
                    company_uuid=company_uuid,
                    connection_uuid=connection_uuid,
                    record=record,
                    asset_id=asset_uuid,
                    actor_id=actor_id,
                    selected_parent_id=selected_parent_id,
                    lifecycle_mapping=lifecycle_mapping,
                    before=before_mapping,
                )
            else:
                try:
                    asset_uuid = str(uuid.UUID(str(asset_id)))
                except (TypeError, ValueError) as error:
                    raise ValueError("A canonical target is required for update or link") from error
                cursor.execute(
                    """
                    SELECT ci.id, company.slug, ci.display_name, ci.ci_type,
                           ci.lifecycle_status, ci.operational_status, ci.attributes,
                           ci.updated_at
                    FROM configuration_items ci
                    JOIN companies company ON company.id = ci.company_id
                    WHERE ci.id = %s::uuid
                      AND ci.company_id = %s::uuid
                      AND ci.retired_at IS NULL
                    FOR UPDATE OF ci
                    """,
                    (asset_uuid, company_uuid),
                )
                asset_row = cursor.fetchone()
                if not asset_row:
                    raise ValueError("Configuration item is unavailable in this customer")
                if action == "update" and (
                    not mapping_row or str(mapping_row[7] or "") != asset_uuid
                ):
                    raise ValueError(
                        "Provider update target no longer matches its immutable mapping. "
                        "Run a new preview."
                    )
                before_asset = self._asset_from_row(asset_row)
                # The mapping guard and write happen before an existing CI is
                # updated, closing the preflight-to-write retirement race.
                mapping = self._record_provider_ci_mapping_with_cursor(
                    cursor,
                    kind=kind,
                    company_id=company_id,
                    company_uuid=company_uuid,
                    connection_uuid=connection_uuid,
                    record=record,
                    asset_id=asset_uuid,
                    actor_id=actor_id,
                    selected_parent_id=selected_parent_id,
                    lifecycle_mapping=lifecycle_mapping,
                    before=before_mapping,
                )
                if changes:
                    stored_asset = {
                        **before_asset,
                        **deepcopy(changes),
                        "updatedAt": utc_now(),
                    }
                    metadata = stored_asset.get("metadata") or {}
                    attributes = self._asset_attributes(stored_asset)
                    cursor.execute(
                        """
                        UPDATE configuration_items
                        SET ci_type = %s,
                            display_name = %s,
                            normalized_name = %s,
                            lifecycle_status = %s,
                            operational_status = %s,
                            attributes = %s::jsonb,
                            updated_at = now()
                        WHERE id = %s::uuid
                          AND company_id = %s::uuid
                          AND retired_at IS NULL
                        """,
                        (
                            stored_asset["type"],
                            stored_asset["name"],
                            normalized_name(stored_asset["name"]),
                            metadata.get("lifecycle", "in_service"),
                            metadata.get("operationalStatus", "unknown"),
                            json.dumps(attributes),
                            asset_uuid,
                            company_uuid,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Configuration item changed during import")
                    self._insert_audit(
                        cursor,
                        company_id,
                        actor_id,
                        "configuration_item",
                        asset_uuid,
                        "updated",
                        before_asset,
                        stored_asset,
                    )
                else:
                    stored_asset = before_asset
                self._provider_import_failpoint("after_asset_write")

            resolved = 0
            if parsed_review_item_id is not None:
                cursor.execute(
                    """
                    UPDATE integration_ci_review_items
                    SET state = 'resolved', reviewed_by = %s::uuid, reviewed_at = now()
                    WHERE id = %s::uuid
                      AND policy_id = %s::uuid
                      AND content_hash = %s
                      AND state = 'pending'
                    """,
                    (
                        actor_id,
                        parsed_review_item_id,
                        parsed_policy_id,
                        review_content_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Reviewed queue item changed. Run a new preview.")
                resolved = 1
            elif policy_id:
                try:
                    policy_uuid = str(uuid.UUID(str(policy_id)))
                except (TypeError, ValueError) as error:
                    raise ValueError("CI policy identity is invalid") from error
                cursor.execute(
                    """
                    UPDATE integration_ci_review_items item
                    SET state = 'resolved', reviewed_by = %s::uuid, reviewed_at = now()
                    FROM integration_ci_policies policy
                    WHERE item.policy_id = policy.id
                      AND policy.id = %s::uuid
                      AND policy.integration_connection_id = %s::uuid
                      AND policy.company_id = %s::uuid
                      AND (%s = '' OR policy.external_parent_id = %s)
                      AND item.external_id = %s
                      AND item.state <> 'resolved'
                    """,
                    (
                        actor_id,
                        policy_uuid,
                        connection_uuid,
                        company_uuid,
                        selected_parent_id,
                        selected_parent_id,
                        record["externalId"],
                    ),
                )
                resolved = int(cursor.rowcount or 0)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return {
            "action": action,
            "asset": stored_asset,
            "mapping": mapping,
            "reviewItemsResolved": resolved,
        }

    def record_provider_ci_mapping(
        self,
        kind: str,
        company_id: str,
        record: dict,
        asset_id: str,
        actor_id: str | None = None,
        *,
        provider_parent_id: str | None = None,
    ) -> dict:
        """Upsert provider identity and append a content-addressed observation."""

        connection_record = self.get_integration_connection(kind)
        if not connection_record:
            raise ValueError("Integration connection not found")
        before = next(
            (
                item
                for item in self.list_provider_ci_mappings(kind, company_id)
                if item["externalId"] == record["externalId"]
            ),
            None,
        )
        mapping_uuid = canonical_uuid(
            "external_ci_mapping",
            f"{connection_record['uuid']}:{record['externalId']}",
        )
        payload_hash = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observation_uuid = canonical_uuid("ci_source_observation", f"{mapping_uuid}:{payload_hash}")
        selected_parent_id = str(provider_parent_id or record.get("providerParentId") or "").strip()
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{connection_record['uuid']}:configuration:{record['externalId']}",),
            )
            cursor.execute(
                """
                SELECT mapping.active, presence.state, presence.id, presence.revision,
                       company.slug, mapping.external_parent_id
                FROM external_object_mappings mapping
                JOIN configuration_items ci
                  ON ci.id = mapping.canonical_entity_id
                 AND mapping.canonical_entity_type = 'configuration_item'
                JOIN companies company ON company.id = ci.company_id
                LEFT JOIN integration_ci_presence presence
                  ON presence.mapping_id = mapping.id
                WHERE mapping.integration_connection_id = %s::uuid
                  AND mapping.external_object_type = 'configuration'
                  AND mapping.external_id = %s
                FOR UPDATE OF mapping
                """,
                (connection_record["uuid"], record["externalId"]),
            )
            lifecycle_mapping = cursor.fetchone()
            if lifecycle_mapping:
                company_matches = lifecycle_mapping[4] == company_id
                parent_matches = bool(
                    not selected_parent_id
                    or not lifecycle_mapping[5]
                    or lifecycle_mapping[5] == selected_parent_id
                )
                decision, reason = _provider_ci_import_decision(
                    mapping_exists=True,
                    mapping_active=bool(lifecycle_mapping[0]),
                    lifecycle_state=str(lifecycle_mapping[1] or ""),
                    company_matches=company_matches,
                    provider_parent_matches=parent_matches,
                )
                if decision != "allow":
                    raise ValueError(reason)
            cursor.execute(
                """
                SELECT ci.id, ci.company_id FROM configuration_items ci
                JOIN companies company ON company.id = ci.company_id
                WHERE ci.id = %s::uuid AND company.slug = %s AND ci.retired_at IS NULL
                """,
                (asset_id, company_id),
            )
            ci_row = cursor.fetchone()
            if not ci_row:
                raise ValueError("Configuration item is unavailable in this customer")
            cursor.execute(
                """
                INSERT INTO external_object_mappings (
                    id, integration_connection_id, external_object_type, external_id,
                    canonical_entity_type, canonical_entity_id, external_name,
                    external_version, external_parent_id, active,
                    first_seen_at, last_seen_at, last_synced_at
                ) VALUES (
                    %s::uuid, %s::uuid, 'configuration', %s,
                    'configuration_item', %s::uuid, %s, %s, %s,
                    true, now(), now(), now()
                )
                ON CONFLICT (integration_connection_id, external_object_type, external_id)
                DO UPDATE SET canonical_entity_type = 'configuration_item',
                    canonical_entity_id = EXCLUDED.canonical_entity_id,
                    external_name = EXCLUDED.external_name,
                    external_version = EXCLUDED.external_version,
                    external_parent_id = COALESCE(
                        NULLIF(EXCLUDED.external_parent_id, ''),
                        external_object_mappings.external_parent_id
                    ),
                    active = true, last_seen_at = now(), last_synced_at = now()
                """,
                (
                    mapping_uuid,
                    connection_record["uuid"],
                    record["externalId"],
                    asset_id,
                    record.get("name") or None,
                    record.get("providerVersion") or None,
                    selected_parent_id or None,
                ),
            )
            if lifecycle_mapping and lifecycle_mapping[2]:
                cursor.execute(
                    """
                    SELECT policy.id, policy.revision, integration.revision
                    FROM integration_ci_policies policy
                    JOIN integration_connections integration
                      ON integration.id = policy.integration_connection_id
                    WHERE policy.integration_connection_id = %s::uuid
                      AND policy.company_id = %s::uuid
                      AND policy.external_parent_id = %s
                      AND policy.external_object_type = 'configuration'
                    """,
                    (connection_record["uuid"], str(ci_row[1]), selected_parent_id),
                )
                selected_policy = cursor.fetchone()
                if selected_policy:
                    cursor.execute(
                        """
                        UPDATE integration_ci_presence
                        SET policy_id = %s::uuid,
                            company_id = %s::uuid,
                            ci_id = %s::uuid,
                            external_id = %s,
                            external_name = %s,
                            provider_parent_id = %s,
                            state = 'observed',
                            absence_count = 0,
                            first_missing_at = NULL,
                            last_missing_at = NULL,
                            candidate_since = NULL,
                            last_observed_at = now(),
                            last_evaluated_at = now(),
                            last_evaluated_run_id = NULL,
                            evaluation_reason =
                                'Reviewed import observed and re-scoped this provider mapping.',
                            discovery_scope_fingerprint = repeat('0', 64),
                            policy_decision_fingerprint = repeat('0', 64),
                            connection_revision = %s,
                            policy_revision = %s,
                            revision = revision + 1,
                            updated_at = now()
                        WHERE id = %s::uuid
                        """,
                        (
                            str(selected_policy[0]),
                            str(ci_row[1]),
                            asset_id,
                            record["externalId"],
                            record.get("name") or record["externalId"],
                            selected_parent_id,
                            int(selected_policy[2] or 0),
                            int(selected_policy[1] or 0),
                            str(lifecycle_mapping[2]),
                        ),
                    )
                    presence_after: dict[str, Any] | None = {
                        "id": str(lifecycle_mapping[2]),
                        "mappingId": mapping_uuid,
                        "companyId": company_id,
                        "assetId": asset_id,
                        "providerParentId": selected_parent_id,
                        "state": "observed",
                        "revision": int(lifecycle_mapping[3] or 0) + 1,
                    }
                else:
                    cursor.execute(
                        "DELETE FROM integration_ci_presence WHERE id = %s::uuid",
                        (str(lifecycle_mapping[2]),),
                    )
                    presence_after = None
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "integration_ci_presence",
                    str(lifecycle_mapping[2]),
                    "mapping_rescoped",
                    {
                        "id": str(lifecycle_mapping[2]),
                        "state": lifecycle_mapping[1],
                        "revision": int(lifecycle_mapping[3] or 0),
                    },
                    presence_after,
                    metadata={"mappingId": mapping_uuid, "assetId": asset_id},
                )
            cursor.execute(
                """
                DELETE FROM ci_identifiers
                WHERE source_mapping_id = %s::uuid AND ci_id <> %s::uuid
                """,
                (mapping_uuid, asset_id),
            )
            cursor.execute(
                """
                INSERT INTO ci_identifiers (
                    id, ci_id, company_id, ci_type, identifier_type,
                    identifier_value, verified_at, source_mapping_id
                )
                SELECT %s::uuid, ci.id, ci.company_id, ci.ci_type,
                       'provider_native', %s, now(), %s::uuid
                FROM configuration_items ci WHERE ci.id = %s::uuid
                ON CONFLICT (ci_id, identifier_type, identifier_value)
                DO UPDATE SET verified_at = now(), source_mapping_id = EXCLUDED.source_mapping_id
                """,
                (
                    canonical_uuid(
                        "ci_identifier", f"{asset_id}:provider_native:{record['externalId']}"
                    ),
                    record["externalId"],
                    mapping_uuid,
                    asset_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO ci_source_observations (
                    id, ci_id, mapping_id, observed_at, payload_hash, fields
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, now(), %s, %s::jsonb)
                ON CONFLICT (mapping_id, payload_hash)
                DO UPDATE SET observed_at = now()
                """,
                (observation_uuid, asset_id, mapping_uuid, payload_hash, json.dumps(record)),
            )
            after = {
                "id": mapping_uuid,
                "provider": kind,
                "companyId": company_id,
                "externalId": record["externalId"],
                "externalName": record.get("name", ""),
                "externalVersion": record.get("providerVersion", ""),
                "providerParentId": selected_parent_id or None,
                "assetId": asset_id,
                "active": True,
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "external_ci_mapping",
                mapping_uuid,
                (
                    "remapped"
                    if before and before.get("assetId") != asset_id
                    else ("source_observed" if before else "mapped")
                ),
                before,
                after,
                metadata={"provider": kind, "payloadHash": payload_hash},
            )
        return next(
            item
            for item in self.list_provider_ci_mappings(kind, company_id)
            if item["externalId"] == record["externalId"]
        )

    def _require_provider_ci_mapping(
        self,
        cursor: Any,
        kind: str,
        company_id: str,
        mapping_id: str,
        asset_id: str | None = None,
        provider_parent_id: str | None = None,
    ) -> tuple[str, str]:
        """Lock and return an active mapping constrained to one provider customer."""

        try:
            mapping_uuid = str(uuid.UUID(str(mapping_id)))
            asset_uuid = str(uuid.UUID(str(asset_id))) if asset_id else None
        except (TypeError, ValueError) as error:
            raise ValueError("Provider mapping identity is invalid") from error
        cursor.execute(
            """
            SELECT mapping.id, ci.id
            FROM external_object_mappings mapping
            JOIN integration_connections integration
              ON integration.id = mapping.integration_connection_id
            JOIN configuration_items ci
              ON ci.id = mapping.canonical_entity_id
             AND mapping.canonical_entity_type = 'configuration_item'
            JOIN companies company ON company.id = ci.company_id
            WHERE mapping.id = %s::uuid
              AND integration.provider = %s
              AND company.slug = %s
              AND mapping.external_object_type = 'configuration'
              AND mapping.active = true
              AND ci.retired_at IS NULL
              AND (%s::uuid IS NULL OR ci.id = %s::uuid)
              AND (%s::text IS NULL OR mapping.external_parent_id = %s)
            FOR UPDATE OF mapping
            """,
            (
                mapping_uuid,
                PROVIDER_TO_DB.get(kind, kind),
                company_id,
                asset_uuid,
                asset_uuid,
                provider_parent_id,
                provider_parent_id,
            ),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("Provider mapping is unavailable for this customer asset")
        return str(row[0]), str(row[1])

    def replace_ci_inventory(
        self,
        kind: str,
        company_id: str,
        asset_id: str,
        mapping_id: str,
        collections: dict[str, Any],
        *,
        observed_at: str | None = None,
        retention: int = 3,
    ) -> dict:
        """Replace canonical provider inventory and retain bounded snapshot history."""

        source = (
            collections.get("inventoryCollections")
            if isinstance(collections, dict)
            and isinstance(collections.get("inventoryCollections"), dict)
            else collections
        )
        if not isinstance(source, dict):
            raise ValueError("Inventory collections must be an object")
        observed = observed_at or utc_now()
        if not parse_timestamp(observed):
            raise ValueError("Inventory observed timestamp is invalid")
        keep = max(1, min(int(retention), 12))
        normalized_collections: dict[str, Any] = {}
        interfaces: list[dict[str, Any]] = []
        for raw_type, raw_payload in source.items():
            collection_type = _inventory_collection_type(raw_type)
            if collection_type == "network_interfaces":
                if not isinstance(raw_payload, list):
                    raise ValueError("Network interface inventory must be an array")
                if len(raw_payload) > INVENTORY_MAX_NETWORK_INTERFACES:
                    raise ValueError("Network interface inventory contains too many items")
                interfaces = [
                    _normalized_interface(item, ordinal) for ordinal, item in enumerate(raw_payload)
                ]
                normalized_collections[collection_type] = [
                    {key: value for key, value in item.items() if key != "fingerprint"}
                    for item in interfaces
                ]
            else:
                normalized_collections[collection_type] = raw_payload

        summaries: dict[str, dict[str, Any]] = {}
        stale_types: list[str] = []
        with self.connection_factory() as connection, connection.cursor() as cursor:
            mapping_uuid, asset_uuid = self._require_provider_ci_mapping(
                cursor,
                kind,
                company_id,
                mapping_id,
                asset_id,
            )
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            company_uuid = str(company_row[0])

            for collection_type, raw_payload in normalized_collections.items():
                payload, item_count, fingerprint = _bounded_inventory_payload(raw_payload)
                cursor.execute(
                    """
                    SELECT id, fingerprint, first_observed_at, last_observed_at
                    FROM ci_inventory_snapshots
                    WHERE source_mapping_id = %s::uuid
                      AND collection_type = %s
                      AND superseded_at IS NULL
                    FOR UPDATE
                    """,
                    (mapping_uuid, collection_type),
                )
                current = cursor.fetchone()
                incoming_time = parse_timestamp(observed)
                current_time = parse_timestamp(current[3]) if current else None
                is_stale = bool(current_time and incoming_time and incoming_time < current_time)
                if is_stale:
                    stale_types.append(collection_type)
                    cursor.execute(
                        """
                        INSERT INTO ci_inventory_snapshots (
                            id, company_id, ci_id, source_mapping_id,
                            collection_type, fingerprint, payload, item_count,
                            completeness, first_observed_at, last_observed_at,
                            superseded_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s::jsonb, %s, 'unknown',
                            %s::timestamptz, %s::timestamptz, %s::timestamptz
                        )
                        ON CONFLICT (
                            source_mapping_id, collection_type, fingerprint
                        ) DO UPDATE SET
                            first_observed_at = LEAST(
                                ci_inventory_snapshots.first_observed_at,
                                EXCLUDED.first_observed_at
                            ),
                            last_observed_at = GREATEST(
                                ci_inventory_snapshots.last_observed_at,
                                EXCLUDED.last_observed_at
                            )
                        """,
                        (
                            canonical_uuid(
                                "ci_inventory_snapshot",
                                f"{mapping_uuid}:{collection_type}:{fingerprint}",
                            ),
                            company_uuid,
                            asset_uuid,
                            mapping_uuid,
                            collection_type,
                            fingerprint,
                            json.dumps(payload),
                            item_count,
                            observed,
                            observed,
                            self._timestamp(current[2]) if current else observed,
                        ),
                    )
                    summaries[collection_type] = {
                        "fingerprint": str(current[1]) if current else fingerprint,
                        "itemCount": item_count,
                    }
                else:
                    cursor.execute(
                        """
                        UPDATE ci_inventory_snapshots
                        SET superseded_at = %s::timestamptz
                        WHERE source_mapping_id = %s::uuid
                          AND collection_type = %s
                          AND superseded_at IS NULL
                          AND fingerprint <> %s
                        """,
                        (observed, mapping_uuid, collection_type, fingerprint),
                    )
                    cursor.execute(
                        """
                        INSERT INTO ci_inventory_snapshots (
                            id, company_id, ci_id, source_mapping_id,
                            collection_type, fingerprint, payload, item_count,
                            completeness, first_observed_at, last_observed_at,
                            superseded_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s::jsonb, %s, 'unknown',
                            %s::timestamptz, %s::timestamptz, NULL
                        )
                        ON CONFLICT (
                            source_mapping_id, collection_type, fingerprint
                        ) DO UPDATE SET
                            company_id = EXCLUDED.company_id,
                            ci_id = EXCLUDED.ci_id,
                            payload = EXCLUDED.payload,
                            item_count = EXCLUDED.item_count,
                            first_observed_at = LEAST(
                                ci_inventory_snapshots.first_observed_at,
                                EXCLUDED.first_observed_at
                            ),
                            last_observed_at = GREATEST(
                                ci_inventory_snapshots.last_observed_at,
                                EXCLUDED.last_observed_at
                            ),
                            superseded_at = NULL
                        """,
                        (
                            canonical_uuid(
                                "ci_inventory_snapshot",
                                f"{mapping_uuid}:{collection_type}:{fingerprint}",
                            ),
                            company_uuid,
                            asset_uuid,
                            mapping_uuid,
                            collection_type,
                            fingerprint,
                            json.dumps(payload),
                            item_count,
                            observed,
                            observed,
                        ),
                    )
                    summaries[collection_type] = {
                        "fingerprint": fingerprint,
                        "itemCount": item_count,
                    }
                cursor.execute(
                    """
                    WITH ranked AS (
                        SELECT id, row_number() OVER (
                            ORDER BY
                                (superseded_at IS NULL) DESC,
                                last_observed_at DESC,
                                id DESC
                        ) AS ordinal
                        FROM ci_inventory_snapshots
                        WHERE source_mapping_id = %s::uuid
                          AND collection_type = %s
                    )
                    DELETE FROM ci_inventory_snapshots snapshot
                    USING ranked
                    WHERE snapshot.id = ranked.id AND ranked.ordinal > %s
                    """,
                    (mapping_uuid, collection_type, keep),
                )

            if (
                "network_interfaces" in normalized_collections
                and "network_interfaces" not in stale_types
            ):
                interface_keys: list[str] = []
                for interface in interfaces:
                    interface_keys.append(interface["interfaceKey"])
                    cursor.execute(
                        """
                        INSERT INTO ci_network_interfaces (
                            id, company_id, ci_id, source_mapping_id,
                            interface_key, name, description, mac_address,
                            ip_addresses, gateways, dns_servers, dhcp_enabled,
                            vlan_id, operational_state, speed_mbps, attributes,
                            fingerprint, first_observed_at, last_observed_at,
                            retired_at
                        ) VALUES (
                            %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                            %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s, %s::jsonb, %s,
                            %s::timestamptz, %s::timestamptz, NULL
                        )
                        ON CONFLICT (source_mapping_id, interface_key)
                        DO UPDATE SET
                            company_id = EXCLUDED.company_id,
                            ci_id = EXCLUDED.ci_id,
                            name = EXCLUDED.name,
                            description = EXCLUDED.description,
                            mac_address = EXCLUDED.mac_address,
                            ip_addresses = EXCLUDED.ip_addresses,
                            gateways = EXCLUDED.gateways,
                            dns_servers = EXCLUDED.dns_servers,
                            dhcp_enabled = EXCLUDED.dhcp_enabled,
                            vlan_id = EXCLUDED.vlan_id,
                            operational_state = EXCLUDED.operational_state,
                            speed_mbps = EXCLUDED.speed_mbps,
                            attributes = EXCLUDED.attributes,
                            fingerprint = EXCLUDED.fingerprint,
                            first_observed_at = LEAST(
                                ci_network_interfaces.first_observed_at,
                                EXCLUDED.first_observed_at
                            ),
                            last_observed_at = EXCLUDED.last_observed_at,
                            retired_at = NULL
                        WHERE EXCLUDED.last_observed_at
                              >= ci_network_interfaces.last_observed_at
                        """,
                        (
                            canonical_uuid(
                                "ci_network_interface",
                                f"{mapping_uuid}:{interface['interfaceKey']}",
                            ),
                            company_uuid,
                            asset_uuid,
                            mapping_uuid,
                            interface["interfaceKey"],
                            interface["name"] or None,
                            interface["description"] or None,
                            interface["macAddress"] or None,
                            json.dumps(interface["ipAddresses"]),
                            json.dumps(interface["gateways"]),
                            json.dumps(interface["dnsServers"]),
                            interface["dhcpEnabled"],
                            interface["vlanId"] or None,
                            interface["operationalState"] or None,
                            interface["speedMbps"],
                            json.dumps(interface["attributes"]),
                            interface["fingerprint"],
                            observed,
                            observed,
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE ci_network_interfaces
                    SET retired_at = %s::timestamptz
                    WHERE source_mapping_id = %s::uuid
                      AND retired_at IS NULL
                      AND last_observed_at <= %s::timestamptz
                      AND interface_key <> ALL(%s::text[])
                    """,
                    (observed, mapping_uuid, observed, interface_keys),
                )
        return {
            "companyId": company_id,
            "assetId": asset_id,
            "mappingId": mapping_id,
            "observedAt": observed,
            "collections": summaries,
            "networkInterfaceCount": len(interfaces),
            "staleCollections": stale_types,
            "retention": keep,
        }

    def get_ci_inventory(
        self,
        company_id: str,
        asset_id: str,
        *,
        collection_types: Iterable[str] | None = None,
        include_history: bool = False,
    ) -> dict:
        """Return canonical technical inventory constrained to one customer CI."""

        try:
            asset_uuid = str(uuid.UUID(str(asset_id)))
        except (TypeError, ValueError) as error:
            raise ValueError("Configuration item identity is invalid") from error
        selected = (
            sorted({_inventory_collection_type(item) for item in collection_types})
            if collection_types is not None
            else []
        )
        snapshots: list[dict] = []
        include_interfaces = not selected or "network_interfaces" in selected
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot.id, snapshot.collection_type, snapshot.fingerprint,
                       snapshot.payload, snapshot.item_count, snapshot.completeness,
                       snapshot.first_observed_at, snapshot.last_observed_at,
                       snapshot.superseded_at, snapshot.source_mapping_id,
                       integration.provider
                FROM ci_inventory_snapshots snapshot
                JOIN configuration_items ci ON ci.id = snapshot.ci_id
                JOIN companies company
                  ON company.id = snapshot.company_id
                 AND company.id = ci.company_id
                JOIN external_object_mappings mapping
                  ON mapping.id = snapshot.source_mapping_id
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                WHERE company.slug = %s
                  AND ci.id = %s::uuid
                  AND ci.retired_at IS NULL
                  AND (%s OR snapshot.superseded_at IS NULL)
                  AND (%s = false OR snapshot.collection_type = ANY(%s::text[]))
                ORDER BY snapshot.collection_type,
                         snapshot.last_observed_at DESC, snapshot.id DESC
                """,
                (
                    company_id,
                    asset_uuid,
                    include_history,
                    bool(selected),
                    selected,
                ),
            )
            for row in cursor.fetchall():
                snapshots.append(
                    {
                        "id": str(row[0]),
                        "collectionType": row[1],
                        "fingerprint": row[2],
                        "payload": row[3],
                        "itemCount": int(row[4]),
                        "completeness": row[5],
                        "firstObservedAt": self._timestamp(row[6]),
                        "lastObservedAt": self._timestamp(row[7]),
                        "supersededAt": self._timestamp(row[8]) or None,
                        "mappingId": str(row[9]),
                        "provider": PROVIDER_FROM_DB.get(row[10], row[10]),
                    }
                )
            interfaces: list[dict] = []
            if include_interfaces:
                cursor.execute(
                    """
                    SELECT interface.id, interface.interface_key, interface.name,
                           interface.description, interface.mac_address,
                           interface.ip_addresses, interface.gateways,
                           interface.dns_servers, interface.dhcp_enabled,
                           interface.vlan_id, interface.operational_state,
                           interface.speed_mbps, interface.attributes,
                           interface.fingerprint, interface.first_observed_at,
                           interface.last_observed_at, interface.retired_at,
                           interface.source_mapping_id, integration.provider
                    FROM ci_network_interfaces interface
                    JOIN configuration_items ci ON ci.id = interface.ci_id
                    JOIN companies company
                      ON company.id = interface.company_id
                     AND company.id = ci.company_id
                    JOIN external_object_mappings mapping
                      ON mapping.id = interface.source_mapping_id
                    JOIN integration_connections integration
                      ON integration.id = mapping.integration_connection_id
                    WHERE company.slug = %s
                      AND ci.id = %s::uuid
                      AND ci.retired_at IS NULL
                      AND (%s OR interface.retired_at IS NULL)
                    ORDER BY interface.name, interface.interface_key
                    """,
                    (company_id, asset_uuid, include_history),
                )
                interfaces = [
                    {
                        "id": str(row[0]),
                        "interfaceKey": row[1],
                        "name": row[2] or "",
                        "description": row[3] or "",
                        "macAddress": row[4] or "",
                        "ipAddresses": row[5] or [],
                        "gateways": row[6] or [],
                        "dnsServers": row[7] or [],
                        "dhcpEnabled": row[8],
                        "vlanId": row[9] or "",
                        "operationalState": row[10] or "",
                        "speedMbps": int(row[11]) if row[11] is not None else None,
                        "attributes": row[12] or {},
                        "fingerprint": row[13],
                        "firstObservedAt": self._timestamp(row[14]),
                        "lastObservedAt": self._timestamp(row[15]),
                        "retiredAt": self._timestamp(row[16]) or None,
                        "mappingId": str(row[17]),
                        "provider": PROVIDER_FROM_DB.get(row[18], row[18]),
                    }
                    for row in cursor.fetchall()
                ]
        grouped: dict[str, list[dict]] = {}
        for snapshot in snapshots:
            grouped.setdefault(snapshot["collectionType"], []).append(snapshot)
        return {
            "companyId": company_id,
            "assetId": asset_id,
            "collections": grouped,
            "networkInterfaces": interfaces,
        }

    @staticmethod
    def _relationship_candidate_from_row(row: tuple) -> dict:
        """Convert one canonical relationship candidate row to the API shape."""

        return {
            "id": str(row[0]),
            "companyId": row[1],
            "sourceMappingId": str(row[2]),
            "provider": PROVIDER_FROM_DB.get(row[3], row[3]),
            "candidateKey": row[4],
            "fromCiId": str(row[5]) if row[5] else None,
            "fromName": row[6] or "",
            "toCiId": str(row[7]) if row[7] else None,
            "toName": row[8] or "",
            "fromExternalIdentity": row[9] or {},
            "toExternalIdentity": row[10] or {},
            "relationshipType": row[11],
            "confidence": float(row[12]),
            "evidence": row[13] or {},
            "state": row[14],
            "firstObservedAt": PostgresCmdbRepository._timestamp(row[15]),
            "lastSeenAt": PostgresCmdbRepository._timestamp(row[16]),
            "retiredAt": PostgresCmdbRepository._timestamp(row[17]) or None,
            "decidedBy": str(row[18]) if row[18] else None,
            "decidedAt": PostgresCmdbRepository._timestamp(row[19]) or None,
            "decisionNotes": row[20] or "",
            "approvedRelationshipId": str(row[21]) if row[21] else None,
            "revision": int(row[22]),
            "observationCount": int(row[23]),
        }

    def _require_postgres_provider_relationship_context(
        self,
        cursor: Any,
        kind: str,
        company_id: str,
        *,
        policy_id: str | None,
        expected_policy_revision: int | None,
        expected_connection_revision: int | None,
        provider_parent_id: str | None,
    ) -> dict[str, Any] | None:
        """Lock and validate one provider topology generation in the current transaction."""

        supplied = any(
            value is not None
            for value in (
                policy_id,
                expected_policy_revision,
                expected_connection_revision,
                provider_parent_id,
            )
        )
        if not supplied:
            return None
        if (
            not policy_id
            or expected_policy_revision is None
            or expected_connection_revision is None
            or not provider_parent_id
        ):
            raise ValueError(
                "Provider relationship context is stale: the expected provider generation "
                "is incomplete. Run a new preview."
            )
        try:
            policy_uuid = str(uuid.UUID(str(policy_id)))
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Provider relationship context is stale: the CI policy identity is invalid."
            ) from error
        cursor.execute(
            """
            SELECT policy.revision, policy.enabled, integration.id,
                   integration.revision, integration.enabled,
                   integration.lifecycle_status, company.id
            FROM integration_ci_policies policy
            JOIN integration_connections integration
              ON integration.id = policy.integration_connection_id
            JOIN companies company ON company.id = policy.company_id
            WHERE policy.id = %s::uuid
              AND integration.provider = %s
              AND integration.company_id IS NULL
              AND company.slug = %s
              AND policy.external_parent_id = %s
            FOR SHARE OF policy, integration
            """,
            (
                policy_uuid,
                PROVIDER_TO_DB.get(kind, kind),
                company_id,
                provider_parent_id,
            ),
        )
        current = cursor.fetchone()
        reason = ""
        if not current:
            reason = "the CI policy is missing"
        elif int(current[0] or 0) != int(expected_policy_revision):
            reason = "the CI policy changed after discovery"
        elif not bool(current[4]):
            reason = "the integration is missing or disabled"
        elif str(current[5] or "active") != "active":
            reason = "the integration is not active"
        elif int(current[3] or 0) != int(expected_connection_revision):
            reason = "the integration settings changed after discovery"
        if reason:
            raise ValueError(
                f"Provider relationship context is stale: {reason}. Run a new preview."
            )
        assert current is not None
        cursor.execute(
            """
            SELECT mapping.id
            FROM external_object_mappings mapping
            JOIN provider_company_observations observation
              ON observation.integration_connection_id = mapping.integration_connection_id
             AND observation.external_id = mapping.external_id
            WHERE mapping.integration_connection_id = %s::uuid
              AND mapping.external_object_type = 'company'
              AND mapping.external_id = %s
              AND mapping.canonical_entity_type = 'company'
              AND mapping.canonical_entity_id = %s::uuid
              AND mapping.active = true
              AND observation.active = true
              AND observation.deleted = false
            FOR SHARE OF mapping, observation
            """,
            (str(current[2]), provider_parent_id, str(current[6])),
        )
        if not cursor.fetchone():
            raise ValueError(
                "Provider relationship context is stale: the provider customer mapping "
                "changed or is inactive. Run a new preview."
            )
        return {
            "policyId": policy_uuid,
            "policyRevision": int(expected_policy_revision),
            "connectionRevision": int(expected_connection_revision),
            "providerParentId": provider_parent_id,
        }

    def upsert_relationship_candidates(
        self,
        kind: str,
        company_id: str,
        source_mapping_id: str,
        candidates: list[dict[str, Any]],
        *,
        observed_at: str | None = None,
        policy_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
        provider_parent_id: str | None = None,
    ) -> list[dict]:
        """Replace one provider mapping's proposal set without changing topology."""

        observed = observed_at or utc_now()
        if not parse_timestamp(observed):
            raise ValueError("Relationship candidate timestamp is invalid")
        prepared: list[dict[str, Any]] = []
        endpoint_ids: set[str] = set()
        for value in candidates:
            if not isinstance(value, dict):
                raise ValueError("Relationship candidates must be objects")
            try:
                from_ci_id = (
                    str(uuid.UUID(str(value["fromCiId"]))) if value.get("fromCiId") else None
                )
                to_ci_id = str(uuid.UUID(str(value["toCiId"]))) if value.get("toCiId") else None
            except (TypeError, ValueError) as error:
                raise ValueError("Relationship candidate endpoint is invalid") from error
            from_external = deepcopy(value.get("fromExternalIdentity") or {})
            to_external = deepcopy(value.get("toExternalIdentity") or {})
            if not isinstance(from_external, dict) or not isinstance(to_external, dict):
                raise ValueError("Relationship candidate identities must be objects")
            if not from_ci_id and not from_external:
                raise ValueError("Relationship candidate source identity is required")
            if not to_ci_id and not to_external:
                raise ValueError("Relationship candidate target identity is required")
            if from_ci_id and from_ci_id == to_ci_id:
                raise ValueError("Relationship candidate endpoints must be different")
            relationship_type = str(
                value.get("relationshipType") or value.get("type") or ""
            ).strip()
            if not relationship_type or len(relationship_type) > 80:
                raise ValueError("Relationship candidate type is invalid")
            confidence = float(value.get("confidence", 0))
            if confidence < 0 or confidence > 1:
                raise ValueError("Relationship candidate confidence must be between 0 and 1")
            evidence = deepcopy(value.get("evidence") or {})
            if not isinstance(evidence, dict):
                raise ValueError("Relationship candidate evidence must be an object")
            if len(json.dumps(evidence, separators=(",", ":")).encode("utf-8")) > 262_144:
                raise ValueError("Relationship candidate evidence is too large")
            endpoint_ids.update(item for item in (from_ci_id, to_ci_id) if item)
            prepared.append(
                {
                    "candidateKey": _relationship_candidate_key(value),
                    "fromCiId": from_ci_id,
                    "toCiId": to_ci_id,
                    "fromExternalIdentity": from_external,
                    "toExternalIdentity": to_external,
                    "relationshipType": relationship_type,
                    "confidence": confidence,
                    "evidence": evidence,
                }
            )

        candidate_keys = [item["candidateKey"] for item in prepared]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            provider_context = self._require_postgres_provider_relationship_context(
                cursor,
                kind,
                company_id,
                policy_id=policy_id,
                expected_policy_revision=expected_policy_revision,
                expected_connection_revision=expected_connection_revision,
                provider_parent_id=provider_parent_id,
            )
            if provider_context is not None:
                for candidate in prepared:
                    candidate["evidence"]["providerContext"] = deepcopy(provider_context)
            mapping_uuid, _source_asset_uuid = self._require_provider_ci_mapping(
                cursor,
                kind,
                company_id,
                source_mapping_id,
                provider_parent_id=(
                    str(provider_context["providerParentId"])
                    if provider_context is not None
                    else None
                ),
            )
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            company_uuid = str(company_row[0])
            if endpoint_ids:
                cursor.execute(
                    """
                    SELECT ci.id
                    FROM configuration_items ci
                    JOIN companies company ON company.id = ci.company_id
                    WHERE company.slug = %s
                      AND ci.id = ANY(%s::uuid[])
                      AND ci.retired_at IS NULL
                    """,
                    (company_id, sorted(endpoint_ids)),
                )
                available = {str(row[0]) for row in cursor.fetchall()}
                if available != endpoint_ids:
                    raise ValueError("Relationship candidate crosses the customer boundary")
            for candidate in prepared:
                candidate_id = canonical_uuid(
                    "ci_relationship_candidate",
                    f"{mapping_uuid}:{candidate['candidateKey']}",
                )
                cursor.execute(
                    """
                    INSERT INTO ci_relationship_candidates (
                        id, company_id, source_mapping_id, candidate_key,
                        from_ci_id, to_ci_id, from_external_identity,
                        to_external_identity, relationship_type, confidence,
                        evidence, state, first_observed_at, last_seen_at,
                        retired_at, updated_at
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s,
                        %s::uuid, %s::uuid, %s::jsonb, %s::jsonb,
                        %s, %s, %s::jsonb, 'pending',
                        %s::timestamptz, %s::timestamptz, NULL, now()
                    )
                    ON CONFLICT (source_mapping_id, candidate_key)
                    DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        from_ci_id = EXCLUDED.from_ci_id,
                        to_ci_id = EXCLUDED.to_ci_id,
                        from_external_identity = EXCLUDED.from_external_identity,
                        to_external_identity = EXCLUDED.to_external_identity,
                        relationship_type = EXCLUDED.relationship_type,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        state = CASE
                            WHEN ci_relationship_candidates.state IN ('ignored', 'rejected')
                             AND lower(COALESCE(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(COALESCE(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint'
                                 ) <> lower(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint'
                                 )
                            THEN 'pending'
                            ELSE ci_relationship_candidates.state
                        END,
                        decided_by = CASE
                            WHEN ci_relationship_candidates.state IN ('ignored', 'rejected')
                             AND lower(COALESCE(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(COALESCE(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint'
                                 ) <> lower(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint'
                                 )
                            THEN NULL
                            ELSE ci_relationship_candidates.decided_by
                        END,
                        decided_at = CASE
                            WHEN ci_relationship_candidates.state IN ('ignored', 'rejected')
                             AND lower(COALESCE(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(COALESCE(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint'
                                 ) <> lower(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint'
                                 )
                            THEN NULL
                            ELSE ci_relationship_candidates.decided_at
                        END,
                        decision_notes = CASE
                            WHEN ci_relationship_candidates.state IN ('ignored', 'rejected')
                             AND lower(COALESCE(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(COALESCE(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint'
                                 ) <> lower(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint'
                                 )
                            THEN NULL
                            ELSE ci_relationship_candidates.decision_notes
                        END,
                        approved_relationship_id = CASE
                            WHEN ci_relationship_candidates.state IN ('ignored', 'rejected')
                             AND lower(COALESCE(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(COALESCE(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint',
                                   ''
                                 )) ~ '^[0-9a-f]{64}$'
                             AND lower(
                                   ci_relationship_candidates.evidence
                                     ->> 'evidenceFingerprint'
                                 ) <> lower(
                                   EXCLUDED.evidence ->> 'evidenceFingerprint'
                                 )
                            THEN NULL
                            ELSE ci_relationship_candidates.approved_relationship_id
                        END,
                        last_seen_at = EXCLUDED.last_seen_at,
                        retired_at = NULL,
                        revision = ci_relationship_candidates.revision + 1,
                        observation_count = ci_relationship_candidates.observation_count + 1,
                        updated_at = now()
                    WHERE EXCLUDED.last_seen_at
                          >= ci_relationship_candidates.last_seen_at
                    """,
                    (
                        candidate_id,
                        company_uuid,
                        mapping_uuid,
                        candidate["candidateKey"],
                        candidate["fromCiId"],
                        candidate["toCiId"],
                        json.dumps(candidate["fromExternalIdentity"]),
                        json.dumps(candidate["toExternalIdentity"]),
                        candidate["relationshipType"],
                        candidate["confidence"],
                        json.dumps(candidate["evidence"]),
                        observed,
                        observed,
                    ),
                )
            cursor.execute(
                """
                UPDATE ci_relationship_candidates
                SET retired_at = %s::timestamptz,
                    revision = revision + 1,
                    updated_at = now()
                WHERE source_mapping_id = %s::uuid
                  AND retired_at IS NULL
                  AND last_seen_at <= %s::timestamptz
                  AND candidate_key <> ALL(%s::text[])
                """,
                (observed, mapping_uuid, observed, candidate_keys),
            )
        return self.list_relationship_candidates(
            company_id,
            provider=kind,
            include_retired=False,
        )

    def list_relationship_candidates(
        self,
        company_id: str,
        *,
        state: str | None = None,
        asset_id: str | None = None,
        provider: str | None = None,
        include_retired: bool = False,
        limit: int = 250,
    ) -> list[dict]:
        """Return a bounded canonical relationship proposal queue."""

        if state is not None and state not in RELATIONSHIP_CANDIDATE_STATES:
            raise ValueError("Relationship candidate state is invalid")
        try:
            asset_uuid = str(uuid.UUID(str(asset_id))) if asset_id else None
        except (TypeError, ValueError) as error:
            raise ValueError("Configuration item identity is invalid") from error
        database_provider = PROVIDER_TO_DB.get(provider, provider) if provider else None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidate.id, company.slug, candidate.source_mapping_id,
                       integration.provider, candidate.candidate_key,
                       candidate.from_ci_id, from_ci.display_name,
                       candidate.to_ci_id, to_ci.display_name,
                       candidate.from_external_identity,
                       candidate.to_external_identity,
                       candidate.relationship_type, candidate.confidence,
                       candidate.evidence, candidate.state,
                       candidate.first_observed_at, candidate.last_seen_at,
                       candidate.retired_at, candidate.decided_by,
                       candidate.decided_at, candidate.decision_notes,
                       candidate.approved_relationship_id,
                       candidate.revision, candidate.observation_count
                FROM ci_relationship_candidates candidate
                JOIN companies company ON company.id = candidate.company_id
                JOIN external_object_mappings mapping
                  ON mapping.id = candidate.source_mapping_id
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                LEFT JOIN configuration_items from_ci
                  ON from_ci.id = candidate.from_ci_id
                 AND from_ci.company_id = candidate.company_id
                LEFT JOIN configuration_items to_ci
                  ON to_ci.id = candidate.to_ci_id
                 AND to_ci.company_id = candidate.company_id
                WHERE company.slug = %s
                  AND mapping.active = true
                  AND (%s::text IS NULL OR candidate.state = %s)
                  AND (
                      %s::uuid IS NULL
                      OR candidate.from_ci_id = %s::uuid
                      OR candidate.to_ci_id = %s::uuid
                  )
                  AND (%s::text IS NULL OR integration.provider = %s)
                  AND (%s OR candidate.retired_at IS NULL)
                ORDER BY candidate.last_seen_at DESC, candidate.id DESC
                LIMIT %s
                """,
                (
                    company_id,
                    state,
                    state,
                    asset_uuid,
                    asset_uuid,
                    asset_uuid,
                    database_provider,
                    database_provider,
                    include_retired,
                    max(1, min(int(limit), 1_000)),
                ),
            )
            return [self._relationship_candidate_from_row(row) for row in cursor.fetchall()]

    def decide_relationship_candidate(
        self,
        company_id: str,
        candidate_id: str,
        state: str,
        actor_id: str | None = None,
        notes: str = "",
        approved_relationship_id: str | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict:
        """Audit a proposal decision without creating or retiring relationships."""

        if state not in RELATIONSHIP_CANDIDATE_STATES:
            raise ValueError("Relationship candidate state is invalid")
        try:
            candidate_uuid = str(uuid.UUID(str(candidate_id)))
        except (TypeError, ValueError) as error:
            raise ValueError("Relationship candidate identity is invalid") from error
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidate.id, company.slug, candidate.source_mapping_id,
                       integration.provider, candidate.candidate_key,
                       candidate.from_ci_id, from_ci.display_name,
                       candidate.to_ci_id, to_ci.display_name,
                       candidate.from_external_identity,
                       candidate.to_external_identity,
                       candidate.relationship_type, candidate.confidence,
                       candidate.evidence, candidate.state,
                       candidate.first_observed_at, candidate.last_seen_at,
                       candidate.retired_at, candidate.decided_by,
                       candidate.decided_at, candidate.decision_notes,
                       candidate.approved_relationship_id,
                       candidate.revision, candidate.observation_count,
                       mapping.active
                FROM ci_relationship_candidates candidate
                JOIN companies company ON company.id = candidate.company_id
                JOIN external_object_mappings mapping
                  ON mapping.id = candidate.source_mapping_id
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                LEFT JOIN configuration_items from_ci
                  ON from_ci.id = candidate.from_ci_id
                 AND from_ci.company_id = candidate.company_id
                LEFT JOIN configuration_items to_ci
                  ON to_ci.id = candidate.to_ci_id
                 AND to_ci.company_id = candidate.company_id
                WHERE company.slug = %s AND candidate.id = %s::uuid
                FOR UPDATE OF candidate, mapping
                """,
                (company_id, candidate_uuid),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Relationship candidate not found")
            if state == "approved" and not bool(row[24]):
                raise ValueError("Restore the provider mapping before approving this suggestion")
            before = self._relationship_candidate_from_row(row)
            if expected_revision is not None and int(expected_revision) != int(before["revision"]):
                raise ValueError("Relationship candidate changed; refresh it before deciding")
            if state == "approved" and (not before.get("fromCiId") or not before.get("toCiId")):
                raise ValueError("Resolve both candidate endpoints before approval")
            approved_relationship_uuid = None
            if approved_relationship_id:
                try:
                    approved_relationship_uuid = str(uuid.UUID(str(approved_relationship_id)))
                except (TypeError, ValueError) as error:
                    raise ValueError("Approved relationship identity is invalid") from error
                if state != "approved":
                    raise ValueError("Only approved candidates can link a relationship")
                cursor.execute(
                    """
                    SELECT relationship.id
                    FROM ci_relationships relationship
                    JOIN companies company ON company.id = relationship.company_id
                    WHERE company.slug = %s
                      AND relationship.id = %s::uuid
                      AND relationship.from_ci_id = %s::uuid
                      AND relationship.to_ci_id = %s::uuid
                      AND relationship.relationship_type = %s
                      AND relationship.retired_at IS NULL
                    """,
                    (
                        company_id,
                        approved_relationship_uuid,
                        before["fromCiId"],
                        before["toCiId"],
                        before["relationshipType"],
                    ),
                )
                if not cursor.fetchone():
                    raise ValueError("Approved relationship does not match this candidate")
            linked_relationship_id = (
                approved_relationship_uuid or before.get("approvedRelationshipId")
                if state == "approved"
                else None
            )
            decided_at = utc_now() if state != "pending" else None
            decision_notes = str(notes or "")[:2000] if state != "pending" else ""
            actor_uuid = None
            if actor_id:
                try:
                    actor_uuid = str(uuid.UUID(str(actor_id)))
                except (TypeError, ValueError):
                    cursor.execute(
                        "SELECT id FROM users WHERE attributes->>'legacyId' = %s LIMIT 1",
                        (actor_id,),
                    )
                    actor_row = cursor.fetchone()
                    actor_uuid = str(actor_row[0]) if actor_row else None
            cursor.execute(
                """
                UPDATE ci_relationship_candidates
                SET state = %s,
                    decided_by = %s::uuid,
                    decided_at = %s::timestamptz,
                    decision_notes = %s,
                    approved_relationship_id = %s::uuid,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    state,
                    actor_uuid if state != "pending" else None,
                    decided_at,
                    decision_notes,
                    linked_relationship_id,
                    candidate_uuid,
                ),
            )
            after = {
                **before,
                "state": state,
                "decidedBy": actor_uuid if state != "pending" else None,
                "decidedAt": decided_at,
                "decisionNotes": decision_notes,
                "approvedRelationshipId": linked_relationship_id,
                "revision": int(before["revision"]) + 1,
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "relationship_candidate",
                candidate_uuid,
                "reopened" if state == "pending" else state,
                before,
                after,
                metadata={
                    "sourceMappingId": before["sourceMappingId"],
                    "relationshipType": before["relationshipType"],
                },
            )
        return after

    def approve_relationship_candidate(
        self,
        company_id: str,
        candidate_id: str,
        actor_id: str | None = None,
        notes: str = "",
        *,
        expected_revision: int | None = None,
        policy_id: str | None = None,
        expected_policy_revision: int | None = None,
        expected_connection_revision: int | None = None,
        provider_parent_id: str | None = None,
    ) -> tuple[dict, dict]:
        """Materialize and approve one locked candidate in a single transaction."""

        try:
            candidate_uuid = str(uuid.UUID(str(candidate_id)))
        except (TypeError, ValueError) as error:
            raise ValueError("Relationship candidate identity is invalid") from error
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT candidate.id, company.slug, candidate.source_mapping_id,
                       integration.provider, candidate.candidate_key,
                       candidate.from_ci_id, from_ci.display_name,
                       candidate.to_ci_id, to_ci.display_name,
                       candidate.from_external_identity,
                       candidate.to_external_identity,
                       candidate.relationship_type, candidate.confidence,
                       candidate.evidence, candidate.state,
                       candidate.first_observed_at, candidate.last_seen_at,
                       candidate.retired_at, candidate.decided_by,
                       candidate.decided_at, candidate.decision_notes,
                       candidate.approved_relationship_id,
                       candidate.revision, candidate.observation_count,
                       mapping.active
                FROM ci_relationship_candidates candidate
                JOIN companies company ON company.id = candidate.company_id
                JOIN external_object_mappings mapping
                  ON mapping.id = candidate.source_mapping_id
                JOIN integration_connections integration
                  ON integration.id = mapping.integration_connection_id
                LEFT JOIN configuration_items from_ci
                  ON from_ci.id = candidate.from_ci_id
                 AND from_ci.company_id = candidate.company_id
                LEFT JOIN configuration_items to_ci
                  ON to_ci.id = candidate.to_ci_id
                 AND to_ci.company_id = candidate.company_id
                WHERE company.slug = %s AND candidate.id = %s::uuid
                FOR UPDATE OF candidate, mapping
                """,
                (company_id, candidate_uuid),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Relationship candidate not found")
            if not bool(row[24]):
                raise ValueError("Restore the provider mapping before approving this suggestion")
            before = self._relationship_candidate_from_row(row)
            if expected_revision is not None and int(expected_revision) != int(before["revision"]):
                raise ValueError("Relationship candidate changed; refresh it before deciding")
            stored_context = _required_relationship_provider_context(before.get("evidence"))
            supplied_context = _supplied_relationship_provider_context(
                policy_id=policy_id,
                expected_policy_revision=expected_policy_revision,
                expected_connection_revision=expected_connection_revision,
                provider_parent_id=provider_parent_id,
            )
            if supplied_context is not None and stored_context != supplied_context:
                raise ValueError(
                    "Provider relationship context is stale: the candidate belongs to a "
                    "different provider generation. Run a new preview."
                )
            validated_context = self._require_postgres_provider_relationship_context(
                cursor,
                str(before.get("provider") or ""),
                company_id,
                policy_id=str(stored_context["policyId"]),
                expected_policy_revision=int(stored_context["policyRevision"]),
                expected_connection_revision=int(stored_context["connectionRevision"]),
                provider_parent_id=str(stored_context["providerParentId"]),
            )
            if validated_context != stored_context:
                raise ValueError(
                    "Provider relationship context is stale: the candidate belongs to a "
                    "different provider generation. Run a new preview."
                )
            self._require_provider_ci_mapping(
                cursor,
                str(before.get("provider") or ""),
                company_id,
                str(before.get("sourceMappingId") or ""),
                provider_parent_id=str(stored_context["providerParentId"]),
            )
            if before.get("retiredAt"):
                raise ValueError("Refresh provider evidence before approving this suggestion")
            from_ci_id = str(before.get("fromCiId") or "")
            to_ci_id = str(before.get("toCiId") or "")
            if not from_ci_id or not to_ci_id:
                raise ValueError("Resolve both candidate endpoints before approval")
            relationship_type = str(before.get("relationshipType") or "")
            if relationship_type not in CANONICAL_RELATIONSHIP_TYPES:
                raise ValueError("The provider proposed an unsupported relationship type")
            evidence = deepcopy(before.get("evidence") or {})
            if not isinstance(evidence, dict):
                raise ValueError("Relationship evidence must be an object")
            impact_policy = str(evidence.get("impactPolicy") or "required")
            if impact_policy not in RELATIONSHIP_IMPACT_POLICIES:
                impact_policy = "required"
            relationship_evidence = {
                **evidence,
                "candidateId": candidate_id,
                "provider": before.get("provider"),
            }
            if (
                len(json.dumps(relationship_evidence, separators=(",", ":")).encode("utf-8"))
                > 262_144
            ):
                raise ValueError("Relationship evidence is too large")
            confidence = float(before.get("confidence") or 0)
            if confidence < 0 or confidence > 1:
                raise ValueError("Relationship confidence must be between 0 and 1")
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            company_uuid = str(company_row[0])
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ci_relationships:{company_uuid}",),
            )
            cursor.execute(
                """
                SELECT id
                FROM configuration_items
                WHERE company_id = %s::uuid
                  AND id = ANY(%s::uuid[])
                  AND retired_at IS NULL
                """,
                (company_uuid, [from_ci_id, to_ci_id]),
            )
            if {str(item[0]) for item in cursor.fetchall()} != {from_ci_id, to_ci_id}:
                raise ValueError("Both candidate endpoints must remain in this customer")
            cursor.execute(
                """
                SELECT id, from_ci_id, to_ci_id, relationship_type, impact_policy,
                       source_mapping_id, confidence, evidence, provenance
                FROM ci_relationships
                WHERE company_id = %s::uuid AND retired_at IS NULL
                FOR UPDATE
                """,
                (company_uuid,),
            )
            relationships = [
                {
                    "id": str(item[0]),
                    "fromId": str(item[1]),
                    "toId": str(item[2]),
                    "type": item[3],
                    "impactPolicy": item[4],
                    "sourceMappingId": str(item[5]) if item[5] else None,
                    "confidence": float(item[6]),
                    "evidence": item[7] or {},
                    "provenance": item[8],
                }
                for item in cursor.fetchall()
            ]
            relationship = _matching_relationship(
                relationships,
                from_ci_id,
                to_ci_id,
                relationship_type,
            )
            if (
                not relationship
                and relationship_type == "depends_on"
                and _dependency_cycle(relationships, from_ci_id, to_ci_id)
            ):
                raise ValueError("That provider suggestion would create a dependency cycle")
            if not relationship:
                relationship_uuid = str(uuid.uuid4())
                cursor.execute(
                    """
                    INSERT INTO ci_relationships (
                        id, company_id, from_ci_id, to_ci_id, relationship_type,
                        impact_policy, source_mapping_id, confidence, evidence,
                        provenance
                    ) VALUES (
                        %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s,
                        %s::uuid, %s, %s::jsonb, 'provider'
                    )
                    ON CONFLICT (from_ci_id, to_ci_id, relationship_type) DO UPDATE SET
                        company_id = EXCLUDED.company_id,
                        impact_policy = CASE
                            WHEN ci_relationships.provenance <> 'manual'
                                THEN EXCLUDED.impact_policy
                            ELSE ci_relationships.impact_policy
                        END,
                        source_mapping_id = CASE
                            WHEN ci_relationships.provenance = 'manual'
                                THEN ci_relationships.source_mapping_id
                            ELSE EXCLUDED.source_mapping_id
                        END,
                        confidence = CASE
                            WHEN ci_relationships.provenance <> 'manual'
                                THEN EXCLUDED.confidence
                            ELSE ci_relationships.confidence
                        END,
                        evidence = CASE
                            WHEN ci_relationships.provenance <> 'manual'
                                THEN EXCLUDED.evidence
                            ELSE ci_relationships.evidence
                        END,
                        provenance = ci_relationships.provenance,
                        retired_at = NULL
                    RETURNING id, source_mapping_id, confidence, evidence,
                              provenance, impact_policy
                    """,
                    (
                        relationship_uuid,
                        company_uuid,
                        from_ci_id,
                        to_ci_id,
                        relationship_type,
                        impact_policy,
                        before["sourceMappingId"],
                        confidence,
                        json.dumps(relationship_evidence),
                    ),
                )
                stored_row = cursor.fetchone()
                stored_id = str(stored_row[0])
                reactivated = stored_id != relationship_uuid
                relationship = {
                    "id": stored_id,
                    "fromId": from_ci_id,
                    "toId": to_ci_id,
                    "type": relationship_type,
                    "impactPolicy": stored_row[5],
                    "sourceMappingId": str(stored_row[1]) if stored_row[1] else None,
                    "confidence": float(stored_row[2]),
                    "evidence": stored_row[3] or {},
                    "provenance": stored_row[4],
                }
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "relationship",
                    stored_id,
                    "reactivated" if reactivated else "created",
                    {**relationship, "retired": True} if reactivated else None,
                    relationship,
                )
            actor_uuid = None
            if actor_id:
                try:
                    actor_uuid = str(uuid.UUID(str(actor_id)))
                except (TypeError, ValueError):
                    cursor.execute(
                        "SELECT id FROM users WHERE attributes->>'legacyId' = %s LIMIT 1",
                        (actor_id,),
                    )
                    actor_row = cursor.fetchone()
                    actor_uuid = str(actor_row[0]) if actor_row else None
            decided_at = utc_now()
            decision_notes = str(notes or "")[:2000]
            cursor.execute(
                """
                UPDATE ci_relationship_candidates
                SET state = 'approved',
                    decided_by = %s::uuid,
                    decided_at = %s::timestamptz,
                    decision_notes = %s,
                    approved_relationship_id = %s::uuid,
                    revision = revision + 1,
                    updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    actor_uuid,
                    decided_at,
                    decision_notes,
                    relationship["id"],
                    candidate_uuid,
                ),
            )
            after = {
                **before,
                "state": "approved",
                "decidedBy": actor_uuid,
                "decidedAt": decided_at,
                "decisionNotes": decision_notes,
                "approvedRelationshipId": relationship["id"],
                "revision": int(before["revision"]) + 1,
            }
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "relationship_candidate",
                candidate_uuid,
                "approved",
                before,
                after,
                metadata={
                    "sourceMappingId": before["sourceMappingId"],
                    "relationshipType": relationship_type,
                },
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return after, relationship

    def get_msp_branding(self) -> dict:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT display_name, logo_text, accent_color, secondary_color,
                       logo_data_url, logo_file_name, support_email, support_url,
                       support_phone, welcome_message, report_footer,
                       confidentiality_label
                FROM msp_branding WHERE id = 1
                """
            )
            row = cursor.fetchone()
        if not row:
            return deepcopy(DEFAULT_MSP_BRANDING)
        return {
            "name": row[0],
            "logoText": row[1],
            "accent": row[2],
            "secondaryAccent": row[3],
            "logoDataUrl": row[4] or "",
            "logoFileName": row[5] or "",
            "supportEmail": row[6] or "",
            "supportUrl": row[7] or "",
            "supportPhone": row[8] or "",
            "welcomeMessage": row[9] or "",
            "reportFooter": row[10] or "",
            "confidentialityLabel": row[11] or "",
        }

    def update_msp_branding(self, brand: dict, actor_id: str | None = None) -> dict:
        before = self.get_msp_branding()
        stored = {**DEFAULT_MSP_BRANDING, **deepcopy(brand)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO msp_branding (
                    id, display_name, logo_text, accent_color, secondary_color,
                    logo_data_url, logo_file_name, support_email, support_url,
                    support_phone, welcome_message, report_footer,
                    confidentiality_label, updated_by, updated_at
                ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, now())
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    logo_text = EXCLUDED.logo_text,
                    accent_color = EXCLUDED.accent_color,
                    secondary_color = EXCLUDED.secondary_color,
                    logo_data_url = EXCLUDED.logo_data_url,
                    logo_file_name = EXCLUDED.logo_file_name,
                    support_email = EXCLUDED.support_email,
                    support_url = EXCLUDED.support_url,
                    support_phone = EXCLUDED.support_phone,
                    welcome_message = EXCLUDED.welcome_message,
                    report_footer = EXCLUDED.report_footer,
                    confidentiality_label = EXCLUDED.confidentiality_label,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                (
                    stored["name"],
                    stored["logoText"],
                    stored["accent"],
                    stored["secondaryAccent"],
                    stored["logoDataUrl"] or None,
                    stored["logoFileName"] or None,
                    stored["supportEmail"] or None,
                    stored["supportUrl"] or None,
                    stored["supportPhone"] or None,
                    stored["welcomeMessage"] or None,
                    stored["reportFooter"] or None,
                    stored["confidentialityLabel"] or None,
                    actor_id,
                ),
            )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "msp_branding",
                canonical_uuid("msp_branding", "msp"),
                "updated",
                branding_audit_value(before),
                branding_audit_value(stored),
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_msp_branding()

    def get_company_branding(self, company_id: str) -> dict:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.name, cb.display_name, cb.logo_text, cb.accent_color,
                       cb.secondary_color, cb.logo_data_url, cb.logo_file_name
                FROM companies c
                LEFT JOIN company_branding cb ON cb.company_id = c.id
                WHERE c.slug = %s AND c.status <> 'inactive'
                """,
                (company_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise ValueError("Customer not found")
        (
            company_name,
            display_name,
            logo_text,
            accent,
            secondary,
            logo_data_url,
            logo_file_name,
        ) = row
        return {
            **default_company_branding(company_name),
            **(
                {
                    "name": display_name,
                    "logoText": logo_text,
                    "accent": accent,
                    "secondaryAccent": secondary,
                    "logoDataUrl": logo_data_url or "",
                    "logoFileName": logo_file_name or "",
                }
                if display_name
                else {}
            ),
        }

    def list_company_branding(self) -> dict[str, dict]:
        return {
            company["id"]: self.get_company_branding(company["id"])
            for company in self.list_companies()
        }

    def update_company_branding(
        self, company_id: str, brand: dict, actor_id: str | None = None
    ) -> dict:
        before = self.get_company_branding(company_id)
        stored = {**before, **deepcopy(brand)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM companies WHERE slug = %s AND status <> 'inactive'",
                (company_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise ValueError("Customer not found")
            company_uuid = str(row[0])
            cursor.execute(
                """
                INSERT INTO company_branding (
                    company_id, display_name, logo_text, accent_color,
                    secondary_color, logo_data_url, logo_file_name,
                    updated_by, updated_at
                ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid, now())
                ON CONFLICT (company_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    logo_text = EXCLUDED.logo_text,
                    accent_color = EXCLUDED.accent_color,
                    secondary_color = EXCLUDED.secondary_color,
                    logo_data_url = EXCLUDED.logo_data_url,
                    logo_file_name = EXCLUDED.logo_file_name,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                (
                    company_uuid,
                    stored["name"],
                    stored["logoText"],
                    stored["accent"],
                    stored["secondaryAccent"],
                    stored.get("logoDataUrl") or None,
                    stored.get("logoFileName") or None,
                    actor_id,
                ),
            )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "company_branding",
                company_uuid,
                "updated",
                branding_audit_value(before),
                branding_audit_value(stored),
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_company_branding(company_id)

    def get_email_connection(self) -> dict:
        """Load the singleton MSP email connection from canonical PostgreSQL."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, enabled, auth_mode, tenant_id, client_id,
                       service_principal_object_id, managed_identity_client_id,
                       sender_address, sender_name,
                       reply_to, graph_base_url, client_secret_encrypted,
                       client_secret_nonce, status, last_test_at, last_error,
                       revision, created_at, updated_at
                FROM email_connections WHERE scope = 'msp'
                """
            )
            row = cursor.fetchone()
        if not row:
            return deepcopy(DEFAULT_EMAIL_CONNECTION)
        return {
            "id": str(row[0]),
            "scope": "msp",
            "provider": "microsoft_graph",
            "enabled": bool(row[1]),
            "authMode": row[2],
            "tenantId": row[3] or "",
            "clientId": row[4] or "",
            "servicePrincipalObjectId": row[5] or "",
            "managedIdentityClientId": row[6] or "",
            "senderAddress": row[7] or "",
            "senderName": row[8] or "",
            "replyTo": row[9] or "",
            "graphBaseUrl": row[10],
            "clientSecretEncrypted": row[11] or "",
            "clientSecretNonce": row[12] or "",
            "status": row[13],
            "lastTestAt": self._timestamp(row[14]) or None,
            "lastError": row[15] or "",
            "revision": row[16],
            "createdAt": self._timestamp(row[17]),
            "updatedAt": self._timestamp(row[18]),
        }

    def update_email_connection(self, connection: dict, actor_id: str | None = None) -> dict:
        """Upsert and audit the singleton MSP email connection."""

        before = self.get_email_connection()
        stored = {**before, **deepcopy(connection)}
        connection_uuid = canonical_uuid("email_connection", "msp-email")
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO email_connections (
                    id, scope, provider, enabled, auth_mode, tenant_id, client_id,
                    service_principal_object_id, managed_identity_client_id,
                    sender_address, sender_name, reply_to,
                    graph_base_url, client_secret_encrypted, client_secret_nonce,
                    status, last_test_at, last_error, revision, updated_by, updated_at
                ) VALUES (
                    %s::uuid, 'msp', 'microsoft_graph', %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s, 1,
                    %s::uuid, now()
                )
                ON CONFLICT (scope) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    auth_mode = EXCLUDED.auth_mode,
                    tenant_id = EXCLUDED.tenant_id,
                    client_id = EXCLUDED.client_id,
                    service_principal_object_id = EXCLUDED.service_principal_object_id,
                    managed_identity_client_id = EXCLUDED.managed_identity_client_id,
                    sender_address = EXCLUDED.sender_address,
                    sender_name = EXCLUDED.sender_name,
                    reply_to = EXCLUDED.reply_to,
                    graph_base_url = EXCLUDED.graph_base_url,
                    client_secret_encrypted = EXCLUDED.client_secret_encrypted,
                    client_secret_nonce = EXCLUDED.client_secret_nonce,
                    status = EXCLUDED.status,
                    last_test_at = EXCLUDED.last_test_at,
                    last_error = EXCLUDED.last_error,
                    revision = email_connections.revision + 1,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                (
                    connection_uuid,
                    bool(stored.get("enabled")),
                    stored.get("authMode", "managed_identity"),
                    stored.get("tenantId") or None,
                    stored.get("clientId") or None,
                    stored.get("servicePrincipalObjectId") or None,
                    stored.get("managedIdentityClientId") or None,
                    stored.get("senderAddress") or None,
                    stored.get("senderName") or None,
                    stored.get("replyTo") or None,
                    stored.get("graphBaseUrl") or "https://graph.microsoft.com/v1.0",
                    stored.get("clientSecretEncrypted") or None,
                    stored.get("clientSecretNonce") or None,
                    stored.get("status", "configured"),
                    stored.get("lastTestAt") or None,
                    stored.get("lastError") or None,
                    actor_id,
                ),
            )
            self._insert_audit(
                cursor,
                None,
                actor_id,
                "email_connection",
                connection_uuid,
                "updated",
                email_connection_audit_value(before),
                email_connection_audit_value(stored),
            )
        return self.get_email_connection()

    def create_email_outbox(self, message: dict, actor_id: str | None = None) -> dict:
        """Insert an idempotent provider-neutral email into the canonical outbox."""

        connection = self.get_email_connection()
        if connection.get("id") == "msp-email":
            connection = self.update_email_connection(connection, actor_id)
        message_id = str(uuid.uuid4())
        company_id = message.get("companyId")
        with self.connection_factory() as database, database.cursor() as cursor:
            company_uuid = None
            if company_id:
                cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
                company_row = cursor.fetchone()
                company_uuid = str(company_row[0]) if company_row else None
            cursor.execute(
                """
                INSERT INTO email_outbox (
                    id, company_id, connection_id, idempotency_key, to_addresses,
                    cc_addresses, bcc_addresses, subject, body_html, body_text,
                    template_key, template_version, status, attempts, max_attempts,
                    next_attempt_at, created_by
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s, %s, 'queued', 0, %s, now(), %s::uuid
                )
                ON CONFLICT (idempotency_key) DO UPDATE
                    SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING id
                """,
                (
                    message_id,
                    company_uuid,
                    connection["id"],
                    message["idempotencyKey"],
                    json.dumps(message.get("to") or []),
                    json.dumps(message.get("cc") or []),
                    json.dumps(message.get("bcc") or []),
                    message.get("subject") or "",
                    message.get("bodyHtml") or None,
                    message.get("bodyText") or None,
                    message.get("templateKey", "manual"),
                    int(message.get("templateVersion") or 1),
                    int(message.get("maxAttempts") or 5),
                    actor_id,
                ),
            )
            message_id = str(cursor.fetchone()[0])
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "email_message",
                message_id,
                "queued",
                None,
                {
                    "to": message.get("to") or [],
                    "subject": message.get("subject") or "",
                    "templateKey": message.get("templateKey", "manual"),
                },
            )
        return self.get_email_outbox(message_id) or {}

    def list_email_outbox(self, limit: int = 100) -> list[dict]:
        """List recent email delivery records without their bodies."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT eo.id, c.slug, eo.connection_id, eo.idempotency_key,
                       eo.to_addresses, eo.cc_addresses, eo.bcc_addresses,
                       eo.subject, eo.template_key, eo.template_version, eo.status,
                       eo.attempts, eo.max_attempts, eo.next_attempt_at,
                       eo.accepted_at, eo.last_error, eo.provider_request_id,
                       eo.created_by, eo.created_at, eo.updated_at
                FROM email_outbox eo
                LEFT JOIN companies c ON c.id = eo.company_id
                ORDER BY eo.created_at DESC
                LIMIT %s
                """,
                (max(1, min(limit, 500)),),
            )
            return [self._email_outbox_row(row, include_body=False) for row in cursor.fetchall()]

    def get_email_outbox(self, message_id: str) -> dict | None:
        """Load one complete email outbox record."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT eo.id, c.slug, eo.connection_id, eo.idempotency_key,
                       eo.to_addresses, eo.cc_addresses, eo.bcc_addresses,
                       eo.subject, eo.template_key, eo.template_version, eo.status,
                       eo.attempts, eo.max_attempts, eo.next_attempt_at,
                       eo.accepted_at, eo.last_error, eo.provider_request_id,
                       eo.created_by, eo.created_at, eo.updated_at,
                       eo.body_html, eo.body_text
                FROM email_outbox eo
                LEFT JOIN companies c ON c.id = eo.company_id
                WHERE eo.id = %s::uuid
                """,
                (message_id,),
            )
            row = cursor.fetchone()
        return self._email_outbox_row(row, include_body=True) if row else None

    def update_email_outbox(
        self, message_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Persist one outbox delivery outcome and write governance evidence."""

        current = self.get_email_outbox(message_id)
        if not current:
            return None
        stored = {**current, **deepcopy(changes)}
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE email_outbox SET status = %s, attempts = %s,
                    next_attempt_at = %s::timestamptz,
                    accepted_at = %s::timestamptz, last_error = %s,
                    provider_request_id = %s, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    stored.get("status", "queued"),
                    int(stored.get("attempts") or 0),
                    stored.get("nextAttemptAt") or None,
                    stored.get("acceptedAt") or None,
                    stored.get("lastError") or None,
                    stored.get("providerRequestId") or None,
                    message_id,
                ),
            )
            self._insert_audit(
                cursor,
                stored.get("companyId"),
                actor_id,
                "email_message",
                message_id,
                f"delivery_{stored.get('status', 'updated')}",
                {"status": current.get("status")},
                {
                    "status": stored.get("status"),
                    "attempts": stored.get("attempts"),
                    "providerRequestId": stored.get("providerRequestId"),
                },
                outcome="failed"
                if stored.get("status") in {"failed", "dead_letter"}
                else "success",
                severity="warning"
                if stored.get("status") in {"failed", "dead_letter"}
                else "informational",
                reason=str(stored.get("lastError") or ""),
            )
        return self.get_email_outbox(message_id)

    def _email_outbox_row(self, row: Any, *, include_body: bool) -> dict:
        record = {
            "id": str(row[0]),
            "companyId": row[1],
            "connectionId": str(row[2]),
            "idempotencyKey": row[3],
            "to": row[4] or [],
            "cc": row[5] or [],
            "bcc": row[6] or [],
            "subject": row[7],
            "templateKey": row[8],
            "templateVersion": row[9],
            "status": row[10],
            "attempts": row[11],
            "maxAttempts": row[12],
            "nextAttemptAt": self._timestamp(row[13]) or None,
            "acceptedAt": self._timestamp(row[14]) or None,
            "lastError": row[15] or "",
            "providerRequestId": row[16] or "",
            "createdBy": str(row[17]) if row[17] else None,
            "createdAt": self._timestamp(row[18]),
            "updatedAt": self._timestamp(row[19]),
        }
        if include_body:
            record.update(bodyHtml=row[20] or "", bodyText=row[21] or "")
        return record

    def claim_email_outbox(self, message_id: str | None = None) -> dict | None:
        """Claim one due outbox record with row locking for multi-replica safety."""

        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT eo.id, company.slug AS company_slug, eo.status AS previous_status,
                           eo.attempts AS previous_attempts
                    FROM email_outbox eo
                    LEFT JOIN companies company ON company.id = eo.company_id
                    WHERE (%s::uuid IS NULL OR eo.id = %s::uuid)
                      AND eo.attempts < eo.max_attempts
                      AND (
                        (eo.status IN ('queued', 'failed') AND COALESCE(eo.next_attempt_at, now()) <= now())
                        OR (eo.status = 'sending' AND eo.updated_at <= now() - interval '15 minutes')
                      )
                    ORDER BY eo.next_attempt_at NULLS FIRST, eo.created_at
                    FOR UPDATE OF eo SKIP LOCKED
                    LIMIT 1
                )
                UPDATE email_outbox target
                SET status = 'sending', attempts = target.attempts + 1,
                    last_error = NULL, updated_at = now()
                FROM candidate
                WHERE target.id = candidate.id
                RETURNING target.id, candidate.company_slug, candidate.previous_status,
                          candidate.previous_attempts, target.attempts
                """,
                (message_id, message_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            self._insert_audit(
                cursor,
                row[1],
                None,
                "email_message",
                str(row[0]),
                "delivery_claimed",
                {"status": row[2], "attempts": row[3]},
                {"status": "sending", "attempts": row[4]},
                metadata={"worker": True},
            )
        return self.get_email_outbox(str(row[0]))

    def list_notification_rules(self) -> list[dict]:
        """Load global and customer-specific notification rules."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rule.id, company.slug, rule.rule_key, rule.name, rule.event_type,
                       rule.enabled, rule.lead_days, rule.cadence, rule.recipient_roles,
                       rule.fallback_addresses, rule.template_key, rule.max_attempts,
                       rule.last_run_at, rule.revision, rule.created_at, rule.updated_at
                FROM notification_rules rule
                LEFT JOIN companies company ON company.id = rule.company_id
                ORDER BY company.name NULLS FIRST, rule.name
                """
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "key": row[2],
                    "name": row[3],
                    "eventType": row[4],
                    "enabled": bool(row[5]),
                    "leadDays": int(row[6]),
                    "cadence": row[7],
                    "recipientRoles": row[8] or [],
                    "fallbackAddresses": row[9] or [],
                    "templateKey": row[10],
                    "maxAttempts": int(row[11]),
                    "lastRunAt": self._timestamp(row[12]) or None,
                    "revision": int(row[13]),
                    "createdAt": self._timestamp(row[14]),
                    "updatedAt": self._timestamp(row[15]),
                }
                for row in cursor.fetchall()
            ]

    def update_notification_rule(
        self, rule_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Persist mutable rule controls and audit the change."""

        before = next(
            (item for item in self.list_notification_rules() if item["id"] == rule_id), None
        )
        if not before:
            return None
        stored = {**before, **deepcopy(changes)}
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_rules SET name = %s, enabled = %s, lead_days = %s,
                    cadence = %s, recipient_roles = %s::jsonb,
                    fallback_addresses = %s::jsonb, template_key = %s,
                    max_attempts = %s, last_run_at = %s::timestamptz,
                    revision = revision + 1, updated_by = %s::uuid, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    stored["name"],
                    bool(stored["enabled"]),
                    int(stored["leadDays"]),
                    stored["cadence"],
                    json.dumps(stored.get("recipientRoles") or []),
                    json.dumps(stored.get("fallbackAddresses") or []),
                    stored["templateKey"],
                    int(stored.get("maxAttempts") or 5),
                    stored.get("lastRunAt") or None,
                    actor_id,
                    rule_id,
                ),
            )
            self._insert_audit(
                cursor,
                stored.get("companyId"),
                actor_id,
                "notification_rule",
                rule_id,
                "updated",
                before,
                stored,
            )
        return next(
            (item for item in self.list_notification_rules() if item["id"] == rule_id), None
        )

    def mark_notification_rule_run(self, rule_id: str, run_at: str) -> None:
        """Record scheduler progress without incrementing the editable revision."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_rules
                SET last_run_at = %s::timestamptz, updated_at = now()
                WHERE id = %s::uuid
                """,
                (run_at, rule_id),
            )

    def list_notification_templates(self) -> list[dict]:
        """Load global and customer-specific message templates."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT template.id, company.slug, template.template_key, template.name,
                       template.subject_template, template.html_template,
                       template.text_template, template.enabled, template.version,
                       template.created_at, template.updated_at
                FROM notification_templates template
                LEFT JOIN companies company ON company.id = template.company_id
                ORDER BY company.name NULLS FIRST, template.name
                """
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "key": row[2],
                    "name": row[3],
                    "subjectTemplate": row[4],
                    "htmlTemplate": row[5],
                    "textTemplate": row[6],
                    "enabled": bool(row[7]),
                    "version": int(row[8]),
                    "createdAt": self._timestamp(row[9]),
                    "updatedAt": self._timestamp(row[10]),
                }
                for row in cursor.fetchall()
            ]

    def update_notification_template(
        self, template_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Persist a new version of an editable notification template."""

        before = next(
            (item for item in self.list_notification_templates() if item["id"] == template_id),
            None,
        )
        if not before:
            return None
        stored = {**before, **deepcopy(changes)}
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_templates SET name = %s, subject_template = %s,
                    html_template = %s, text_template = %s, enabled = %s,
                    version = version + 1, updated_by = %s::uuid, updated_at = now()
                WHERE id = %s::uuid
                """,
                (
                    stored["name"],
                    stored["subjectTemplate"],
                    stored["htmlTemplate"],
                    stored["textTemplate"],
                    bool(stored["enabled"]),
                    actor_id,
                    template_id,
                ),
            )
            self._insert_audit(
                cursor,
                stored.get("companyId"),
                actor_id,
                "notification_template",
                template_id,
                "updated",
                before,
                stored,
            )
        return next(
            (item for item in self.list_notification_templates() if item["id"] == template_id),
            None,
        )

    def list_notification_preferences(self, company_id: str | None = None) -> list[dict]:
        """Load notification choices for contacts and portal users."""

        company_filter = company_id or None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT preference.id, company.slug, preference.contact_id,
                       preference.user_id, preference.email_enabled,
                       preference.event_types, preference.digest_mode,
                       preference.created_at, preference.updated_at
                FROM notification_preferences preference
                JOIN companies company ON company.id = preference.company_id
                WHERE (%s::text IS NULL OR company.slug = %s)
                ORDER BY company.name, preference.created_at
                """,
                (company_filter, company_filter),
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "contactId": str(row[2]) if row[2] else None,
                    "userId": str(row[3]) if row[3] else None,
                    "emailEnabled": bool(row[4]),
                    "eventTypes": row[5] or ["*"],
                    "digestMode": row[6],
                    "createdAt": self._timestamp(row[7]),
                    "updatedAt": self._timestamp(row[8]),
                }
                for row in cursor.fetchall()
            ]

    def upsert_notification_preference(self, preference: dict, actor_id: str | None = None) -> dict:
        """Upsert a contact or user email preference."""

        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (preference["companyId"],))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            existing = next(
                (
                    item
                    for item in self.list_notification_preferences(preference["companyId"])
                    if (
                        preference.get("contactId")
                        and item.get("contactId") == preference["contactId"]
                    )
                    or (preference.get("userId") and item.get("userId") == preference["userId"])
                ),
                None,
            )
            preference_id = existing["id"] if existing else str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO notification_preferences (
                    id, company_id, contact_id, user_id, email_enabled,
                    event_types, digest_mode, updated_by, updated_at
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb, %s, %s::uuid, now())
                ON CONFLICT (id) DO UPDATE SET email_enabled = EXCLUDED.email_enabled,
                    event_types = EXCLUDED.event_types, digest_mode = EXCLUDED.digest_mode,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
                """,
                (
                    preference_id,
                    str(company_row[0]),
                    preference.get("contactId"),
                    preference.get("userId"),
                    bool(preference.get("emailEnabled", True)),
                    json.dumps(preference.get("eventTypes") or ["*"]),
                    preference.get("digestMode", "instant"),
                    actor_id,
                ),
            )
            self._insert_audit(
                cursor,
                preference["companyId"],
                actor_id,
                "notification_preference",
                preference_id,
                "updated" if existing else "created",
                existing,
                preference,
            )
        return next(
            item
            for item in self.list_notification_preferences(preference["companyId"])
            if item["id"] == preference_id
        )

    def create_notification_event(self, event: dict, actor_id: str | None = None) -> dict:
        """Insert one idempotent notification event and audit its creation."""

        event_id = str(uuid.uuid4())
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (event["companyId"],))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            cursor.execute(
                """
                INSERT INTO notification_events (
                    id, company_id, rule_id, event_type, entity_type, entity_id,
                    entity_name, dedupe_key, status, recipients, missing_roles,
                    context, email_outbox_id, scheduled_for
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s, %s, %s::uuid, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::uuid, %s::timestamptz
                ) ON CONFLICT (dedupe_key) DO NOTHING
                RETURNING id
                """,
                (
                    event_id,
                    str(company_row[0]),
                    event["ruleId"],
                    event["eventType"],
                    event["entityType"],
                    canonical_uuid(event["entityType"], event["entityId"]),
                    event["entityName"],
                    event["dedupeKey"],
                    event.get("status", "pending"),
                    json.dumps(event.get("recipients") or []),
                    json.dumps(event.get("missingRoles") or []),
                    json.dumps(event.get("context") or {}),
                    event.get("emailOutboxId"),
                    event.get("scheduledFor") or utc_now(),
                ),
            )
            created = cursor.fetchone()
            if created:
                self._insert_audit(
                    cursor,
                    event["companyId"],
                    actor_id,
                    "notification_event",
                    event_id,
                    "created",
                    None,
                    {key: value for key, value in event.items() if key != "context"},
                )
            else:
                cursor.execute(
                    "SELECT id FROM notification_events WHERE dedupe_key = %s",
                    (event["dedupeKey"],),
                )
                event_id = str(cursor.fetchone()[0])
        return next(
            item for item in self.list_notification_events(limit=500) if item["id"] == event_id
        )

    def update_notification_event(
        self, event_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        """Update notification evidence after recipient resolution or delivery."""

        before = next(
            (item for item in self.list_notification_events(limit=500) if item["id"] == event_id),
            None,
        )
        if not before:
            return None
        stored = {**before, **deepcopy(changes)}
        with self.connection_factory() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_events SET status = %s, recipients = %s::jsonb,
                    missing_roles = %s::jsonb, context = %s::jsonb,
                    email_outbox_id = %s::uuid, scheduled_for = %s::timestamptz,
                    updated_at = now() WHERE id = %s::uuid
                """,
                (
                    stored["status"],
                    json.dumps(stored.get("recipients") or []),
                    json.dumps(stored.get("missingRoles") or []),
                    json.dumps(stored.get("context") or {}),
                    stored.get("emailOutboxId"),
                    stored.get("scheduledFor") or utc_now(),
                    event_id,
                ),
            )
            self._insert_audit(
                cursor,
                stored.get("companyId"),
                actor_id,
                "notification_event",
                event_id,
                "updated",
                {"status": before.get("status")},
                {"status": stored.get("status"), "recipients": stored.get("recipients")},
            )
        return next(
            (item for item in self.list_notification_events(limit=500) if item["id"] == event_id),
            None,
        )

    def update_notification_event_for_outbox(
        self, outbox_id: str, status: str, actor_id: str | None = None
    ) -> dict | None:
        """Mirror outbox delivery status to its notification event."""

        event = next(
            (
                item
                for item in self.list_notification_events(limit=500)
                if item.get("emailOutboxId") == outbox_id
            ),
            None,
        )
        return (
            self.update_notification_event(event["id"], {"status": status}, actor_id)
            if event
            else None
        )

    def list_notification_events(
        self, company_id: str | None = None, limit: int = 200
    ) -> list[dict]:
        """List recent notification evidence, optionally for one customer."""

        company_filter = company_id or None
        row_limit = max(1, min(limit, 500))
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.id, company.slug, rule.id, rule.name, event.event_type,
                       event.entity_type, event.entity_id, event.entity_name,
                       event.dedupe_key, event.status, event.recipients,
                       event.missing_roles, event.context, event.email_outbox_id,
                       event.scheduled_for, event.created_at, event.updated_at
                FROM notification_events event
                JOIN companies company ON company.id = event.company_id
                JOIN notification_rules rule ON rule.id = event.rule_id
                WHERE (%s::text IS NULL OR company.slug = %s)
                ORDER BY event.created_at DESC
                LIMIT %s
                """,
                (company_filter, company_filter, row_limit),
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "ruleId": str(row[2]),
                    "ruleName": row[3],
                    "eventType": row[4],
                    "entityType": row[5],
                    "entityId": str(row[6]),
                    "entityName": row[7],
                    "dedupeKey": row[8],
                    "status": row[9],
                    "recipients": row[10] or [],
                    "missingRoles": row[11] or [],
                    "context": row[12] or {},
                    "emailOutboxId": str(row[13]) if row[13] else None,
                    "scheduledFor": self._timestamp(row[14]),
                    "createdAt": self._timestamp(row[15]),
                    "updatedAt": self._timestamp(row[16]),
                }
                for row in cursor.fetchall()
            ]

    def _export_change_templates(self) -> list[dict]:
        """Export template identities with every immutable content version."""

        templates = {item["id"]: {**item, "versions": []} for item in self.list_change_templates()}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version.template_id, version.version, version.content,
                       version.created_by, version.created_at
                FROM change_template_versions version
                ORDER BY version.template_id, version.version
                """
            )
            for template_id, version, content, created_by, created_at in cursor.fetchall():
                template = templates.get(str(template_id))
                if not template:
                    continue
                template["versions"].append(
                    {
                        "version": int(version),
                        "content": content or {},
                        "createdBy": str(created_by) if created_by else None,
                        "createdAt": self._timestamp(created_at),
                    }
                )
        return list(templates.values())

    def export_state(self) -> dict:
        """Assemble a portable document from canonical tables, never a JSON mirror."""
        return {
            "companies": self.list_companies(),
            "users": self.list_users(include_credentials=True),
            "contacts": self.list_contacts(),
            "contactResponsibilities": self.list_contact_responsibilities(include_inactive=True),
            "notificationRules": self.list_notification_rules(),
            "notificationTemplates": self.list_notification_templates(),
            "changeTemplates": self._export_change_templates(),
            "notificationPreferences": self.list_notification_preferences(),
            "accessGroups": self.list_access_groups(),
            "assets": self.list_assets(),
            "relationships": self.list_relationships(),
            "changes": self.list_changes(),
            "integrations": self.list_integrations(),
            "syncRuns": self.list_sync_runs(),
            "branding": self.list_company_branding(),
            "mspBranding": self.get_msp_branding(),
            "auditEvents": [],
        }

    def import_state(self, state: dict, actor_id: str | None = None) -> dict[str, int]:
        """Explicitly merge a validated portable state into canonical tables."""
        self.state.clear()
        self.state.update(deepcopy(state))
        self.state.setdefault("auditEvents", [])
        return self.bootstrap()

    @staticmethod
    def _ci_preview_run_from_row(row: tuple) -> dict[str, Any]:
        """Normalize one canonical sync-run row, retaining internal lease state."""

        attributes = row[10] or {}
        company_id = row[11] or attributes.get("companyId")
        policy_id = str(row[12]) if row[12] else attributes.get("policyId")
        return {
            "id": str(row[0]),
            "type": PROVIDER_FROM_DB.get(row[1], row[1]),
            "status": "success" if row[2] == "succeeded" else row[2],
            "message": row[3] or "",
            "startedAt": PostgresCmdbRepository._timestamp(row[4]) or None,
            "finishedAt": PostgresCmdbRepository._timestamp(row[5]) or None,
            "discovered": int(row[6] or 0),
            "imported": int(row[7] or 0),
            "updated": int(row[8] or 0),
            "review": int(row[9] or 0),
            "attributes": attributes,
            "companyId": company_id,
            "providerCompanyId": attributes.get("providerCompanyId"),
            "policyId": policy_id,
            "trigger": attributes.get("trigger", ""),
            "requestedBy": str(row[13]) if row[13] else None,
            "requestedAt": PostgresCmdbRepository._timestamp(row[14]),
            "availableAt": PostgresCmdbRepository._timestamp(row[15]),
            "attemptCount": int(row[16] or 0),
            "maxAttempts": int(row[17] or 3),
            "leaseOwner": row[18] or None,
            "leaseUntil": PostgresCmdbRepository._timestamp(row[19]) or None,
            "heartbeatAt": PostgresCmdbRepository._timestamp(row[20]) or None,
            "cancelRequestedAt": PostgresCmdbRepository._timestamp(row[21]) or None,
            "cancelledAt": PostgresCmdbRepository._timestamp(row[22]) or None,
            "retryOfId": str(row[23]) if row[23] else None,
            "dedupeKey": row[24] or None,
            "progress": row[25] or {},
            "updatedAt": PostgresCmdbRepository._timestamp(row[26]),
            "errorSummary": row[27] or "",
        }

    @staticmethod
    def _select_ci_preview_run(
        cursor: Any,
        run_id: str,
        *,
        company_ids: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        """Select one sync run with optional canonical customer scoping."""

        restrict_companies = company_ids is not None
        permitted_companies = sorted(set(company_ids or []))
        cursor.execute(
            """
            SELECT run.id, integration.provider, run.status, run.message,
                   run.started_at, run.finished_at, run.discovered_count,
                   run.created_count, run.updated_count, run.review_count,
                   run.attributes, company.slug, run.policy_id, run.requested_by,
                   run.requested_at, run.available_at, run.attempt_count,
                   run.max_attempts, run.lease_owner, run.lease_until,
                   run.heartbeat_at, run.cancel_requested_at, run.cancelled_at,
                   run.retry_of_id, run.dedupe_key, run.progress, run.updated_at,
                   run.error_summary
            FROM sync_runs run
            JOIN integration_connections integration
              ON integration.id = run.integration_connection_id
            LEFT JOIN companies company ON company.id = run.company_id
            WHERE run.id::text = %s
              AND (
                %s = false
                OR COALESCE(company.slug, run.attributes ->> 'companyId') IS NULL
                OR COALESCE(company.slug, run.attributes ->> 'companyId')
                    = ANY(%s::text[])
              )
            """,
            (run_id, restrict_companies, permitted_companies),
        )
        row = cursor.fetchone()
        return PostgresCmdbRepository._ci_preview_run_from_row(row) if row else None

    def create_ci_preview_run(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
        policy_id: str,
        trigger: str,
        actor_id: str | None,
        policy_snapshot: dict,
        retry_of_id: str | None = None,
        schedule_trigger: str | None = None,
    ) -> dict:
        """Queue and reserve one policy-scoped preview in canonical PostgreSQL."""

        provider = PROVIDER_TO_DB.get(kind, kind)
        run_id = str(uuid.uuid4())
        dedupe_key = _ci_preview_dedupe_key(
            kind,
            company_id,
            provider_parent_id,
            policy_id,
        )
        selected_trigger = str(trigger or "manual_preview")[:80]
        selected_schedule_trigger = str(schedule_trigger or selected_trigger)[:80]
        snapshot = deepcopy(policy_snapshot or {})
        attributes = {
            "operation": _ci_preview_operation(kind),
            "trigger": selected_trigger,
            "scheduleTrigger": selected_schedule_trigger,
            "companyId": company_id,
            "providerCompanyId": provider_parent_id,
            "policyId": policy_id,
            "policyRevision": int(snapshot.get("revision") or 0),
            "policySnapshot": snapshot,
            "readOnly": True,
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT integration.id, company.id, policy.id, policy.lease_until
                FROM integration_ci_policies policy
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = policy.company_id
                WHERE policy.id::text = %s
                  AND integration.provider = %s
                  AND integration.company_id IS NULL
                  AND integration.enabled = true
                  AND integration.lifecycle_status = 'active'
                  AND company.slug = %s
                  AND policy.external_parent_id = %s
                FOR UPDATE OF policy
                """,
                (policy_id, provider, company_id, provider_parent_id),
            )
            policy_row = cursor.fetchone()
            if not policy_row:
                raise ValueError("CI preview policy not found or integration is inactive")
            integration_id, company_uuid, canonical_policy_id, policy_lease_until = policy_row
            cursor.execute(
                """
                SELECT id::text
                FROM sync_runs
                WHERE policy_id = %s::uuid
                  AND status IN ('queued', 'running')
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (str(canonical_policy_id),),
            )
            active = cursor.fetchone()
            if active:
                existing = self._select_ci_preview_run(cursor, active[0])
                if existing:
                    return _public_sync_run(existing)
            if policy_lease_until and policy_lease_until > datetime.now(UTC):
                raise ValueError("This policy is already running; refresh and retry")
            cursor.execute(
                """
                INSERT INTO sync_runs (
                    id, integration_connection_id, company_id, policy_id,
                    requested_by, retry_of_id, status, requested_at, available_at,
                    started_at, finished_at, attempt_count, max_attempts,
                    dedupe_key, progress, message, attributes, updated_at
                ) VALUES (
                    %s::uuid, %s::uuid, %s::uuid, %s::uuid,
                    %s::uuid, %s::uuid, 'queued', now(), now(),
                    NULL, NULL, 0, 3, %s, '{"phase":"queued"}'::jsonb,
                    'Preview queued for background processing.', %s::jsonb, now()
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    run_id,
                    str(integration_id),
                    str(company_uuid),
                    str(canonical_policy_id),
                    actor_id,
                    retry_of_id,
                    dedupe_key,
                    json.dumps(attributes),
                ),
            )
            inserted = cursor.fetchone()
            if not inserted:
                cursor.execute(
                    """
                    SELECT id::text
                    FROM sync_runs
                    WHERE policy_id = %s::uuid
                      AND status IN ('queued', 'running')
                    ORDER BY requested_at DESC
                    LIMIT 1
                    """,
                    (str(canonical_policy_id),),
                )
                active = cursor.fetchone()
                if not active:
                    raise ValueError("Could not queue the integration preview")
                existing = self._select_ci_preview_run(cursor, active[0])
                if not existing:
                    raise ValueError("Could not load the active integration preview")
                return _public_sync_run(existing)
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = %s,
                    lease_until = now() + interval '1 hour'
                WHERE id = %s::uuid
                """,
                (f"queued:{run_id}"[:120], str(canonical_policy_id)),
            )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "sync_run",
                run_id,
                "queued",
                None,
                {
                    "status": "queued",
                    "provider": kind,
                    "policyId": str(canonical_policy_id),
                    "retryOfId": retry_of_id,
                },
            )
        stored = self.get_sync_run(run_id)
        if not stored:
            raise ValueError("Queued integration preview could not be loaded")
        return stored

    def get_sync_run(
        self,
        run_id: str,
        company_ids: Iterable[str] | None = None,
    ) -> dict | None:
        """Return one canonical sync run using server-enforced customer scope."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            run = self._select_ci_preview_run(cursor, run_id, company_ids=company_ids)
        return _public_sync_run(run) if run else None

    def get_latest_ci_preview_run(
        self,
        kind: str,
        company_id: str,
        provider_parent_id: str,
    ) -> dict | None:
        """Return the newest canonical preview for a provider/customer scope."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run.id::text
                FROM sync_runs run
                JOIN integration_connections integration
                  ON integration.id = run.integration_connection_id
                JOIN companies company ON company.id = run.company_id
                WHERE integration.provider = %s
                  AND company.slug = %s
                  AND run.attributes ->> 'providerCompanyId' = %s
                  AND run.attributes ->> 'operation' = %s
                ORDER BY run.requested_at DESC, run.id DESC
                LIMIT 1
                """,
                (
                    PROVIDER_TO_DB.get(kind, kind),
                    company_id,
                    provider_parent_id,
                    _ci_preview_operation(kind),
                ),
            )
            row = cursor.fetchone()
            run = self._select_ci_preview_run(cursor, row[0]) if row else None
        return _public_sync_run(run) if run else None

    def claim_ci_preview_run(
        self,
        worker_id: str,
        provider: str | None = None,
        lease_seconds: int = 180,
    ) -> dict | None:
        """Atomically claim queued or stale work across PostgreSQL replicas."""

        selected_provider = PROVIDER_TO_DB.get(provider, provider) if provider else None
        bounded_lease = max(30, min(int(lease_seconds), 3600))
        claimed_id: str | None = None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH exhausted AS (
                    SELECT run.id,
                           CASE
                               WHEN run.status = 'queued'
                                   THEN left('queued:' || run.id::text, 120)
                               ELSE left(run.lease_owner, 120)
                           END AS expected_policy_owner
                    FROM sync_runs run
                    JOIN integration_connections integration
                      ON integration.id = run.integration_connection_id
                    WHERE (%s::text IS NULL OR integration.provider = %s)
                      AND run.attempt_count >= run.max_attempts
                      AND (
                        (run.status = 'queued' AND run.available_at <= now())
                        OR (
                            run.status = 'running'
                            AND COALESCE(run.lease_until, run.updated_at) <= now()
                        )
                      )
                    FOR UPDATE OF run SKIP LOCKED
                )
                UPDATE sync_runs target
                SET status = CASE
                        WHEN target.cancel_requested_at IS NULL THEN 'failed'
                        ELSE 'cancelled'
                    END,
                    message = CASE
                        WHEN target.cancel_requested_at IS NULL
                            THEN 'Preview failed after the maximum number of attempts.'
                        ELSE 'Preview cancelled.'
                    END,
                    error_summary = CASE
                        WHEN target.cancel_requested_at IS NULL
                            THEN 'Maximum preview attempts reached'
                        ELSE NULL
                    END,
                    finished_at = now(),
                    cancelled_at = CASE
                        WHEN target.cancel_requested_at IS NULL THEN NULL
                        ELSE now()
                    END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    heartbeat_at = now(),
                    progress = jsonb_build_object(
                        'phase',
                        CASE
                            WHEN target.cancel_requested_at IS NULL THEN 'failed'
                            ELSE 'cancelled'
                        END
                    ),
                    updated_at = now()
                FROM exhausted
                WHERE target.id = exhausted.id
                RETURNING target.policy_id, exhausted.expected_policy_owner
                """,
                (selected_provider, selected_provider),
            )
            exhausted_policy_leases = [
                (str(row[0]), str(row[1])[:120]) for row in cursor.fetchall() if row[0] and row[1]
            ]
            for exhausted_policy_id, expected_policy_owner in exhausted_policy_leases:
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET lease_owner = NULL, lease_until = NULL
                    WHERE id = %s::uuid
                      AND lease_owner = %s
                    """,
                    (exhausted_policy_id, expected_policy_owner),
                )
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT run.id, company.slug AS company_slug,
                           integration.provider, run.policy_id
                    FROM sync_runs run
                    JOIN integration_connections integration
                      ON integration.id = run.integration_connection_id
                    LEFT JOIN companies company ON company.id = run.company_id
                    WHERE (%s::text IS NULL OR integration.provider = %s)
                      AND integration.enabled = true
                      AND integration.lifecycle_status = 'active'
                      AND run.attempt_count < run.max_attempts
                      AND (
                        (run.status = 'queued' AND run.available_at <= now())
                        OR (
                            run.status = 'running'
                            AND COALESCE(run.lease_until, run.updated_at) <= now()
                        )
                      )
                    ORDER BY
                        CASE WHEN run.status = 'queued' THEN 0 ELSE 1 END,
                        run.available_at,
                        run.requested_at,
                        run.id
                    FOR UPDATE OF run SKIP LOCKED
                    LIMIT 1
                )
                UPDATE sync_runs target
                SET status = 'running',
                    started_at = COALESCE(target.started_at, now()),
                    attempt_count = target.attempt_count + 1,
                    lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second'),
                    heartbeat_at = now(),
                    progress = target.progress || jsonb_build_object(
                        'phase',
                        CASE
                            WHEN target.cancel_requested_at IS NULL THEN 'starting'
                            ELSE 'cancelling'
                        END
                    ),
                    updated_at = now()
                FROM candidate
                WHERE target.id = candidate.id
                RETURNING target.id, target.policy_id, candidate.company_slug,
                          candidate.provider, target.attempt_count
                """,
                (
                    selected_provider,
                    selected_provider,
                    str(worker_id)[:160],
                    bounded_lease,
                ),
            )
            claimed = cursor.fetchone()
            if not claimed:
                return None
            claimed_id = str(claimed[0])
            claimed_policy_id = str(claimed[1]) if claimed[1] else None
            if claimed_policy_id:
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET lease_owner = %s,
                        lease_until = now() + (%s * interval '1 second')
                    WHERE id = %s::uuid
                    """,
                    (str(worker_id)[:120], bounded_lease, claimed_policy_id),
                )
            self._insert_audit(
                cursor,
                claimed[2],
                None,
                "sync_run",
                claimed_id,
                "claimed",
                None,
                {"status": "running", "attemptCount": int(claimed[4])},
                metadata={
                    "provider": PROVIDER_FROM_DB.get(claimed[3], claimed[3]),
                    "worker": True,
                },
                actor_type="system",
                source_system="integration_worker",
            )
            internal = self._select_ci_preview_run(cursor, claimed_id)
        return _public_sync_run(internal, include_internal=True) if internal else None

    def renew_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        progress: dict,
        message: str = "",
        lease_seconds: int = 180,
    ) -> dict | None:
        """Renew an owned job and policy lease while persisting bounded progress."""

        bounded_lease = max(30, min(int(lease_seconds), 3600))
        safe_progress = _bounded_preview_progress(progress)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sync_runs
                SET heartbeat_at = now(),
                    lease_until = now() + (%s * interval '1 second'),
                    progress = progress || %s::jsonb,
                    message = CASE WHEN %s = '' THEN message ELSE %s END,
                    updated_at = now()
                WHERE id::text = %s
                  AND status = 'running'
                  AND lease_owner = %s
                RETURNING policy_id
                """,
                (
                    bounded_lease,
                    json.dumps(safe_progress),
                    str(message)[:1000],
                    str(message)[:1000],
                    run_id,
                    str(worker_id)[:160],
                ),
            )
            row = cursor.fetchone()
            if not row:
                return None
            if row[0]:
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET lease_owner = %s,
                        lease_until = now() + (%s * interval '1 second')
                    WHERE id = %s::uuid
                    """,
                    (str(worker_id)[:120], bounded_lease, str(row[0])),
                )
            run = self._select_ci_preview_run(cursor, run_id)
        return _public_sync_run(run) if run else None

    def is_sync_run_cancel_requested(
        self,
        run_id: str,
        worker_id: str | None = None,
    ) -> bool:
        """Return the cooperative-cancellation signal for an optional owner."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cancel_requested_at IS NOT NULL OR status = 'cancelled'
                FROM sync_runs
                WHERE id::text = %s
                  AND (%s::text IS NULL OR lease_owner = %s)
                """,
                (run_id, worker_id, str(worker_id)[:160] if worker_id else None),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def request_sync_run_cancel(
        self,
        run_id: str,
        actor_id: str | None,
    ) -> dict | None:
        """Cancel queued work immediately or request cooperative running cancellation."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run.status, run.policy_id, company.slug
                FROM sync_runs run
                LEFT JOIN companies company ON company.id = run.company_id
                WHERE run.id::text = %s
                FOR UPDATE OF run
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            status, policy_id, company_id = row
            if status == "queued":
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'cancelled',
                        cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        cancelled_at = now(),
                        finished_at = now(),
                        lease_owner = NULL,
                        lease_until = NULL,
                        progress = '{"phase":"cancelled"}'::jsonb,
                        message = 'Preview cancelled before execution.',
                        updated_at = now()
                    WHERE id::text = %s
                    """,
                    (run_id,),
                )
                if policy_id:
                    cursor.execute(
                        """
                        UPDATE integration_ci_policies
                        SET lease_owner = NULL, lease_until = NULL
                        WHERE id = %s::uuid
                          AND lease_owner = %s
                        """,
                        (str(policy_id), f"queued:{run_id}"[:120]),
                    )
            elif status == "running":
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        progress = progress || '{"phase":"cancelling"}'::jsonb,
                        message = (
                            'Cancellation requested; waiting for the current provider request.'
                        ),
                        updated_at = now()
                    WHERE id::text = %s
                    """,
                    (run_id,),
                )
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "sync_run",
                run_id,
                "cancel_requested",
                {"status": status},
                {"status": "cancelled" if status == "queued" else status},
            )
            run = self._select_ci_preview_run(cursor, run_id)
        return _public_sync_run(run) if run else None

    def complete_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        preview_summary: dict,
        actor_id: str | None = None,
    ) -> dict | None:
        """Complete an owned preview and release its policy lease atomically."""

        summary = _sanitized_preview_summary(preview_summary)
        counts = summary["counts"]
        review_count = sum(counts.get(key, 0) for key in ("create", "update", "link", "conflict"))
        message = str(preview_summary.get("message") or "Preview completed.")[:1000]
        progress = {
            "phase": "completed",
            "discovered": summary["discovered"],
            "included": summary["included"],
            "excluded": summary["excluded"],
            "reviewed": review_count,
            "current": summary["discovered"],
            "total": summary["discovered"],
            "percent": 100,
            "counts": counts,
        }
        bounded_owner = str(worker_id)[:160]
        policy_owner = bounded_owner[:120]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run.cancel_requested_at, run.progress, run.policy_id, run.attributes
                FROM sync_runs run
                JOIN integration_ci_policies policy ON policy.id = run.policy_id
                WHERE run.id::text = %s
                  AND run.status = 'running'
                  AND run.lease_owner = %s
                  AND run.lease_until > now()
                  AND policy.lease_owner = %s
                  AND policy.lease_until > now()
                FOR UPDATE OF run, policy
                """,
                (run_id, bounded_owner, policy_owner),
            )
            owned = cursor.fetchone()
            if not owned:
                return None
            if owned[0]:
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'cancelled', cancelled_at = now(), finished_at = now(),
                        lease_owner = NULL, lease_until = NULL, heartbeat_at = now(),
                        progress = '{"phase":"cancelled"}'::jsonb,
                        message = 'Preview cancelled.', error_summary = NULL,
                        updated_at = now()
                    WHERE id::text = %s
                    RETURNING policy_id, company_id
                    """,
                    (run_id,),
                )
            else:
                progress["enriched"] = int(_bounded_preview_progress(owned[1]).get("enriched") or 0)
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'succeeded',
                        finished_at = now(),
                        discovered_count = %s,
                        review_count = %s,
                        lease_owner = NULL,
                        lease_until = NULL,
                        heartbeat_at = now(),
                        progress = %s::jsonb,
                        message = %s,
                        error_summary = NULL,
                        attributes = attributes || %s::jsonb,
                        updated_at = now()
                    WHERE id::text = %s
                      AND status = 'running'
                      AND lease_owner = %s
                    RETURNING policy_id, company_id
                    """,
                    (
                        summary["discovered"],
                        review_count,
                        json.dumps(progress),
                        message,
                        json.dumps({"resultSummary": summary}),
                        run_id,
                        bounded_owner,
                    ),
                )
            completed = cursor.fetchone()
            if not completed:
                return None
            _returned_policy_id, company_uuid = completed
            policy_id = owned[2]
            if policy_id:
                trigger = _ci_preview_schedule_trigger({"attributes": owned[3] or {}})
                if trigger in {"continuous_preview", "manual_sync"} and not owned[0]:
                    cursor.execute(
                        """
                        UPDATE integration_ci_policies
                        SET last_run_at = now(),
                            last_success_at = now(),
                            last_error = NULL,
                            consecutive_failures = 0,
                            next_run_at = CASE
                                WHEN enabled AND sync_mode = 'continuous_preview'
                                    THEN now() + (interval_minutes * interval '1 minute')
                                ELSE NULL
                            END,
                            lease_owner = NULL,
                            lease_until = NULL
                        WHERE id = %s::uuid
                          AND lease_owner = %s
                        """,
                        (str(policy_id), policy_owner),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE integration_ci_policies
                        SET lease_owner = NULL, lease_until = NULL
                        WHERE id = %s::uuid
                          AND lease_owner = %s
                        """,
                        (str(policy_id), policy_owner),
                    )
                if cursor.rowcount != 1:
                    raise RuntimeError("Preview policy lease is no longer owned")
            company_id = None
            if company_uuid:
                cursor.execute(
                    "SELECT slug FROM companies WHERE id = %s::uuid",
                    (str(company_uuid),),
                )
                company_row = cursor.fetchone()
                company_id = company_row[0] if company_row else None
            was_cancelled = bool(owned[0])
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "sync_run",
                run_id,
                "cancelled" if was_cancelled else "completed",
                None,
                {
                    "status": "cancelled" if was_cancelled else "success",
                    "resultSummary": None if was_cancelled else summary,
                },
                actor_type="system" if actor_id is None else "user",
                source_system="integration_worker" if actor_id is None else "web",
            )
            run = self._select_ci_preview_run(cursor, run_id)
        return _public_sync_run(run) if run else None

    def publish_and_complete_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        review_items: list[dict],
        preview_summary: dict,
        actor_id: str | None = None,
        *,
        presence_snapshot: dict[str, Any] | None = None,
    ) -> dict | None:
        """Publish review observations and terminal evidence in one transaction."""

        bounded_owner = str(worker_id)[:160]
        policy_owner = bounded_owner[:120]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run.cancel_requested_at, run.progress, run.policy_id,
                       company.slug, company.id, integration.provider, run.attributes,
                       run.lease_until, policy.lease_owner, policy.lease_until,
                       policy.external_parent_id
                FROM sync_runs run
                JOIN integration_connections integration
                  ON integration.id = run.integration_connection_id
                JOIN companies company ON company.id = run.company_id
                JOIN integration_ci_policies policy ON policy.id = run.policy_id
                WHERE run.id::text = %s
                  AND run.status = 'running'
                  AND run.lease_owner = %s
                  AND run.lease_until > now()
                  AND policy.lease_owner = %s
                  AND policy.lease_until > now()
                FOR UPDATE OF run, policy
                """,
                (run_id, bounded_owner, policy_owner),
            )
            owned = cursor.fetchone()
            if not owned:
                return None
            (
                cancel_requested_at,
                previous_progress,
                policy_uuid,
                company_id,
                company_uuid,
                provider,
                attributes,
                _run_lease_until,
                _policy_lease_owner,
                _policy_lease_until,
                provider_parent_id,
            ) = owned
            attributes = attributes if isinstance(attributes, dict) else {}
            if presence_snapshot is not None:
                attributes = {
                    **attributes,
                    "connectionRevision": int(
                        _normalized_ci_presence_snapshot(presence_snapshot)["connectionRevision"]
                    ),
                }
            if (
                str(attributes.get("companyId") or "") != str(company_id)
                or str(attributes.get("providerCompanyId") or "") != str(provider_parent_id)
                or str(attributes.get("policyId") or "") != str(policy_uuid)
            ):
                raise ValueError("Preview run scope does not match its canonical policy")

            if cancel_requested_at:
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = 'cancelled', cancelled_at = now(), finished_at = now(),
                        lease_owner = NULL, lease_until = NULL, heartbeat_at = now(),
                        progress = '{"phase":"cancelled"}'::jsonb,
                        message = 'Preview cancelled.', error_summary = NULL,
                        updated_at = now()
                    WHERE id::text = %s
                      AND status = 'running'
                      AND lease_owner = %s
                    """,
                    (run_id, bounded_owner),
                )
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET lease_owner = NULL, lease_until = NULL
                    WHERE id = %s::uuid
                      AND lease_owner = %s
                    """,
                    (str(policy_uuid), policy_owner),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Preview policy lease is no longer owned")
                self._insert_audit(
                    cursor,
                    company_id,
                    actor_id,
                    "sync_run",
                    run_id,
                    "cancelled",
                    None,
                    {"status": "cancelled", "resultSummary": None},
                    outcome="success",
                    severity="informational",
                    actor_type="system" if actor_id is None else "user",
                    source_system="integration_worker" if actor_id is None else "web",
                )
                run = self._select_ci_preview_run(cursor, run_id)
                return _public_sync_run(run) if run else None

            queue_summary = self._replace_ci_review_items_with_cursor(
                cursor,
                str(policy_uuid),
                str(company_id),
                run_id,
                review_items,
                actor_id,
            )
            if presence_snapshot is not None:
                self._apply_postgres_ci_presence_snapshot(
                    cursor,
                    kind=PROVIDER_FROM_DB.get(provider, provider),
                    policy_id=str(policy_uuid),
                    company_uuid=str(company_uuid),
                    company_id=str(company_id),
                    provider_parent_id=str(provider_parent_id),
                    run_id=run_id,
                    presence_snapshot=presence_snapshot,
                    expected_policy_revision=int(attributes.get("policyRevision") or 0),
                )
            aggregate = deepcopy(preview_summary)
            aggregate["queueSummary"] = queue_summary
            summary = _sanitized_preview_summary(aggregate)
            counts = summary["counts"]
            review_count = sum(
                counts.get(key, 0) for key in ("create", "update", "link", "conflict")
            )
            progress = {
                "phase": "completed",
                "discovered": summary["discovered"],
                "enriched": int(_bounded_preview_progress(previous_progress).get("enriched") or 0),
                "included": summary["included"],
                "excluded": summary["excluded"],
                "reviewed": review_count,
                "current": summary["discovered"],
                "total": summary["discovered"],
                "percent": 100,
                "counts": counts,
            }
            message = str(preview_summary.get("message") or "Preview completed.")[:1000]
            cursor.execute(
                """
                UPDATE sync_runs
                SET status = 'succeeded',
                    finished_at = now(),
                    discovered_count = %s,
                    review_count = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    heartbeat_at = now(),
                    progress = %s::jsonb,
                    message = %s,
                    error_summary = NULL,
                    attributes = attributes || %s::jsonb,
                    updated_at = now()
                WHERE id::text = %s
                  AND status = 'running'
                  AND lease_owner = %s
                """,
                (
                    summary["discovered"],
                    review_count,
                    json.dumps(progress),
                    message,
                    json.dumps(
                        {
                            "resultSummary": summary,
                            **(
                                {"connectionRevision": attributes["connectionRevision"]}
                                if "connectionRevision" in attributes
                                else {}
                            ),
                        }
                    ),
                    run_id,
                    bounded_owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Preview run lease is no longer owned")
            trigger = _ci_preview_schedule_trigger({"attributes": attributes})
            if trigger in {"continuous_preview", "manual_sync"}:
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET last_run_at = now(),
                        last_success_at = now(),
                        last_error = NULL,
                        consecutive_failures = 0,
                        next_run_at = CASE
                            WHEN enabled AND sync_mode = 'continuous_preview'
                                THEN now() + (interval_minutes * interval '1 minute')
                            ELSE NULL
                        END,
                        lease_owner = NULL,
                        lease_until = NULL
                    WHERE id = %s::uuid
                      AND lease_owner = %s
                    """,
                    (str(policy_uuid), policy_owner),
                )
            else:
                cursor.execute(
                    """
                    UPDATE integration_ci_policies
                    SET lease_owner = NULL, lease_until = NULL
                    WHERE id = %s::uuid
                      AND lease_owner = %s
                    """,
                    (str(policy_uuid), policy_owner),
                )
            if cursor.rowcount != 1:
                raise RuntimeError("Preview policy lease is no longer owned")
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "sync_run",
                run_id,
                "completed",
                None,
                {"status": "success", "resultSummary": summary},
                metadata={"provider": PROVIDER_FROM_DB.get(provider, provider)},
                actor_type="system" if actor_id is None else "user",
                source_system="integration_worker" if actor_id is None else "web",
            )
            run = self._select_ci_preview_run(cursor, run_id)
        return _public_sync_run(run) if run else None

    def fail_ci_preview_run(
        self,
        run_id: str,
        worker_id: str,
        error: Any,
        actor_id: str | None = None,
        cancelled: bool = False,
    ) -> dict | None:
        """Fail or cancel an owned preview and release its policy lease atomically."""

        detail = str(error)[:1000]
        bounded_owner = str(worker_id)[:160]
        policy_owner = bounded_owner[:120]
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run.cancel_requested_at, run.policy_id, run.company_id,
                       run.attributes
                FROM sync_runs run
                JOIN integration_ci_policies policy ON policy.id = run.policy_id
                WHERE run.id::text = %s
                  AND run.status = 'running'
                  AND run.lease_owner = %s
                  AND run.lease_until > now()
                  AND policy.lease_owner = %s
                  AND policy.lease_until > now()
                FOR UPDATE OF run, policy
                """,
                (run_id, bounded_owner, policy_owner),
            )
            owned = cursor.fetchone()
            if not owned:
                return None
            cursor.execute(
                """
                UPDATE sync_runs
                SET status = CASE
                        WHEN %s OR cancel_requested_at IS NOT NULL
                            THEN 'cancelled'
                        ELSE 'failed'
                    END,
                    cancelled_at = CASE
                        WHEN %s OR cancel_requested_at IS NOT NULL THEN now()
                        ELSE NULL
                    END,
                    finished_at = now(),
                    lease_owner = NULL,
                    lease_until = NULL,
                    heartbeat_at = now(),
                    progress = jsonb_build_object(
                        'phase',
                        CASE
                            WHEN %s OR cancel_requested_at IS NOT NULL
                                THEN 'cancelled'
                            ELSE 'failed'
                        END
                    ),
                    message = CASE
                        WHEN %s OR cancel_requested_at IS NOT NULL
                            THEN 'Preview cancelled.'
                        ELSE %s
                    END,
                    error_summary = CASE
                        WHEN %s OR cancel_requested_at IS NOT NULL THEN NULL
                        ELSE %s
                    END,
                    updated_at = now()
                WHERE id::text = %s
                  AND status = 'running'
                  AND lease_owner = %s
                RETURNING policy_id, company_id, status
                """,
                (
                    cancelled,
                    cancelled,
                    cancelled,
                    cancelled,
                    detail,
                    cancelled,
                    detail,
                    run_id,
                    bounded_owner,
                ),
            )
            failed = cursor.fetchone()
            if not failed:
                return None
            policy_id, company_uuid, terminal_status = failed
            if policy_id:
                trigger = _ci_preview_schedule_trigger({"attributes": owned[3] or {}})
                if terminal_status == "failed" and trigger in {"continuous_preview", "manual_sync"}:
                    cursor.execute(
                        """
                        UPDATE integration_ci_policies
                        SET last_run_at = now(),
                            last_error = %s,
                            consecutive_failures = consecutive_failures + 1,
                            next_run_at = CASE
                                WHEN enabled AND sync_mode = 'continuous_preview'
                                    THEN now() + (
                                        LEAST(
                                            1440,
                                            15 * power(2, LEAST(consecutive_failures, 7))
                                        ) * interval '1 minute'
                                    )
                                ELSE NULL
                            END,
                            lease_owner = NULL,
                            lease_until = NULL
                        WHERE id = %s::uuid
                          AND lease_owner = %s
                        """,
                        (detail, str(policy_id), policy_owner),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE integration_ci_policies
                        SET lease_owner = NULL, lease_until = NULL
                        WHERE id = %s::uuid
                          AND lease_owner = %s
                        """,
                        (str(policy_id), policy_owner),
                    )
                if cursor.rowcount != 1:
                    raise RuntimeError("Preview policy lease is no longer owned")
            company_id = None
            if company_uuid:
                cursor.execute(
                    "SELECT slug FROM companies WHERE id = %s::uuid",
                    (str(company_uuid),),
                )
                company_row = cursor.fetchone()
                company_id = company_row[0] if company_row else None
            self._insert_audit(
                cursor,
                company_id,
                actor_id,
                "sync_run",
                run_id,
                "cancelled" if terminal_status == "cancelled" else "failed",
                None,
                {"status": terminal_status},
                outcome="success" if terminal_status == "cancelled" else "failed",
                severity="informational" if terminal_status == "cancelled" else "warning",
                reason="" if terminal_status == "cancelled" else detail,
                actor_type="system" if actor_id is None else "user",
                source_system="integration_worker" if actor_id is None else "web",
            )
            run = self._select_ci_preview_run(cursor, run_id)
        return _public_sync_run(run) if run else None

    def retry_ci_preview_run(self, run_id: str, actor_id: str | None) -> dict:
        """Create a new immutable queued run linked to a retryable terminal run."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            source = self._select_ci_preview_run(cursor, run_id)
        if not source:
            raise ValueError("Sync run not found")
        if source.get("status") not in RETRYABLE_CI_PREVIEW_RUN_STATUSES:
            raise ValueError("Only failed or cancelled preview runs can be retried")
        attributes = source.get("attributes") or {}
        schedule_trigger = _ci_preview_schedule_trigger(source)
        return self.create_ci_preview_run(
            str(source.get("type") or ""),
            str(source.get("companyId") or attributes.get("companyId") or ""),
            str(source.get("providerCompanyId") or attributes.get("providerCompanyId") or ""),
            str(source.get("policyId") or attributes.get("policyId") or ""),
            "retry",
            actor_id,
            deepcopy(attributes.get("policySnapshot") or {}),
            retry_of_id=source["id"],
            schedule_trigger=schedule_trigger,
        )

    def release_ci_sync_policy_lease(
        self,
        policy_id: str,
        lease_owner: str,
    ) -> dict | None:
        """Release one owned canonical policy lease without altering its schedule."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies policy
                SET lease_owner = NULL, lease_until = NULL
                FROM integration_connections integration, companies company
                WHERE policy.id::text = %s
                  AND integration.id = policy.integration_connection_id
                  AND company.id = policy.company_id
                  AND policy.lease_owner = %s
                RETURNING integration.provider, company.slug,
                          policy.external_parent_id
                """,
                (policy_id, str(lease_owner)[:120]),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self.get_ci_sync_policy(
            PROVIDER_FROM_DB.get(row[0], row[0]),
            row[1],
            row[2],
        )

    def list_ci_review_items_for_run(
        self,
        kind: str,
        run_id: str,
        company_id: str,
    ) -> list[dict]:
        """Return reviewable observations produced by one canonical preview run."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item.id, integration.provider, policy.id, company.slug, company.name,
                       policy.external_parent_id, item.external_id, item.external_name,
                       item.decision, item.candidate_ci_id, ci.display_name, item.reason,
                       item.provider_record, item.evidence, item.state,
                       item.first_seen_at, item.last_seen_at, item.reviewed_at,
                       item.review_notes
                FROM integration_ci_review_items item
                JOIN integration_ci_policies policy ON policy.id = item.policy_id
                JOIN integration_connections integration
                  ON integration.id = policy.integration_connection_id
                JOIN companies company ON company.id = item.company_id
                LEFT JOIN configuration_items ci ON ci.id = item.candidate_ci_id
                WHERE integration.provider = %s
                  AND company.slug = %s
                  AND item.last_sync_run_id::text = %s
                ORDER BY item.last_seen_at DESC, item.external_name
                """,
                (PROVIDER_TO_DB.get(kind, kind), company_id, run_id),
            )
            rows = cursor.fetchall()
        return [self._ci_review_from_row(row) for row in rows]

    def list_sync_runs(
        self,
        kind: str | None = None,
        status: str | None = None,
        operation: str | None = None,
        company_id: str | None = None,
        limit: int = 50,
        *,
        company_ids: Iterable[str] | None = None,
    ) -> list[dict]:
        """Return recent sync evidence using indexed, bounded filters."""

        provider = PROVIDER_TO_DB.get(kind, kind) if kind else None
        database_status = "succeeded" if status == "success" else status
        restrict_companies = company_ids is not None
        permitted_companies = sorted(set(company_ids or []))
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sr.id, ic.provider, sr.status, sr.message, sr.started_at,
                       sr.finished_at, sr.discovered_count, sr.created_count,
                       sr.updated_count, sr.review_count, sr.attributes,
                       company.slug, sr.policy_id, sr.requested_by, sr.requested_at,
                       sr.available_at, sr.attempt_count, sr.max_attempts,
                       sr.lease_owner, sr.lease_until, sr.heartbeat_at,
                       sr.cancel_requested_at, sr.cancelled_at, sr.retry_of_id,
                       sr.dedupe_key, sr.progress, sr.updated_at, sr.error_summary
                FROM sync_runs sr
                JOIN integration_connections ic ON ic.id = sr.integration_connection_id
                LEFT JOIN companies company ON company.id = sr.company_id
                WHERE (%s::text IS NULL OR ic.provider = %s)
                  AND (%s::text IS NULL OR sr.status = %s)
                  AND (%s::text IS NULL OR sr.attributes ->> 'operation' = %s)
                  AND (%s::text IS NULL
                       OR COALESCE(company.slug, sr.attributes ->> 'companyId') = %s)
                  AND (
                      %s = false
                      OR COALESCE(company.slug, sr.attributes ->> 'companyId') IS NULL
                      OR COALESCE(company.slug, sr.attributes ->> 'companyId')
                          = ANY(%s::text[])
                  )
                ORDER BY sr.requested_at DESC, sr.id DESC
                LIMIT %s
                """,
                (
                    provider,
                    provider,
                    database_status,
                    database_status,
                    operation,
                    operation,
                    company_id,
                    company_id,
                    restrict_companies,
                    permitted_companies,
                    max(1, min(limit, 250)),
                ),
            )
            return [
                _public_sync_run(self._ci_preview_run_from_row(row)) for row in cursor.fetchall()
            ]

    def record_sync_run(
        self,
        kind: str,
        run: dict,
        configured: bool,
        actor_id: str | None = None,
    ) -> dict:
        status_to_db = {
            "success": "succeeded",
            "queued": "queued",
            "running": "running",
            "review_required": "review_required",
            "blocked": "blocked",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        run_status = run.get("status")
        database_status = (
            status_to_db.get(run_status, "review_required")
            if isinstance(run_status, str)
            else "review_required"
        )
        run_uuid = canonical_uuid("sync_run", run["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, company_id FROM integration_connections WHERE provider = %s AND company_id IS NULL ORDER BY created_at LIMIT 1",
                (PROVIDER_TO_DB.get(kind, kind),),
            )
            connection_row = cursor.fetchone()
            if not connection_row:
                raise ValueError("Integration connection not found")
            integration_uuid, company_uuid = str(connection_row[0]), connection_row[1]
            cursor.execute(
                """
                INSERT INTO sync_runs (
                    id, integration_connection_id, status, started_at, finished_at,
                    discovered_count, created_count, updated_count, review_count,
                    error_summary, message, attributes
                ) VALUES (
                    %s::uuid, %s::uuid, %s, COALESCE(%s::timestamptz, now()),
                    %s::timestamptz, %s, %s, %s, %s, %s, %s, %s::jsonb
                )
                """,
                (
                    run_uuid,
                    integration_uuid,
                    database_status,
                    run.get("startedAt") or None,
                    run.get("finishedAt") or None,
                    int(run.get("discovered") or 0),
                    int(run.get("imported") or 0),
                    int(run.get("updated") or 0),
                    int(run.get("review") or 0),
                    run.get("message") if database_status == "failed" else None,
                    run.get("message") or "",
                    json.dumps(
                        {
                            "apiStatus": run.get("status"),
                            **deepcopy(run.get("attributes") or {}),
                        }
                    ),
                ),
            )
            company_slug = None
            if company_uuid:
                cursor.execute(
                    "SELECT slug FROM companies WHERE id = %s::uuid",
                    (str(company_uuid),),
                )
                company_slug = cursor.fetchone()[0]
            stored = {**deepcopy(run), "id": run_uuid}
            self._insert_audit(
                cursor,
                company_slug,
                actor_id,
                "integration_connection",
                integration_uuid,
                "sync_completed",
                None,
                stored,
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return next(item for item in self.list_sync_runs() if item["id"] == run_uuid)

    @staticmethod
    def _asset_attributes(asset: dict) -> dict:
        return {
            "legacyId": asset.get("id"),
            "status": asset.get("status", "Active"),
            "source": asset.get("source", "manual"),
            "externalId": asset.get("externalId"),
            "lastSeen": asset.get("lastSeen"),
            "fields": asset.get("fields", {}),
            "metadata": asset.get("metadata", {}),
        }

    @staticmethod
    def _asset_from_row(row: tuple) -> dict:
        (
            item_id,
            company_slug,
            name,
            ci_type,
            lifecycle,
            operational,
            attributes,
            updated_at,
        ) = row
        attributes = attributes or {}
        metadata = {
            **(attributes.get("metadata") or {}),
            "lifecycle": lifecycle,
            "operationalStatus": operational,
        }
        return {
            "id": str(item_id),
            "companyId": company_slug,
            "name": name,
            "type": ci_type,
            "status": attributes.get("status", "Active"),
            "source": attributes.get("source", "manual"),
            "externalId": attributes.get("externalId"),
            "lastSeen": attributes.get("lastSeen"),
            "fields": attributes.get("fields") or {},
            "metadata": metadata,
            "updatedAt": updated_at.isoformat().replace("+00:00", "Z") if updated_at else None,
        }

    def _refresh_state_mirror(self) -> None:
        self.state["companies"] = self.list_companies()
        self.state["users"] = self.list_users(include_credentials=True)
        self.state["contacts"] = self.list_contacts()
        self.state["contactResponsibilities"] = self.list_contact_responsibilities(
            include_inactive=True
        )
        self.state["accessGroups"] = self.list_access_groups()
        self.state["assets"] = self.list_assets()
        self.state["relationships"] = self.list_relationships()
        self.state["changes"] = self.list_changes()
        self.state["changeTemplates"] = self.list_change_templates()
        self.state["integrations"] = self.list_integrations()
        self.state["syncRuns"] = self.list_sync_runs()
        self.state["branding"] = self.list_company_branding()
        self.state["mspBranding"] = self.get_msp_branding()
        self.state.pop("apiTokens", None)

    def _rewrite_change_ids(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        for change in self.state.get("changes", []):
            if "scopeAssetIds" in change:
                change["scopeAssetIds"] = [
                    mapping.get(item, item) for item in change["scopeAssetIds"]
                ]
            for impact in change.get("impactSnapshot", []):
                impact["assetId"] = mapping.get(impact.get("assetId"), impact.get("assetId"))
                impact["pathAssetIds"] = [
                    mapping.get(item, item) for item in impact.get("pathAssetIds", [])
                ]

    def list_audit_events(
        self,
        company_id: str | None = None,
        *,
        actor_id: str | None = None,
        category: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
        limit: int = 250,
    ) -> list[dict]:
        clauses = ["(%s::text IS NULL OR c.slug = %s::text)"]
        parameters: list[Any] = [company_id, company_id]
        optional = [
            (actor_id, "ae.actor_user_id = %s::uuid"),
            (category, "ae.event_category = %s"),
            (action, "ae.action = %s"),
            (entity_type, "ae.entity_type = %s"),
            (entity_id, "ae.entity_id = %s::uuid"),
            (outcome, "ae.outcome = %s"),
        ]
        for value, clause in optional:
            if value:
                try:
                    parameters.append(
                        str(uuid.UUID(value))
                    ) if "::uuid" in clause else parameters.append(value)
                except (ValueError, TypeError):
                    return []
                clauses.append(clause)
        if search:
            clauses.append(
                "concat_ws(' ', ae.actor_label, ae.entity_name, ae.entity_type, ae.action, ae.correlation_id) ILIKE %s"
            )
            parameters.append(f"%{search[:200]}%")
        parameters.append(max(1, min(limit, 1000)))
        with self.connection_factory() as connection, connection.cursor() as cursor:
            where_clause = sql.SQL(" AND ").join(sql.SQL(clause) for clause in clauses)
            query = sql.SQL(
                """
                SELECT ae.id, c.slug, ae.actor_user_id, ae.actor_label, ae.actor_type,
                       ae.source_system, ae.event_category, ae.entity_type, ae.entity_id,
                       ae.entity_name, ae.action, ae.outcome, ae.severity, ae.request_id,
                       ae.correlation_id, ae.before_value, ae.after_value, ae.changes,
                       ae.reason, ae.metadata, ae.created_at
                FROM audit_events ae
                LEFT JOIN companies c ON c.id = ae.company_id
                WHERE {where_clause}
                ORDER BY ae.created_at DESC, ae.id DESC
                LIMIT %s
                """
            ).format(where_clause=where_clause)
            cursor.execute(
                query,
                tuple(parameters),
            )
            return [
                {
                    "id": str(row[0]),
                    "companyId": row[1],
                    "actorUserId": str(row[2]) if row[2] else None,
                    "actorLabel": row[3] or "System",
                    "actorType": row[4],
                    "sourceSystem": row[5],
                    "category": row[6],
                    "entityType": row[7],
                    "entityId": str(row[8]) if row[8] else None,
                    "entityName": row[9] or "",
                    "action": row[10],
                    "outcome": row[11],
                    "severity": row[12],
                    "requestId": row[13] or "",
                    "correlationId": row[14] or "",
                    "before": row[15],
                    "after": row[16],
                    "changes": row[17] or [],
                    "reason": row[18] or "",
                    "metadata": row[19] or {},
                    "createdAt": self._timestamp(row[20]),
                }
                for row in cursor.fetchall()
            ]

    def record_audit_event(
        self,
        company_id: str | None,
        actor_id: str | None,
        entity_type: str,
        entity_id: str,
        action: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        outcome: str = "success",
        severity: str = "informational",
        reason: str = "",
        metadata: dict | None = None,
        actor_type: str = "user",
        source_system: str | None = None,
    ) -> dict:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            event_id = self._insert_audit(
                cursor,
                company_id,
                actor_id,
                entity_type,
                entity_id,
                action,
                before,
                after,
                outcome=outcome,
                severity=severity,
                reason=reason,
                metadata=metadata,
                actor_type=actor_type,
                source_system=source_system,
            )
        records = self.list_audit_events(entity_id=canonical_uuid(entity_type, entity_id), limit=10)
        return next(
            (item for item in records if item["id"] == event_id),
            records[0] if records else {},
        )

    def _insert_audit(
        self,
        cursor: Any,
        company_slug: str | None,
        actor_id: str | None,
        entity_type: str,
        entity_id: str,
        action: str,
        before: dict | None,
        after: dict | None,
        *,
        outcome: str = "success",
        severity: str = "informational",
        reason: str = "",
        metadata: dict | None = None,
        actor_type: str = "user",
        source_system: str | None = None,
    ) -> str:
        company_uuid = None
        if company_slug:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_slug,))
            row = cursor.fetchone()
            company_uuid = str(row[0]) if row else None
        actor_uuid = None
        actor_label = "System"
        if actor_id:
            try:
                actor_uuid = str(uuid.UUID(actor_id))
            except (ValueError, TypeError):
                cursor.execute(
                    "SELECT id FROM users WHERE attributes->>'legacyId' = %s LIMIT 1",
                    (actor_id,),
                )
                actor_row = cursor.fetchone()
                actor_uuid = str(actor_row[0]) if actor_row else None
            if actor_uuid:
                cursor.execute("SELECT email::text FROM users WHERE id = %s::uuid", (actor_uuid,))
                actor_row = cursor.fetchone()
                actor_label = actor_row[0] if actor_row else actor_id
        context = current_audit_context()
        safe_before = sanitize_audit_value(deepcopy(before)) if before is not None else None
        safe_after = sanitize_audit_value(deepcopy(after)) if after is not None else None
        event_id = str(uuid.uuid4())
        entity_uuid = canonical_uuid(entity_type, entity_id)
        safe_metadata = sanitize_audit_value(
            {
                **(metadata or {}),
                "clientAddress": context.client_address,
                "userAgent": context.user_agent,
            }
        )
        cursor.execute(
            """
            INSERT INTO audit_events (
                id, company_id, actor_user_id, actor_type, actor_label, source_system,
                event_category, entity_type, entity_id, entity_name, action, outcome,
                severity, request_id, correlation_id, before_value, after_value,
                changes, reason, metadata
            ) VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s::uuid, %s,
                %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb
            )
            """,
            (
                event_id,
                company_uuid,
                actor_uuid,
                actor_type if actor_uuid or actor_type != "user" else "system",
                actor_label,
                source_system or context.source_system,
                event_category(entity_type, action),
                entity_type,
                entity_uuid,
                entity_name(before, after, entity_id),
                action,
                outcome,
                severity,
                context.request_id or None,
                context.correlation_id or context.request_id or None,
                json.dumps(safe_before) if safe_before is not None else None,
                json.dumps(safe_after) if safe_after is not None else None,
                json.dumps(field_changes(before, after)),
                reason[:1000] or None,
                json.dumps(safe_metadata),
            ),
        )
        return event_id
