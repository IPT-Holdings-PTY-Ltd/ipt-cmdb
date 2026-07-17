"""FastAPI application for the CMDB Hub web platform.

FastAPI owns the complete public API surface. PostgreSQL is the sole
operational source of truth whenever a database is configured; the local state
repository exists only for setup, development and isolated unit tests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

import app as core
from src.cmdb.audit import AuditContext, reset_audit_context, set_audit_context
from src.cmdb.change_control import (
    change_pdf_filename,
    create_change_record,
    preview_change_impact,
    render_change_pdf,
    transition_change_record,
    update_change_record,
)
from src.cmdb.data_quality import RULES as DATA_QUALITY_RULES
from src.cmdb.data_quality import evaluate_data_quality
from src.cmdb.reports import (
    build_report,
    render_csv,
    render_pdf,
    render_xlsx,
    report_catalog,
    report_filename,
)
from src.cmdb.repository import PostgresCmdbRepository, StateRepository, canonical_uuid

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", ROOT / "frontend" / "dist"))
LOGGER = logging.getLogger("cmdb.api")


api = FastAPI(
    title="CMDB Hub API",
    version="0.4.0",
    description="Tenant-aware CMDB API served directly by FastAPI.",
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


def current_user(request: Request) -> dict:
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

    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    session = core.SESSIONS.get(token)
    if not session or session["expiresAt"] <= datetime.now(UTC):
        core.SESSIONS.pop(token, None)
        raise HTTPException(401, "Sign in required")
    user = next((item for item in users if item["id"] == session["userId"]), None)
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
    companyIds: list[str] = Field(min_length=1)


class UserCreateRequest(BaseModel):
    """Validate a root or customer portal account."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=512)
    accountType: str
    companyId: str | None = None
    companyIds: list[str] = Field(default_factory=list)
    groupIds: list[str] = Field(default_factory=list)


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
    provider: str = Field(pattern="^(connectwise|ncentral|passportal|future)$")
    priority: int = Field(default=100, ge=0, le=32767)


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


@api.get("/api/v2/health", tags=["platform"])
@api.get("/api/health", tags=["platform"])
def health() -> dict:
    return {
        "status": "setup_required" if core.DATABASE_MODE == "database setup" else "ok",
        "api": "FastAPI",
        "databaseMode": core.DATABASE_MODE,
        "repositoryMode": REPOSITORY.mode,
        "repositoryError": core.DATABASE_ERROR,
        "expectedSchemaVersion": core.SCHEMA_VERSION,
        "authentication": os.getenv("AUTH_MODE", "local"),
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
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=core.SESSION_TTL_SECONDS)
    core.SESSIONS[token] = {"userId": user["id"], "expiresAt": expires_at}
    request.state.current_user = user
    REPOSITORY.record_audit_event(
        None,
        user["id"],
        "authentication",
        user["id"],
        "login_succeeded",
        after={"email": user["email"], "role": user["role"]},
        metadata={"mode": "local"},
    )
    return {
        "token": token,
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "user": core.public_user(user),
    }


@api.get("/api/auth/config", tags=["authentication"])
def auth_config() -> dict:
    mode = os.getenv("AUTH_MODE", "local").lower()
    external = mode == "easy_auth"
    return {
        "mode": mode,
        "external": external,
        "localLoginEnabled": _local_login_enabled(),
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
    user = None
    if session:
        user = next(
            (item for item in REPOSITORY.list_users() if item["id"] == session["userId"]),
            None,
        )
    core.SESSIONS.pop(bearer, None)
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
    return core.public_user(current_user(request))


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
        core.DATABASE_ERROR = f"Database prepared but repository activation failed: {str(error).splitlines()[0][:180]}"
        raise HTTPException(500, core.DATABASE_ERROR) from error
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


def _validate_group(
    payload: AccessGroupRequest, current_id: str | None = None
) -> tuple[str, list[str]]:
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
    return name, company_ids


@api.get("/api/access-groups", tags=["access"])
def list_access_groups(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "MSP access groups require root or MSP role",
    )
    visible = []
    for group in REPOSITORY.list_access_groups():
        company_ids = [
            company["id"]
            for company in REPOSITORY.list_companies()
            if core.allowed(user, company["id"]) and company["id"] in group["companyIds"]
        ]
        if company_ids:
            visible.append(
                {
                    "id": group["id"],
                    "name": group["name"],
                    "companyIds": company_ids,
                    "system": bool(group.get("system") or group["id"] == "all-managed-customers"),
                }
            )
    return visible


@api.post("/api/access-groups", status_code=201, tags=["access"])
def create_access_group(payload: AccessGroupRequest, request: Request) -> dict:
    actor = current_user(request)
    _require_role(
        actor,
        {"platform_admin"},
        "Customer group management requires platform admin role",
    )
    name, company_ids = _validate_group(payload)
    group_id = re.sub(r"[^a-z0-9]+", "-", (payload.id or name).lower()).strip("-")[:48]
    if not group_id:
        raise HTTPException(400, "Provide a valid group name")
    if any(group["id"] == group_id for group in REPOSITORY.list_access_groups()):
        raise HTTPException(409, "A group with that ID already exists")
    group = {"id": group_id, "name": name, "companyIds": company_ids, "system": False}
    with core.LOCK:
        group = REPOSITORY.create_access_group(group, actor["id"])
    return group


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
    name, company_ids = _validate_group(payload, group_id)
    with core.LOCK:
        group = REPOSITORY.update_access_group(
            group_id, {"name": name, "companyIds": company_ids}, actor["id"]
        )
    if group is None:
        raise HTTPException(404, "Customer group not found")
    return group


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
    with core.LOCK:
        REPOSITORY.delete_access_group(group_id, actor["id"])
    return {"deletedId": group_id}


@api.get("/api/rbac/roles", tags=["access"])
def rbac_roles(request: Request) -> list[dict]:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "RBAC requires platform admin role")
    return core.ROLE_TEMPLATES


