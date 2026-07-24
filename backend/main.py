"""FastAPI application for the CMDB Hub web platform.

FastAPI owns the complete public API surface. PostgreSQL is the sole
operational source of truth whenever a database is configured; the local state
repository exists only for setup, development and isolated unit tests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.staticfiles import StaticFiles

import app as core
from src.cmdb.audit import AuditContext, reset_audit_context, set_audit_context
from src.cmdb.change_control import (
    change_pdf_filename,
    create_change_record,
    derive_change_approvers,
    preview_change_impact,
    record_external_approval,
    render_change_pdf,
    transition_change_record,
    update_change_record,
)
from src.cmdb.connectwise import (
    ConnectWiseClient,
    ConnectWiseConfigurationError,
    ConnectWiseRequestError,
    normalize_base_url,
)
from src.cmdb.data_quality import RULES as DATA_QUALITY_RULES
from src.cmdb.data_quality import evaluate_data_quality
from src.cmdb.email_delivery import (
    EmailConfigurationError,
    EmailDeliveryError,
    GraphEmailSender,
    exchange_rbac_script,
    public_email_connection,
    valid_email_address,
)
from src.cmdb.field_authority import authority_catalogue, preset_rules
from src.cmdb.integration_reconciliation import (
    apply_ci_policy,
    apply_ci_type_mappings,
    configuration_catalogue,
    normalize_ci_policy,
    reconcile_configuration_items,
)
from src.cmdb.integrations import provider_registry
from src.cmdb.integrations.providers.connectwise import (
    ConnectWiseProvider,
    normalized_policy,
)
from src.cmdb.mfa import (
    MfaConfigurationError,
    decrypt_secret,
    encrypt_secret,
    encryption_key,
    new_totp_secret,
    opaque_token_hash,
    provisioning_uri,
    qr_data_uri,
    recovery_codes,
    verify_totp,
)
from src.cmdb.notifications import (
    notification_candidates,
    notification_dedupe_key,
    render_notification_template,
    resolve_notification_recipients,
)
from src.cmdb.reports import (
    build_report,
    render_csv,
    render_pdf,
    render_xlsx,
    report_catalog,
    report_filename,
)
from src.cmdb.repository import (
    PostgresCmdbRepository,
    StateRepository,
    canonical_uuid,
    hash_password,
    integration_connection_audit_value,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", ROOT / "frontend" / "dist"))
LOGGER = logging.getLogger("cmdb.api")
EMAIL_SENDER = GraphEmailSender()


def _notification_worker_interval() -> int:
    """Return a bounded worker interval even when deployment input is invalid."""

    try:
        configured = int(os.getenv("NOTIFICATION_WORKER_INTERVAL_SECONDS", "60"))
    except ValueError:
        LOGGER.warning("Invalid NOTIFICATION_WORKER_INTERVAL_SECONDS; using 60 seconds")
        configured = 60
    return max(15, min(configured, 3600))


def _integration_worker_interval() -> int:
    """Return the bounded polling interval for due integration policies."""

    try:
        configured = int(os.getenv("INTEGRATION_WORKER_INTERVAL_SECONDS", "60"))
    except ValueError:
        LOGGER.warning("Invalid INTEGRATION_WORKER_INTERVAL_SECONDS; using 60 seconds")
        configured = 60
    return max(15, min(configured, 3600))


async def _notification_worker_loop() -> None:
    """Periodically evaluate rules and drain the durable email outbox."""

    interval = _notification_worker_interval()
    while True:
        try:
            await asyncio.to_thread(_run_notification_scan)
            await asyncio.to_thread(_process_notification_outbox)
        except Exception:
            LOGGER.exception("Notification worker cycle failed")
        await asyncio.sleep(interval)


async def _integration_worker_loop() -> None:
    """Lease and run due read-only integration discovery policies."""

    interval = _integration_worker_interval()
    while True:
        try:
            await asyncio.to_thread(_run_due_integration_previews)
        except Exception:
            LOGGER.exception("Integration preview worker cycle failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def application_lifespan(_application: FastAPI):
    """Run optional durable workers and stop them cleanly on shutdown."""

    tasks: list[asyncio.Task[None]] = []
    if os.getenv("NOTIFICATION_WORKER_ENABLED", "false").lower() in {"1", "true", "yes"}:
        tasks.append(asyncio.create_task(_notification_worker_loop()))
    if os.getenv("INTEGRATION_WORKER_ENABLED", "false").lower() in {"1", "true", "yes"}:
        tasks.append(asyncio.create_task(_integration_worker_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            # Gathering cancelled tasks lets their cleanup handlers finish.
            shutdown_results = await asyncio.gather(*tasks, return_exceptions=True)
            for shutdown_result in shutdown_results:
                if isinstance(shutdown_result, BaseException) and not isinstance(
                    shutdown_result, asyncio.CancelledError
                ):
                    raise shutdown_result


api = FastAPI(
    title="CMDB Hub API",
    version="0.4.0",
    description="Tenant-aware CMDB API served directly by FastAPI.",
    lifespan=application_lifespan,
)


@api.middleware("http")
async def audit_and_correlation_context(request: Request, call_next):
    """Correlate diagnostics and audit evidence without logging credentials."""
    requested_id = request.headers.get("x-request-id", "").strip()
    request_id = (
        requested_id[:100]
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", requested_id)
        else str(uuid.uuid4())
    )
    correlation = request.headers.get("x-correlation-id", "").strip()[:100] or request_id
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    client_address = forwarded or (request.client.host if request.client else "")
    request.state.client_address = client_address[:120]
    token = set_audit_context(
        AuditContext(
            request_id=request_id,
            correlation_id=correlation,
            source_system=request.headers.get("x-cmdb-source", "web")[:80] or "web",
            client_address=client_address[:120],
            user_agent=request.headers.get("user-agent", "")[:500],
        )
    )
    started = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = correlation
        response.headers["Referrer-Policy"] = "no-referrer"
        if (
            request.url.path.startswith("/api/")
            and response.status_code in {401, 403}
            and request.url.path != "/api/login"
        ):
            actor = getattr(request.state, "current_user", None)
            try:
                REPOSITORY.record_audit_event(
                    None,
                    actor.get("id") if actor else None,
                    "authorization",
                    canonical_uuid("authorization", f"{request.method}:{request.url.path}"),
                    "access_denied",
                    outcome="denied",
                    severity="warning",
                    metadata={
                        "method": request.method,
                        "path": request.url.path,
                        "statusCode": response.status_code,
                    },
                )
            except Exception:
                LOGGER.exception("audit_denial_record_failed")
        LOGGER.info(
            "request_complete",
            extra={
                "request_id": request_id,
                "correlation_id": correlation,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response
    finally:
        reset_audit_context(token)


@api.middleware("http")
async def require_operational_database(request: Request, call_next):
    """Expose only setup/auth endpoints until canonical PostgreSQL is active."""
    if core.DATABASE_MODE != "database setup" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    allowed = {
        "/api/live",
        "/api/ready",
        "/api/health",
        "/api/v2/health",
        "/api/auth/config",
        "/api/login",
        "/api/logout",
        "/api/me",
        "/api/branding/public",
        "/api/database/status",
        "/api/database/test",
        "/api/database/config",
        "/api/companies",
    }
    if request.url.path in allowed:
        return await call_next(request)
    return JSONResponse(
        {"detail": "Configure PostgreSQL before using operational CMDB features"},
        status_code=503,
    )


def _build_repository() -> StateRepository:
    local = StateRepository(core.DB, core.save_db)
    if core.DATABASE_MODE != "PostgreSQL" or not core.DATABASE_URL:
        return local
    core.apply_postgres_schema(core.DATABASE_URL)
    repository = PostgresCmdbRepository(core.DB, core.save_db, core.postgres_connection)
    if not repository.is_initialized():
        repository.bootstrap()
        core.CANONICAL_DATABASE_INITIALIZED = True
    else:
        repository.migrate_legacy_company_branding()
        core.DB.clear()
        core.DB.update(repository.export_state())
    return repository


REPOSITORY = _build_repository()


def _easy_auth_email(request: Request) -> str | None:
    if os.getenv("AUTH_MODE", "local").lower() != "easy_auth":
        return None
    principal_name_header = os.getenv(
        "ENTRA_PRINCIPAL_NAME_HEADER", "x-ms-client-principal-name"
    ).lower()
    principal_name = request.headers.get(principal_name_header)
    if principal_name:
        return principal_name.strip().lower()
    principal_header = os.getenv("ENTRA_PRINCIPAL_HEADER", "x-ms-client-principal").lower()
    encoded = request.headers.get(principal_header)
    if not encoded:
        return None
    try:
        principal = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    claims = {
        claim.get("typ", "").lower(): claim.get("val") for claim in principal.get("claims", [])
    }
    claim_names = [
        item.strip().lower()
        for item in os.getenv(
            "ENTRA_EMAIL_CLAIMS",
            "preferred_username,http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress,emails",
        ).split(",")
        if item.strip()
    ]
    for name in claim_names:
        value = claims.get(name)
        if value:
            return str(value).strip().lower()
    return None


def _local_login_enabled() -> bool:
    return (
        os.getenv("AUTH_MODE", "local").lower() == "local"
        or os.getenv("ALLOW_LOCAL_BREAK_GLASS", "false").lower() == "true"
    )


API_TOKEN_ROUTE_PREFIXES = (
    "/api/assets",
    "/api/relationships",
    "/api/contacts",
    "/api/contact-responsibilities",
    "/api/changes",
    "/api/data-quality",
    "/api/reports",
    "/api/audit-events",
    "/api/dashboard",
    "/api/companies",
)
API_TOKEN_READ_ONLY_PREFIXES = ("/api/dashboard", "/api/companies", "/api/audit-events")


def _api_token_user(bearer: str, request: Request) -> dict | None:
    if not bearer.startswith("cmdb_pat_"):
        return None
    authenticated = REPOSITORY.authenticate_api_token(
        hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    )
    if not authenticated:
        raise HTTPException(401, "API token is invalid, expired or revoked")
    path = request.url.path
    if not any(path.startswith(prefix) for prefix in API_TOKEN_ROUTE_PREFIXES):
        raise HTTPException(403, "API tokens cannot access administrative endpoints")
    if request.method not in {"GET", "HEAD"} and any(
        path.startswith(prefix) for prefix in API_TOKEN_READ_ONLY_PREFIXES
    ):
        raise HTTPException(403, "This API resource is read-only for personal tokens")
    token = authenticated["token"]
    required_scope = "cmdb:read" if request.method in {"GET", "HEAD"} else "cmdb:write"
    if required_scope not in token["scopes"]:
        raise HTTPException(403, f"API token requires {required_scope} scope")
    user = {
        **authenticated["user"],
        "authType": "api_token",
        "apiTokenId": token["id"],
        "apiTokenCompanyIds": token.get("companyIds", []),
    }
    request.state.api_token = token
    request.state.current_user = user
    return user


def current_user(request: Request) -> dict:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    api_user = _api_token_user(bearer, request)
    if api_user:
        return api_user

    users = REPOSITORY.list_users()
    if os.getenv("AUTH_MODE", "local").lower() == "easy_auth":
        email = _easy_auth_email(request)
        if email:
            user = next((item for item in users if item["email"].lower() == email.lower()), None)
            if not user:
                raise HTTPException(403, "Your Entra identity has not been assigned CMDB access")
            request.state.current_user = user
            return user
        if not _local_login_enabled():
            raise HTTPException(401, "Microsoft sign-in is required")

    user_id = REPOSITORY.authenticate_session(opaque_token_hash(bearer)) if bearer else None
    if not user_id:
        session = core.SESSIONS.get(bearer)
        if session and session["expiresAt"] > datetime.now(UTC):
            user_id = session["userId"]
    if not user_id:
        core.SESSIONS.pop(bearer, None)
        raise HTTPException(401, "Sign in required")
    user = next((item for item in users if item["id"] == user_id), None)
    if not user:
        raise HTTPException(401, "Sign in required")
    request.state.current_user = user
    return user


def _require_role(user: dict, roles: set[str], detail: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, detail)


def _company(company_id: str) -> dict:
    company = next((item for item in REPOSITORY.list_companies() if item["id"] == company_id), None)
    if not company:
        raise HTTPException(404, "Customer not found")
    return company


def _known_company(company_id: str | None) -> bool:
    return bool(
        company_id and any(company["id"] == company_id for company in REPOSITORY.list_companies())
    )


def _company_for_user(company_id: str, user: dict, require_manage: bool = False) -> dict:
    company = _company(company_id)
    permitted = (
        core.can_manage(user, company_id) if require_manage else core.allowed(user, company_id)
    )
    if not permitted:
        raise HTTPException(403, "You do not have access to this company")
    return company


class LoginRequest(BaseModel):
    """Validate local password login credentials."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class MfaLoginRequest(BaseModel):
    """Validate the second step of a password-authenticated login."""

    challengeToken: str = Field(min_length=32, max_length=512)
    code: str = Field(min_length=6, max_length=64)


class MfaChallengeRequest(BaseModel):
    """Identify a password-verified login transaction."""

    challengeToken: str = Field(min_length=32, max_length=512)


class MfaCodeRequest(BaseModel):
    """Validate a current authenticator or recovery code."""

    code: str = Field(min_length=6, max_length=64)


class MfaDisableRequest(MfaCodeRequest):
    """Require both local factors before disabling MFA."""

    password: str = Field(min_length=1, max_length=512)


class MfaResetRequest(BaseModel):
    """Capture authorization and governance evidence for an administrative MFA reset."""

    reason: str = Field(min_length=4, max_length=500)
    ticketReference: str = Field(default="", max_length=120)
    confirmation: str = Field(min_length=3, max_length=320)
    administratorPassword: str = Field(default="", max_length=512)
    administratorCode: str = Field(default="", max_length=64)


class DatabaseSettingsRequest(BaseModel):
    """Validate PostgreSQL connection and initialization settings."""

    url: str | None = None
    host: str | None = None
    port: int = Field(default=5432, ge=1, le=65535)
    database: str | None = None
    username: str | None = None
    password: str = ""
    sslmode: str = "prefer"
    seedMode: str = Field(default="current", pattern="^(current|empty|demo)$")


class BackupRestoreRequest(BaseModel):
    """Accept a portable backup document for restore operations."""

    model_config = ConfigDict(extra="allow")

    format: str
    version: int
    state: dict[str, Any]
    createdAt: str | None = None
    databaseMode: str | None = None


class CompanyCreateRequest(BaseModel):
    """Validate a new customer tenant."""

    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(default="", max_length=100)


class AccessGroupRequest(BaseModel):
    """Validate a reusable customer access group."""

    id: str = ""
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    companyIds: list[str] = Field(min_length=1)
    ownerUserId: str | None = None
    membershipMode: str = Field(default="manual", pattern="^(manual|dynamic)$")
    membershipRules: dict[str, Any] = Field(default_factory=dict)
    expectedRevision: int | None = Field(default=None, ge=1)


