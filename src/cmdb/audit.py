"""Request-scoped audit helpers shared by the API and repositories."""

from __future__ import annotations

from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

SENSITIVE_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "private_key",
    "connectionstring",
    "connection_string",
    "databaseurl",
    "database_url",
    "logodataurl",
    "logo_data_url",
)


@dataclass(frozen=True)
class AuditContext:
    """Carry request provenance into repository-generated audit events."""

    request_id: str = ""
    correlation_id: str = ""
    source_system: str = "web"
    client_address: str = ""
    user_agent: str = ""


_context: ContextVar[AuditContext | None] = ContextVar("cmdb_audit_context", default=None)


def set_audit_context(context: AuditContext) -> Token[AuditContext | None]:
    """Activate request audit metadata and return its reset token."""

    return _context.set(context)


def reset_audit_context(token: Token[AuditContext | None]) -> None:
    """Restore the audit context that preceded ``token``."""

    _context.reset(token)


def current_audit_context() -> AuditContext:
    """Return current request metadata or an empty background-job context."""

    return _context.get() or AuditContext()


def _sensitive(key: str) -> bool:
    normalized = key.casefold().replace("-", "").replace(" ", "")
    return any(part.replace("_", "") in normalized for part in SENSITIVE_PARTS)


def sanitize_audit_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """Redact credentials and bound audit payload size without mutating input."""
    if _sensitive(key):
        return "[redacted]"
    if depth > 8:
        return "[maximum depth reached]"
    if isinstance(value, dict):
        return {
            str(child_key)[:120]: sanitize_audit_value(child_value, str(child_key), depth + 1)
            for child_key, child_value in list(value.items())[:250]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_audit_value(item, key, depth + 1) for item in list(value)[:250]]
    if isinstance(value, str):
        return value if len(value) <= 4000 else f"{value[:4000]}… [truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4000]


def field_changes(before: dict | None, after: dict | None) -> list[dict]:
    """Create a stable, readable top-level diff for the audit UI and reports."""
    before_value = sanitize_audit_value(deepcopy(before or {}))
    after_value = sanitize_audit_value(deepcopy(after or {}))
    changes: list[dict] = []
    for field in sorted(set(before_value) | set(after_value)):
        old = before_value.get(field)
        new = after_value.get(field)
        if old != new:
            changes.append({"field": field, "before": old, "after": new})
    return changes[:250]


def event_category(entity_type: str, action: str) -> str:
    """Map an audited entity and action to a stable governance category."""

    if entity_type in {"authentication", "session"}:
        return "authentication"
    if entity_type in {"user", "access_group", "role", "permission"}:
        return "access"
    if entity_type in {"database", "portable_export", "recovery"}:
        return "recovery"
    if entity_type in {"integration_connection", "sync_run"} or action.startswith("sync_"):
        return "integration"
    if entity_type in {"report", "report_run"}:
        return "report"
    if "branding" in entity_type or entity_type == "company":
        return "configuration"
    return "data"


def entity_name(before: dict | None, after: dict | None, fallback: str = "") -> str:
    """Derive a human-readable entity label from an audit snapshot."""

    value = after or before or {}
    for key in ("name", "displayName", "display_name", "title", "number", "email"):
        if value.get(key):
            return str(value[key])[:240]
    return fallback[:240]