@api.get("/api/rbac/effective", tags=["access"])
def rbac_effective(userId: str, request: Request) -> dict:
    user = current_user(request)
    _require_role(user, {"platform_admin"}, "RBAC requires platform admin role")
    target = next((item for item in REPOSITORY.list_users() if item["id"] == userId), None)
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
        for item in REPOSITORY.list_users()
        if user["role"] == "platform_admin"
        or any(core.allowed(user, company_id) for company_id in item["companyIds"])
    ]
    if companyId:
        records = [
            item
            for item in records
            if companyId in item["companyIds"] or item["role"] == "platform_admin"
        ]
    return [core.visible_user(item) for item in records]


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
        company_ids = set(payload.companyIds)
        groups = {group["id"]: group for group in REPOSITORY.list_access_groups()}
        for group_id in payload.groupIds:
            group = groups.get(group_id)
            if not group:
                raise HTTPException(400, "Choose valid MSP access groups")
            company_ids.update(
                company["id"]
                for company in REPOSITORY.list_companies()
                if company["id"] in group["companyIds"]
            )
        assigned_companies = sorted(company_ids)
        if not assigned_companies or not all(
            _known_company(company_id) for company_id in assigned_companies
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
        "groupIds": sorted(set(payload.groupIds)) if payload.accountType == "root" else [],
        "accountType": payload.accountType,
    }
    with core.LOCK:
        new_user = REPOSITORY.create_user(new_user, payload.password, actor["id"])
    return core.visible_user(new_user)


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
    from_asset = REPOSITORY.get_asset(payload.fromId)
    to_asset = REPOSITORY.get_asset(payload.toId)
    if not from_asset or not to_asset or from_asset["companyId"] != to_asset["companyId"]:
        raise HTTPException(400, "Choose two assets in the same company")
    user = current_user(request)
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
    relationship = next(
        (item for item in REPOSITORY.list_relationships() if item["id"] == relationship_id),
        None,
    )
    if not relationship:
        raise HTTPException(404, "Relationship not found")
    source = REPOSITORY.get_asset(relationship["fromId"])
    if not source:
        raise HTTPException(404, "Relationship source asset not found")
    user = current_user(request)
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
        configured = core.configured(item["type"])
        records.append(
            {
                **item,
                "scope": "msp",
                "enabled": configured or item["enabled"],
                "status": "Ready"
                if configured and item["status"] == "Not configured"
                else item["status"],
            }
        )
    return records


@api.post("/api/integrations/{kind}/sync", tags=["integrations"])
def run_integration_sync(kind: str, request: Request) -> dict:
    user = current_user(request)
    _require_role(
        user,
        {"platform_admin", "msp_operator"},
        "Sync requires MSP operator or platform admin role",
    )
    if kind not in {"connectwise", "ncentral", "passportal"}:
        raise HTTPException(404, "Integration not found")
    if not any(
        item["type"] == kind and item.get("scope", "msp") == "msp"
        for item in REPOSITORY.list_integrations()
    ):
        raise HTTPException(404, "Integration not found")
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


@api.get("/{path:path}", include_in_schema=False)
def react_application(path: str):
    if not FRONTEND_DIST.exists():
        return HTMLResponse(
            "<h1>Frontend build is missing</h1><p>Run <code>npm run build</code> and restart the application.</p>",
            status_code=503,
        )
    candidate = (FRONTEND_DIST / path).resolve()
    if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
        return FileResponse(candidate)
    if not path:
        return FileResponse(FRONTEND_DIST / "index.html")
    return Response(status_code=404)


app = api