class UserCreateRequest(BaseModel):
    """Validate a root or customer portal account."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=512)
    accountType: str
    companyId: str | None = None
    companyIds: list[str] = Field(default_factory=list)
    groupIds: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    """Validate profile, role, scope and API-access changes."""

    email: str = Field(min_length=3, max_length=320)
    displayName: str = Field(min_length=1, max_length=160)
    role: str
    companyId: str | None = None
    companyIds: list[str] = Field(default_factory=list)
    groupIds: list[str] = Field(default_factory=list)
    apiAccessEnabled: bool = False
    mfaRequired: bool = False
    reason: str = Field(default="Administrative user update", max_length=500)


class UserStatusRequest(BaseModel):
    """Validate an account enable or disable action."""

    status: str = Field(pattern="^(active|disabled)$")
    reason: str = Field(min_length=4, max_length=500)


class UserPasswordRequest(BaseModel):
    """Validate an administrative local-password reset."""

    password: str = Field(min_length=12, max_length=512)


class PasswordResetRequest(BaseModel):
    """Validate a public local-account recovery request."""

    email: str = Field(min_length=3, max_length=320)


class PasswordResetCompleteRequest(BaseModel):
    """Validate completion of a token-based password recovery."""

    token: str = Field(min_length=32, max_length=512)
    newPassword: str = Field(min_length=12, max_length=512)
    revokeApiTokens: bool = True


class PasswordChangeRequest(BaseModel):
    """Validate an authenticated local password change."""

    currentPassword: str = Field(min_length=1, max_length=512)
    newPassword: str = Field(min_length=12, max_length=512)
    mfaCode: str = Field(default="", max_length=100)
    revokeApiTokens: bool = False


class ApiTokenCreateRequest(BaseModel):
    """Validate a restricted, expiring personal API token."""

    name: str = Field(min_length=2, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=2)
    companyIds: list[str] = Field(default_factory=list)
    expiresInDays: int = Field(default=90, ge=1, le=365)


class ContactCreateRequest(BaseModel):
    """Validate a customer contact profile."""

    companyId: str = Field(min_length=1)
    displayName: str = Field(min_length=1, max_length=180)
    firstName: str = Field(default="", max_length=100)
    lastName: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=80)
    mobile: str = Field(default="", max_length=80)
    jobTitle: str = Field(default="", max_length=160)
    department: str = Field(default="", max_length=160)
    location: str = Field(default="", max_length=160)
    timezone: str = Field(default="", max_length=100)
    managerContactId: str | None = None
    status: str = "active"
    attributes: dict[str, Any] = Field(default_factory=dict)


class ContactPatchRequest(BaseModel):
    """Validate editable contact profile and lifecycle fields."""

    displayName: str | None = Field(default=None, min_length=1, max_length=180)
    firstName: str | None = Field(default=None, max_length=100)
    lastName: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=80)
    mobile: str | None = Field(default=None, max_length=80)
    jobTitle: str | None = Field(default=None, max_length=160)
    department: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, max_length=100)
    managerContactId: str | None = None
    status: str | None = None
    attributes: dict[str, Any] | None = None
    reason: str = Field(default="", max_length=1000)


class PortalUserCreateRequest(BaseModel):
    """Validate temporary credentials for linked portal access."""

    password: str = Field(min_length=8, max_length=512)


class ContactReassignRequest(BaseModel):
    """Validate a bulk responsibility handover."""

    replacementContactId: str = Field(min_length=1)
    reason: str = Field(min_length=4, max_length=1000)


class ResponsibilityInput(BaseModel):
    """Describe one contact responsibility assigned to a CI."""

    contactId: str = Field(min_length=1)
    role: str = Field(min_length=1, max_length=80)
    isPrimary: bool = True
    escalationOrder: int = Field(default=1, ge=1, le=100)
    notes: str = Field(default="", max_length=1000)


class AssetCreateRequest(BaseModel):
    """Validate a new configuration item."""

    companyId: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=240)
    type: str = Field(min_length=1, max_length=120)
    status: str = Field(default="Active", max_length=80)
    fields: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None
    responsibilities: list[ResponsibilityInput] = Field(default_factory=list)


class AssetPatchRequest(BaseModel):
    """Validate editable configuration item fields."""

    name: str | None = Field(default=None, min_length=1, max_length=240)
    type: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, max_length=80)
    fields: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    responsibilities: list[ResponsibilityInput] | None = None


class RelationshipCreateRequest(BaseModel):
    """Validate a relationship between two configuration items."""

    fromId: str = Field(min_length=1)
    toId: str = Field(min_length=1)
    type: str = "related_to"
    impactPolicy: str = "required"


class DataQualityExceptionRequest(BaseModel):
    """Validate a governed data-quality exception."""

    companyId: str = Field(min_length=1)
    ruleKey: str = Field(min_length=1, max_length=80)
    entityId: str = Field(min_length=1)
    reason: str = Field(min_length=4, max_length=2000)
    expiresAt: str | None = None


class ReconciliationDecisionRequest(BaseModel):
    """Validate a source-record reconciliation decision."""

    decision: str = Field(pattern="^(use_existing|create_new|ignore)$")
    notes: str = Field(min_length=4, max_length=2000)
    targetAssetId: str | None = None


class FieldAuthorityRequest(BaseModel):
    """Validate field-level integration authority."""

    companyId: str = Field(min_length=1)
    ciType: str = Field(default="*", min_length=1, max_length=120)
    fieldName: str = Field(min_length=1, max_length=160)
    provider: str = Field(pattern="^(connectwise|ncentral|passportal|cmdb|future)$")
    priority: int = Field(default=100, ge=0, le=32767)


class FieldAuthorityPresetRequest(BaseModel):
    """Apply one curated, auditable field-authority baseline to a customer."""

    companyId: str = Field(min_length=1, max_length=100)
    presetKey: str = Field(min_length=1, max_length=80)


class IntegrationReviewBulkDismissRequest(BaseModel):
    """Dismiss a bounded set of unchanged review observations with one reason."""

    itemIds: list[str] = Field(min_length=1, max_length=100)
    notes: str = Field(min_length=4, max_length=2000)


class IntegrationSuppressionRestoreRequest(BaseModel):
    """Restore one durable provider-object exclusion with governed notes."""

    notes: str = Field(min_length=4, max_length=2000)


class IntegrationLifecycleRequest(BaseModel):
    """Validate an audited, reversible integration lifecycle transition."""

    action: str = Field(pattern="^(pause|resume|disable|reenable|restore)$")
    reason: str = Field(min_length=4, max_length=1000)
    expectedRevision: int | None = Field(default=None, ge=1)


class IntegrationRemovalRequest(BaseModel):
    """Validate credential-destructive integration removal."""

    confirmation: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=4, max_length=1000)
    expectedRevision: int | None = Field(default=None, ge=1)


class ConnectWiseConfigurationRequest(BaseModel):
    """Validate root-managed ConnectWise PSA connection settings."""

    enabled: bool = True
    baseUrl: str = Field(min_length=8, max_length=500)
    companyId: str = Field(min_length=1, max_length=160)
    clientId: str = Field(min_length=1, max_length=160)
    publicKey: str = Field(default="", max_length=1000)
    privateKey: str = Field(default="", max_length=2000)
    pageSize: int = Field(default=100, ge=25, le=1000)
    expectedRevision: int | None = Field(default=None, ge=1)


class ConnectWiseCompanyMappingRequest(BaseModel):
    """Validate an explicit ConnectWise-company to CMDB-customer mapping."""

    companyId: str = Field(min_length=1, max_length=100)


class ConnectWiseDiscoveryPolicyRequest(BaseModel):
    """Validate a reviewable company-discovery filter policy."""

    includedStatuses: list[str] = Field(default_factory=list, max_length=500)
    includedTypes: list[str] = Field(default_factory=list, max_length=500)
    includedSites: list[str] = Field(default_factory=list, max_length=500)
    includeDeleted: bool = False
    excludedExternalIds: list[str] = Field(default_factory=list, max_length=500)
    expectedRevision: int | None = Field(default=None, ge=1)


class ConnectWiseConfigurationPreviewRequest(BaseModel):
    """Select one explicitly mapped provider company for a read-only CI preview."""

    companyId: str = Field(min_length=1, max_length=100)
    providerCompanyId: str = Field(min_length=1, max_length=160)


class ConnectWiseConfigurationImportRequest(ConnectWiseConfigurationPreviewRequest):
    """Approve a bounded set of previewed provider records for canonical import."""

    externalIds: list[str] = Field(min_length=1, max_length=1000)
    decisionNotes: str = Field(default="", max_length=2000)


class ConnectWiseCiPolicyRequest(ConnectWiseConfigurationPreviewRequest):
    """Validate an immutable-ID CI filter and future continuous-preview schedule."""

    typeMode: str = Field(default="all", pattern="^(all|selected)$")
    includedTypeIds: list[str] = Field(default_factory=list, max_length=500)
    typeMappings: dict[str, str] = Field(default_factory=dict, max_length=500)
    blockUnmappedTypes: bool = False
    statusMode: str = Field(default="all", pattern="^(all|selected)$")
    includedStatusIds: list[str] = Field(default_factory=list, max_length=500)
    excludedExternalIds: list[str] = Field(default_factory=list, max_length=1000)
    syncMode: str = Field(default="manual", pattern="^(manual|continuous_preview)$")
    intervalMinutes: int = Field(default=360, ge=15, le=10080)
    enabled: bool = False
    expectedRevision: int | None = Field(default=None, ge=0)


class ConnectWiseConfigurationLinkRequest(ConnectWiseConfigurationPreviewRequest):
    """Explicitly map one immutable provider CI identity to a canonical CI."""

    externalId: str = Field(min_length=1, max_length=160)
    assetId: str = Field(min_length=1, max_length=100)


class ConnectWiseReviewDismissRequest(BaseModel):
    """Record why one unchanged CI review observation can be ignored."""

    notes: str = Field(min_length=4, max_length=1000)


class BrandingRequest(BaseModel):
    """Validate MSP or customer branding settings."""

    scope: str = "customer"
    companyId: str | None = None
    name: str = Field(default="", max_length=80)
    accent: str = "#50d5b9"
    secondaryAccent: str = "#7997ff"
    logoText: str = Field(default="", max_length=3)
    logoDataUrl: str = Field(default="", max_length=1_500_000)
    logoFileName: str = Field(default="", max_length=180)
    supportEmail: str = Field(default="", max_length=160)
    supportUrl: str = Field(default="", max_length=300)
    supportPhone: str = Field(default="", max_length=80)
    welcomeMessage: str = Field(default="", max_length=180)
    reportFooter: str = Field(default="", max_length=180)
    confidentialityLabel: str = Field(default="", max_length=80)


class EmailConfigurationRequest(BaseModel):
    """Validate the root-managed Microsoft 365 email connection."""

    enabled: bool = False
    authMode: str = Field(
        default="managed_identity",
        pattern="^(managed_identity|client_secret|certificate)$",
    )
    tenantId: str = Field(default="", max_length=80)
    clientId: str = Field(default="", max_length=80)
    servicePrincipalObjectId: str = Field(default="", max_length=80)
    managedIdentityClientId: str = Field(default="", max_length=80)
    senderAddress: str = Field(default="", max_length=320)
    senderName: str = Field(default="", max_length=160)
    replyTo: str = Field(default="", max_length=320)
    clientSecret: str = Field(default="", max_length=2048)
    expectedRevision: int | None = Field(default=None, ge=1)


class EmailSetupScriptRequest(BaseModel):
    """Validate values embedded in a generated Exchange setup package."""

    authMode: str = Field(
        default="managed_identity",
        pattern="^(managed_identity|client_secret|certificate)$",
    )
    tenantId: str = Field(default="", max_length=80)
    clientId: str = Field(min_length=1, max_length=80)
    servicePrincipalObjectId: str = Field(min_length=1, max_length=80)
    senderAddress: str = Field(min_length=3, max_length=320)
    senderName: str = Field(default="IPT CMDB", max_length=160)
    createSharedMailbox: bool = False


class EmailTestRequest(BaseModel):
    """Validate an explicit administrative email test."""

    recipient: str = Field(min_length=3, max_length=320)
    subject: str = Field(default="CMDB Hub Microsoft 365 email test", max_length=240)


class NotificationRuleRequest(BaseModel):
    """Validate editable notification timing, routing, and retry controls."""

    name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    leadDays: int = Field(default=0, ge=0, le=3650)
    cadence: str = Field(default="daily", pattern="^(immediate|daily|weekly)$")
    recipientRoles: list[str] = Field(default_factory=list, max_length=12)
    fallbackAddresses: list[str] = Field(default_factory=list, max_length=20)
    templateKey: str = Field(min_length=1, max_length=100)
    maxAttempts: int = Field(default=5, ge=1, le=20)
    expectedRevision: int | None = Field(default=None, ge=1)


class NotificationTemplateRequest(BaseModel):
    """Validate safe constrained notification template content."""

    name: str = Field(min_length=1, max_length=160)
    subjectTemplate: str = Field(min_length=1, max_length=998)
    htmlTemplate: str = Field(min_length=1, max_length=20_000)
    textTemplate: str = Field(min_length=1, max_length=10_000)
    enabled: bool = True
    expectedVersion: int | None = Field(default=None, ge=1)


class NotificationPreferenceRequest(BaseModel):
    """Validate contact or portal-user notification preferences."""

    companyId: str = Field(min_length=1, max_length=100)
    contactId: str | None = None
    userId: str | None = None
    emailEnabled: bool = True
    eventTypes: list[str] = Field(default_factory=lambda: ["*"], max_length=20)
    digestMode: str = Field(default="instant", pattern="^(instant|daily|weekly)$")


class ChangeImpactRequest(BaseModel):
    """Validate the scope used for change impact analysis."""

    companyId: str = Field(min_length=1)
    scopeAssetIds: list[str] = Field(min_length=1)
    outageExpected: bool = False


class ChangeCreateRequest(ChangeImpactRequest):
    """Validate a new change-control record."""

    title: str = Field(min_length=1, max_length=240)
    changeType: str = "normal"
    category: str = "infrastructure"
    priority: str = "medium"
    riskLevel: str = ""
    plannedStart: str = ""
    plannedEnd: str = ""
    reason: str = Field(min_length=1, max_length=8000)
    businessImpact: str = ""
    implementationPlan: str = Field(min_length=1, max_length=8000)
    validationPlan: str = Field(min_length=1, max_length=8000)
    rollbackPlan: str = Field(min_length=1, max_length=8000)
    communicationStatus: str = "required"
    communicationPlan: str = ""
    assignedTechnician: str = ""
    approver: str = ""
    notes: str = ""


class ChangeUpdateRequest(BaseModel):
    """Validate an optimistic-concurrency change update."""

    expectedRevision: int = Field(ge=1)
    scopeAssetIds: list[str] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    changeType: str | None = None
    category: str | None = None
    priority: str | None = None
    riskLevel: str | None = None
    outageExpected: bool | None = None
    plannedStart: str | None = None
    plannedEnd: str | None = None
    reason: str | None = Field(default=None, max_length=8000)
    businessImpact: str | None = Field(default=None, max_length=8000)
    implementationPlan: str | None = Field(default=None, max_length=8000)
    validationPlan: str | None = Field(default=None, max_length=8000)
    rollbackPlan: str | None = Field(default=None, max_length=8000)
    communicationStatus: str | None = None
    communicationPlan: str | None = Field(default=None, max_length=8000)
    assignedTechnician: str | None = Field(default=None, max_length=240)
    approver: str | None = Field(default=None, max_length=240)
    notes: str | None = Field(default=None, max_length=8000)


class ChangeTransitionRequest(BaseModel):
    """Validate a change lifecycle transition."""

    status: str = Field(min_length=1, max_length=80)
    expectedRevision: int = Field(ge=1)
    reason: str = Field(default="", max_length=8000)
    actualStart: str = ""
    actualEnd: str = ""
    actualOutageMinutes: int = Field(default=0, ge=0)
    validationResult: str = Field(default="", max_length=8000)
    rollbackResult: str = Field(default="", max_length=8000)
    closureNotes: str = Field(default="", max_length=8000)


class ChangeApprovalCreateRequest(BaseModel):
    """Validate creation or replacement of an external approval batch."""

    expectedRevision: int = Field(ge=1)
    expiresInHours: int = Field(default=72, ge=1, le=168)


class ChangeApprovalResponseRequest(BaseModel):
    """Validate a one-time external approval response."""

    token: str = Field(min_length=32, max_length=512)
    decision: str = Field(pattern="^(approved|declined)$")
    comments: str = Field(default="", max_length=4000)


class ChangeApprovalValidateRequest(BaseModel):
    """Validate the bearer value used to open an external review."""

    token: str = Field(min_length=32, max_length=512)


@api.get("/api/v2/health", tags=["platform"])
@api.get("/api/health", tags=["platform"])
def health() -> dict:
    database_unavailable = core.DATABASE_MODE == "PostgreSQL unavailable"
    database_available = core.DATABASE_MODE == "PostgreSQL"
    return {
        "status": "setup_required"
        if core.DATABASE_MODE == "database setup"
        else "degraded"
        if database_unavailable
        else "ok",
        "api": "FastAPI",
        "databaseMode": core.DATABASE_MODE,
        "repositoryMode": REPOSITORY.mode,
        "databaseAvailable": database_available,
        "expectedSchemaVersion": core.SCHEMA_VERSION,
        "authentication": os.getenv("AUTH_MODE", "local"),
    }


@api.get("/api/live", tags=["platform"])
def liveness() -> dict[str, str]:
    """Confirm that the API process can accept HTTP requests."""

    return {"status": "alive", "api": "FastAPI"}


@api.get("/api/ready", tags=["platform"])
def readiness() -> JSONResponse:
    """Report whether the canonical PostgreSQL repository is ready for traffic."""

    ready = core.DATABASE_MODE == "PostgreSQL" and REPOSITORY.mode == "canonical_postgresql"
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "databaseAvailable": ready,
            "expectedSchemaVersion": core.SCHEMA_VERSION,
        },
        status_code=200 if ready else 503,
    )


def _user_by_id(user_id: str) -> dict | None:
    return next((item for item in REPOSITORY.list_users() if item["id"] == user_id), None)


def _mfa_policy_requires(user: dict) -> bool:
    if user.get("authSource") != "local":
        return False
    policy = os.getenv("LOCAL_MFA_POLICY", "optional").strip().lower()
    policy_requires = policy == "all" or (
        policy == "admins" and user.get("role") == "platform_admin"
    )
    return bool(user.get("mfaRequired") or policy_requires)


def _new_login_challenge(user: dict, purpose: str) -> dict:
    raw_token = f"cmdb_mfa_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    REPOSITORY.create_login_challenge(
        {
            "tokenHash": opaque_token_hash(raw_token),
            "userId": user["id"],
            "purpose": purpose,
            "attempts": 0,
            "maxAttempts": 5,
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        }
    )
    return {
        "mfaRequired": True,
        "mfaEnrollmentRequired": purpose == "enroll",
        "challengeToken": raw_token,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
    }


def _issue_session(user: dict, *, mfa_method: str = "none") -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=core.SESSION_TTL_SECONDS)
    expires_text = expires_at.isoformat().replace("+00:00", "Z")
    REPOSITORY.create_session(opaque_token_hash(token), user["id"], expires_text)
    # Kept as a compatibility mirror for the lightweight local repository.
    core.SESSIONS[token] = {"userId": user["id"], "expiresAt": expires_at}
    REPOSITORY.record_user_login(user["id"])
    REPOSITORY.record_audit_event(
        None,
        user["id"],
        "authentication",
        user["id"],
        "login_succeeded",
        after={"email": user["email"], "role": user["role"]},
        metadata={"mode": "local", "mfaMethod": mfa_method},
    )
    return {
        "token": token,
        "expiresAt": expires_text,
        "user": core.public_user(_user_by_id(user["id"]) or user),
    }


def _challenge(token: str, purpose: str | None = None) -> tuple[dict, dict]:
    token_hash = opaque_token_hash(token)
    challenge = REPOSITORY.get_login_challenge(token_hash)
    if not challenge or (purpose and challenge["purpose"] != purpose):
        raise HTTPException(401, "MFA challenge is invalid or expired")
    user = _user_by_id(challenge["userId"])
    if not user:
        raise HTTPException(401, "MFA challenge is invalid or expired")
    return challenge, user


def _start_mfa_enrollment(user: dict, actor_id: str | None) -> dict:
    secret = new_totp_secret()
    encrypted, nonce = encrypt_secret(secret, user["id"])
    REPOSITORY.save_mfa_enrollment(user["id"], encrypted, nonce, actor_id)
    issuer = REPOSITORY.get_msp_branding().get("name") or "CMDB Hub"
    uri = provisioning_uri(secret, user["email"], issuer)
    return {
        "status": "pending",
        "manualKey": secret,
        "provisioningUri": uri,
        "qrCodeDataUri": qr_data_uri(uri),
    }


def _verify_enabled_mfa(user: dict, code: str) -> str | None:
    credential = REPOSITORY.get_mfa_credential(user["id"])
    if not credential or credential.get("status") != "enabled":
        return None
    compact = code.strip().upper().replace(" ", "")
    recovery_value = "".join(character for character in compact if character.isalnum())
    if len(recovery_value) > 6:
        canonical_recovery = "-".join(
            recovery_value[index : index + 4] for index in range(0, len(recovery_value), 4)
        )
        return (
            "recovery_code"
            if REPOSITORY.consume_recovery_code(user["id"], canonical_recovery)
            else None
        )
    secret = decrypt_secret(credential["encryptedSecret"], credential["secretNonce"], user["id"])
    counter = verify_totp(secret, compact, last_counter=credential.get("lastAcceptedCounter"))
    if counter is None or not REPOSITORY.accept_mfa_counter(user["id"], counter):
        return None
    return "totp"


def _mfa_failure(user: dict, token_hash: str, action: str) -> None:
    attempts = REPOSITORY.record_login_challenge_attempt(token_hash)
    REPOSITORY.record_audit_event(
        None,
        user["id"],
        "authentication",
        user["id"],
        action,
        outcome="failed",
        severity="warning",
        metadata={"attempt": attempts},
    )


def _public_base_url() -> str | None:
    """Return the configured, trusted origin used in security email links."""

    configured = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000").strip().rstrip("/")
    parsed = urlparse(configured)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and parsed.hostname not in local_hosts)
    ):
        LOGGER.warning("Password recovery is unavailable because PUBLIC_BASE_URL is invalid")
        return None
    return configured


def _password_reset_available() -> bool:
    """Return whether local recovery can safely queue Microsoft 365 email."""

    connection = REPOSITORY.get_email_connection()
    return bool(
        _local_login_enabled()
        and _public_base_url()
        and connection.get("enabled")
        and connection.get("senderAddress")
        and connection.get("status") in {"configured", "verified"}
    )


def _requester_hash(request: Request) -> str:
    """Hash the request source so throttling does not retain a plain IP address."""

    address = str(getattr(request.state, "client_address", "") or "unknown")
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


def _password_reset_message(user: dict, raw_token: str) -> dict:
    """Build a branded, single-use recovery email without persisting its token."""

    base_url = _public_base_url()
    if not base_url:
        raise EmailConfigurationError("PUBLIC_BASE_URL is not safe for password recovery")
    brand = REPOSITORY.get_msp_branding()
    raw_brand_name = str(brand.get("name") or "CMDB Hub")
    raw_support_email = str(brand.get("supportEmail") or "")
    brand_name = html.escape(raw_brand_name)
    support_email = html.escape(raw_support_email)
    accent = html.escape(str(brand.get("accent") or "#50d5b9"), quote=True)
    reset_url = f"{base_url}/#/login?resetToken={quote(raw_token, safe='')}"
    safe_url = html.escape(reset_url, quote=True)
    support = (
        f'<p style="color:#64748b">Need help? Contact {support_email}.</p>' if support_email else ""
    )
    return {
        "idempotencyKey": f"local-password-reset:{uuid.uuid4()}",
        "to": [user["email"]],
        "subject": f"Reset your {raw_brand_name} password",
        "bodyHtml": (
            f'<div style="font-family:Arial,sans-serif;color:#172033;max-width:600px">'
            f"<h2>{brand_name} password reset</h2>"
            "<p>We received a request to reset your local account password.</p>"
            f'<p><a href="{safe_url}" style="display:inline-block;padding:12px 18px;'
            f"background:{accent};color:#07111f;text-decoration:none;border-radius:6px;"
            '">Reset password</a></p>'
            "<p>This single-use link expires in 30 minutes. If you did not request this, "
            "you can safely ignore this email.</p>"
            f"{support}</div>"
        ),
        "bodyText": (
            f"Reset your {raw_brand_name} local password within 30 minutes:\n"
            f"{reset_url}\n\n"
            "If you did not request this, you can safely ignore this email."
        ),
        "templateKey": "local_password_reset",
        "templateVersion": 1,
        "maxAttempts": 5,
    }


def _password_changed_message(email: str) -> dict:
    """Build the post-change security notification for a local account."""

    brand = REPOSITORY.get_msp_branding()
    raw_brand_name = str(brand.get("name") or "CMDB Hub")
    raw_support_email = str(brand.get("supportEmail") or "")
    brand_name = html.escape(raw_brand_name)
    support = (
        f" Contact {raw_support_email} immediately if this was not you."
        if raw_support_email
        else " Contact your platform administrator immediately if this was not you."
    )
    html_support = (
        f" Contact {html.escape(raw_support_email)} immediately if this was not you."
        if raw_support_email
        else " Contact your platform administrator immediately if this was not you."
    )
    return {
        "idempotencyKey": f"local-password-changed:{uuid.uuid4()}",
        "to": [email],
        "subject": f"Your {raw_brand_name} password was changed",
        "bodyHtml": (
            f"<h2>{brand_name} password changed</h2>"
            f"<p>Your local account password was changed at {html.escape(core.now())}.</p>"
            f"<p>{html_support.strip()}</p>"
        ),
        "bodyText": (
            f"Your {raw_brand_name} local account password was changed at {core.now()}.{support}"
        ),
        "templateKey": "local_password_changed",
        "templateVersion": 1,
        "maxAttempts": 5,
    }


def _process_password_reset_request(email: str, requester_hash: str) -> None:
    """Create and send one recovery transaction after the generic response begins."""

    try:
        user = next(
            (
                item
                for item in REPOSITORY.list_users()
                if item.get("email", "").casefold() == email.casefold()
                and item.get("authSource") == "local"
            ),
            None,
        )
        raw_token = f"cmdb_reset_{secrets.token_urlsafe(32)}"
        expires_at = datetime.now(UTC) + timedelta(minutes=30)
        stored = REPOSITORY.create_password_reset(
            {
                "tokenHash": opaque_token_hash(raw_token),
                "userId": user["id"] if user else None,
                "identifierHash": hashlib.sha256(email.encode("utf-8")).hexdigest(),
                "requesterHash": requester_hash,
                "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
            }
        )
        if stored and user:
            message = _password_reset_message(user, raw_token)
            connection, client_secret = _email_connection_for_delivery()
            try:
                result = EMAIL_SENDER.send(connection, message, client_secret=client_secret)
            except (EmailConfigurationError, EmailDeliveryError) as error:
                REPOSITORY.record_audit_event(
                    None,
                    None,
                    "authentication",
                    user["id"],
                    "password_reset_email_failed",
                    outcome="failed",
                    severity="warning",
                    metadata={"error": str(error)[:200]},
                )
                LOGGER.warning("Password recovery email was not accepted")
            else:
                REPOSITORY.record_audit_event(
                    None,
                    None,
                    "authentication",
                    user["id"],
                    "password_reset_email_accepted",
                    metadata={"providerRequestId": result.provider_request_id},
                )
    except Exception:
        LOGGER.exception("Password recovery request processing failed")


def _deliver_security_email_quietly(message_id: str) -> None:
    """Attempt background delivery without leaking provider failures publicly."""

    try:
        _attempt_email_delivery(message_id, None)
    except HTTPException:
        LOGGER.warning("Security email delivery was deferred", extra={"message_id": message_id})
    except Exception:
        LOGGER.exception("Security email delivery failed", extra={"message_id": message_id})


def _queue_password_changed_email(email: str, background_tasks: BackgroundTasks) -> None:
    """Queue and opportunistically deliver a password-change notice."""

    if not _password_reset_available():
        return
    queued = REPOSITORY.create_email_outbox(_password_changed_message(email), None)
    background_tasks.add_task(_deliver_security_email_quietly, queued["id"])


@api.post("/api/password-reset/request", status_code=202, tags=["authentication"])
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Queue a generic local-account recovery request without enumeration."""

    generic = "If an eligible local account exists, a password reset email has been queued."
    if not _password_reset_available():
        return {"message": generic}
    email = payload.email.strip().lower()
    background_tasks.add_task(_process_password_reset_request, email, _requester_hash(request))
    return {"message": generic}


@api.get("/api/password-reset/validate", tags=["authentication"])
def validate_password_reset(token: str = "") -> dict:
    """Validate a recovery token without exposing account identity."""

    reset = (
        REPOSITORY.get_password_reset(opaque_token_hash(token))
        if _local_login_enabled() and len(token) >= 32
        else None
    )
    return {
        "valid": bool(reset),
        "expiresAt": reset.get("expiresAt") if reset else None,
    }


