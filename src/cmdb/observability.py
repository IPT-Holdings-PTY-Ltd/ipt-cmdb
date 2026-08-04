"""Structured, redacted runtime logging for web and worker processes."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cmdb.audit import current_audit_context
from src.cmdb.sensitive import is_sensitive_field_name

_MANAGED_HANDLER_ATTRIBUTE = "_cmdb_observability_handler"
_MAX_LOG_TEXT = 16_384
_ALLOWED_EXTRA_FIELDS = (
    "method",
    "route",
    "status_code",
    "duration_ms",
    "error_type",
    "message_id",
    "provider",
    "operation",
    "migration_count",
    "schema_version",
    "policy_id",
    "run_id",
    "worker_name",
    "worker_id",
    "processed",
)
_URI_USERINFO = re.compile(r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<user>[^/\s:@]*):[^@\s/]+@")
_AUTHORIZATION_VALUE = re.compile(r"(?i)\b(?P<scheme>Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_JWT_VALUE = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_FIELD_ASSIGNMENT = re.compile(
    r"""(?x)
    (?<![\w.-])
    (?P<quote>["']?)
    (?P<key>[\w.-]{1,80})
    (?P=quote)
    \s*[:=]\s*
    (?:
        "(?:\\.|[^"])*" |
        '(?:\\.|[^'])*' |
        [^\s,;]+
    )
    """
)


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    """Redact one assignment when its normalized field name is sensitive."""

    if not is_sensitive_field_name(match.group("key")):
        return match.group(0)
    quote = match.group("quote")
    return f"{quote}{match.group('key')}{quote}=[REDACTED]"


def _configured_level() -> int:
    """Return a safe logging level for an operator-supplied name."""

    configured = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, configured, None)
    return level if isinstance(level, int) else logging.INFO


def _process_role() -> str:
    """Return a stable process role without importing worker runtime logging."""

    configured = os.getenv("CMDB_PROCESS_ROLE", "combined").strip().casefold()
    return configured if configured in {"combined", "web", "worker"} else "combined"


def sanitize_log_text(value: Any) -> str:
    """Redact common credential forms and bound a value before logging it."""

    text = str(value)
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)
    text = _URI_USERINFO.sub(
        lambda match: f"{match.group('scheme')}://{match.group('user')}:[REDACTED]@",
        text,
    )
    text = _AUTHORIZATION_VALUE.sub(
        lambda match: f"{match.group('scheme')} [REDACTED]",
        text,
    )
    text = _JWT_VALUE.sub("[REDACTED JWT]", text)
    text = _FIELD_ASSIGNMENT.sub(_redact_sensitive_assignment, text)
    if len(text) > _MAX_LOG_TEXT:
        return f"{text[:_MAX_LOG_TEXT]}… [truncated]"
    return text


def _safe_extra(record: logging.LogRecord, name: str) -> Any:
    """Return one allowlisted primitive extra value, if present."""

    value = getattr(record, name, None)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_log_text(value)


def _safe_traceback(record: logging.LogRecord) -> list[dict[str, Any]]:
    """Return stack locations without serializing exception messages or source lines."""

    if not record.exc_info or not record.exc_info[2]:
        return []
    frames = traceback.extract_tb(record.exc_info[2])[-20:]
    return [
        {
            "file": Path(frame.filename).name,
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in frames
    ]


def _record_payload(record: logging.LogRecord) -> dict[str, Any]:
    """Build the shared, allowlisted representation of one log record."""

    context = current_audit_context()
    message = sanitize_log_text(record.getMessage())
    event = sanitize_log_text(getattr(record, "event", "") or message)
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": record.levelname,
        "logger": record.name,
        "event": event,
        "message": message,
        "service": "ipt-cmdb",
        "process_role": _process_role(),
    }
    if context.request_id:
        payload["request_id"] = context.request_id
    if context.correlation_id:
        payload["correlation_id"] = context.correlation_id
    for name in _ALLOWED_EXTRA_FIELDS:
        value = _safe_extra(record, name)
        if value not in (None, ""):
            payload[name] = value
    if record.exc_info:
        exception_type = record.exc_info[0]
        payload["exception_type"] = (
            exception_type.__name__ if exception_type is not None else "Exception"
        )
        safe_traceback = _safe_traceback(record)
        if safe_traceback:
            payload["traceback"] = safe_traceback
    return payload


class JsonLogFormatter(logging.Formatter):
    """Render an allowlisted log record as compact single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` without arbitrary extras or credential-bearing values."""

        return json.dumps(_record_payload(record), separators=(",", ":"), ensure_ascii=False)


class TextLogFormatter(logging.Formatter):
    """Render the same safe fields in an operator-friendly local format."""

    def format(self, record: logging.LogRecord) -> str:
        """Format ``record`` as bounded text while retaining correlation IDs."""

        payload = _record_payload(record)
        fields = [
            payload["timestamp"],
            payload["level"],
            payload["logger"],
            payload["message"],
        ]
        for name in ("request_id", "correlation_id", *_ALLOWED_EXTRA_FIELDS):
            if name in payload:
                fields.append(f"{name}={payload[name]}")
        if "exception_type" in payload:
            fields.append(f"exception_type={payload['exception_type']}")
        return " ".join(str(item) for item in fields)


def configure_logging() -> None:
    """Configure CMDB loggers once while allowing runtime level/format updates."""

    logger = logging.getLogger("cmdb")
    logger.setLevel(_configured_level())
    logger.propagate = False
    handler = next(
        (item for item in logger.handlers if getattr(item, _MANAGED_HANDLER_ATTRIBUTE, False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _MANAGED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(handler)
    handler.setLevel(_configured_level())
    log_format = os.getenv("LOG_FORMAT", "json").strip().casefold()
    handler.setFormatter(TextLogFormatter() if log_format == "text" else JsonLogFormatter())