@api.post("/api/password-reset/complete", tags=["authentication"])
def complete_password_reset(
    payload: PasswordResetCompleteRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Consume a recovery token, change the password and revoke credentials."""

    if not _local_login_enabled():
        raise HTTPException(400, "Reset link is invalid or expired")
    try:
        completed = REPOSITORY.complete_password_reset(
            opaque_token_hash(payload.token),
            payload.newPassword,
            revoke_api_tokens=payload.revokeApiTokens,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not completed:
        raise HTTPException(400, "Reset link is invalid or expired")
    revoked_sessions = _revoke_user_sessions(completed["userId"])
    _queue_password_changed_email(completed["email"], background_tasks)
    return {
        "message": "Password updated. Sign in with your new password.",
        "revokedSessions": max(revoked_sessions, int(completed.get("revokedSessions") or 0)),
        "revokedApiTokens": int(completed.get("revokedApiTokens") or 0),
    }


@api.post("/api/login", tags=["authentication"])
def login(payload: LoginRequest, request: Request) -> dict:
    if not _local_login_enabled():
        raise HTTPException(403, "Local password login is disabled; use Microsoft sign-in")
    email = payload.email.strip().lower()
    user = REPOSITORY.authenticate(email, payload.password)
    if not user:
        REPOSITORY.record_audit_event(
            None,
            None,
            "authentication",
            canonical_uuid("authentication", email),
            "login_failed",
            outcome="failed",
            severity="warning",
            actor_type="anonymous",
            metadata={"email": email, "mode": "local"},
        )
        raise HTTPException(401, "Invalid credentials")
    request.state.current_user = user
    credential = REPOSITORY.get_mfa_credential(user["id"])
    if credential and credential.get("status") == "enabled":
        return _new_login_challenge(user, "verify")
    if _mfa_policy_requires(user):
        return _new_login_challenge(user, "enroll")
    return _issue_session(user)


@api.post("/api/login/mfa/enrollment", tags=["authentication"])
def start_login_mfa_enrollment(payload: MfaChallengeRequest) -> dict:
    _, user = _challenge(payload.challengeToken, "enroll")
    try:
        return _start_mfa_enrollment(user, user["id"])
    except MfaConfigurationError as error:
        raise HTTPException(503, str(error)) from error


@api.post("/api/login/mfa", tags=["authentication"])
def complete_mfa_login(payload: MfaLoginRequest, request: Request) -> dict:
    challenge, user = _challenge(payload.challengeToken)
    token_hash = opaque_token_hash(payload.challengeToken)
    method: str | None
    if challenge["purpose"] == "enroll":
        credential = REPOSITORY.get_mfa_credential(user["id"])
        if not credential or credential.get("status") != "pending":
            raise HTTPException(409, "Start authenticator enrollment first")
        try:
            secret = decrypt_secret(
                credential["encryptedSecret"], credential["secretNonce"], user["id"]
            )
        except MfaConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        counter = verify_totp(secret, payload.code)
        if counter is None:
            _mfa_failure(user, token_hash, "mfa_enrollment_failed")
            raise HTTPException(401, "Authenticator code was not accepted")
        codes = recovery_codes()
        if not REPOSITORY.enable_mfa(
            user["id"], counter, [hash_password(code) for code in codes], user["id"]
        ):
            raise HTTPException(409, "Authenticator enrollment is no longer pending")
        method = "totp"
    else:
        try:
            method = _verify_enabled_mfa(user, payload.code)
        except MfaConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        if not method:
            _mfa_failure(user, token_hash, "mfa_challenge_failed")
            raise HTTPException(401, "Authenticator or recovery code was not accepted")
        codes = []
    REPOSITORY.record_login_challenge_attempt(token_hash)
    REPOSITORY.consume_login_challenge(token_hash)
    request.state.current_user = user
    result = _issue_session(user, mfa_method=method)
    if codes:
        result["recoveryCodes"] = codes
    return result


@api.get("/api/auth/config", tags=["authentication"])
def auth_config() -> dict:
    mode = os.getenv("AUTH_MODE", "local").lower()
    external = mode == "easy_auth"
    try:
        encryption_key()
        mfa_available = True
    except MfaConfigurationError:
        mfa_available = False
    return {
        "mode": mode,
        "external": external,
        "localLoginEnabled": _local_login_enabled(),
        "passwordResetAvailable": _password_reset_available(),
        "mfaAvailable": mfa_available,
        "localMfaPolicy": os.getenv("LOCAL_MFA_POLICY", "optional").strip().lower(),
        "externalLoginUrl": os.getenv(
            "ENTRA_LOGIN_URL", "/.auth/login/aad?post_login_redirect_uri=/"
        )
        if external
        else "",
        "externalLogoutUrl": os.getenv(
            "ENTRA_LOGOUT_URL", "/.auth/logout?post_logout_redirect_uri=/#/login"
        )
        if external
        else "",
    }


@api.post("/api/logout", status_code=204, tags=["authentication"])
def logout(request: Request) -> Response:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    session = core.SESSIONS.get(bearer)
    persisted_user_id = (
        REPOSITORY.authenticate_session(opaque_token_hash(bearer)) if bearer else None
    )
    user = None
    user_id = session["userId"] if session else persisted_user_id
    if user_id:
        user = next(
            (item for item in REPOSITORY.list_users() if item["id"] == user_id),
            None,
        )
    core.SESSIONS.pop(bearer, None)
    if bearer:
        REPOSITORY.revoke_session(opaque_token_hash(bearer))
    if user:
        request.state.current_user = user
        REPOSITORY.record_audit_event(
            None,
            user["id"],
            "session",
            user["id"],
            "logout",
            after={"email": user["email"]},
        )
    return Response(status_code=204)


@api.get("/api/me", tags=["authentication"])
def me(request: Request) -> dict:
    user = current_user(request)
    return {**core.public_user(user), "mfaRequired": _mfa_policy_requires(user)}


@api.put("/api/me/password", tags=["authentication"])
def change_my_password(
    payload: PasswordChangeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Change the current local password after re-authentication."""

    user = current_user(request)
    if user.get("authSource") != "local":
        raise HTTPException(409, "Your password is managed by Microsoft Entra ID")
    if not REPOSITORY.authenticate(user["email"], payload.currentPassword):
        raise HTTPException(400, "Current password or authenticator code was not accepted")
    if REPOSITORY.authenticate(user["email"], payload.newPassword):
        raise HTTPException(400, "New password must be different from the current password")
    if user.get("mfaEnabled"):
        try:
            method = _verify_enabled_mfa(user, payload.mfaCode)
        except MfaConfigurationError as error:
            raise HTTPException(503, str(error)) from error
        if not method:
            raise HTTPException(400, "Current password or authenticator code was not accepted")
    with core.LOCK:
        changed = REPOSITORY.set_user_password(
            user["id"],
            payload.newPassword,
            user["id"],
            action="password_changed_by_user",
        )
        revoked_api_tokens = (
            REPOSITORY.revoke_user_api_tokens(user["id"], user["id"])
            if payload.revokeApiTokens
            else 0
        )
    if not changed:
        raise HTTPException(409, "Password could not be changed")
    revoked_sessions = _revoke_user_sessions(user["id"])
    _queue_password_changed_email(user["email"], background_tasks)
    return {
        "message": "Password changed. Sign in again with your new password.",
        "revokedSessions": revoked_sessions,
        "revokedApiTokens": revoked_api_tokens,
    }


@api.get("/api/me/mfa", tags=["authentication"])
def my_mfa_status(request: Request) -> dict:
    user = current_user(request)
    return {
        "available": bool(auth_config()["mfaAvailable"]),
        "authSource": user.get("authSource"),
        "required": _mfa_policy_requires(user),
        "enabled": bool(user.get("mfaEnabled")),
        "recoveryCodesRemaining": int(user.get("mfaRecoveryCodesRemaining", 0)),
    }


@api.post("/api/me/mfa/enrollment", tags=["authentication"])
def start_my_mfa_enrollment(request: Request) -> dict:
    user = current_user(request)
    if user.get("authSource") != "local":
        raise HTTPException(409, "MFA for this identity is managed by Microsoft Entra ID")
    try:
        return _start_mfa_enrollment(user, user["id"])
    except MfaConfigurationError as error:
        raise HTTPException(503, str(error)) from error


@api.post("/api/me/mfa/confirm", tags=["authentication"])
def confirm_my_mfa_enrollment(payload: MfaCodeRequest, request: Request) -> dict:
    user = current_user(request)
    credential = REPOSITORY.get_mfa_credential(user["id"])
    if not credential or credential.get("status") != "pending":
        raise HTTPException(409, "Start authenticator enrollment first")
    try:
        secret = decrypt_secret(
            credential["encryptedSecret"], credential["secretNonce"], user["id"]
        )
    except MfaConfigurationError as error:
        raise HTTPException(503, str(error)) from error
    counter = verify_totp(secret, payload.code)
    if counter is None:
        raise HTTPException(401, "Authenticator code was not accepted")
    codes = recovery_codes()
    if not REPOSITORY.enable_mfa(
        user["id"], counter, [hash_password(code) for code in codes], user["id"]
    ):
        raise HTTPException(409, "Authenticator enrollment is no longer pending")
    return {"enabled": True, "recoveryCodes": codes}


@api.post("/api/me/mfa/recovery-codes", tags=["authentication"])
def regenerate_my_recovery_codes(payload: MfaCodeRequest, request: Request) -> dict:
    user = current_user(request)
    try:
        method = _verify_enabled_mfa(user, payload.code)
    except MfaConfigurationError as error:
        raise HTTPException(503, str(error)) from error
    if not method:
        raise HTTPException(401, "Current authenticator or recovery code was not accepted")
    codes = recovery_codes()
    if not REPOSITORY.replace_recovery_codes(
        user["id"], [hash_password(code) for code in codes], user["id"]
    ):
        raise HTTPException(409, "MFA is not enabled")
    return {"recoveryCodes": codes}


@api.post("/api/me/mfa/disable", status_code=204, tags=["authentication"])
def disable_my_mfa(payload: MfaDisableRequest, request: Request) -> Response:
    user = current_user(request)
    if _mfa_policy_requires(user):
        raise HTTPException(409, "MFA is required by your account or MSP policy")
    if not REPOSITORY.authenticate(user["email"], payload.password):
        raise HTTPException(401, "Password or authenticator code was not accepted")
    try:
        method = _verify_enabled_mfa(user, payload.code)
    except MfaConfigurationError as error:
        raise HTTPException(503, str(error)) from error
    if not method:
        raise HTTPException(401, "Password or authenticator code was not accepted")
    if not REPOSITORY.disable_mfa(
        user["id"], user["id"], reason="User disabled their authenticator"
    ):
        raise HTTPException(409, "MFA is not enabled")
    _revoke_user_sessions(user["id"])
    return Response(status_code=204)


@api.get("/api/database/status", tags=["platform"])
def database_status(request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Database configuration requires platform admin role")
    return core.database_status()


@api.post("/api/database/test", tags=["platform"])
def database_test(payload: DatabaseSettingsRequest, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Database configuration requires platform admin role")
    try:
        result = core.test_database_url(
            core.database_url_from_settings(payload.model_dump(exclude_none=True))
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "Database connection failed"))
    return result


@api.put("/api/database/config", tags=["platform"])
def database_config(payload: DatabaseSettingsRequest, request: Request) -> dict:
    global REPOSITORY
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Database configuration requires platform admin role")
    try:
        with core.LOCK:
            result = core.save_database_settings(payload.model_dump(exclude_none=True))
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error
    if not result["ok"]:
        raise HTTPException(400, result.get("error", "Database configuration failed"))
    try:
        repository = PostgresCmdbRepository(core.DB, core.save_db, core.postgres_connection)
        if not repository.is_initialized():
            result["bootstrap"] = repository.bootstrap()
            core.CANONICAL_DATABASE_INITIALIZED = True
        else:
            repository.migrate_legacy_company_branding()
            core.DB.clear()
            core.DB.update(repository.export_state())
        REPOSITORY = repository
    except Exception as error:
        LOGGER.exception("database_repository_activation_failed")
        core.DATABASE_ERROR = (
            "Database prepared but repository activation failed; review server logs"
        )
        raise HTTPException(500, "Database activation failed; review server logs") from error
    return {**result, "repositoryMode": REPOSITORY.mode}


@api.get("/api/database/backup", tags=["platform"])
def database_backup(request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Database backup requires platform admin role")
    document = core.backup_document(REPOSITORY.export_state())
    REPOSITORY.record_audit_event(
        None,
        user["id"],
        "portable_export",
        canonical_uuid("portable_export", document["createdAt"]),
        "exported",
        after={
            "createdAt": document["createdAt"],
            "format": document["format"],
            "version": document["version"],
        },
    )
    return document


@api.post("/api/database/restore/preview", tags=["platform"])
def database_restore_preview(payload: BackupRestoreRequest, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Backup validation requires platform admin role")
    try:
        return core.preview_backup(payload.model_dump())
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@api.post("/api/database/restore", tags=["platform"])
def database_restore(payload: BackupRestoreRequest, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Backup restore requires platform admin role")
    try:
        document = payload.model_dump()
        preview = core.preview_backup(document)
        state = document["state"]
        state.setdefault("branding", {})
        state.setdefault("mspBranding", core.DB.get("mspBranding", {}))
        state.setdefault(
            "accessGroups",
            [
                {
                    "id": "all-managed-customers",
                    "name": "All managed customers",
                    "companyIds": ["*"],
                    "system": True,
                }
            ],
        )
        state.setdefault("changes", [])
        with core.LOCK:
            imported = REPOSITORY.import_state(state, user["id"])
        REPOSITORY.record_audit_event(
            None,
            user["id"],
            "recovery",
            canonical_uuid("recovery", payload.createdAt or str(uuid.uuid4())),
            "portable_import_completed",
            after={
                "restoredFrom": payload.createdAt,
                "companies": imported.get("companies"),
                "assets": imported.get("assets"),
            },
            severity="warning",
        )
        return {
            "message": "Portable backup imported successfully.",
            "companies": imported.get("companies", len(state["companies"])),
            "assets": imported.get("assets", len(state["assets"])),
            "restoredFrom": payload.createdAt or "an unknown date",
            "warning": preview["warning"],
        }
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@api.get("/api/companies", tags=["customers"])
def list_companies(request: Request) -> list[dict]:
    user = current_user(request)
    return [item for item in REPOSITORY.list_companies() if core.allowed(user, item["id"])]


@api.post("/api/companies", status_code=201, tags=["customers"])
def create_company(payload: CompanyCreateRequest, request: Request) -> dict:
    actor = current_user(request)
    _require_role(actor, {"platform_admin"}, "Customer creation requires platform admin role")
    name = payload.name.strip()[:100]
    slug = re.sub(r"[^a-z0-9]+", "-", (payload.slug or name).strip().lower()).strip("-")[:48]
    if not name or not slug:
        raise HTTPException(400, "Customer name is required")
    if any(
        company["id"] == slug or company["name"].lower() == name.lower()
        for company in REPOSITORY.list_companies()
    ):
        raise HTTPException(409, "A customer with that name or ID already exists")
    company = {"id": slug, "name": name, "externalIds": {}}
    with core.LOCK:
        company = REPOSITORY.create_company(company, actor["id"])
    return company


@api.get("/api/root-attention", tags=["customers"])
def root_attention(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP overview requires root or MSP role",
    )
    assets = [asset for asset in REPOSITORY.list_assets() if core.allowed(user, asset["companyId"])]
    return core.attention_items(assets, REPOSITORY.list_companies())


@api.get("/api/root-overview", tags=["customers"])
def root_overview(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP overview requires root or MSP role",
    )
    companies = [
        company for company in REPOSITORY.list_companies() if core.allowed(user, company["id"])
    ]
    assets = [asset for asset in REPOSITORY.list_assets() if core.allowed(user, asset["companyId"])]
    return core.customer_overview(companies, assets)


@api.get("/api/dashboard", tags=["dashboard"])
def dashboard(request: Request, companyId: str | None = None) -> dict:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP dashboard requires root or MSP role",
        )
    companies = [
        company for company in REPOSITORY.list_companies() if core.allowed(user, company["id"])
    ]
    assets = [asset for asset in REPOSITORY.list_assets() if core.allowed(user, asset["companyId"])]
    asset_ids = {
        asset["id"] for asset in assets if not companyId or asset["companyId"] == companyId
    }
    relationships = [
        item
        for item in REPOSITORY.list_relationships()
        if item["fromId"] in asset_ids and item["toId"] in asset_ids
    ]
    changes = [item for item in REPOSITORY.list_changes() if core.allowed(user, item["companyId"])]
    integrations = REPOSITORY.list_integrations() if not companyId else []
    sync_runs = REPOSITORY.list_sync_runs() if not companyId else []
    return core.dashboard_snapshot(
        companies,
        assets,
        relationships,
        changes,
        integrations,
        sync_runs,
        company_id=companyId,
    )


def _validate_group(payload: AccessGroupRequest, current_id: str | None = None) -> dict:
    name = payload.name.strip()[:80]
    company_ids = sorted(set(payload.companyIds))
    if (
        not name
        or not company_ids
        or not all(_known_company(company_id) for company_id in company_ids)
    ):
        raise HTTPException(400, "Provide a group name and at least one valid customer")
    if any(
        item["id"] != current_id and item["name"].lower() == name.lower()
        for item in REPOSITORY.list_access_groups()
    ):
        raise HTTPException(409, "A group with that name already exists")
    if payload.membershipMode != "manual" or payload.membershipRules:
        raise HTTPException(
            400,
            "Rule-based dynamic groups are reserved for a future release; choose manual membership",
        )
    return {
        "name": name,
        "description": payload.description.strip()[:500],
        "companyIds": company_ids,
        "membershipMode": "manual",
        "membershipRules": {},
    }


def _validated_group_owner(owner_user_id: str | None, fallback_user_id: str) -> str:
    selected_id = owner_user_id or fallback_user_id
    owner = next(
        (
            item
            for item in REPOSITORY.list_users(include_inactive=True)
            if item["id"] == selected_id
        ),
        None,
    )
    if (
        not owner
        or owner.get("status", "active") != "active"
        or owner["role"] not in {"platform_admin", "msp_operator"}
    ):
        raise HTTPException(400, "Choose an active MSP or platform user as group owner")
    return selected_id


def _group_view(group: dict, actor: dict) -> dict | None:
    group_company_ids = set(group["companyIds"])
    company_ids = [
        company["id"]
        for company in REPOSITORY.list_companies()
        if core.allowed(actor, company["id"])
        and ("*" in group_company_ids or company["id"] in group_company_ids)
    ]
    if not company_ids:
        return None
    return {
        **group,
        "companyIds": company_ids,
        "system": bool(group.get("system") or group["id"] == "all-managed-customers"),
        "membershipMode": group.get(
            "membershipMode", "dynamic" if group.get("system") else "manual"
        ),
        "membershipRules": group.get("membershipRules") or {},
        "description": group.get("description", ""),
        "ownerLabel": group.get("ownerLabel") or "Unassigned",
        "assignedUserCount": int(group.get("assignedUserCount", 0)),
        "revision": int(group.get("revision", 1)),
    }


def _access_group_impact(group: dict) -> dict:
    companies = REPOSITORY.list_companies()
    company_names = {item["id"]: item["name"] for item in companies}
    all_company_ids = set(company_names)
    groups = {item["id"]: item for item in REPOSITORY.list_access_groups()}

    def resolved_company_ids(candidate: dict) -> set[str]:
        configured = set(candidate.get("companyIds", []))
        return all_company_ids if "*" in configured else configured & all_company_ids

    target_company_ids = resolved_company_ids(group)
    affected_users = []
    for user in REPOSITORY.list_users(include_inactive=True):
        if group["id"] not in user.get("groupIds", []):
            continue
        if user["role"] == "platform_admin":
            retained_company_ids = set(all_company_ids)
        else:
            direct = set(user.get("directCompanyIds", []))
            retained_company_ids = all_company_ids if "*" in direct else direct
            for other_group_id in user.get("groupIds", []):
                if other_group_id == group["id"]:
                    continue
                other_group = groups.get(other_group_id)
                if other_group:
                    retained_company_ids.update(resolved_company_ids(other_group))
        lost_company_ids = sorted(target_company_ids - retained_company_ids)
        affected_users.append(
            {
                "id": user["id"],
                "displayName": user.get("displayName") or user["email"],
                "email": user["email"],
                "status": user.get("status", "active"),
                "lostCompanyIds": lost_company_ids,
                "lostCustomers": [company_names[item] for item in lost_company_ids],
            }
        )
    return {
        "groupId": group["id"],
        "groupName": group["name"],
        "assignedUserCount": len(affected_users),
        "activeAssignedUserCount": sum(item["status"] == "active" for item in affected_users),
        "usersLosingAccess": sum(bool(item["lostCompanyIds"]) for item in affected_users),
        "lostCustomerAssignments": sum(len(item["lostCompanyIds"]) for item in affected_users),
        "users": affected_users,
    }


@api.get("/api/access-groups", tags=["access"])
def list_access_groups(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP access groups require root or MSP role",
    )
    return [
        visible
        for group in REPOSITORY.list_access_groups()
        if (visible := _group_view(group, user)) is not None
    ]


@api.post("/api/access-groups", status_code=201, tags=["access"])
def create_access_group(payload: AccessGroupRequest, request: Request) -> dict:
    actor = current_user(request)
    _require_role(
        actor,
        {"platform_admin"},
        "Customer group management requires platform admin role",
    )
    values = _validate_group(payload)
    group_id = re.sub(r"[^a-z0-9]+", "-", (payload.id or values["name"]).lower()).strip("-")[:48]
    if not group_id:
        raise HTTPException(400, "Provide a valid group name")
    if any(group["id"] == group_id for group in REPOSITORY.list_access_groups()):
        raise HTTPException(409, "A group with that ID already exists")
    group = {
        "id": group_id,
        **values,
        "ownerUserId": _validated_group_owner(payload.ownerUserId, actor["id"]),
        "system": False,
        "revision": 1,
    }
    with core.LOCK:
        group = REPOSITORY.create_access_group(group, actor["id"])
    visible_group = _group_view(group, actor)
    if visible_group is None:
        raise HTTPException(500, "Created customer group is outside the administrator scope")
    return visible_group


@api.put("/api/access-groups/{group_id}", tags=["access"])
def update_access_group(group_id: str, payload: AccessGroupRequest, request: Request) -> dict:
    actor = current_user(request)
    _require_role(
        actor,
        {"platform_admin"},
        "Customer group management requires platform admin role",
    )
    group = next(
        (item for item in REPOSITORY.list_access_groups() if item["id"] == group_id),
        None,
    )
    if not group:
        raise HTTPException(404, "Customer group not found")
    if group.get("system") or group["id"] == "all-managed-customers":
        raise HTTPException(400, "The All managed customers group is dynamic and cannot be edited")
    if payload.expectedRevision is not None and payload.expectedRevision != group.get(
        "revision", 1
    ):
        raise HTTPException(409, "This group changed since it was opened; refresh and try again")
    values = {
        **_validate_group(payload, group_id),
        "ownerUserId": _validated_group_owner(payload.ownerUserId, actor["id"]),
        "expectedRevision": group.get("revision", 1),
    }
    with core.LOCK:
        group = REPOSITORY.update_access_group(group_id, values, actor["id"])
    if group is None:
        raise HTTPException(409, "This group changed since it was opened; refresh and try again")
    visible_group = _group_view(group, actor)
    if visible_group is None:
        raise HTTPException(500, "Updated customer group is outside the administrator scope")
    return visible_group


@api.get("/api/access-groups/{group_id}/impact", tags=["access"])
def access_group_delete_impact(group_id: str, request: Request) -> dict:
    actor = current_user(request)
    _require_role(
        actor,
        {"platform_admin"},
        "Customer group impact review requires platform admin role",
    )
    group = next(
        (item for item in REPOSITORY.list_access_groups() if item["id"] == group_id),
        None,
    )
    if not group:
        raise HTTPException(404, "Customer group not found")
    return _access_group_impact(group)


@api.delete("/api/access-groups/{group_id}", tags=["access"])
def delete_access_group(group_id: str, request: Request) -> dict:
    actor = current_user(request)
    _require_role(
        actor,
        {"platform_admin"},
        "Customer group management requires platform admin role",
    )
    group = next(
        (item for item in REPOSITORY.list_access_groups() if item["id"] == group_id),
        None,
    )
    if not group:
        raise HTTPException(404, "Customer group not found")
    if group.get("system") or group["id"] == "all-managed-customers":
        raise HTTPException(400, "The All managed customers group is dynamic and cannot be deleted")
    impact = _access_group_impact(group)
    with core.LOCK:
        REPOSITORY.delete_access_group(group_id, actor["id"])
    return {"deletedId": group_id, "impact": impact}


@api.get("/api/rbac/roles", tags=["access"])
def rbac_roles(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "RBAC requires platform admin role")
    return core.ROLE_TEMPLATES


@api.get("/api/rbac/effective", tags=["access"])
def rbac_effective(userId: str, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "RBAC requires platform admin role")
    target = next(
        (item for item in REPOSITORY.list_users(include_inactive=True) if item["id"] == userId),
        None,
    )
    if not target:
        raise HTTPException(404, "User not found")
    template = next((item for item in core.ROLE_TEMPLATES if item["id"] == target["role"]), None)
    companies = REPOSITORY.list_companies()
    customer_ids = (
        [company["id"] for company in companies]
        if target["role"] == "platform_admin"
        else target["companyIds"]
    )
    customers = [company["name"] for company in companies if company["id"] in customer_ids]
    return {
        "user": core.visible_user(target),
        "role": template,
        "customers": customers,
        "scope": "All customers"
        if target["role"] == "platform_admin"
        else ", ".join(customers) or "No customer access",
    }


@api.get("/api/users", tags=["access"])
def list_users(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "User management requires MSP operator or platform admin role",
    )
    if companyId:
        _company_for_user(companyId, user)
    records = [
        item
        for item in REPOSITORY.list_users(include_inactive=True)
        if user["role"] == "platform_admin"
        or any(core.allowed(user, company_id) for company_id in item["companyIds"])
    ]
    if companyId:
        records = [
            item
            for item in records
            if companyId in item["companyIds"] or item["role"] == "platform_admin"
        ]
    return [
        {**core.visible_user(item), "mfaRequired": _mfa_policy_requires(item)} for item in records
    ]


@api.post("/api/users", status_code=201, tags=["access"])
def create_user(payload: UserCreateRequest, request: Request) -> dict:
    actor = current_user(request)
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    if any(item["email"].lower() == email for item in REPOSITORY.list_users()):
        raise HTTPException(409, "A user with this email already exists")
    if payload.accountType == "root":
        _require_role(actor, {"platform_admin"}, "Only platform admins can create root/MSP users")
        direct_company_ids = set(payload.companyIds)
        effective_company_ids = set(direct_company_ids)
        groups = {group["id"]: group for group in REPOSITORY.list_access_groups()}
        for group_id in payload.groupIds:
            group = groups.get(group_id)
            if not group:
                raise HTTPException(400, "Choose valid MSP access groups")
            group_company_ids = set(group["companyIds"])
            effective_company_ids.update(
                company["id"]
                for company in REPOSITORY.list_companies()
                if "*" in group_company_ids or company["id"] in group_company_ids
            )
        assigned_companies = sorted(direct_company_ids)
        if not effective_company_ids or not all(
            _known_company(company_id) for company_id in effective_company_ids
        ):
            raise HTTPException(
                400, "Choose at least one valid customer permission or access group"
            )
        role = "msp_operator"
    elif payload.accountType == "customer":
        if (
            not payload.companyId
            or not _known_company(payload.companyId)
            or not core.can_manage(actor, payload.companyId)
        ):
            raise HTTPException(403, "Choose a customer you manage")
        assigned_companies = [payload.companyId]
        role = "client_reader"
    else:
        raise HTTPException(400, "Choose root or customer account type")
    new_user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "role": role,
        "companyIds": assigned_companies,
        "directCompanyIds": assigned_companies,
        "groupIds": sorted(set(payload.groupIds)) if payload.accountType == "root" else [],
        "accountType": payload.accountType,
    }
    with core.LOCK:
        new_user = REPOSITORY.create_user(new_user, payload.password, actor["id"])
    return core.visible_user(new_user)


def _managed_user(user_id: str, actor: dict) -> dict:
    target = next(
        (item for item in REPOSITORY.list_users(include_inactive=True) if item["id"] == user_id),
        None,
    )
    if not target:
        raise HTTPException(404, "User not found")
    if actor["role"] == "platform_admin":
        return target
    target_company_ids = [item for item in target["companyIds"] if item != "*"]
    if (
        actor["role"] != "msp_operator"
        or target["role"] != "client_reader"
        or not target_company_ids
        or not all(core.can_manage(actor, item) for item in target_company_ids)
    ):
        raise HTTPException(403, "You cannot manage this user")
    return target


def _active_platform_admin_count() -> int:
    return sum(
        item["role"] == "platform_admin" and item.get("status", "active") == "active"
        for item in REPOSITORY.list_users(include_inactive=True)
    )


def _revoke_user_sessions(user_id: str) -> int:
    tokens = [token for token, session in core.SESSIONS.items() if session.get("userId") == user_id]
    for token in tokens:
        core.SESSIONS.pop(token, None)
    persisted = REPOSITORY.revoke_user_sessions(user_id)
    return max(len(tokens), persisted)


def _validate_user_scope(payload: UserUpdateRequest, actor: dict) -> dict:
    if payload.role not in {"platform_admin", "msp_operator", "client_reader"}:
        raise HTTPException(400, "Choose a supported role")
    if actor["role"] != "platform_admin" and payload.role != "client_reader":
        raise HTTPException(403, "Only platform admins can assign MSP or platform roles")
    email = payload.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    display_name = payload.displayName.strip()
    if payload.role == "platform_admin":
        return {
            "email": email,
            "displayName": display_name,
            "role": payload.role,
            "accountType": "root",
            "directCompanyIds": ["*"],
            "groupIds": [],
            "apiAccessEnabled": payload.apiAccessEnabled,
            "mfaRequired": payload.mfaRequired,
        }
    if payload.role == "client_reader":
        company_id = payload.companyId or next(iter(payload.companyIds), None)
        if (
            not company_id
            or not _known_company(company_id)
            or not core.can_manage(actor, company_id)
        ):
            raise HTTPException(403, "Choose a customer you manage")
        return {
            "email": email,
            "displayName": display_name,
            "role": payload.role,
            "accountType": "customer",
            "directCompanyIds": [company_id],
            "groupIds": [],
            "apiAccessEnabled": payload.apiAccessEnabled,
            "mfaRequired": payload.mfaRequired,
        }

    direct_ids = sorted(set(payload.companyIds))
    groups = {item["id"]: item for item in REPOSITORY.list_access_groups()}
    effective_ids = set(direct_ids)
    for group_id in payload.groupIds:
        group = groups.get(group_id)
        if not group:
            raise HTTPException(400, "Choose valid MSP access groups")
        group_company_ids = set(group["companyIds"])
        effective_ids.update(
            company["id"]
            for company in REPOSITORY.list_companies()
            if "*" in group_company_ids or company["id"] in group_company_ids
        )
    if not effective_ids or not all(_known_company(item) for item in effective_ids):
        raise HTTPException(400, "Choose at least one valid customer permission or access group")
    return {
        "email": email,
        "displayName": display_name,
        "role": payload.role,
        "accountType": "root",
        "directCompanyIds": direct_ids,
        "groupIds": sorted(set(payload.groupIds)),
        "apiAccessEnabled": payload.apiAccessEnabled,
        "mfaRequired": payload.mfaRequired,
    }


@api.patch("/api/users/{user_id}", tags=["access"])
def update_user(user_id: str, payload: UserUpdateRequest, request: Request) -> dict:
    actor = current_user(request)
    current = _managed_user(user_id, actor)
    values = _validate_user_scope(payload, actor)
    if any(
        item["id"] != user_id and item["email"].lower() == values["email"]
        for item in REPOSITORY.list_users(include_inactive=True)
    ):
        raise HTTPException(409, "A user with this email already exists")
    if (
        current["role"] == "platform_admin"
        and values["role"] != "platform_admin"
        and _active_platform_admin_count() <= 1
    ):
        raise HTTPException(409, "The final active platform administrator cannot be demoted")
    changed_access = any(
        current.get(key) != values.get(key)
        for key in ("email", "role", "directCompanyIds", "groupIds", "mfaRequired")
    )
    with core.LOCK:
        stored = REPOSITORY.update_user(user_id, values, actor["id"], reason=payload.reason.strip())
        if not values["apiAccessEnabled"]:
            REPOSITORY.revoke_user_api_tokens(user_id, actor["id"])
    if not stored:
        raise HTTPException(404, "User not found")
    revoked_sessions = _revoke_user_sessions(user_id) if changed_access else 0
    return {**core.visible_user(stored), "revokedSessions": revoked_sessions}


@api.post("/api/users/{user_id}/status", tags=["access"])
def update_user_status(user_id: str, payload: UserStatusRequest, request: Request) -> dict:
    actor = current_user(request)
    target = _managed_user(user_id, actor)
    if user_id == actor["id"] and payload.status == "disabled":
        raise HTTPException(409, "You cannot disable your own account")
    if (
        target["role"] == "platform_admin"
        and payload.status == "disabled"
        and _active_platform_admin_count() <= 1
    ):
        raise HTTPException(409, "The final active platform administrator cannot be disabled")
    with core.LOCK:
        updated = REPOSITORY.set_user_status(
            user_id, payload.status, actor["id"], reason=payload.reason.strip()
        )
        if payload.status == "disabled":
            REPOSITORY.revoke_user_api_tokens(user_id, actor["id"])
    if not updated:
        raise HTTPException(404, "User not found")
    revoked_sessions = _revoke_user_sessions(user_id) if payload.status == "disabled" else 0
    stored = _managed_user(user_id, actor)
    return {**core.visible_user(stored), "revokedSessions": revoked_sessions}


@api.delete("/api/users/{user_id}", tags=["access"])
def archive_user(user_id: str, request: Request) -> dict:
    actor = current_user(request)
    target = _managed_user(user_id, actor)
    if user_id == actor["id"]:
        raise HTTPException(409, "You cannot archive your own account")
    if target["role"] == "platform_admin" and _active_platform_admin_count() <= 1:
        raise HTTPException(409, "The final active platform administrator cannot be archived")
    with core.LOCK:
        archived = REPOSITORY.set_user_status(
            user_id, "archived", actor["id"], reason="Account archived by administrator"
        )
        revoked_tokens = REPOSITORY.revoke_user_api_tokens(user_id, actor["id"])
    if not archived:
        raise HTTPException(404, "User not found")
    return {
        "archivedId": user_id,
        "revokedSessions": _revoke_user_sessions(user_id),
        "revokedApiTokens": revoked_tokens,
    }


@api.put("/api/users/{user_id}/password", status_code=204, tags=["access"])
def reset_user_password(user_id: str, payload: UserPasswordRequest, request: Request) -> Response:
    actor = current_user(request)
    target = _managed_user(user_id, actor)
    if target.get("authSource") == "entra":
        raise HTTPException(409, "Reset this user's password in Microsoft Entra ID")
    with core.LOCK:
        changed = REPOSITORY.set_user_password(user_id, payload.password, actor["id"])
    if not changed:
        raise HTTPException(404, "User not found")
    _revoke_user_sessions(user_id)
    return Response(status_code=204)


@api.post("/api/users/{user_id}/mfa/reset", tags=["access"])
def reset_user_mfa(user_id: str, payload: MfaResetRequest, request: Request) -> dict:
    actor = current_user(request)
    _require_role(actor, {"platform_admin"}, "MFA reset requires platform admin role")
    target = _managed_user(user_id, actor)
    if target.get("authSource") != "local":
        raise HTTPException(409, "MFA for this identity is managed by Microsoft Entra ID")
    if not target.get("mfaEnabled"):
        raise HTTPException(409, "MFA is not enabled for this user")
    if payload.confirmation.strip().casefold() != target["email"].casefold():
        raise HTTPException(400, "Type the user's email address to confirm the MFA reset")

    administrator_method = "entra_session"
    if actor.get("authSource") == "local":
        if not payload.administratorPassword or not REPOSITORY.authenticate(
            actor["email"], payload.administratorPassword
        ):
            raise HTTPException(401, "Administrator verification was not accepted")
        administrator_method = "password"
        if actor.get("mfaEnabled"):
            if not payload.administratorCode:
                raise HTTPException(401, "Administrator verification was not accepted")
            try:
                factor_method = _verify_enabled_mfa(actor, payload.administratorCode)
            except MfaConfigurationError as error:
                raise HTTPException(503, str(error)) from error
            if not factor_method:
                raise HTTPException(401, "Administrator verification was not accepted")
            administrator_method = f"password+{factor_method}"

    if not REPOSITORY.disable_mfa(
        user_id,
        actor["id"],
        reason=payload.reason.strip(),
        action="mfa_reset_by_admin",
        metadata={
            "ticketReference": payload.ticketReference.strip(),
            "administratorVerification": administrator_method,
            "targetEmail": target["email"],
        },
    ):
        raise HTTPException(409, "MFA is not enabled for this user")
    revoked_sessions = _revoke_user_sessions(user_id)
    stored = _managed_user(user_id, actor)
    return {**core.visible_user(stored), "revokedSessions": revoked_sessions}


@api.get("/api/users/{user_id}/tokens", tags=["access"])
def list_user_api_tokens(user_id: str, request: Request) -> list[dict]:
    actor = current_user(request)
    _managed_user(user_id, actor)
    return REPOSITORY.list_api_tokens(user_id)


@api.post("/api/users/{user_id}/tokens", status_code=201, tags=["access"])
def create_user_api_token(user_id: str, payload: ApiTokenCreateRequest, request: Request) -> dict:
    actor = current_user(request)
    target = _managed_user(user_id, actor)
    if target.get("status") != "active":
        raise HTTPException(409, "Enable the user before creating an API token")
    if not target.get("apiAccessEnabled"):
        raise HTTPException(409, "Enable API access for this user first")
    scopes = sorted(set(payload.scopes))
    if not scopes or not set(scopes).issubset({"cmdb:read", "cmdb:write"}):
        raise HTTPException(400, "Choose only supported CMDB API scopes")
    company_ids = sorted(set(payload.companyIds))
    if company_ids and (
        not all(_known_company(item) for item in company_ids)
        or not all(core.allowed(target, item) for item in company_ids)
    ):
        raise HTTPException(403, "Token customer scope cannot exceed the user's access")
    raw_token = f"cmdb_pat_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(UTC) + timedelta(days=payload.expiresInDays)
    stored = REPOSITORY.create_api_token(
        {
            "id": str(uuid.uuid4()),
            "userId": user_id,
            "name": payload.name.strip(),
            "tokenPrefix": raw_token[:20],
            "tokenHash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "scopes": scopes,
            "companyIds": company_ids,
            "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        },
        actor["id"],
    )
    return {**stored, "token": raw_token}


@api.delete("/api/users/{user_id}/tokens/{token_id}", tags=["access"])
def revoke_user_api_token(user_id: str, token_id: str, request: Request) -> dict:
    actor = current_user(request)
    _managed_user(user_id, actor)
    if not any(item["id"] == token_id for item in REPOSITORY.list_api_tokens(user_id)):
        raise HTTPException(404, "API token not found")
    revoked = REPOSITORY.revoke_api_token(
        token_id, actor["id"], reason="Revoked from user management"
    )
    if not revoked:
        raise HTTPException(404, "API token not found")
    return revoked


CONTACT_STATUSES = {"active", "on_leave", "left_company", "inactive"}
RESPONSIBILITY_ROLES = {
    "business_owner",
    "service_owner",
    "technical_owner",
    "custodian",
    "change_approver",
    "signoff_delegate",
    "support_contact",
}
RESPONSIBILITY_METADATA_FIELDS = {
    "business_owner": "businessOwner",
    "service_owner": "serviceOwner",
    "technical_owner": "technicalOwner",
    "custodian": "custodian",
    "signoff_delegate": "signoffDelegate",
}


def _contact_for_user(contact_id: str, user: dict, require_manage: bool = False) -> dict:
    contact = REPOSITORY.get_contact(contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    _company_for_user(contact["companyId"], user, require_manage=require_manage)
    return contact


def _contact_values(
    payload: ContactCreateRequest | ContactPatchRequest, current: dict | None = None
) -> dict:
    values = {**(current or {}), **payload.model_dump(exclude_unset=True)}
    values.pop("reason", None)
    values["displayName"] = str(values.get("displayName") or "").strip()
    if not values["displayName"]:
        raise HTTPException(400, "Enter a contact name")
    values["email"] = str(values.get("email") or "").strip().lower()
    if values["email"] and "@" not in values["email"]:
        raise HTTPException(400, "Enter a valid contact email address")
    values["status"] = values.get("status") or "active"
    if values["status"] not in CONTACT_STATUSES:
        raise HTTPException(400, "Choose a valid contact status")
    for field in (
        "firstName",
        "lastName",
        "phone",
        "mobile",
        "jobTitle",
        "department",
        "location",
        "timezone",
    ):
        values[field] = str(values.get(field) or "").strip()
    return values


def _validate_contact_assignments(
    company_id: str, assignments: list[ResponsibilityInput]
) -> list[dict]:
    contacts = {item["id"]: item for item in REPOSITORY.list_contacts(company_id)}
    normalized = []
    seen: set[tuple[str, str]] = set()
    primaries: set[str] = set()
    for assignment in assignments:
        item = assignment.model_dump()
        if item["role"] not in RESPONSIBILITY_ROLES:
            raise HTTPException(400, "Choose a valid responsibility role")
        contact = contacts.get(item["contactId"])
        if not contact:
            raise HTTPException(400, "Choose contacts belonging to this customer")
        if contact["status"] in {"left_company", "inactive"}:
            raise HTTPException(400, f"{contact['displayName']} is not an active contact")
        key = (item["role"], item["contactId"])
        if key in seen:
            raise HTTPException(400, "A contact can only hold each responsibility once")
        if item["isPrimary"] and item["role"] in primaries:
            raise HTTPException(400, "Choose only one primary contact for each responsibility")
        seen.add(key)
        if item["isPrimary"]:
            primaries.add(item["role"])
        normalized.append(item)
    return normalized


def _responsibility_metadata(
    metadata: dict | None, company_id: str, assignments: list[dict]
) -> dict:
    result = dict(metadata or {})
    for field in RESPONSIBILITY_METADATA_FIELDS.values():
        result[field] = ""
    contacts = {item["id"]: item for item in REPOSITORY.list_contacts(company_id)}
    ordered = sorted(
        assignments,
        key=lambda item: (
            not item.get("isPrimary", True),
            int(item.get("escalationOrder", 1)),
        ),
    )
    for assignment in ordered:
        metadata_field = RESPONSIBILITY_METADATA_FIELDS.get(assignment["role"])
        contact = contacts.get(assignment["contactId"])
        if metadata_field and contact and not result.get(metadata_field):
            result[metadata_field] = contact["displayName"]
    return result


@api.get("/api/contacts", tags=["contacts"])
def list_contacts(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP contact directory requires a root role",
        )
    return [
        item
        for item in REPOSITORY.list_contacts(companyId)
        if core.allowed(user, item["companyId"])
    ]


@api.get("/api/contacts/{contact_id}", tags=["contacts"])
def get_contact(contact_id: str, request: Request) -> dict:
    contact = _contact_for_user(contact_id, current_user(request))
    return {
        **contact,
        "responsibilities": REPOSITORY.list_contact_responsibilities(
            contact["companyId"],
            contact_id=contact_id,
            include_inactive=True,
        ),
    }


@api.post("/api/contacts", status_code=201, tags=["contacts"])
def create_contact(payload: ContactCreateRequest, request: Request) -> dict:
    actor = current_user(request)
    _company_for_user(payload.companyId, actor, require_manage=True)
    values = _contact_values(payload)
    if values["email"] and any(
        item["email"].lower() == values["email"]
        for item in REPOSITORY.list_contacts(payload.companyId)
    ):
        raise HTTPException(409, "A contact with this email already exists for the customer")
    if values.get("managerContactId"):
        manager = _contact_for_user(values["managerContactId"], actor)
        if manager["companyId"] != payload.companyId:
            raise HTTPException(400, "Choose a manager from the same customer")
    contact = {
        "id": str(uuid.uuid4()),
        "companyId": payload.companyId,
        "linkedUserId": None,
        **values,
        "source": "manual",
        "syncStatus": "not_synced",
        "lastSeen": core.now(),
        "lastSynced": None,
    }
    with core.LOCK:
        return REPOSITORY.create_contact(contact, actor["id"])


@api.patch("/api/contacts/{contact_id}", tags=["contacts"])
def update_contact(contact_id: str, payload: ContactPatchRequest, request: Request) -> dict:
    actor = current_user(request)
    current = _contact_for_user(contact_id, actor, require_manage=True)
    values = _contact_values(payload, current)
    if values["email"] and any(
        item["id"] != contact_id and item["email"].lower() == values["email"]
        for item in REPOSITORY.list_contacts(current["companyId"])
    ):
        raise HTTPException(409, "A contact with this email already exists for the customer")
    if values.get("managerContactId"):
        manager = _contact_for_user(values["managerContactId"], actor)
        if manager["companyId"] != current["companyId"] or manager["id"] == contact_id:
            raise HTTPException(400, "Choose another contact from the same customer as manager")
    if values["status"] in {"left_company", "inactive"} and len(payload.reason.strip()) < 4:
        raise HTTPException(400, "Enter a reason when offboarding or deactivating a contact")
    if (
        values["status"] in {"left_company", "inactive"}
        and current.get("responsibilityCount", 0) > 0
    ):
        raise HTTPException(
            409, "Reassign this contact's active responsibilities before offboarding"
        )
    values.pop("responsibilityCount", None)
    values.pop("portalUser", None)
    with core.LOCK:
        if values["status"] in {"left_company", "inactive"} and current.get("linkedUserId"):
            REPOSITORY.set_user_status(
                current["linkedUserId"],
                "disabled",
                actor["id"],
                reason=payload.reason.strip(),
            )
        stored = REPOSITORY.update_contact(
            contact_id, values, actor["id"], reason=payload.reason.strip()
        )
    if not stored:
        raise HTTPException(404, "Contact not found")
    return stored


@api.post("/api/contacts/{contact_id}/reassign", tags=["contacts"])
def reassign_contact(contact_id: str, payload: ContactReassignRequest, request: Request) -> dict:
    actor = current_user(request)
    current = _contact_for_user(contact_id, actor, require_manage=True)
    replacement = _contact_for_user(payload.replacementContactId, actor, require_manage=True)
    if replacement["companyId"] != current["companyId"] or replacement["id"] == current["id"]:
        raise HTTPException(400, "Choose another active contact from the same customer")
    if replacement["status"] not in {"active", "on_leave"}:
        raise HTTPException(400, "Choose an active replacement contact")
    affected = REPOSITORY.list_contact_responsibilities(current["companyId"], contact_id=contact_id)
    asset_ids = sorted({item["assetId"] for item in affected})
    transferred = 0
    with core.LOCK:
        for asset_id in asset_ids:
            assignments = REPOSITORY.list_contact_responsibilities(
                current["companyId"], asset_id=asset_id
            )
            existing_replacement_roles = {
                item["role"] for item in assignments if item["contactId"] == replacement["id"]
            }
            revised = []
            for item in assignments:
                assignment = {
                    "contactId": item["contactId"],
                    "role": item["role"],
                    "isPrimary": item["isPrimary"],
                    "escalationOrder": item["escalationOrder"],
                    "notes": item.get("notes", ""),
                    "source": "manual",
                }
                if item["contactId"] == current["id"]:
                    transferred += 1
                    if item["role"] in existing_replacement_roles:
                        continue
                    assignment["contactId"] = replacement["id"]
                revised.append(assignment)
            REPOSITORY.replace_asset_responsibilities(
                asset_id,
                revised,
                current["companyId"],
                actor["id"],
                reason=payload.reason.strip(),
            )
    return {
        "contact": REPOSITORY.get_contact(contact_id),
        "replacement": REPOSITORY.get_contact(replacement["id"]),
        "transferred": transferred,
        "assets": len(asset_ids),
    }


@api.post("/api/contacts/{contact_id}/portal-user", status_code=201, tags=["contacts"])
def create_contact_portal_user(
    contact_id: str, payload: PortalUserCreateRequest, request: Request
) -> dict:
    actor = current_user(request)
    contact = _contact_for_user(contact_id, actor, require_manage=True)
    if contact.get("linkedUserId"):
        raise HTTPException(409, "This contact already has portal access")
    if contact["status"] not in {"active", "on_leave"}:
        raise HTTPException(409, "Portal access cannot be created for an inactive contact")
    if not contact.get("email"):
        raise HTTPException(400, "Add an email address before creating portal access")
    existing = next(
        (
            item
            for item in REPOSITORY.list_users()
            if item["email"].lower() == contact["email"].lower()
        ),
        None,
    )
    if existing:
        if not core.allowed(existing, contact["companyId"]):
            raise HTTPException(
                409,
                "An existing account with this email does not have access to this customer",
            )
        user = existing
    else:
        user = {
            "id": str(uuid.uuid4()),
            "email": contact["email"],
            "role": "client_reader",
            "companyIds": [contact["companyId"]],
            "groupIds": [],
            "accountType": "customer",
        }
        with core.LOCK:
            user = REPOSITORY.create_user(user, payload.password, actor["id"])
    with core.LOCK:
        stored = REPOSITORY.update_contact(
            contact_id,
            {"linkedUserId": user["id"]},
            actor["id"],
            reason="Portal access linked",
        )
    if stored is None:
        raise HTTPException(404, "Contact not found")
    return stored


@api.get("/api/contact-responsibilities", tags=["contacts"])
def list_contact_responsibilities(
    request: Request,
    companyId: str | None = None,
    contactId: str | None = None,
    assetId: str | None = None,
    includeInactive: bool = False,
) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP responsibility view requires a root role",
        )
    return [
        item
        for item in REPOSITORY.list_contact_responsibilities(
            companyId,
            contact_id=contactId,
            asset_id=assetId,
            include_inactive=includeInactive,
        )
        if core.allowed(user, item["companyId"])
    ]


def _validated_colour(value: str, fallback: str) -> str:
    return value.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", value or "") else fallback


def _validated_logo_data_url(value: str) -> str:
    if not value:
        return ""
    match = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=\r\n]+)", value)
    if not match:
        raise HTTPException(400, "Logo must be a PNG or JPEG image")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except ValueError as error:
        raise HTTPException(400, "Logo data is invalid") from error
    if len(raw) > 1_000_000:
        raise HTTPException(400, "Logo must be 1 MB or smaller")
    return f"data:{match.group(1)};base64,{match.group(2)}"


@api.get("/api/branding/public", tags=["branding"])
def get_public_branding() -> dict:
    """Return the non-sensitive MSP identity used before login."""
    return REPOSITORY.get_msp_branding()


@api.get("/api/branding", tags=["branding"])
def get_branding(request: Request, scope: str | None = None, companyId: str | None = None) -> dict:
    user = current_user(request)
    if scope == "msp":
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP branding requires root or MSP role",
        )
        return REPOSITORY.get_msp_branding()
    if companyId:
        _company_for_user(companyId, user)
        return REPOSITORY.get_company_branding(companyId)
    return {
        key: value
        for key, value in REPOSITORY.list_company_branding().items()
        if core.allowed(user, key)
    }


@api.put("/api/branding", tags=["branding"])
def update_branding(payload: BrandingRequest, request: Request) -> dict:
    user = current_user(request)
    if payload.scope == "msp":
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP branding requires root or MSP role",
        )
        brand = {
            "name": (payload.name or "CMDB Hub")[:80],
            "accent": _validated_colour(payload.accent, "#50d5b9"),
            "secondaryAccent": _validated_colour(payload.secondaryAccent, "#7997ff"),
            "logoText": (payload.logoText or "C").upper()[:3],
            "logoDataUrl": _validated_logo_data_url(payload.logoDataUrl),
            "logoFileName": payload.logoFileName[:180] if payload.logoDataUrl else "",
            "supportEmail": payload.supportEmail.strip()[:160],
            "supportUrl": payload.supportUrl.strip()[:300],
            "supportPhone": payload.supportPhone.strip()[:80],
            "welcomeMessage": payload.welcomeMessage.strip()[:180],
            "reportFooter": payload.reportFooter.strip()[:180],
            "confidentialityLabel": payload.confidentialityLabel.strip()[:80],
        }
        with core.LOCK:
            return REPOSITORY.update_msp_branding(brand, user["id"])
    if not payload.companyId:
        raise HTTPException(400, "Choose a customer")
    company = _company_for_user(payload.companyId, user, require_manage=True)
    brand = {
        "name": (payload.name or company["name"])[:80],
        "accent": _validated_colour(payload.accent, "#50d5b9"),
        "secondaryAccent": _validated_colour(payload.secondaryAccent, "#7997ff"),
        "logoText": (payload.logoText or company["name"][0]).upper()[:3],
        "logoDataUrl": _validated_logo_data_url(payload.logoDataUrl),
        "logoFileName": payload.logoFileName[:180] if payload.logoDataUrl else "",
    }
    with core.LOCK:
        return REPOSITORY.update_company_branding(payload.companyId, brand, user["id"])


def _email_connection_for_delivery() -> tuple[dict, str]:
    connection = REPOSITORY.get_email_connection()
    client_secret = ""
    if connection.get("authMode") == "client_secret":
        encrypted = str(connection.get("clientSecretEncrypted") or "")
        nonce = str(connection.get("clientSecretNonce") or "")
        if encrypted and nonce:
            try:
                client_secret = decrypt_secret(encrypted, nonce, "email:msp")
            except MfaConfigurationError as error:
                raise EmailConfigurationError(
                    "Stored email credentials cannot be decrypted by this installation"
                ) from error
    return connection, client_secret


def _deliver_claimed_email(message: dict, actor_id: str | None = None) -> dict:
    """Deliver a previously claimed message and schedule a bounded retry on failure."""

    message_id = message["id"]
    connection, client_secret = _email_connection_for_delivery()
    attempts = int(message.get("attempts") or 0)
    try:
        result = EMAIL_SENDER.send(connection, message, client_secret=client_secret)
    except (EmailConfigurationError, EmailDeliveryError) as error:
        failure = str(error)[:500]
        terminal = attempts >= int(message.get("maxAttempts") or 5)
        status = "dead_letter" if terminal else "failed"
        delay_minutes = min(5 * (2 ** max(attempts - 1, 0)), 360)
        retry_at = (datetime.now(UTC) + timedelta(minutes=delay_minutes)).isoformat()
        REPOSITORY.update_email_outbox(
            message_id,
            {
                "status": status,
                "attempts": attempts,
                "lastError": failure,
                "nextAttemptAt": retry_at,
            },
            actor_id,
        )
        REPOSITORY.update_notification_event_for_outbox(message_id, status, actor_id)
        REPOSITORY.update_email_connection(
            {**connection, "status": "error", "lastError": failure}, actor_id
        )
        raise HTTPException(502, failure) from error
    accepted_at = core.now()
    delivered = REPOSITORY.update_email_outbox(
        message_id,
        {
            "status": "accepted",
            "attempts": attempts,
            "acceptedAt": accepted_at,
            "lastError": "",
            "providerRequestId": result.provider_request_id,
        },
        actor_id,
    )
    REPOSITORY.update_notification_event_for_outbox(message_id, "accepted", actor_id)
    REPOSITORY.update_email_connection(
        {
            **connection,
            "status": "verified",
            "lastTestAt": accepted_at,
            "lastError": "",
        },
        actor_id,
    )
    return delivered or {}


def _attempt_email_delivery(message_id: str, actor_id: str | None) -> dict:
    """Atomically claim and synchronously deliver one selected outbox item."""

    if not REPOSITORY.get_email_outbox(message_id):
        raise HTTPException(404, "Email outbox item not found")
    message = REPOSITORY.claim_email_outbox(message_id)
    if not message:
        raise HTTPException(409, "Email is not due or has reached its retry limit")
    return _deliver_claimed_email(message, actor_id)


def _rule_is_due(rule: dict, now: datetime) -> bool:
    """Return whether a rule's cadence allows another evaluation."""

    last_run = rule.get("lastRunAt")
    if not last_run:
        return True
    try:
        parsed = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
    except ValueError:
        return True
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=UTC)
    minimum = {
        "immediate": timedelta(minutes=1),
        "daily": timedelta(hours=20),
        "weekly": timedelta(days=6),
    }.get(str(rule.get("cadence")), timedelta(days=1))
    return now - parsed >= minimum


def _notification_template_for(
    templates: list[dict], template_key: str, company_id: str
) -> dict | None:
    """Prefer a customer override before falling back to the global template."""

    matching = [
        item for item in templates if item.get("key") == template_key and item.get("enabled", True)
    ]
    return next(
        (item for item in matching if item.get("companyId") == company_id),
        next((item for item in matching if not item.get("companyId")), None),
    )


def _run_notification_scan(
    actor_id: str | None = None,
    company_id: str | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Evaluate due rules, resolve owners, and enqueue idempotent messages."""

    now = datetime.now(UTC)
    rules = REPOSITORY.list_notification_rules()
    templates = REPOSITORY.list_notification_templates()
    companies = REPOSITORY.list_companies()
    assets = REPOSITORY.list_assets()
    changes = REPOSITORY.list_changes()
    responsibilities = REPOSITORY.list_contact_responsibilities(include_inactive=False)
    preferences = REPOSITORY.list_notification_preferences()
    summary = {
        "rulesEvaluated": 0,
        "candidates": 0,
        "queued": 0,
        "missingRecipients": 0,
        "duplicates": 0,
        "missingTemplates": 0,
    }
    for rule in rules:
        if not rule.get("enabled"):
            continue
        if company_id and rule.get("companyId") not in {None, company_id}:
            continue
        if not force and not _rule_is_due(rule, now):
            continue
        scoped_rule = {**rule, "companyId": rule.get("companyId") or company_id}
        candidates = notification_candidates(scoped_rule, assets, changes, companies)
        summary["rulesEvaluated"] += 1
        summary["candidates"] += len(candidates)
        for candidate in candidates:
            if company_id and candidate["companyId"] != company_id:
                continue
            dedupe_key = notification_dedupe_key(rule, candidate)
            event = REPOSITORY.create_notification_event(
                {
                    "companyId": candidate["companyId"],
                    "ruleId": rule["id"],
                    "eventType": candidate["eventType"],
                    "entityType": candidate["entityType"],
                    "entityId": candidate["entityId"],
                    "entityName": candidate["entityName"],
                    "dedupeKey": dedupe_key,
                    "context": candidate["context"],
                },
                actor_id,
            )
            if event.get("status") not in {"pending", "missing_recipient"}:
                summary["duplicates"] += 1
                continue
            recipients, missing_roles = resolve_notification_recipients(
                rule,
                candidate,
                responsibilities,
                [item for item in preferences if item.get("companyId") == candidate["companyId"]],
            )
            if not recipients:
                REPOSITORY.update_notification_event(
                    event["id"],
                    {
                        "status": "missing_recipient",
                        "recipients": [],
                        "missingRoles": missing_roles,
                        "context": candidate["context"],
                    },
                    actor_id,
                )
                summary["missingRecipients"] += 1
                continue
            template = _notification_template_for(
                templates, str(rule.get("templateKey") or ""), candidate["companyId"]
            )
            if not template:
                summary["missingTemplates"] += 1
                continue
            rendered = render_notification_template(template, candidate["context"])
            queued = REPOSITORY.create_email_outbox(
                {
                    "companyId": candidate["companyId"],
                    "idempotencyKey": f"notification:{dedupe_key}",
                    "to": recipients,
                    "subject": rendered["subject"],
                    "bodyHtml": rendered["bodyHtml"],
                    "bodyText": rendered["bodyText"],
                    "templateKey": template["key"],
                    "templateVersion": template.get("version", 1),
                    "maxAttempts": rule.get("maxAttempts", 5),
                },
                actor_id,
            )
            REPOSITORY.update_notification_event(
                event["id"],
                {
                    "status": "queued",
                    "recipients": recipients,
                    "missingRoles": missing_roles,
                    "context": candidate["context"],
                    "emailOutboxId": queued["id"],
                },
                actor_id,
            )
            summary["queued"] += 1
        REPOSITORY.mark_notification_rule_run(rule["id"], now.isoformat())
    return summary


def _process_notification_outbox(limit: int = 20) -> dict[str, int]:
    """Drain a bounded number of due messages without stopping on one failure."""

    summary = {"processed": 0, "accepted": 0, "failed": 0, "deadLetter": 0}
    for _index in range(max(1, min(limit, 100))):
        message = REPOSITORY.claim_email_outbox()
        if not message:
            break
        summary["processed"] += 1
        try:
            delivered = _deliver_claimed_email(message)
            summary["accepted" if delivered.get("status") == "accepted" else "failed"] += 1
        except HTTPException:
            current = REPOSITORY.get_email_outbox(message["id"]) or {}
            key = "deadLetter" if current.get("status") == "dead_letter" else "failed"
            summary[key] += 1
    return summary


@api.get("/api/email/config", tags=["email"])
def get_email_configuration(request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email configuration requires platform admin access")
    return public_email_connection(REPOSITORY.get_email_connection())


@api.put("/api/email/config", tags=["email"])
def update_email_configuration(payload: EmailConfigurationRequest, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email configuration requires platform admin access")
    current = REPOSITORY.get_email_connection()
    if payload.expectedRevision and payload.expectedRevision != int(current.get("revision") or 1):
        raise HTTPException(409, "Email settings changed. Reload them before saving.")
    sender = payload.senderAddress.strip().lower()
    reply_to = payload.replyTo.strip().lower()
    if sender and not valid_email_address(sender):
        raise HTTPException(400, "Enter a valid sender mailbox")
    if reply_to and not valid_email_address(reply_to):
        raise HTTPException(400, "Enter a valid reply-to address")
    if payload.authMode in {"client_secret", "certificate"} and (
        not payload.tenantId.strip() or not payload.clientId.strip()
    ):
        raise HTTPException(400, "Tenant ID and application client ID are required")
    encrypted = current.get("clientSecretEncrypted", "")
    nonce = current.get("clientSecretNonce", "")
    if payload.clientSecret:
        try:
            encrypted, nonce = encrypt_secret(payload.clientSecret, "email:msp")
        except MfaConfigurationError as error:
            raise HTTPException(
                503,
                "Configure MFA_ENCRYPTION_KEY before storing an application secret",
            ) from error
    if payload.authMode == "client_secret" and not encrypted:
        raise HTTPException(400, "Enter a client secret before selecting client-secret mode")
    ready = bool(sender) and (
        payload.authMode == "managed_identity"
        or bool(payload.tenantId.strip() and payload.clientId.strip())
    )
    connection = {
        "enabled": payload.enabled,
        "authMode": payload.authMode,
        "tenantId": payload.tenantId.strip(),
        "clientId": payload.clientId.strip(),
        "servicePrincipalObjectId": payload.servicePrincipalObjectId.strip(),
        "managedIdentityClientId": payload.managedIdentityClientId.strip(),
        "senderAddress": sender,
        "senderName": payload.senderName.strip(),
        "replyTo": reply_to,
        "graphBaseUrl": "https://graph.microsoft.com/v1.0",
        "clientSecretEncrypted": encrypted,
        "clientSecretNonce": nonce,
        "status": "configured"
        if payload.enabled and ready
        else "disabled"
        if not payload.enabled
        else "not_configured",
        "lastError": "",
    }
    with core.LOCK:
        stored = REPOSITORY.update_email_connection(connection, user["id"])
    return public_email_connection(stored)


@api.post("/api/email/test", tags=["email"])
def send_email_test(payload: EmailTestRequest, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email testing requires platform admin access")
    recipient = payload.recipient.strip().lower()
    if not valid_email_address(recipient):
        raise HTTPException(400, "Enter a valid test recipient")
    brand = REPOSITORY.get_msp_branding()
    identifier = f"email-test:{user['id']}:{uuid.uuid4()}"
    message = {
        "idempotencyKey": identifier,
        "to": [recipient],
        "subject": payload.subject.strip() or "CMDB Hub Microsoft 365 email test",
        "bodyHtml": (
            f"<h2>{brand.get('name', 'CMDB Hub')} email is ready</h2>"
            "<p>This test message was explicitly requested by a platform administrator.</p>"
            f"<p>Request time: {core.now()}</p>"
        ),
        "bodyText": "Microsoft 365 email delivery is ready.",
        "templateKey": "platform_email_test",
        "templateVersion": 1,
    }
    with core.LOCK:
        queued = REPOSITORY.create_email_outbox(message, user["id"])
    return _attempt_email_delivery(queued["id"], user["id"])


@api.get("/api/email/outbox", tags=["email"])
def list_email_outbox(request: Request, limit: int = 100) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email delivery history requires admin access")
    return REPOSITORY.list_email_outbox(limit)


@api.post("/api/email/outbox/{message_id}/retry", tags=["email"])
def retry_email_outbox(message_id: str, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email retry requires platform admin access")
    message = REPOSITORY.get_email_outbox(message_id)
    if not message:
        raise HTTPException(404, "Email outbox item not found")
    if message.get("status") not in {"failed", "queued"}:
        raise HTTPException(409, "Only queued or failed messages can be retried")
    if int(message.get("attempts") or 0) >= int(message.get("maxAttempts") or 5):
        raise HTTPException(409, "This message reached its retry limit")
    REPOSITORY.update_email_outbox(
        message_id,
        {"status": "queued", "nextAttemptAt": core.now(), "lastError": ""},
        user["id"],
    )
    return _attempt_email_delivery(message_id, user["id"])


@api.post("/api/email/outbox/{message_id}/requeue", tags=["email"])
def requeue_dead_letter(message_id: str, request: Request) -> dict:
    """Reset a dead-letter message after an administrator resolves its cause."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email requeue requires platform admin access")
    message = REPOSITORY.get_email_outbox(message_id)
    if not message:
        raise HTTPException(404, "Email outbox item not found")
    if message.get("status") != "dead_letter":
        raise HTTPException(409, "Only dead-letter messages can be requeued")
    stored = REPOSITORY.update_email_outbox(
        message_id,
        {
            "status": "queued",
            "attempts": 0,
            "nextAttemptAt": core.now(),
            "lastError": "",
        },
        user["id"],
    )
    REPOSITORY.update_notification_event_for_outbox(message_id, "queued", user["id"])
    return stored or {}


NOTIFICATION_RECIPIENT_ROLES = {
    "business_owner",
    "service_owner",
    "technical_owner",
    "custodian",
    "change_approver",
    "signoff_delegate",
    "support_contact",
}
NOTIFICATION_EVENT_TYPES = {
    "asset_renewal",
    "asset_eol",
    "change_approval",
    "missing_owner",
}


@api.get("/api/notifications/status", tags=["notifications"])
def get_notification_status(request: Request) -> dict:
    """Summarize queue health and worker configuration for MSP administrators."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notifications require platform admin access")
    outbox = REPOSITORY.list_email_outbox(500)
    events = REPOSITORY.list_notification_events(limit=500)
    return {
        "workerEnabled": os.getenv("NOTIFICATION_WORKER_ENABLED", "false").lower()
        in {"1", "true", "yes"},
        "workerIntervalSeconds": _notification_worker_interval(),
        "email": public_email_connection(REPOSITORY.get_email_connection()),
        "queued": sum(item.get("status") == "queued" for item in outbox),
        "failed": sum(item.get("status") == "failed" for item in outbox),
        "deadLetter": sum(item.get("status") == "dead_letter" for item in outbox),
        "missingRecipients": sum(item.get("status") == "missing_recipient" for item in events),
    }


@api.get("/api/notifications/rules", tags=["notifications"])
def list_notification_rules(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification rules require admin access")
    return REPOSITORY.list_notification_rules()


@api.patch("/api/notifications/rules/{rule_id}", tags=["notifications"])
def update_notification_rule(
    rule_id: str, payload: NotificationRuleRequest, request: Request
) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification rules require admin access")
    current = next(
        (item for item in REPOSITORY.list_notification_rules() if item["id"] == rule_id),
        None,
    )
    if not current:
        raise HTTPException(404, "Notification rule not found")
    if payload.expectedRevision and payload.expectedRevision != current.get("revision"):
        raise HTTPException(409, "Notification rule changed. Reload it before saving.")
    unknown_roles = set(payload.recipientRoles) - NOTIFICATION_RECIPIENT_ROLES
    if unknown_roles:
        raise HTTPException(400, f"Unknown recipient role: {sorted(unknown_roles)[0]}")
    fallbacks = list(dict.fromkeys(item.strip().lower() for item in payload.fallbackAddresses))
    if any(not valid_email_address(item) for item in fallbacks):
        raise HTTPException(400, "Fallback recipients must be valid email addresses")
    templates = REPOSITORY.list_notification_templates()
    if not any(item.get("key") == payload.templateKey for item in templates):
        raise HTTPException(400, "Choose an existing notification template")
    stored = REPOSITORY.update_notification_rule(
        rule_id,
        {
            "name": payload.name.strip(),
            "enabled": payload.enabled,
            "leadDays": payload.leadDays,
            "cadence": payload.cadence,
            "recipientRoles": list(dict.fromkeys(payload.recipientRoles)),
            "fallbackAddresses": fallbacks,
            "templateKey": payload.templateKey,
            "maxAttempts": payload.maxAttempts,
        },
        user["id"],
    )
    return stored or {}


@api.get("/api/notifications/templates", tags=["notifications"])
def list_notification_templates(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification templates require admin access")
    return REPOSITORY.list_notification_templates()


@api.patch("/api/notifications/templates/{template_id}", tags=["notifications"])
def update_notification_template(
    template_id: str, payload: NotificationTemplateRequest, request: Request
) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification templates require admin access")
    current = next(
        (item for item in REPOSITORY.list_notification_templates() if item["id"] == template_id),
        None,
    )
    if not current:
        raise HTTPException(404, "Notification template not found")
    if payload.expectedVersion and payload.expectedVersion != current.get("version"):
        raise HTTPException(409, "Notification template changed. Reload it before saving.")
    unsafe = re.compile(r"<(script|iframe|object)|javascript\s*:", re.IGNORECASE)
    if unsafe.search(payload.htmlTemplate):
        raise HTTPException(400, "Template contains unsafe active content")
    stored = REPOSITORY.update_notification_template(
        template_id,
        {
            "name": payload.name.strip(),
            "subjectTemplate": payload.subjectTemplate.strip(),
            "htmlTemplate": payload.htmlTemplate.strip(),
            "textTemplate": payload.textTemplate.strip(),
            "enabled": payload.enabled,
        },
        user["id"],
    )
    return stored or {}


@api.get("/api/notifications/preferences", tags=["notifications"])
def list_notification_preferences(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification preferences require admin access")
    if companyId:
        _company(companyId)
    return REPOSITORY.list_notification_preferences(companyId)


@api.put("/api/notifications/preferences", tags=["notifications"])
def update_notification_preference(
    payload: NotificationPreferenceRequest, request: Request
) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification preferences require admin access")
    _company(payload.companyId)
    if bool(payload.contactId) == bool(payload.userId):
        raise HTTPException(400, "Choose either one contact or one portal user")
    if payload.contactId:
        contact = REPOSITORY.get_contact(payload.contactId)
        if not contact or contact.get("companyId") != payload.companyId:
            raise HTTPException(400, "Contact does not belong to this customer")
    if payload.userId and not any(
        item["id"] == payload.userId for item in REPOSITORY.list_users(include_inactive=True)
    ):
        raise HTTPException(400, "Portal user not found")
    event_types = list(dict.fromkeys(payload.eventTypes or ["*"]))
    if set(event_types) - ({"*"} | NOTIFICATION_EVENT_TYPES):
        raise HTTPException(400, "Notification preference contains an unknown event type")
    if payload.digestMode != "instant":
        raise HTTPException(400, "Digest delivery is reserved but not enabled in this release")
    return REPOSITORY.upsert_notification_preference(
        {
            "companyId": payload.companyId,
            "contactId": payload.contactId,
            "userId": payload.userId,
            "emailEnabled": payload.emailEnabled,
            "eventTypes": event_types,
            "digestMode": payload.digestMode,
        },
        user["id"],
    )


@api.get("/api/notifications/events", tags=["notifications"])
def list_notification_events(
    request: Request, companyId: str | None = None, limit: int = 200
) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification evidence requires admin access")
    if companyId:
        _company(companyId)
    return REPOSITORY.list_notification_events(companyId, limit)


@api.post("/api/notifications/run", tags=["notifications"])
def run_notifications(request: Request, companyId: str | None = None) -> dict:
    """Evaluate every enabled rule now; this queues but does not send email."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification execution requires admin access")
    if companyId:
        _company(companyId)
    return _run_notification_scan(user["id"], companyId, force=True)


@api.post("/api/notifications/process", tags=["notifications"])
def process_notifications(request: Request, limit: int = 20) -> dict:
    """Explicitly send a bounded batch of due outbox messages."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Notification delivery requires admin access")
    return _process_notification_outbox(limit)


@api.get("/api/email/exchange-rbac-script", tags=["email"])
def get_exchange_rbac_script(request: Request) -> Response:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email setup requires platform admin access")
    script = exchange_rbac_script(REPOSITORY.get_email_connection())
    return Response(
        script,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=configure-cmdb-exchange-rbac.ps1"},
    )


@api.post("/api/email/setup-script", tags=["email"])
def generate_email_setup_script(payload: EmailSetupScriptRequest, request: Request) -> Response:
    """Return a tailored, secret-free Microsoft 365 administrator script."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Email setup requires platform admin access")
    sender = payload.senderAddress.strip().lower()
    if not valid_email_address(sender):
        raise HTTPException(400, "Enter a valid sender mailbox")
    script = exchange_rbac_script(
        {
            "authMode": payload.authMode,
            "tenantId": payload.tenantId.strip(),
            "clientId": payload.clientId.strip(),
            "servicePrincipalObjectId": payload.servicePrincipalObjectId.strip(),
            "senderAddress": sender,
            "senderName": payload.senderName.strip() or "IPT CMDB",
        },
        create_shared_mailbox=payload.createSharedMailbox,
    )
    return Response(
        script,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=setup-ipt-cmdb-email.ps1"},
    )


def _asset_for_user(asset_id: str, user: dict, require_manage: bool = False) -> dict:
    asset = REPOSITORY.get_asset(asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    _company_for_user(asset["companyId"], user, require_manage=require_manage)
    return asset


@api.get("/api/assets", tags=["assets"])
def list_assets(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    return [
        core.asset_view(item)
        for item in REPOSITORY.list_assets()
        if (not companyId or item["companyId"] == companyId)
        and core.allowed(user, item["companyId"])
    ]


@api.get("/api/v2/assets/{asset_id}", tags=["assets"])
@api.get("/api/assets/{asset_id}", tags=["assets"])
def get_asset(asset_id: str, request: Request) -> dict:
    return core.asset_view(_asset_for_user(asset_id, current_user(request)))


@api.post("/api/assets", status_code=201, tags=["assets"])
def create_asset(payload: AssetCreateRequest, request: Request) -> dict:
    user = current_user(request)
    _company_for_user(payload.companyId, user, require_manage=True)
    assignments = _validate_contact_assignments(payload.companyId, payload.responsibilities)
    try:
        metadata = core.normalise_metadata(
            _responsibility_metadata(payload.metadata, payload.companyId, assignments),
            payload.status,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    asset = {
        "id": str(uuid.uuid4()),
        "companyId": payload.companyId,
        "name": payload.name,
        "type": payload.type,
        "status": payload.status,
        "source": "manual",
        "externalId": None,
        "lastSeen": core.now(),
        "fields": payload.fields,
        "metadata": metadata,
    }
    with core.LOCK:
        stored = REPOSITORY.create_asset(asset, user["id"])
        if assignments:
            REPOSITORY.replace_asset_responsibilities(
                stored["id"],
                assignments,
                payload.companyId,
                user["id"],
                reason="Initial ownership assigned",
            )
    return core.asset_view(REPOSITORY.get_asset(stored["id"]) or stored)


@api.patch("/api/assets/{asset_id}", tags=["assets"])
def update_asset(asset_id: str, payload: AssetPatchRequest, request: Request) -> dict:
    user = current_user(request)
    asset = _asset_for_user(asset_id, user, require_manage=True)
    changes = payload.model_dump(exclude_unset=True)
    raw_assignments = changes.pop("responsibilities", None)
    assignments = (
        _validate_contact_assignments(asset["companyId"], payload.responsibilities or [])
        if raw_assignments is not None
        else None
    )
    if assignments is not None and "metadata" not in changes:
        changes["metadata"] = _responsibility_metadata(
            asset.get("metadata"), asset["companyId"], assignments
        )
    if "metadata" in changes:
        try:
            changes["metadata"] = core.normalise_metadata(
                _responsibility_metadata(changes["metadata"], asset["companyId"], assignments)
                if assignments is not None
                else changes["metadata"],
                changes.get("status", asset.get("status")),
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    with core.LOCK:
        updated = REPOSITORY.update_asset(asset_id, changes, user["id"])
        if updated and assignments is not None:
            REPOSITORY.replace_asset_responsibilities(
                asset_id,
                assignments,
                asset["companyId"],
                user["id"],
                reason="Ownership updated from CI editor",
            )
    if not updated:
        raise HTTPException(404, "Asset not found")
    return core.asset_view(REPOSITORY.get_asset(asset_id) or updated)


@api.post("/api/demo-data", tags=["assets"])
def demo_data(request: Request) -> dict:
    user = current_user(request)
    _company_for_user("acme", user)
    _require_role(user, {"platform_admin"}, "Demo data requires platform admin role")
    existing = REPOSITORY.list_assets()
    by_name = {(item["companyId"], item["name"]): item for item in existing}
    legacy_to_canonical: dict[str, str] = {}
    assets_added = 0
    for item in core.DEMO_ASSETS:
        current = by_name.get((item["companyId"], item["name"]))
        if current:
            legacy_to_canonical[item["id"]] = current["id"]
            # Re-running the explicit demo seed upgrades demo-owned records as
            # the sample model evolves, without touching imported/manual CIs.
            if current.get("source") == "demo":
                desired_metadata = core.normalise_metadata(
                    {**(current.get("metadata") or {}), **(item.get("metadata") or {})},
                    item.get("status"),
                )
                changes = {}
                if current.get("type") != item.get("type"):
                    changes["type"] = item["type"]
                if current.get("fields") != item.get("fields"):
                    changes["fields"] = item.get("fields") or {}
                if current.get("metadata") != desired_metadata:
                    changes["metadata"] = desired_metadata
                if changes:
                    REPOSITORY.update_asset(current["id"], changes, user["id"])
            continue
        asset = {
            **item,
            "id": canonical_uuid("configuration_item", item["id"]),
            "metadata": core.normalise_metadata(item.get("metadata"), item.get("status")),
        }
        stored = REPOSITORY.create_asset(asset, user["id"])
        legacy_to_canonical[item["id"]] = stored["id"]
        assets_added += 1
    relationships_added = 0
    relationships = REPOSITORY.list_relationships()
    for item in core.DEMO_RELATIONSHIPS:
        from_id = legacy_to_canonical.get(item["fromId"])
        to_id = legacy_to_canonical.get(item["toId"])
        if not from_id or not to_id:
            continue
        if core.relationship_exists(relationships, from_id, to_id, item["type"]):
            continue
        stored = REPOSITORY.create_relationship(
            {
                **item,
                "id": canonical_uuid("relationship", item["id"]),
                "fromId": from_id,
                "toId": to_id,
            },
            "acme",
            user["id"],
        )
        relationships.append(stored)
        relationships_added += 1
    return {"assetsAdded": assets_added, "relationshipsAdded": relationships_added}


@api.get("/api/relationships", tags=["relationships"])
def list_relationships(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    asset_ids = {
        item["id"]
        for item in REPOSITORY.list_assets()
        if core.allowed(user, item["companyId"])
        and (not companyId or item["companyId"] == companyId)
    }
    return [
        item
        for item in REPOSITORY.list_relationships()
        if item["fromId"] in asset_ids and item["toId"] in asset_ids
    ]


@api.post("/api/relationships", status_code=201, tags=["relationships"])
def create_relationship(payload: RelationshipCreateRequest, request: Request) -> dict:
    user = current_user(request)
    from_asset = REPOSITORY.get_asset(payload.fromId)
    to_asset = REPOSITORY.get_asset(payload.toId)
    if not from_asset or not to_asset or from_asset["companyId"] != to_asset["companyId"]:
        raise HTTPException(400, "Choose two assets in the same company")
    _company_for_user(from_asset["companyId"], user, require_manage=True)
    if from_asset["id"] == to_asset["id"]:
        raise HTTPException(400, "A configuration item cannot be related to itself")
    if payload.type not in core.RELATIONSHIP_TYPES:
        raise HTTPException(400, "Choose a valid relationship type")
    if payload.impactPolicy not in core.IMPACT_POLICIES:
        raise HTTPException(400, "Choose a valid impact policy")
    relationships = REPOSITORY.list_relationships()
    if core.relationship_exists(relationships, from_asset["id"], to_asset["id"], payload.type):
        raise HTTPException(409, "That relationship already exists")
    if payload.type == "depends_on" and core.would_create_dependency_cycle(
        relationships, from_asset["id"], to_asset["id"]
    ):
        raise HTTPException(409, "That dependency would create a cycle")
    relationship = {
        "id": str(uuid.uuid4()),
        "fromId": from_asset["id"],
        "toId": to_asset["id"],
        "type": payload.type,
        "impactPolicy": payload.impactPolicy,
    }
    with core.LOCK:
        return REPOSITORY.create_relationship(relationship, from_asset["companyId"], user["id"])


@api.delete("/api/relationships/{relationship_id}", tags=["relationships"])
def delete_relationship(relationship_id: str, request: Request) -> dict:
    user = current_user(request)
    relationship = next(
        (item for item in REPOSITORY.list_relationships() if item["id"] == relationship_id),
        None,
    )
    if not relationship:
        raise HTTPException(404, "Relationship not found")
    source = REPOSITORY.get_asset(relationship["fromId"])
    if not source:
        raise HTTPException(404, "Relationship source asset not found")
    _company_for_user(source["companyId"], user, require_manage=True)
    with core.LOCK:
        deleted = REPOSITORY.delete_relationship(relationship_id, source["companyId"], user["id"])
    if not deleted:
        raise HTTPException(404, "Relationship not found")
    return {"deletedId": relationship_id}


def _quality_company_scope(
    company_id: str | None, user: dict, *, require_manage: bool = False
) -> list[dict]:
    if company_id:
        return [_company_for_user(company_id, user, require_manage=require_manage)]
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP data quality requires a root role",
    )
    return [company for company in REPOSITORY.list_companies() if core.allowed(user, company["id"])]


@api.get("/api/data-quality", tags=["governance"])
def get_data_quality(request: Request, companyId: str | None = None) -> dict:
    user = current_user(request)
    companies = _quality_company_scope(companyId, user)
    company_ids = {company["id"] for company in companies}
    assets = [
        core.asset_view(item)
        for item in REPOSITORY.list_assets()
        if item["companyId"] in company_ids
    ]
    asset_ids = {item["id"] for item in assets}
    relationships = [
        item
        for item in REPOSITORY.list_relationships()
        if item["fromId"] in asset_ids and item["toId"] in asset_ids
    ]
    exceptions = [
        item
        for item in REPOSITORY.list_data_quality_exceptions(companyId)
        if item["companyId"] in company_ids
    ]
    result = evaluate_data_quality(companies, assets, relationships, exceptions)
    candidates = [
        item
        for item in REPOSITORY.list_reconciliation_candidates(companyId, "pending")
        if item.get("companyId") in company_ids
    ]
    result["summary"]["pendingReconciliationCount"] = len(candidates)
    result["candidates"] = candidates
    result["exceptions"] = [item for item in exceptions if item.get("state") == "active"]
    result["fieldAuthority"] = [
        item
        for item in REPOSITORY.list_field_authority(companyId)
        if item["companyId"] in company_ids
    ]
    return result


@api.post("/api/data-quality/exceptions", status_code=201, tags=["governance"])
def create_data_quality_exception(payload: DataQualityExceptionRequest, request: Request) -> dict:
    user = current_user(request)
    _company_for_user(payload.companyId, user, require_manage=True)
    if payload.ruleKey not in DATA_QUALITY_RULES:
        raise HTTPException(400, "Choose a valid data-quality rule")
    asset = _asset_for_user(payload.entityId, user, require_manage=True)
    if asset["companyId"] != payload.companyId:
        raise HTTPException(400, "The CI does not belong to that customer")
    if payload.expiresAt:
        try:
            datetime.fromisoformat(payload.expiresAt.replace("Z", "+00:00"))
        except ValueError as error:
            raise HTTPException(400, "Choose a valid exception expiry") from error
    with core.LOCK:
        return REPOSITORY.create_data_quality_exception(
            {
                "id": str(uuid.uuid4()),
                "companyId": payload.companyId,
                "ruleKey": payload.ruleKey,
                "entityType": "configuration_item",
                "entityId": asset["id"],
                "reason": payload.reason.strip(),
                "expiresAt": payload.expiresAt,
            },
            user["id"],
        )


@api.delete("/api/data-quality/exceptions/{exception_id}", tags=["governance"])
def resolve_data_quality_exception(exception_id: str, request: Request) -> dict:
    user = current_user(request)
    current = next(
        (item for item in REPOSITORY.list_data_quality_exceptions() if item["id"] == exception_id),
        None,
    )
    if not current:
        raise HTTPException(404, "Exception not found")
    _company_for_user(current["companyId"], user, require_manage=True)
    with core.LOCK:
        result = REPOSITORY.resolve_data_quality_exception(exception_id, user["id"])
    if not result:
        raise HTTPException(409, "The exception is no longer active")
    return result


@api.get("/api/reconciliation-candidates", tags=["integrations"])
def list_reconciliation_candidates(
    request: Request, companyId: str | None = None, state: str | None = "pending"
) -> list[dict]:
    user = current_user(request)
    companies = _quality_company_scope(companyId, user)
    company_ids = {item["id"] for item in companies}
    return [
        item
        for item in REPOSITORY.list_reconciliation_candidates(companyId, state)
        if item.get("companyId") in company_ids
    ]


@api.get("/api/integration-reconciliation", tags=["integrations"])
def integration_reconciliation_workbench(
    request: Request,
    companyId: str | None = None,
    provider: str | None = None,
    state: str = "pending",
    action: str | None = None,
    search: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Return a tenant-safe, provider-neutral queue with exact server pagination."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    permitted_company_ids: set[str] | None = None
    if companyId:
        _company_for_user(companyId, user)
    elif user.get("role") != "platform_admin":
        permitted_company_ids = {
            company["id"]
            for company in REPOSITORY.list_companies()
            if core.allowed(user, company["id"])
        }
    if state not in {"pending", "dismissed", "resolved", "all"}:
        raise HTTPException(400, "Choose a valid review state")
    if action not in {None, "", "create", "update", "link", "conflict"}:
        raise HTTPException(400, "Choose a valid reconciliation decision")
    if len(search) > 200:
        raise HTTPException(400, "Search text is too long")
    return REPOSITORY.query_ci_review_items(
        kind=provider or None,
        company_id=companyId,
        company_ids=permitted_company_ids,
        state=None if state == "all" else state,
        action=action or None,
        search=search,
        limit=max(1, min(limit, 250)),
        offset=max(0, offset),
    )


@api.post("/api/integration-reconciliation/dismiss", tags=["integrations"])
def bulk_dismiss_integration_reconciliation(
    payload: IntegrationReviewBulkDismissRequest, request: Request
) -> dict:
    """Dismiss selected current observations after validating every tenant boundary."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    item_ids = list(dict.fromkeys(payload.itemIds))
    if len(item_ids) != len(payload.itemIds):
        raise HTTPException(400, "Review item selections must be unique")
    items = [REPOSITORY.get_ci_review_item(item_id) for item_id in item_ids]
    if any(item is None for item in items):
        raise HTTPException(404, "One or more review items no longer exist")
    for item in items:
        assert item is not None
        _company_for_user(item["companyId"], user, require_manage=True)
        if item.get("state") != "pending":
            raise HTTPException(409, "One or more review items are no longer pending")
    with core.LOCK:
        stored = [
            REPOSITORY.dismiss_ci_review_item(item_id, payload.notes.strip(), user["id"])
            for item_id in item_ids
        ]
    return {"dismissed": sum(item is not None for item in stored), "itemIds": item_ids}


@api.post("/api/integration-reconciliation/ignore", tags=["integrations"])
def bulk_ignore_integration_reconciliation(
    payload: IntegrationReviewBulkDismissRequest, request: Request
) -> dict:
    """Durably exclude selected immutable provider IDs from future reconciliation."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    item_ids = list(dict.fromkeys(payload.itemIds))
    if len(item_ids) != len(payload.itemIds):
        raise HTTPException(400, "Review item selections must be unique")
    items = [REPOSITORY.get_ci_review_item(item_id) for item_id in item_ids]
    if any(item is None for item in items):
        raise HTTPException(404, "One or more review items no longer exist")
    policy_ids: set[str] = set()
    for item in items:
        assert item is not None
        _company_for_user(item["companyId"], user, require_manage=True)
        if item.get("state") != "pending":
            raise HTTPException(409, "One or more review items are no longer pending")
        policy_ids.add(item["policyId"])
    if len(policy_ids) != 1:
        raise HTTPException(400, "Ignored configurations must belong to one customer policy")
    try:
        with core.LOCK:
            stored = REPOSITORY.ignore_ci_review_items(item_ids, payload.notes.strip(), user["id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "ignored": len(stored),
        "itemIds": item_ids,
        "suppressionIds": [item["id"] for item in stored],
    }


@api.get("/api/integration-reconciliation/ignored", tags=["integrations"])
def list_ignored_integration_objects(
    request: Request,
    companyId: str | None = None,
    provider: str | None = None,
    active: bool | None = True,
    search: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Return tenant-safe durable provider-object exclusions and restore history."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    permitted_company_ids: set[str] | None = None
    if companyId:
        _company_for_user(companyId, user)
    elif user.get("role") != "platform_admin":
        permitted_company_ids = {
            company["id"]
            for company in REPOSITORY.list_companies()
            if core.allowed(user, company["id"])
        }
    if len(search) > 200:
        raise HTTPException(400, "Search text is too long")
    return REPOSITORY.query_integration_object_suppressions(
        kind=provider or None,
        company_id=companyId,
        company_ids=permitted_company_ids,
        active=active,
        search=search,
        limit=max(1, min(limit, 250)),
        offset=max(0, offset),
    )


@api.post(
    "/api/integration-reconciliation/ignored/{suppression_id}/restore",
    tags=["integrations"],
)
def restore_ignored_integration_object(
    suppression_id: str,
    payload: IntegrationSuppressionRestoreRequest,
    request: Request,
) -> dict:
    """Restore one durable exclusion without deleting or unlinking a canonical CI."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    current = REPOSITORY.get_integration_object_suppression(suppression_id)
    if not current:
        raise HTTPException(404, "Ignored configuration not found")
    _company_for_user(current["companyId"], user, require_manage=True)
    if not current.get("active"):
        raise HTTPException(409, "The ignored configuration has already been restored")
    try:
        with core.LOCK:
            restored = REPOSITORY.restore_integration_object_suppression(
                suppression_id, payload.notes.strip(), user["id"]
            )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not restored:
        raise HTTPException(409, "The ignored configuration has already been restored")
    return restored


@api.patch("/api/reconciliation-candidates/{candidate_id}", tags=["integrations"])
def decide_reconciliation_candidate(
    candidate_id: str, payload: ReconciliationDecisionRequest, request: Request
) -> dict:
    user = current_user(request)
    candidate = next(
        (
            item
            for item in REPOSITORY.list_reconciliation_candidates()
            if item["id"] == candidate_id
        ),
        None,
    )
    if not candidate:
        raise HTTPException(404, "Reconciliation candidate not found")
    company_id = candidate.get("companyId")
    if not company_id:
        raise HTTPException(409, "The candidate has no customer scope")
    _company_for_user(company_id, user, require_manage=True)
    if payload.decision == "use_existing":
        if not payload.targetAssetId:
            raise HTTPException(400, "Choose the existing CI to use")
        target = _asset_for_user(payload.targetAssetId, user, require_manage=True)
        if target["companyId"] != company_id:
            raise HTTPException(400, "The target CI must belong to the same customer")
    elif payload.targetAssetId:
        raise HTTPException(400, "A target CI is only valid for an existing-CI decision")
    with core.LOCK:
        result = REPOSITORY.resolve_reconciliation_candidate(
            candidate_id,
            payload.decision,
            payload.notes.strip(),
            payload.targetAssetId,
            user["id"],
        )
    if not result:
        raise HTTPException(409, "That candidate has already been reviewed")
    return result


@api.get("/api/field-authority", tags=["integrations"])
def list_field_authority(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    companies = _quality_company_scope(companyId, user)
    company_ids = {item["id"] for item in companies}
    return [
        item
        for item in REPOSITORY.list_field_authority(companyId)
        if item["companyId"] in company_ids
    ]


@api.get("/api/field-authority/catalogue", tags=["integrations"])
def get_field_authority_catalogue(request: Request) -> dict:
    """Return curated canonical fields, providers and reviewed baseline presets."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    catalogue = authority_catalogue()
    provider_names = {
        "cmdb": "CMDB managed",
        "ncentral": "N-central",
        "passportal": "Passportal",
        **{manifest["key"]: manifest["name"] for manifest in provider_registry.manifests()},
    }
    catalogue["providers"] = [{"key": key, "name": name} for key, name in provider_names.items()]
    catalogue["ciTypes"] = [
        "*",
        *sorted(
            {
                str(item.get("type") or "Unclassified")
                for item in REPOSITORY.list_assets()
                if core.allowed(user, item["companyId"])
            }
        ),
    ]
    return catalogue


@api.post("/api/field-authority/presets", tags=["integrations"])
def apply_field_authority_preset(payload: FieldAuthorityPresetRequest, request: Request) -> dict:
    """Apply a curated field-authority baseline as individually audited rules."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    _company_for_user(payload.companyId, user, require_manage=True)
    try:
        rules = preset_rules(payload.presetKey, payload.companyId)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    with core.LOCK:
        stored = [REPOSITORY.upsert_field_authority(rule, user["id"]) for rule in rules]
    return {"applied": len(stored), "rules": stored}


@api.put("/api/field-authority", tags=["integrations"])
def upsert_field_authority(payload: FieldAuthorityRequest, request: Request) -> dict:
    user = current_user(request)
    _company_for_user(payload.companyId, user, require_manage=True)
    with core.LOCK:
        return REPOSITORY.upsert_field_authority(payload.model_dump(), user["id"])


@api.delete("/api/field-authority", tags=["integrations"])
def delete_field_authority(
    request: Request, companyId: str, ciType: str, fieldName: str, provider: str
) -> dict:
    user = current_user(request)
    _company_for_user(companyId, user, require_manage=True)
    with core.LOCK:
        deleted = REPOSITORY.delete_field_authority(
            companyId, ciType, fieldName, provider, user["id"]
        )
    if not deleted:
        raise HTTPException(404, "Source authority rule not found")
    return {"deleted": True}


SUPPORTED_INTEGRATION_KINDS = {"connectwise", "ncentral", "passportal"}


def _integration_connection(kind: str) -> dict:
    """Return one supported root connection or a stable public API error."""

    if kind not in SUPPORTED_INTEGRATION_KINDS:
        raise HTTPException(404, "Integration not found")
    connection = REPOSITORY.get_integration_connection(kind)
    if not connection:
        raise HTTPException(404, "Integration not found")
    return connection


def _require_integration_active(kind: str) -> dict:
    """Enforce the connection-level kill switch before every provider call."""

    connection = _integration_connection(kind)
    lifecycle = connection.get("lifecycleStatus", "active")
    if lifecycle != "active" or not connection.get("enabled"):
        label = lifecycle.replace("_", " ")
        raise HTTPException(
            409,
            f"{connection.get('name') or kind} is {label}. Re-enable the integration "
            "before making provider requests.",
        )
    return connection


@api.get("/api/integrations", tags=["integrations"])
def list_integrations(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP integration tools require root or MSP role",
    )
    records = []
    for item in REPOSITORY.list_integrations():
        if item.get("scope", "msp") != "msp":
            continue
        configured = (
            _connectwise_connection_public().get("configured", False)
            if item["type"] == "connectwise"
            else core.configured(item["type"])
        )
        lifecycle = item.get("lifecycleStatus", "active")
        records.append(
            {
                **item,
                "scope": "msp",
                "lifecycleStatus": lifecycle,
                "enabled": bool(item["enabled"]) and lifecycle == "active",
                "status": (
                    lifecycle.title()
                    if lifecycle != "active"
                    else (
                        "Ready"
                        if configured and item["status"] == "Not configured"
                        else item["status"]
                    )
                ),
            }
        )
    return records


@api.get("/api/integrations/{kind}/lifecycle-impact", tags=["integrations"])
def integration_lifecycle_impact(kind: str, request: Request) -> dict:
    """Preview retained records before a lifecycle or removal decision."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    _integration_connection(kind)
    try:
        impact = REPOSITORY.integration_lifecycle_impact(kind)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    if kind == "connectwise":
        public = _connectwise_connection_public()
        impact["managedByEnvironment"] = public.get("managedByEnvironment", False)
        impact["credentialSource"] = public.get("credentialSource", "not_configured")
    else:
        impact["managedByEnvironment"] = False
        impact["credentialSource"] = "not_configured"
    return impact


@api.post("/api/integrations/{kind}/lifecycle", tags=["integrations"])
def change_integration_lifecycle(
    kind: str, payload: IntegrationLifecycleRequest, request: Request
) -> dict:
    """Pause, disable or restore a provider connection without deleting evidence."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    connection = _integration_connection(kind)
    current = connection.get("lifecycleStatus", "active")
    transitions = {
        ("active", "pause"): "paused",
        ("active", "disable"): "disabled",
        ("paused", "resume"): "active",
        ("paused", "disable"): "disabled",
        ("disabled", "reenable"): "active",
        ("removed", "restore"): "active",
    }
    target = transitions.get((current, payload.action))
    if not target:
        raise HTTPException(
            409,
            f"Cannot {payload.action} an integration whose lifecycle is {current}.",
        )
    if current == "removed":
        try:
            environment_configuration = (
                _connectwise_environment_configuration() if kind == "connectwise" else None
            )
        except ConnectWiseConfigurationError as error:
            raise HTTPException(409, str(error)) from error
        if not environment_configuration:
            raise HTTPException(
                409,
                "Removed database-managed integrations must be configured with new "
                "credentials before they can be installed again.",
            )
    elif target == "active":
        configured = (
            _connectwise_connection_public().get("configured", False)
            if kind == "connectwise"
            else core.configured(kind)
        )
        if not configured:
            raise HTTPException(409, "Configure integration credentials before re-enabling it")
    try:
        with core.LOCK:
            updated = REPOSITORY.change_integration_lifecycle(
                kind,
                target,
                payload.reason.strip(),
                user["id"],
                payload.expectedRevision,
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return integration_connection_audit_value(updated)


@api.post("/api/integrations/{kind}/remove", tags=["integrations"])
def remove_integration(kind: str, payload: IntegrationRemovalRequest, request: Request) -> dict:
    """Remove usable configuration while retaining canonical and audit evidence."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    connection = _integration_connection(kind)
    if payload.confirmation.strip() != str(connection.get("name") or ""):
        raise HTTPException(400, "Enter the exact integration name to confirm removal")
    if connection.get("lifecycleStatus", "active") == "removed":
        raise HTTPException(409, "Integration has already been removed")
    try:
        impact = REPOSITORY.integration_lifecycle_impact(kind)
        with core.LOCK:
            updated = REPOSITORY.change_integration_lifecycle(
                kind,
                "removed",
                payload.reason.strip(),
                user["id"],
                payload.expectedRevision,
                remove_configuration=True,
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {
        "integration": integration_connection_audit_value(updated),
        "retained": impact,
        "message": (
            "Stored credentials and editable configuration were removed. Canonical CIs, "
            "provider identity mappings, sync history and audit evidence were retained."
        ),
    }


def _connectwise_environment_configuration() -> dict | None:
    """Return an all-or-nothing environment-managed ConnectWise connection."""

    keys = {
        "baseUrl": "CW_BASE_URL",
        "companyId": "CW_COMPANY_ID",
        "publicKey": "CW_PUBLIC_KEY",
        "privateKey": "CW_PRIVATE_KEY",
        "clientId": "CW_CLIENT_ID",
    }
    values = {target: str(os.getenv(source) or "").strip() for target, source in keys.items()}
    if not any(values.values()):
        return None
    missing = [target for target, value in values.items() if not value]
    if missing:
        raise ConnectWiseConfigurationError(
            "ConnectWise environment configuration is incomplete: " + ", ".join(missing)
        )
    try:
        page_size = int(os.getenv("CW_PAGE_SIZE", "100"))
    except ValueError as error:
        raise ConnectWiseConfigurationError("CW_PAGE_SIZE must be a number") from error
    return {**values, "baseUrl": normalize_base_url(values["baseUrl"]), "pageSize": page_size}


def _connectwise_stored_configuration() -> tuple[dict, dict]:
    """Load public metadata and decrypt the installation-bound credential bundle."""

    connection = REPOSITORY.get_integration_connection("connectwise")
    if not connection:
        raise ConnectWiseConfigurationError("ConnectWise integration connection is unavailable")
    configuration = deepcopy(connection.get("configuration") or {})
    encrypted = str(connection.get("credentialsEncrypted") or "")
    nonce = str(connection.get("credentialsNonce") or "")
    if not encrypted or not nonce:
        raise ConnectWiseConfigurationError("ConnectWise credentials are not configured")
    try:
        credentials = json.loads(decrypt_secret(encrypted, nonce, "integration:connectwise"))
    except (MfaConfigurationError, json.JSONDecodeError) as error:
        raise ConnectWiseConfigurationError(
            "Stored ConnectWise credentials cannot be decrypted by this installation"
        ) from error
    if not isinstance(credentials, dict):
        raise ConnectWiseConfigurationError("Stored ConnectWise credentials are invalid")
    return connection, {**configuration, **credentials}


def _connectwise_effective_configuration() -> tuple[dict, str]:
    """Prefer complete environment settings, otherwise use encrypted database settings."""

    _require_integration_active("connectwise")
    environment = _connectwise_environment_configuration()
    if environment:
        return environment, "environment"
    _connection, stored = _connectwise_stored_configuration()
    return stored, "encrypted_database"


def _connectwise_connection_public() -> dict:
    """Expose connection metadata without public or private API keys."""

    connection = REPOSITORY.get_integration_connection("connectwise") or {
        "id": "connectwise",
        "revision": 1,
        "enabled": False,
        "connectionStatus": "not_configured",
        "configuration": {},
    }
    configuration = connection.get("configuration") or {}
    try:
        environment = _connectwise_environment_configuration()
    except ConnectWiseConfigurationError as error:
        environment = None
        environment_error = str(error)
    else:
        environment_error = ""
    source = "environment" if environment else "encrypted_database"
    public_configuration = {**configuration, **(environment or {})}
    has_stored = bool(connection.get("credentialsEncrypted"))
    configured = bool(environment) or has_stored
    lifecycle = connection.get("lifecycleStatus", "active")
    return {
        "id": connection.get("id", "connectwise"),
        "enabled": bool(connection.get("enabled")) and lifecycle == "active",
        "baseUrl": str(public_configuration.get("baseUrl") or ""),
        "companyId": str(public_configuration.get("companyId") or ""),
        "clientId": str(public_configuration.get("clientId") or ""),
        "pageSize": int(public_configuration.get("pageSize") or 100),
        "discoveryPolicy": normalized_policy(configuration.get("discoveryPolicy")),
        "configured": configured,
        "hasCredentials": configured,
        "credentialSource": source if configured else "not_configured",
        "managedByEnvironment": bool(environment) or bool(environment_error),
        "connectionStatus": "error"
        if environment_error
        else connection.get("connectionStatus", "not_configured"),
        "lastTestAt": connection.get("lastTestAt"),
        "lastError": environment_error or connection.get("lastError", ""),
        "revision": int(connection.get("revision") or 1),
        "lifecycleStatus": lifecycle,
        "lifecycleReason": connection.get("lifecycleReason", ""),
        "lifecycleChangedAt": connection.get("lifecycleChangedAt"),
        "lifecycleChangedBy": connection.get("lifecycleChangedBy"),
    }


def _connectwise_adapter() -> ConnectWiseProvider:
    """Build the reference adapter while keeping the HTTP client replaceable in tests."""

    return ConnectWiseProvider(client_factory=ConnectWiseClient)


def _connectwise_company_rows() -> list[dict]:
    """Add non-binding exact-match suggestions to persisted provider observations."""

    companies = REPOSITORY.list_companies()
    by_name: dict[str, list[dict]] = {}
    by_external_id: dict[str, dict] = {}
    for company in companies:
        by_name.setdefault(str(company.get("name") or "").casefold().strip(), []).append(company)
        for key, value in (company.get("externalIds") or {}).items():
            if "connectwise" in str(key).casefold() and value:
                by_external_id[str(value)] = company
    rows = []
    for item in REPOSITORY.list_provider_companies("connectwise"):
        suggestion = None
        reason = ""
        if not item.get("mappedCompanyId"):
            suggestion = by_external_id.get(str(item.get("externalId") or ""))
            if suggestion:
                reason = "Existing ConnectWise external ID"
            else:
                exact = by_name.get(str(item.get("name") or "").casefold().strip(), [])
                if len(exact) == 1:
                    suggestion = exact[0]
                    reason = "Exact name; operator review required"
        rows.append(
            {
                **item,
                "suggestedCompanyId": suggestion.get("id") if suggestion else None,
                "suggestedCompanyName": suggestion.get("name") if suggestion else "",
                "suggestionReason": reason,
            }
        )
    return rows


def _run_connectwise_company_discovery(user: dict) -> dict:
    """Discover company metadata and persist a review-gated snapshot."""

    started_at = core.now()
    configuration, _source = _connectwise_effective_configuration()
    adapter = _connectwise_adapter()
    discovered = adapter.discover(configuration)
    policy = _connectwise_connection_public()["discoveryPolicy"]
    companies, excluded = adapter.apply_filters(discovered, policy)
    mapped_ids = {
        item["externalId"]
        for item in REPOSITORY.list_provider_companies("connectwise")
        if item.get("mappedCompanyId")
    }
    review_count = sum(item["externalId"] not in mapped_ids for item in companies)
    run = {
        "id": str(uuid.uuid4()),
        "type": "connectwise",
        "startedAt": started_at,
        "finishedAt": core.now(),
        "status": "review_required" if review_count else "success",
        "discovered": len(companies),
        "imported": 0,
        "updated": 0,
        "review": review_count,
        "message": (
            f"Read {len(discovered)} companies; included {len(companies)}, excluded "
            f"{len(excluded)}, and {review_count} require explicit mapping. "
            "No data was written to ConnectWise."
        ),
        "rawDiscovered": len(discovered),
        "excluded": len(excluded),
    }
    with core.LOCK:
        return REPOSITORY.record_company_discovery("connectwise", run, companies, user["id"])


@api.get("/api/integration-providers", tags=["integrations"])
def list_integration_providers(request: Request) -> list[dict]:
    """Return reviewed provider manifests used by setup and workflow surfaces."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    return provider_registry.manifests()


@api.get("/api/integrations/connectwise/config", tags=["integrations"])
def get_connectwise_configuration(request: Request) -> dict:
    """Return write-only ConnectWise configuration metadata to root operators."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    return _connectwise_connection_public()


@api.put("/api/integrations/connectwise/config", tags=["integrations"])
def update_connectwise_configuration(
    payload: ConnectWiseConfigurationRequest, request: Request
) -> dict:
    """Store ConnectWise keys encrypted; environment-managed settings remain immutable."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    public = _connectwise_connection_public()
    if public.get("managedByEnvironment"):
        raise HTTPException(409, "ConnectWise settings are managed by environment variables")
    try:
        base_url = normalize_base_url(payload.baseUrl)
    except ConnectWiseConfigurationError as error:
        raise HTTPException(400, str(error)) from error
    current = REPOSITORY.get_integration_connection("connectwise") or {}
    reinstalling = current.get("lifecycleStatus", "active") == "removed"
    encrypted = str(current.get("credentialsEncrypted") or "")
    nonce = str(current.get("credentialsNonce") or "")
    if bool(payload.publicKey) != bool(payload.privateKey):
        raise HTTPException(400, "Enter both the public and private API keys when rotating keys")
    if payload.publicKey and payload.privateKey:
        try:
            encryption_key()
            encrypted, nonce = encrypt_secret(
                json.dumps(
                    {
                        "publicKey": payload.publicKey.strip(),
                        "privateKey": payload.privateKey.strip(),
                    }
                ),
                "integration:connectwise",
            )
        except MfaConfigurationError as error:
            raise HTTPException(
                503, "Configure MFA_ENCRYPTION_KEY before storing integration credentials"
            ) from error
    if not encrypted:
        raise HTTPException(400, "Enter the ConnectWise public and private API keys")
    try:
        with core.LOCK:
            REPOSITORY.update_integration_connection(
                "connectwise",
                {
                    "configuration": {
                        "baseUrl": base_url,
                        "companyId": payload.companyId.strip(),
                        "clientId": payload.clientId.strip(),
                        "pageSize": payload.pageSize,
                        "discoveryPolicy": normalized_policy(
                            (current.get("configuration") or {}).get("discoveryPolicy")
                        ),
                    },
                    "credentialsEncrypted": encrypted,
                    "credentialsNonce": nonce,
                    "enabled": payload.enabled
                    and (reinstalling or current.get("lifecycleStatus", "active") == "active"),
                    "lifecycleStatus": (
                        "active" if reinstalling else current.get("lifecycleStatus", "active")
                    ),
                    "lifecycleReason": (
                        "Integration reinstalled with new credentials"
                        if reinstalling
                        else current.get("lifecycleReason", "")
                    ),
                    "connectionStatus": "configured",
                    "lastError": "",
                    "expectedRevision": payload.expectedRevision,
                },
                user["id"],
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return _connectwise_connection_public()


@api.post("/api/integrations/connectwise/test", tags=["integrations"])
def test_connectwise_connection(request: Request) -> dict:
    """Test authentication and company-read permission without retaining provider data."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    try:
        configuration, source = _connectwise_effective_configuration()
        result = _connectwise_adapter().test_connection(configuration)
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        with core.LOCK:
            REPOSITORY.mark_integration_test("connectwise", "error", str(error), user["id"])
        raise HTTPException(502, str(error)) from error
    with core.LOCK:
        REPOSITORY.mark_integration_test(
            "connectwise", "verified", "Company read permission verified", user["id"]
        )
    return {**result, "credentialSource": source, "message": "Company read permission verified"}


@api.put("/api/integrations/connectwise/policy", tags=["integrations"])
def update_connectwise_discovery_policy(
    payload: ConnectWiseDiscoveryPolicyRequest, request: Request
) -> dict:
    """Save discovery filters independently from environment-managed credentials."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    current = REPOSITORY.get_integration_connection("connectwise") or {}
    policy = normalized_policy(payload.model_dump(exclude={"expectedRevision"}))
    try:
        with core.LOCK:
            REPOSITORY.update_integration_connection(
                "connectwise",
                {
                    "configuration": {"discoveryPolicy": policy},
                    "enabled": bool(current.get("enabled")),
                    "connectionStatus": current.get("connectionStatus", "not_configured"),
                    "lastError": current.get("lastError", ""),
                    "expectedRevision": payload.expectedRevision,
                },
                user["id"],
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return _connectwise_connection_public()


@api.post("/api/integrations/connectwise/discovery-preview", tags=["integrations"])
def preview_connectwise_discovery(request: Request) -> dict:
    """Read and filter companies without persisting observations or mappings."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    started_at = core.now()
    try:
        configuration, source = _connectwise_effective_configuration()
        preview = _connectwise_adapter().preview(
            configuration, _connectwise_connection_public()["discoveryPolicy"]
        )
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error
    run = {
        "id": str(uuid.uuid4()),
        "type": "connectwise",
        "status": "success",
        "startedAt": started_at,
        "finishedAt": core.now(),
        "discovered": preview["discovered"],
        "imported": 0,
        "updated": 0,
        "review": preview["included"],
        "message": (
            f"Dry-run preview read {preview['discovered']} "
            f"{'sampled ' if preview.get('truncated') else ''}companies: "
            f"{preview['included']} included and {preview['excluded']} excluded. "
            "No observations, mappings or provider records were changed."
        ),
    }
    with core.LOCK:
        REPOSITORY.record_sync_run("connectwise", run, True, user["id"])
    return {**preview, "credentialSource": source, "message": run["message"]}


@api.get("/api/integrations/connectwise/discovery-options", tags=["integrations"])
def list_connectwise_discovery_options(request: Request) -> dict:
    """Load bounded provider values for filter menus without canonical writes."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    try:
        configuration, source = _connectwise_effective_configuration()
        options = _connectwise_adapter().discovery_options(configuration)
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error
    return {**options, "credentialSource": source}


@api.get("/api/integrations/connectwise/companies", tags=["integrations"])
def list_connectwise_companies(request: Request) -> list[dict]:
    """List sanitized observations and suggestions without exposing provider payloads."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    return _connectwise_company_rows()


@api.put("/api/integrations/connectwise/companies/{external_id}/mapping", tags=["integrations"])
def map_connectwise_company(
    external_id: str, payload: ConnectWiseCompanyMappingRequest, request: Request
) -> dict:
    """Apply an explicit customer mapping after administrator review."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    if not any(item["id"] == payload.companyId for item in REPOSITORY.list_companies()):
        raise HTTPException(404, "CMDB customer not found")
    try:
        with core.LOCK:
            return REPOSITORY.map_provider_company(
                "connectwise", external_id, payload.companyId, user["id"]
            )
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@api.delete(
    "/api/integrations/connectwise/companies/{external_id}/mapping",
    tags=["integrations"],
)
def unmap_connectwise_company(external_id: str, request: Request) -> dict:
    """Deactivate a customer mapping while preserving mapping and audit history."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    with core.LOCK:
        removed = REPOSITORY.unmap_provider_company("connectwise", external_id, user["id"])
    if not removed:
        raise HTTPException(404, "Active ConnectWise company mapping not found")
    return {"unmapped": True}


def _connectwise_mapped_company(company_id: str, provider_company_id: str) -> dict:
    """Return one active explicit provider-company mapping or reject the scope."""

    mapped = next(
        (
            item
            for item in REPOSITORY.list_provider_companies("connectwise")
            if item.get("externalId") == provider_company_id
            and item.get("mappedCompanyId") == company_id
            and item.get("active", True)
        ),
        None,
    )
    if not mapped:
        raise HTTPException(
            409,
            "Choose a ConnectWise company that is explicitly mapped to this CMDB customer",
        )
    return mapped


def _connectwise_configuration_context(
    company_id: str, provider_company_id: str
) -> tuple[dict, list[dict], str, dict]:
    """Validate a company mapping and read its sanitized provider configurations."""

    mapped = _connectwise_mapped_company(company_id, provider_company_id)
    configuration, source = _connectwise_effective_configuration()
    records = ConnectWiseClient(configuration).discover_configurations(provider_company_id)
    policy = REPOSITORY.get_ci_sync_policy("connectwise", company_id, provider_company_id)
    return mapped, records, source, policy


def _connectwise_configuration_preview(company_id: str, provider_company_id: str) -> dict[str, Any]:
    """Read, filter and classify CIs for one explicit customer mapping."""

    mapped, records, source, policy = _connectwise_configuration_context(
        company_id, provider_company_id
    )
    catalogue = configuration_catalogue(records)
    included_records, exclusion_reasons = apply_ci_policy(records, policy)
    mapped_records, type_mapping_summary = apply_ci_type_mappings(included_records, policy)
    assets = [item for item in REPOSITORY.list_assets() if item["companyId"] == company_id]
    mappings = REPOSITORY.list_provider_ci_mappings("connectwise", company_id)
    items = reconcile_configuration_items(
        connection_id="connectwise",
        records=mapped_records,
        assets=assets,
        mappings=mappings,
        field_authority=REPOSITORY.list_field_authority(company_id),
        provider="connectwise",
    )
    counts = {
        action: sum(item["action"] == action for item in items)
        for action in ("create", "update", "link", "unchanged", "conflict")
    }
    return {
        "companyId": company_id,
        "companyName": mapped.get("mappedCompanyName") or company_id,
        "providerCompanyId": provider_company_id,
        "providerCompanyName": mapped.get("name") or provider_company_id,
        "credentialSource": source,
        "readOnly": True,
        "writesAttempted": False,
        "discovered": len(records),
        "included": len(included_records),
        "excluded": len(records) - len(included_records),
        "exclusionReasons": exclusion_reasons,
        "availableTypes": catalogue["types"],
        "availableStatuses": catalogue["statuses"],
        "typeMappingSummary": type_mapping_summary,
        "appliedPolicy": policy,
        "counts": counts,
        "items": items,
    }


def _execute_connectwise_ci_preview(
    company_id: str,
    provider_company_id: str,
    *,
    actor_id: str | None,
    trigger: str,
) -> dict[str, Any]:
    """Execute one read-only preview and persist its current review observations."""

    started_at = core.now()
    preview = _connectwise_configuration_preview(company_id, provider_company_id)
    counts = preview["counts"]
    message = (
        f"Read {preview['discovered']} ConnectWise configuration items for "
        f"{preview['providerCompanyName']}; the saved policy included {preview['included']} and "
        f"excluded {preview['excluded']}: {counts['create']} new, {counts['update']} changed, "
        f"{counts['link']} identity links, {counts['unchanged']} unchanged and "
        f"{counts['conflict']} requiring review. No CMDB or ConnectWise records were changed."
    )
    run = {
        "id": str(uuid.uuid4()),
        "type": "connectwise",
        "status": "success",
        "startedAt": started_at,
        "finishedAt": core.now(),
        "discovered": preview["discovered"],
        "imported": 0,
        "updated": 0,
        "review": counts["create"] + counts["update"] + counts["link"] + counts["conflict"],
        "message": message,
        "attributes": {
            "operation": "configuration_preview",
            "trigger": trigger,
            "companyId": company_id,
            "providerCompanyId": provider_company_id,
            "policyId": preview["appliedPolicy"].get("id") or None,
            "policyRevision": preview["appliedPolicy"].get("revision", 0),
            "included": preview["included"],
            "excluded": preview["excluded"],
            "readOnly": True,
        },
    }
    with core.LOCK:
        stored_run = REPOSITORY.record_sync_run("connectwise", run, True, actor_id)
        policy_id = str(preview["appliedPolicy"].get("id") or "")
        queue_summary = (
            REPOSITORY.replace_ci_review_items(
                policy_id,
                company_id,
                stored_run["id"],
                preview["items"],
                actor_id,
            )
            if policy_id
            else {"pending": 0, "created": 0, "updated": 0, "resolved": 0}
        )
        if policy_id:
            REPOSITORY.complete_ci_sync_policy_run(policy_id, success=True)
    return {
        **preview,
        "message": message,
        "syncRunId": stored_run["id"],
        "queueSummary": queue_summary,
    }


def _record_connectwise_ci_preview_failure(policy: dict, error: Exception) -> None:
    """Persist a sanitized worker failure and release its policy lease."""

    expected = isinstance(error, (ConnectWiseConfigurationError, ConnectWiseRequestError))
    detail = str(error)[:1000] if expected else "Unexpected integration worker failure"
    now = core.now()
    run = {
        "id": str(uuid.uuid4()),
        "type": "connectwise",
        "status": "failed",
        "startedAt": now,
        "finishedAt": now,
        "discovered": 0,
        "imported": 0,
        "updated": 0,
        "review": 0,
        "message": f"Continuous preview failed for {policy.get('companyName') or policy['companyId']}: {detail}",
        "attributes": {
            "operation": "configuration_preview",
            "trigger": "continuous_preview",
            "companyId": policy["companyId"],
            "providerCompanyId": policy["providerParentId"],
            "policyId": policy["id"],
            "readOnly": True,
        },
    }
    with core.LOCK:
        REPOSITORY.record_sync_run("connectwise", run, True, None)
        REPOSITORY.complete_ci_sync_policy_run(policy["id"], success=False, error=detail)


def _run_due_integration_previews(limit: int = 5) -> dict[str, int]:
    """Drain a bounded number of leased read-only policies for this worker cycle."""

    worker_id = f"{os.getpid()}:{uuid.uuid4()}"
    processed = 0
    succeeded = 0
    failed = 0
    for _index in range(max(1, min(limit, 25))):
        policy = REPOSITORY.claim_due_ci_sync_policy("connectwise", worker_id)
        if not policy:
            break
        processed += 1
        token = set_audit_context(
            AuditContext(
                request_id=str(uuid.uuid4()),
                correlation_id=str(uuid.uuid4()),
                source_system="integration_worker",
            )
        )
        try:
            _execute_connectwise_ci_preview(
                policy["companyId"],
                policy["providerParentId"],
                actor_id=None,
                trigger="continuous_preview",
            )
            succeeded += 1
        except Exception as error:  # Worker boundaries must release leases for every failure.
            failed += 1
            LOGGER.exception("Continuous ConnectWise preview failed for policy %s", policy["id"])
            try:
                _record_connectwise_ci_preview_failure(policy, error)
            except Exception:
                LOGGER.exception(
                    "Could not persist integration worker failure for %s", policy["id"]
                )
                REPOSITORY.complete_ci_sync_policy_run(
                    policy["id"], success=False, error="Worker failure could not be persisted"
                )
        finally:
            reset_audit_context(token)
    return {"processed": processed, "succeeded": succeeded, "failed": failed}


@api.get("/api/integrations/connectwise/configurations/policy", tags=["integrations"])
def get_connectwise_ci_policy(request: Request, companyId: str, providerCompanyId: str) -> dict:
    """Return the saved CI filter/schedule policy for one mapped customer."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    _company_for_user(companyId, user)
    _connectwise_mapped_company(companyId, providerCompanyId)
    return REPOSITORY.get_ci_sync_policy("connectwise", companyId, providerCompanyId)


@api.put("/api/integrations/connectwise/configurations/policy", tags=["integrations"])
def update_connectwise_ci_policy(payload: ConnectWiseCiPolicyRequest, request: Request) -> dict:
    """Persist an audited immutable-ID CI policy for future repeatable syncs."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    _company_for_user(payload.companyId, user, require_manage=True)
    _connectwise_mapped_company(payload.companyId, payload.providerCompanyId)
    try:
        with core.LOCK:
            return REPOSITORY.update_ci_sync_policy(
                "connectwise",
                payload.companyId,
                payload.providerCompanyId,
                normalize_ci_policy(payload.model_dump()),
                payload.expectedRevision,
                user["id"],
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@api.post("/api/integrations/connectwise/configurations/options", tags=["integrations"])
def get_connectwise_ci_options(
    payload: ConnectWiseConfigurationPreviewRequest, request: Request
) -> dict:
    """Read immutable provider type/status choices for one mapped company."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    _company_for_user(payload.companyId, user)
    try:
        mapped, records, source, policy = _connectwise_configuration_context(
            payload.companyId, payload.providerCompanyId
        )
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error
    catalogue = configuration_catalogue(records)
    return {
        "companyId": payload.companyId,
        "providerCompanyId": payload.providerCompanyId,
        "providerCompanyName": mapped.get("name") or payload.providerCompanyId,
        "credentialSource": source,
        "readOnly": True,
        "writesAttempted": False,
        "discovered": len(records),
        "availableTypes": catalogue["types"],
        "availableStatuses": catalogue["statuses"],
        "policy": policy,
    }


@api.post("/api/integrations/connectwise/configurations/preview", tags=["integrations"])
def preview_connectwise_configurations(
    payload: ConnectWiseConfigurationPreviewRequest, request: Request
) -> dict:
    """Preview reconciliation and refresh its durable review queue."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    _company_for_user(payload.companyId, user)
    try:
        return _execute_connectwise_ci_preview(
            payload.companyId,
            payload.providerCompanyId,
            actor_id=user["id"],
            trigger="manual",
        )
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error


@api.get("/api/integrations/continuous-preview/status", tags=["integrations"])
def continuous_preview_status(request: Request) -> dict:
    """Summarize worker configuration, schedules and current review backlog."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    policies = REPOSITORY.list_ci_sync_policies("connectwise")
    connection_enabled = bool(_connectwise_connection_public().get("enabled"))
    return {
        "workerEnabled": os.getenv("INTEGRATION_WORKER_ENABLED", "false").lower()
        in {"1", "true", "yes"},
        "workerIntervalSeconds": _integration_worker_interval(),
        "enabledPolicies": sum(
            item.get("enabled") and item.get("syncMode") == "continuous_preview"
            for item in policies
        )
        if connection_enabled
        else 0,
        "pendingReviews": REPOSITORY.count_ci_review_items("connectwise", state="pending"),
        "policies": policies,
    }


@api.get("/api/integrations/connectwise/configurations/review-queue", tags=["integrations"])
def list_connectwise_ci_review_queue(
    request: Request,
    companyId: str | None = None,
    state: str = "pending",
    limit: int = 250,
) -> dict:
    """Return current ConnectWise CI observations awaiting an MSP decision."""

    user = current_user(request)
    _require_role(user, {"platform_admin", "msp_operator"}, "MSP access required")
    if state not in {"pending", "dismissed", "resolved", "all"}:
        raise HTTPException(400, "Review state must be pending, dismissed, resolved or all")
    if companyId:
        _company_for_user(companyId, user)
    selected_state = None if state == "all" else state
    return {
        "items": REPOSITORY.list_ci_review_items(
            "connectwise", companyId, selected_state, max(1, min(limit, 1000))
        ),
        "total": REPOSITORY.count_ci_review_items("connectwise", companyId, selected_state),
    }


@api.post(
    "/api/integrations/connectwise/configurations/review-queue/{item_id}/dismiss",
    tags=["integrations"],
)
def dismiss_connectwise_ci_review_item(
    item_id: str, payload: ConnectWiseReviewDismissRequest, request: Request
) -> dict:
    """Dismiss one unchanged observation until its provider evidence changes."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    item = REPOSITORY.get_ci_review_item(item_id)
    if not item:
        raise HTTPException(404, "CI review item not found")
    _company_for_user(item["companyId"], user, require_manage=True)
    with core.LOCK:
        stored = REPOSITORY.dismiss_ci_review_item(item_id, payload.notes.strip(), user["id"])
    if not stored:
        raise HTTPException(404, "CI review item not found")
    return stored


@api.post("/api/integrations/connectwise/configurations/import", tags=["integrations"])
def import_connectwise_configurations(
    payload: ConnectWiseConfigurationImportRequest, request: Request
) -> dict:
    """Re-read and apply only administrator-selected non-conflicting CI changes."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    selected_ids = {str(value).strip() for value in payload.externalIds if str(value).strip()}
    if len(selected_ids) != len(payload.externalIds):
        raise HTTPException(400, "Configuration item selections must be unique and non-empty")
    started_at = core.now()
    try:
        preview = _connectwise_configuration_preview(payload.companyId, payload.providerCompanyId)
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error
    items_by_id = {item["externalId"]: item for item in preview["items"]}
    invalid = sorted(
        external_id
        for external_id in selected_ids
        if external_id not in items_by_id
        or items_by_id[external_id]["action"] not in {"create", "update", "link"}
    )
    if invalid:
        raise HTTPException(
            409,
            "The preview changed or contains conflicts. Refresh it before importing: "
            + ", ".join(invalid[:10]),
        )

    created = 0
    updated = 0
    linked = 0

    def with_field_sources(metadata: dict, fields: list[str]) -> dict:
        """Record provider ownership for every canonical field changed by this import."""

        stored = deepcopy(metadata)
        sources = stored.get("fieldSources")
        field_sources = deepcopy(sources) if isinstance(sources, dict) else {}
        for field in fields:
            field_sources[str(field)] = "connectwise"
        stored["fieldSources"] = field_sources
        return stored

    with core.LOCK:
        for external_id in selected_ids:
            item = items_by_id[external_id]
            record = item["record"]
            asset_id = item.get("assetId")
            if item["action"] == "create":
                asset = {
                    "id": str(uuid.uuid4()),
                    "companyId": payload.companyId,
                    "name": record["name"],
                    "type": record["type"],
                    "status": record["status"],
                    "source": "connectwise",
                    "externalId": record["externalId"],
                    "lastSeen": core.now(),
                    "fields": record.get("fields") or {},
                    "metadata": with_field_sources(
                        core.normalise_metadata(record.get("metadata") or {}, record["status"]),
                        item.get("appliedFields") or item.get("changedFields") or [],
                    ),
                }
                asset_id = REPOSITORY.create_asset(asset, user["id"])["id"]
                created += 1
            elif item["action"] == "update":
                changes = {
                    **item["changes"],
                    "source": "connectwise",
                    "externalId": record["externalId"],
                    "lastSeen": core.now(),
                }
                if "metadata" in changes:
                    changes["metadata"] = core.normalise_metadata(
                        changes["metadata"], changes.get("status", record["status"])
                    )
                current_asset = next(
                    (asset for asset in REPOSITORY.list_assets() if asset["id"] == asset_id),
                    None,
                )
                current_metadata = deepcopy((current_asset or {}).get("metadata") or {})
                changes["metadata"] = with_field_sources(
                    changes.get("metadata") or current_metadata,
                    item.get("appliedFields") or item.get("changedFields") or [],
                )
                if not asset_id or not REPOSITORY.update_asset(asset_id, changes, user["id"]):
                    raise HTTPException(409, "A selected configuration item changed during import")
                updated += 1
            else:
                linked += 1
            if not asset_id:
                raise HTTPException(409, "A selected configuration item has no canonical target")
            REPOSITORY.record_provider_ci_mapping(
                "connectwise", payload.companyId, record, asset_id, user["id"]
            )

        remaining_review = sum(
            item["action"] in {"create", "update", "link", "conflict"}
            and item["externalId"] not in selected_ids
            for item in preview["items"]
        )
        message = (
            f"Imported {created} new, updated {updated} and linked {linked} ConnectWise "
            f"configuration items for {preview['companyName']}. {remaining_review} remain for review. "
            "No data was written to ConnectWise."
        )
        run = REPOSITORY.record_sync_run(
            "connectwise",
            {
                "id": str(uuid.uuid4()),
                "type": "connectwise",
                "status": "review_required" if remaining_review else "success",
                "startedAt": started_at,
                "finishedAt": core.now(),
                "discovered": preview["discovered"],
                "imported": created + linked,
                "updated": updated,
                "review": remaining_review,
                "message": message,
                "attributes": {
                    "operation": "configuration_import",
                    "companyId": payload.companyId,
                    "providerCompanyId": payload.providerCompanyId,
                    "writesProvider": False,
                    "decisionNotes": payload.decisionNotes.strip(),
                },
            },
            True,
            user["id"],
        )
        policy_id = str(preview["appliedPolicy"].get("id") or "")
        if policy_id:
            REPOSITORY.resolve_ci_review_items(policy_id, sorted(selected_ids), user["id"])
    return {**run, "created": created, "updated": updated, "linked": linked}


@api.post("/api/integrations/connectwise/configurations/link", tags=["integrations"])
def link_connectwise_configuration(
    payload: ConnectWiseConfigurationLinkRequest, request: Request
) -> dict:
    """Explicitly link one provider ID to an existing same-customer canonical CI."""

    user = current_user(request)
    _require_role(user, {"platform_admin"}, "Platform administrator access required")
    target = _asset_for_user(payload.assetId, user, require_manage=True)
    if target["companyId"] != payload.companyId:
        raise HTTPException(409, "The target CI belongs to another customer")
    try:
        preview = _connectwise_configuration_preview(payload.companyId, payload.providerCompanyId)
    except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
        raise HTTPException(502, str(error)) from error
    item = next(
        (row for row in preview["items"] if row["externalId"] == payload.externalId),
        None,
    )
    if not item:
        raise HTTPException(
            409,
            "The provider CI is no longer included by the saved policy; refresh the preview",
        )
    with core.LOCK:
        mapping = REPOSITORY.record_provider_ci_mapping(
            "connectwise",
            payload.companyId,
            item["record"],
            target["id"],
            user["id"],
        )
        policy_id = str(preview["appliedPolicy"].get("id") or "")
        if policy_id:
            REPOSITORY.resolve_ci_review_items(policy_id, [payload.externalId], user["id"])
    return {
        "linked": True,
        "externalId": payload.externalId,
        "assetId": target["id"],
        "assetName": target["name"],
        "mapping": mapping,
        "message": (
            f"Linked ConnectWise configuration {item['name']} to {target['name']} using its "
            "immutable provider ID. No data was written to ConnectWise."
        ),
    }


@api.post("/api/integrations/{kind}/sync", tags=["integrations"])
def run_integration_sync(kind: str, request: Request) -> dict:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "Sync requires MSP operator or platform admin role",
    )
    if kind not in SUPPORTED_INTEGRATION_KINDS:
        raise HTTPException(404, "Integration not found")
    if not any(
        item["type"] == kind and item.get("scope", "msp") == "msp"
        for item in REPOSITORY.list_integrations()
    ):
        raise HTTPException(404, "Integration not found")
    _require_integration_active(kind)
    if kind == "connectwise":
        try:
            return _run_connectwise_company_discovery(user)
        except (ConnectWiseConfigurationError, ConnectWiseRequestError) as error:
            raise HTTPException(502, str(error)) from error
    run = core.execute_sync(kind)
    with core.LOCK:
        return REPOSITORY.record_sync_run(kind, run, core.configured(kind), user["id"])


@api.get("/api/sync-runs", tags=["integrations"])
def list_sync_runs(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "Sync history requires root or MSP role",
    )
    return REPOSITORY.list_sync_runs()


def _company_for_change(company_id: str, user: dict, require_manage: bool = False) -> dict:
    company = next((item for item in REPOSITORY.list_companies() if item["id"] == company_id), None)
    if not company:
        raise HTTPException(404, "Customer not found")
    permitted = (
        core.can_manage(user, company_id) if require_manage else core.allowed(user, company_id)
    )
    if not permitted:
        raise HTTPException(403, "You do not have change-control access for this customer")
    return company


def _change_for_user(change_id: str, user: dict) -> dict:
    change = REPOSITORY.get_change(change_id)
    if not change:
        raise HTTPException(404, "Change package not found")
    _company_for_change(change["companyId"], user)
    return change


def _approval_email_message(change: dict, approver: dict, raw_token: str, expires_at: str) -> dict:
    """Build a branded approval invitation without persisting its bearer token."""

    base_url = _public_base_url()
    if not base_url:
        raise EmailConfigurationError("PUBLIC_BASE_URL is not safe for change approvals")
    brand = REPOSITORY.get_msp_branding()
    raw_brand_name = str(brand.get("name") or "CMDB Hub")
    approval_url = f"{base_url}/#/approve?token={quote(raw_token, safe='')}"
    safe_url = html.escape(approval_url, quote=True)
    accent = html.escape(str(brand.get("accent") or "#50d5b9"), quote=True)
    approver_name = html.escape(str(approver.get("approverName") or "Approver"))
    change_number = html.escape(str(change.get("number") or "Change"))
    title = html.escape(str(change.get("title") or ""))
    company = html.escape(str(change.get("companyName") or ""))
    scope_names = ", ".join(
        str(item.get("name") or "") for item in approver.get("scope") or [] if item.get("name")
    )
    safe_scope = html.escape(scope_names or "Entire change")
    return {
        "idempotencyKey": f"change-approval:{uuid.uuid4()}",
        "to": [approver["approverEmail"]],
        "subject": f"Approval required: {change.get('number')} - {change.get('title')}",
        "bodyHtml": (
            f'<div style="font-family:Arial,sans-serif;color:#172033;max-width:640px">'
            f"<h2>{html.escape(raw_brand_name)} change approval</h2>"
            f"<p>Hello {approver_name},</p>"
            f"<p>Your approval is required for <strong>{change_number} - {title}</strong> "
            f"for {company}.</p><p><strong>Your scope:</strong> {safe_scope}</p>"
            f'<p><a href="{safe_url}" style="display:inline-block;padding:12px 18px;'
            f"background:{accent};color:#07111f;text-decoration:none;border-radius:6px;"
            '">Review change</a></p>'
            f"<p>This single-use link expires at {html.escape(expires_at)}. "
            "Do not forward it to another person.</p></div>"
        ),
        "bodyText": (
            f"Hello {approver.get('approverName') or 'Approver'},\n\n"
            f"Review {change.get('number')} - {change.get('title')} for "
            f"{change.get('companyName')}.\nScope: {scope_names or 'Entire change'}\n\n"
            f"{approval_url}\n\nThis single-use link expires at {expires_at}."
        ),
    }


def _public_change_approval(record: dict, change: dict) -> dict:
    """Return the minimum change context an external approver needs."""

    brand = REPOSITORY.get_msp_branding()
    summary = change.get("impactSummary") or {}
    system_ids = {
        str(item.get("id"))
        for item in record.get("scope") or []
        if item.get("type") == "business_system"
    }
    business_systems = [
        {
            "id": item.get("assetId"),
            "name": item.get("name"),
            "criticality": item.get("criticality"),
            "impactSeverity": item.get("impactSeverity"),
            "department": item.get("department"),
            "userPopulation": item.get("userPopulation"),
        }
        for item in summary.get("businessSystems") or []
        if not system_ids or str(item.get("assetId")) in system_ids
    ]
    return {
        "requestId": record["id"],
        "status": record["status"],
        "expiresAt": record["expiresAt"],
        "approverName": record.get("approverName", ""),
        "scope": record.get("scope") or [],
        "branding": {
            "name": brand.get("name") or "CMDB Hub",
            "logoText": brand.get("logoText") or "C",
            "logoDataUrl": brand.get("logoDataUrl") or "",
            "accent": brand.get("accent") or "#50d5b9",
            "supportEmail": brand.get("supportEmail") or "",
        },
        "change": {
            "number": change.get("number"),
            "title": change.get("title"),
            "companyName": change.get("companyName"),
            "status": change.get("status"),
            "changeType": change.get("changeType"),
            "priority": change.get("priority"),
            "riskLevel": change.get("riskLevel"),
            "plannedStart": change.get("plannedStart"),
            "plannedEnd": change.get("plannedEnd"),
            "outageExpected": change.get("outageExpected"),
            "reason": change.get("reason"),
            "businessImpact": change.get("businessImpact"),
            "implementationPlan": change.get("implementationPlan"),
            "validationPlan": change.get("validationPlan"),
            "rollbackPlan": change.get("rollbackPlan"),
            "requester": (change.get("createdBy") or {}).get("email", ""),
            "businessSystems": business_systems,
        },
    }


def _approval_request_for_token(raw_token: str) -> tuple[dict, dict]:
    """Resolve and validate a public approval link without disclosing failure details."""

    record = REPOSITORY.get_change_approval_request_by_token(opaque_token_hash(raw_token))
    if not record or record.get("status") != "pending":
        raise HTTPException(404, "This approval link is invalid or no longer available")
    try:
        expires_at = datetime.fromisoformat(str(record["expiresAt"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as error:
        raise HTTPException(404, "This approval link is invalid or no longer available") from error
    if expires_at <= datetime.now(UTC):
        raise HTTPException(404, "This approval link is invalid or no longer available")
    change = REPOSITORY.get_change(record["changeId"])
    if not change or change.get("status") != "awaiting_approval":
        raise HTTPException(404, "This approval link is invalid or no longer available")
    return record, change


@api.post("/api/changes/impact-preview", tags=["change control"])
def change_impact_preview(payload: ChangeImpactRequest, request: Request) -> dict:
    user = current_user(request)
    _company_for_change(payload.companyId, user, require_manage=True)
    try:
        return preview_change_impact(
            payload.companyId,
            payload.scopeAssetIds,
            payload.outageExpected,
            [core.asset_view(item) for item in REPOSITORY.list_assets()],
            REPOSITORY.list_relationships(),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@api.get("/api/changes", tags=["change control"])
def list_changes(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_change(companyId, user)
    changes = [
        item
        for item in REPOSITORY.list_changes()
        if (not companyId or item["companyId"] == companyId)
        and core.allowed(user, item["companyId"])
    ]
    return sorted(changes, key=lambda item: item.get("createdAt", ""), reverse=True)


@api.post("/api/changes", status_code=201, tags=["change control"])
def create_change(payload: ChangeCreateRequest, request: Request) -> dict:
    user = current_user(request)
    company = _company_for_change(payload.companyId, user, require_manage=True)
    with core.LOCK:
        year = datetime.now(UTC).year
        number = REPOSITORY.next_change_number(year)
        try:
            change = create_change_record(
                payload.model_dump(),
                number,
                str(uuid.uuid4()),
                company,
                user,
                [core.asset_view(item) for item in REPOSITORY.list_assets()],
                REPOSITORY.list_relationships(),
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        change = REPOSITORY.create_change(change, user["id"])
    return change


@api.patch("/api/changes/{change_id}", tags=["change control"])
def update_change(change_id: str, payload: ChangeUpdateRequest, request: Request) -> dict:
    user = current_user(request)
    current = _change_for_user(change_id, user)
    _company_for_change(current["companyId"], user, require_manage=True)
    if int(current.get("revision") or 1) != payload.expectedRevision:
        raise HTTPException(
            409, "This change was updated by another user. Reload it before saving."
        )
    values = payload.model_dump(exclude_unset=True)
    try:
        updated = update_change_record(
            current,
            values,
            user,
            [core.asset_view(item) for item in REPOSITORY.list_assets()],
            REPOSITORY.list_relationships(),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    with core.LOCK:
        stored = REPOSITORY.update_change(change_id, updated, user["id"], action="updated")
    if not stored:
        raise HTTPException(404, "Change package not found")
    return stored


@api.get("/api/changes/{change_id}/approval-requests", tags=["change control"])
def list_change_approvals(change_id: str, request: Request) -> list[dict]:
    """List delivery and decision evidence for authorized technicians."""

    user = current_user(request)
    change = _change_for_user(change_id, user)
    _company_for_change(change["companyId"], user, require_manage=True)
    return REPOSITORY.list_change_approval_requests(change_id)


@api.post(
    "/api/changes/{change_id}/approval-requests",
    status_code=201,
    tags=["change control"],
)
def create_change_approvals(
    change_id: str,
    payload: ChangeApprovalCreateRequest,
    request: Request,
) -> list[dict]:
    """Create a new approval batch and send its single-use review links."""

    user = current_user(request)
    change = _change_for_user(change_id, user)
    _company_for_change(change["companyId"], user, require_manage=True)
    if change.get("status") != "awaiting_approval":
        raise HTTPException(409, "Move the change to awaiting approval before requesting sign-off")
    if int(change.get("revision") or 1) != payload.expectedRevision:
        raise HTTPException(409, "This change was updated. Reload it before requesting approval.")
    base_url = _public_base_url()
    connection, client_secret = _email_connection_for_delivery()
    if not base_url:
        raise HTTPException(409, "Configure a safe PUBLIC_BASE_URL before sending approvals")
    if not (
        connection.get("enabled")
        and connection.get("senderAddress")
        and connection.get("status") in {"configured", "verified"}
    ):
        raise HTTPException(
            409, "Configure and enable Microsoft 365 email before sending approvals"
        )
    plan = derive_change_approvers(change)
    if plan["missing"]:
        names = ", ".join(item["name"] for item in plan["missing"][:5])
        raise HTTPException(
            409,
            f"Add an email-enabled sign-off delegate or business owner for: {names}",
        )
    if not plan["approvers"]:
        raise HTTPException(409, "Add at least one approver with a valid email address")

    batch_id = str(uuid.uuid4())
    expires_at = (
        (datetime.now(UTC) + timedelta(hours=payload.expiresInHours))
        .isoformat()
        .replace("+00:00", "Z")
    )
    deliveries: list[tuple[dict, str, dict]] = []
    with core.LOCK:
        REPOSITORY.revoke_change_approval_requests(
            change_id,
            user["id"],
            "Replaced by a new approval batch",
        )
        for approver in plan["approvers"]:
            raw_token = f"cmdb_approval_{secrets.token_urlsafe(32)}"
            record = REPOSITORY.create_change_approval_request(
                {
                    "id": str(uuid.uuid4()),
                    "changeId": change_id,
                    "companyId": change["companyId"],
                    "batchId": batch_id,
                    "changeRevision": int(change["revision"]),
                    "approverContactId": approver.get("approverContactId"),
                    "approverUserId": None,
                    "approverName": approver["approverName"],
                    "approverEmail": approver["approverEmail"],
                    "responsibilityRole": ",".join(approver["responsibilityRoles"]),
                    "scope": approver["scope"],
                    "status": "pending",
                    "tokenHash": opaque_token_hash(raw_token),
                    "expiresAt": expires_at,
                    "deliveryStatus": "pending",
                },
                user["id"],
            )
            deliveries.append((record, raw_token, approver))

    for record, raw_token, approver in deliveries:
        try:
            result = EMAIL_SENDER.send(
                connection,
                _approval_email_message(change, approver, raw_token, expires_at),
                client_secret=client_secret,
            )
        except (EmailConfigurationError, EmailDeliveryError) as error:
            REPOSITORY.update_change_approval_request(
                record["id"],
                {"deliveryStatus": "failed", "lastError": str(error)[:500]},
                user["id"],
            )
        else:
            REPOSITORY.update_change_approval_request(
                record["id"],
                {
                    "deliveryStatus": "accepted",
                    "providerRequestId": result.provider_request_id,
                    "lastError": "",
                },
                user["id"],
            )
    return REPOSITORY.list_change_approval_requests(change_id)


@api.post("/api/change-approvals/validate", tags=["change approvals"])
def validate_change_approval(payload: ChangeApprovalValidateRequest) -> dict:
    """Return a constrained review package for a valid public approval link."""

    record, change = _approval_request_for_token(payload.token)
    return _public_change_approval(record, change)


@api.post("/api/change-approvals/respond", tags=["change approvals"])
def respond_to_change_approval(payload: ChangeApprovalResponseRequest) -> dict:
    """Consume a single-use link and apply the batch's lifecycle outcome."""

    token_hash = opaque_token_hash(payload.token)
    with core.LOCK:
        pending, change = _approval_request_for_token(payload.token)
        decided = REPOSITORY.decide_change_approval_request(
            token_hash,
            payload.decision,
            payload.comments,
        )
        if not decided:
            raise HTTPException(409, "This approval link has already been used or has expired")
        batch = [
            item
            for item in REPOSITORY.list_change_approval_requests(change["id"])
            if item.get("batchId") == pending.get("batchId")
        ]
        if payload.decision == "declined":
            REPOSITORY.revoke_change_approval_requests(
                change["id"],
                None,
                "Approval batch ended after a decline",
                pending.get("batchId"),
            )
        all_approved = bool(batch) and all(item.get("status") == "approved" for item in batch)
        updated = record_external_approval(
            change,
            decided,
            payload.decision,
            payload.comments,
            all_approved,
        )
        stored = REPOSITORY.update_change(
            change["id"],
            updated,
            None,
            action=f"external_{payload.decision}",
            reason=payload.comments,
        )
    if not stored:
        raise HTTPException(404, "This approval link is invalid or no longer available")
    return {
        "decision": payload.decision,
        "changeStatus": stored["status"],
        "message": (
            "Your decision has been recorded."
            if stored["status"] != "awaiting_approval"
            else "Your approval has been recorded. Other required approvals are still pending."
        ),
    }


@api.post("/api/changes/{change_id}/transition", tags=["change control"])
def transition_change(change_id: str, payload: ChangeTransitionRequest, request: Request) -> dict:
    user = current_user(request)
    current = _change_for_user(change_id, user)
    _company_for_change(current["companyId"], user, require_manage=True)
    if int(current.get("revision") or 1) != payload.expectedRevision:
        raise HTTPException(
            409,
            "This change was updated by another user. Reload it before changing status.",
        )
    approval_requests = REPOSITORY.list_change_approval_requests(change_id)
    if payload.status == "approved" and approval_requests:
        raise HTTPException(
            409,
            "This change uses external sign-off and will approve automatically when the batch completes.",
        )
    values = payload.model_dump()
    try:
        updated = transition_change_record(current, payload.status, values, user)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    with core.LOCK:
        stored = REPOSITORY.update_change(
            change_id,
            updated,
            user["id"],
            action=f"status_{payload.status}",
            reason=payload.reason,
        )
    if not stored:
        raise HTTPException(404, "Change package not found")
    if current.get("status") == "awaiting_approval" and payload.status in {
        "impact_review",
        "declined",
        "cancelled",
    }:
        REPOSITORY.revoke_change_approval_requests(
            change_id,
            user["id"],
            f"Change moved to {payload.status.replace('_', ' ')}",
        )
    return stored


@api.get("/api/changes/{change_id}/pdf", tags=["change control"])
def download_change_pdf(change_id: str, request: Request) -> Response:
    user = current_user(request)
    change = _change_for_user(change_id, user)
    company = next(
        item for item in REPOSITORY.list_companies() if item["id"] == change["companyId"]
    )
    pdf = render_change_pdf(change, company, REPOSITORY.get_msp_branding())
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{change_pdf_filename(change)}"'},
    )


@api.get("/api/changes/{change_id}", tags=["change control"])
def get_change(change_id: str, request: Request) -> dict:
    return _change_for_user(change_id, current_user(request))


@api.get("/api/audit-events", tags=["governance"])
def list_audit_events(
    request: Request,
    companyId: str | None = None,
    actorId: str | None = None,
    category: str | None = None,
    action: str | None = None,
    entityType: str | None = None,
    entityId: str | None = None,
    outcome: str | None = None,
    search: str | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    limit: int = 250,
) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP audit requires root or MSP role",
        )
    records = REPOSITORY.list_audit_events(
        companyId,
        actor_id=actorId,
        category=category,
        action=action,
        entity_type=entityType,
        entity_id=entityId,
        outcome=outcome,
        search=search,
        limit=max(1, min(limit, 1000)),
    )
    if not companyId and user["role"] != "platform_admin":
        records = [
            item
            for item in records
            if item.get("companyId") is None or core.allowed(user, item["companyId"])
        ]
    if dateFrom:
        records = [item for item in records if item.get("createdAt", "") >= dateFrom]
    if dateTo:
        upper = dateTo if "T" in dateTo else f"{dateTo}T23:59:59.999Z"
        records = [item for item in records if item.get("createdAt", "") <= upper]
    return records


def _governance_report(report_id: str, user: dict, company_id: str | None) -> dict:
    root_scope = company_id is None
    if company_id:
        _company_for_user(company_id, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP reports require root or MSP role",
        )
    if report_id == "access-review" and user["role"] != "platform_admin":
        raise HTTPException(403, "Effective access reporting requires platform admin role")
    if report_id == "integration-health" and not root_scope:
        raise HTTPException(400, "Integration health is an MSP-level report")
    companies = [item for item in REPOSITORY.list_companies() if core.allowed(user, item["id"])]
    assets = [item for item in REPOSITORY.list_assets() if core.allowed(user, item["companyId"])]
    asset_ids = {item["id"] for item in assets if not company_id or item["companyId"] == company_id}
    relationships = [
        item
        for item in REPOSITORY.list_relationships()
        if item["fromId"] in asset_ids and item["toId"] in asset_ids
    ]
    changes = [item for item in REPOSITORY.list_changes() if core.allowed(user, item["companyId"])]
    audit_events = REPOSITORY.list_audit_events(company_id, limit=1000)
    if root_scope and user["role"] != "platform_admin":
        audit_events = [
            item
            for item in audit_events
            if item.get("companyId") is None or core.allowed(user, item["companyId"])
        ]
    return build_report(
        report_id,
        companies=companies,
        assets=assets,
        relationships=relationships,
        changes=changes,
        users=REPOSITORY.list_users() if root_scope else [],
        integrations=REPOSITORY.list_integrations() if root_scope else [],
        sync_runs=REPOSITORY.list_sync_runs() if root_scope else [],
        audit_events=audit_events,
        company_id=company_id,
    )


@api.get("/api/reports/catalog", tags=["governance"])
def reports_catalog(request: Request, companyId: str | None = None) -> list[dict]:
    user = current_user(request)
    if companyId:
        _company_for_user(companyId, user)
    else:
        _require_role(
            user,
            {"platform_admin", "msp_operator"},
            "MSP reports require root or MSP role",
        )
    records = report_catalog(companyId is None)
    if user["role"] != "platform_admin":
        records = [item for item in records if item["id"] != "access-review"]
    return records


@api.get("/api/reports/{report_id}/preview", tags=["governance"])
def preview_report(report_id: str, request: Request, companyId: str | None = None) -> dict:
    user = current_user(request)
    try:
        report = _governance_report(report_id, user, companyId)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return {
        **report,
        "rows": report["rows"][:100],
        "previewLimited": len(report["rows"]) > 100,
    }


@api.get("/api/reports/{report_id}/download", tags=["governance"])
def download_report(
    report_id: str, request: Request, format: str = "pdf", companyId: str | None = None
) -> Response:
    user = current_user(request)
    if format not in {"pdf", "xlsx", "csv"}:
        raise HTTPException(400, "Choose PDF, XLSX or CSV")
    try:
        report = _governance_report(report_id, user, companyId)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    branding = REPOSITORY.get_msp_branding()
    if format == "csv":
        content, media_type = render_csv(report), "text/csv; charset=utf-8"
    elif format == "xlsx":
        content, media_type = (
            render_xlsx(report, branding),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        content, media_type = render_pdf(report, branding), "application/pdf"
    REPOSITORY.record_audit_event(
        companyId,
        user["id"],
        "report",
        canonical_uuid("report", f"{report_id}:{report['generatedAt']}"),
        "downloaded",
        after={
            "reportId": report_id,
            "title": report["title"],
            "format": format,
            "rowCount": report["summary"]["rowCount"],
        },
    )
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{report_filename(report, format)}"'
        },
    )


def mount_frontend(application: FastAPI, frontend_dist: Path) -> None:
    """Mount a compiled frontend or a stable build-required fallback page."""
    if frontend_dist.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=frontend_dist, html=True),
            name="frontend",
        )
        return

    @application.get("/{path:path}", include_in_schema=False)
    def missing_react_application(path: str):
        """Explain how to build the frontend without accessing request-derived paths."""

        return HTMLResponse(
            "<h1>Frontend build is missing</h1><p>Run <code>npm run build</code> and restart the application.</p>",
            status_code=503,
        )


mount_frontend(api, FRONTEND_DIST)


app = api
