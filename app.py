"""CMDB Hub transitional state repository and domain helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.cmdb.migrations import apply_migrations, latest_schema_version

ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_FILE = DATA_DIR / "cmdb.json"
DATABASE_CONFIG_FILE = DATA_DIR / "database-config.json"
DATABASE_URL: str | None = None
DATABASE_SOURCE = "not configured"
DATABASE_ERROR: str | None = None
DATABASE_MODE = "local development state"
CANONICAL_DATABASE_INITIALIZED = False
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
LOCK = threading.Lock()
SCHEMA_VERSION = latest_schema_version(ROOT)
PORTABLE_BACKUP_VERSION = 2

SEED = {
    "companies": [
        {"id": "acme", "name": "Acme Manufacturing", "externalIds": {"connectwise": "125"}},
        {"id": "northwind", "name": "Northwind Traders", "externalIds": {"connectwise": "222"}},
    ],
    "users": [
        {"id": "admin", "email": "admin@example.com", "passwordHash": "pbkdf2_sha256$310000$cSTaQ5-dvwYYII0lPZEJFQ==$lyUPmkLv2PnaWrfhryHh19M_L8OWGRHzy2DtNzkVchc=", "role": "platform_admin", "companyIds": ["*"]},
        {"id": "msp", "email": "msp@example.com", "passwordHash": "pbkdf2_sha256$310000$wHqVsKnu-McTf-ZW-2l-gA==$wuFxNKtUAwz3F98GpQ-MAawD-ViHYN8Atyr8AZ_eqtQ=", "role": "msp_operator", "companyIds": ["acme", "northwind"]},
        {"id": "client", "email": "client@acme.example", "passwordHash": "pbkdf2_sha256$310000$NglhiTlhCfZqjuiUUVCggw==$GhspKU-GzdhnK1WbjWrQaCW-UAAoo_mQ7Znc4y4rWyo=", "role": "client_reader", "companyIds": ["acme"]},
    ],
    "assets": [
        {"id": "asset-1", "companyId": "acme", "name": "ACME-DC01", "type": "Server", "status": "Active", "source": "ncentral", "externalId": "nc-801", "lastSeen": "2026-07-10T07:10:00Z", "fields": {"os": "Windows Server 2022", "serial": "ACME001", "ip": "10.10.0.10"}},
        {"id": "asset-2", "companyId": "acme", "name": "Alice Smith", "type": "Credential owner", "status": "Active", "source": "passportal", "externalId": "pp-445", "lastSeen": "2026-07-10T06:50:00Z", "fields": {"email": "alice@acme.example"}},
        {"id": "asset-3", "companyId": "northwind", "name": "NW-LT-022", "type": "Workstation", "status": "Active", "source": "ncentral", "externalId": "nc-1022", "lastSeen": "2026-07-09T19:00:00Z", "fields": {"os": "Windows 11 Pro", "serial": "NW022"}},
    ],
    "relationships": [{"id": "rel-1", "fromId": "asset-1", "toId": "asset-2", "type": "used_by"}],
    "integrations": [
        {"id": "connectwise", "name": "ConnectWise Manage", "type": "connectwise", "enabled": False, "mode": "configured_by_environment", "lastSync": None, "status": "Not configured"},
        {"id": "ncentral", "name": "N-central", "type": "ncentral", "enabled": False, "mode": "configured_by_environment", "lastSync": None, "status": "Not configured"},
        {"id": "passportal", "name": "Passportal", "type": "passportal", "enabled": False, "mode": "configured_by_environment", "lastSync": None, "status": "Not configured"},
    ], "syncRuns": []
}

def now() -> str: return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def saved_database_url() -> tuple[str | None, str]:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"], "environment"
    if DATABASE_CONFIG_FILE.exists():
        try:
            value = json.loads(DATABASE_CONFIG_FILE.read_text(encoding="utf-8"))
            return value.get("url"), "saved local configuration"
        except (OSError, json.JSONDecodeError):
            return None, "not configured"
    return None, "not configured"

DATABASE_URL, DATABASE_SOURCE = saved_database_url()

def postgres_connection(database_url: str | None = None):
    """Open a short-lived API-owned PostgreSQL connection when DATABASE_URL is set."""
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("PostgreSQL mode needs `python -m pip install -r requirements.txt`.") from error
    return psycopg.connect(database_url or DATABASE_URL, connect_timeout=8)

def load_local_db() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        value = json.loads(DATA_FILE.read_text())
        value.setdefault("branding", {"acme": {"name": "Acme Manufacturing", "accent": "#50d5b9", "logoText": "A"}, "northwind": {"name": "Northwind Traders", "accent": "#7c9cff", "logoText": "N"}})
        return value
    DATA_FILE.write_text(json.dumps(SEED, indent=2)); return json.loads(json.dumps(SEED))

def apply_postgres_schema(database_url: str) -> None:
    apply_migrations(lambda: postgres_connection(database_url), ROOT)

def build_seed_state(mode: str, source: dict | None = None) -> dict:
    """Build the initial API state for a newly created PostgreSQL database."""
    source = deepcopy(source or SEED)
    if mode == "demo":
        state = deepcopy(SEED)
    elif mode == "empty":
        administrators = [item for item in source.get("users", []) if item.get("role") == "platform_admin"]
        if not administrators:
            administrators = [deepcopy(SEED["users"][0])]
        state = {
            "companies": [],
            "users": administrators,
            "assets": [],
            "relationships": [],
            "changes": [],
            "branding": {},
            "mspBranding": deepcopy(source.get("mspBranding") or {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C"}),
            "accessGroups": [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}],
            "integrations": [{**deepcopy(item), "enabled": False, "lastSync": None, "status": "Not configured"} for item in source.get("integrations", SEED["integrations"])],
            "syncRuns": [],
            "auditEvents": [],
        }
    elif mode == "current":
        state = source
    else:
        raise ValueError("Seed mode must be current, empty or demo")
    state.setdefault("branding", {})
    state.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C"})
    state.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
    state.setdefault("changes", [])
    state.setdefault("syncRuns", [])
    state.setdefault("contacts", [])
    state.setdefault("contactResponsibilities", [])
    return state

def database_diagnostics(database_url: str | None = None) -> dict:
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, version(), "
            "has_schema_privilege(current_user, 'public', 'USAGE'), "
            "has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        database, current_user, version, can_use_schema, can_create_schema = cursor.fetchone()
        cursor.execute(
            "SELECT to_regclass('public.schema_migrations'), to_regclass('public.users'), "
            "to_regclass('public.legacy_application_state')"
        )
        migration_table, users_table, legacy_state_table = cursor.fetchone()
        versions: list[str] = []
        initialized = False
        if migration_table:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY applied_at, version")
            versions = [row[0] for row in cursor.fetchall()]
        if users_table:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM users WHERE status <> 'disabled')")
            initialized = bool(cursor.fetchone()[0])
        schema_version = versions[-1] if versions else None
        return {
            "ok": True,
            "database": database,
            "databaseUser": current_user,
            "server": version.split(",")[0],
            "schemaVersion": schema_version,
            "expectedSchemaVersion": SCHEMA_VERSION,
            "migrationsPending": schema_version != SCHEMA_VERSION,
            "initialized": initialized,
            "schemaState": "blank" if not migration_table else "ready" if schema_version == SCHEMA_VERSION else "outdated",
            "canUseSchema": bool(can_use_schema),
            "canCreateSchemaObjects": bool(can_create_schema),
            "legacyStatePending": bool(legacy_state_table and not initialized),
        }

def database_url_from_settings(data: dict) -> str:
    if data.get("url"):
        parsed = urlparse(data["url"])
        if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
            raise ValueError("Use a valid PostgreSQL connection URL")
        return data["url"]
    required = ("host", "database", "username")
    if not all(data.get(item) for item in required):
        raise ValueError("Host, database and username are required")
    port = int(data.get("port") or 5432)
    sslmode = data.get("sslmode") or "prefer"
    password = quote(data.get("password", ""), safe="")
    return f"postgresql://{quote(data['username'], safe='')}:{password}@{data['host']}:{port}/{quote(data['database'], safe='')}?sslmode={quote(sslmode, safe='')}"

def safe_database_settings() -> dict:
    if not DATABASE_URL:
        return {}
    parsed = urlparse(DATABASE_URL)
    query = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in parsed.query.split("&") if "=" in item}
    return {"host": parsed.hostname or "", "port": parsed.port or 5432, "database": parsed.path.lstrip("/"), "username": parsed.username or "", "sslmode": query.get("sslmode", "prefer")}

def database_status() -> dict:
    status = {"configured": bool(DATABASE_URL), "available": DATABASE_MODE == "PostgreSQL", "mode": DATABASE_MODE, "source": DATABASE_SOURCE, "managedByEnvironment": DATABASE_SOURCE == "environment", "error": DATABASE_ERROR, "settings": safe_database_settings(), "expectedSchemaVersion": SCHEMA_VERSION}
    if DATABASE_URL:
        try:
            status.update(database_diagnostics())
        except Exception as error:
            status.update({"available": False, "diagnosticError": str(error).split("\n", 1)[0][:240]})
    return status

def test_database_url(database_url: str) -> dict:
    try:
        return database_diagnostics(database_url)
    except Exception as error:
        return {"ok": False, "error": str(error).split("\n", 1)[0][:240]}

def save_database_settings(data: dict) -> dict:
    global DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE, CANONICAL_DATABASE_INITIALIZED
    if os.getenv("DATABASE_URL") and os.getenv("ALLOW_UI_DATABASE_CONFIG", "false").lower() != "true":
        return {"ok": False, "error": "This connection is managed by the environment. Update the Container App or Key Vault reference and restart the service."}
    database_url = database_url_from_settings(data)
    test = test_database_url(database_url)
    if not test["ok"]: return test
    apply_postgres_schema(database_url)
    seed_mode = data.get("seedMode") or "current"
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM users WHERE status <> 'disabled')")
        initialized = bool(cursor.fetchone()[0])
        cursor.execute("SELECT to_regclass('public.legacy_application_state')")
        legacy_table = cursor.fetchone()[0]
        legacy_row = None
        if legacy_table and not initialized:
            cursor.execute("SELECT state FROM legacy_application_state WHERE state_key = 'cmdb_api'")
            legacy_row = cursor.fetchone()
    seeded = not initialized
    target_state = legacy_row[0] if legacy_row else build_seed_state(seed_mode, DB)
    DB.clear()
    DB.update(deepcopy(target_state))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_CONFIG_FILE.write_text(json.dumps({"url": database_url}), encoding="utf-8")
    try: os.chmod(DATABASE_CONFIG_FILE, 0o600)
    except OSError: pass
    DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE = database_url, "saved local configuration", None, "PostgreSQL"
    CANONICAL_DATABASE_INITIALIZED = initialized
    return {"ok": True, "message": "PostgreSQL configured and activated." if not seeded else f"PostgreSQL initialized with the {seed_mode} seed.", "seeded": seeded, "seedMode": seed_mode if seeded else "existing", **database_status()}

def load_db() -> dict:
    global DATABASE_ERROR, DATABASE_MODE, CANONICAL_DATABASE_INITIALIZED
    if DATABASE_URL:
        try:
            apply_postgres_schema(DATABASE_URL)
            with postgres_connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT EXISTS (SELECT 1 FROM users WHERE status <> 'disabled')")
                CANONICAL_DATABASE_INITIALIZED = bool(cursor.fetchone()[0])
                legacy_row = None
                cursor.execute("SELECT to_regclass('public.legacy_application_state')")
                if cursor.fetchone()[0]:
                    cursor.execute("SELECT state FROM legacy_application_state WHERE state_key = %s", ("cmdb_api",))
                    legacy_row = cursor.fetchone()
                DATABASE_MODE = "PostgreSQL"
                if legacy_row:
                    return legacy_row[0]
                return build_seed_state(os.getenv("DATABASE_SEED_MODE", "current").lower(), SEED)
        except Exception as error:
            DATABASE_ERROR = str(error).split("\n", 1)[0][:240]
            DATABASE_MODE = "PostgreSQL unavailable"
            raise RuntimeError(f"Configured PostgreSQL is unavailable: {DATABASE_ERROR}") from error
    local_state = load_local_db()
    if os.getenv("ALLOW_LOCAL_DEVELOPMENT", "false").lower() == "true":
        DATABASE_MODE = "local development state"
        return local_state
    DATABASE_MODE = "database setup"
    return build_seed_state("empty", local_state)
def save_db(value: dict) -> None:
    global DATABASE_ERROR, DATABASE_MODE
    if DATABASE_URL:
        # Canonical repositories own PostgreSQL writes. The former JSON document
        # is never updated in PostgreSQL mode.
        return
    if DATABASE_MODE == "database setup":
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True); DATA_FILE.write_text(json.dumps(value, indent=2))

def backup_document(state: dict | None = None) -> dict:
    """Create a portable application-state backup without transient sessions."""
    state = json.loads(json.dumps(state if state is not None else DB))
    checksum = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "format": "cmdb-hub-backup",
        "version": PORTABLE_BACKUP_VERSION,
        "createdAt": now(),
        "databaseMode": DATABASE_MODE,
        "backupType": "portable_operational_state",
        "included": ["customers", "contacts and responsibility history", "users and local credential hashes", "configuration items", "relationships", "changes", "integrations", "MSP branding"],
        "excluded": ["PostgreSQL roles and grants", "canonical audit history", "raw integration observations", "point-in-time transaction history"],
        "state": state,
        "checksum": f"sha256:{checksum}",
    }

def preview_backup(document: dict) -> dict:
    if document.get("format") != "cmdb-hub-backup" or document.get("version") not in {1, PORTABLE_BACKUP_VERSION}:
        raise ValueError("Choose a supported CMDB Hub portable backup (version 1 or 2).")
    state = document.get("state")
    required_lists = ("companies", "users", "assets", "relationships", "integrations", "syncRuns")
    if not isinstance(state, dict) or any(not isinstance(state.get(key), list) for key in required_lists):
        raise ValueError("The backup is missing required CMDB collections.")
    if not any(user.get("role") == "platform_admin" and user.get("id") for user in state["users"] if isinstance(user, dict)):
        raise ValueError("The backup must retain at least one platform admin.")
    if document.get("version") == PORTABLE_BACKUP_VERSION:
        expected = "sha256:" + hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if document.get("checksum") != expected:
            raise ValueError("The portable backup checksum does not match; the file may be incomplete or modified.")
    return {
        "valid": True,
        "version": document["version"],
        "createdAt": document.get("createdAt"),
        "companies": len(state["companies"]),
        "users": len(state["users"]),
        "assets": len(state["assets"]),
        "relationships": len(state["relationships"]),
        "changes": len(state.get("changes", [])),
        "contacts": len(state.get("contacts", [])),
        "warning": "Portable import adds or updates operational records. It does not delete canonical records absent from the file and is not a PostgreSQL point-in-time restore.",
    }

def restore_backup(document: dict) -> dict:
    preview = preview_backup(document)
    state = document.get("state")
    state.setdefault("branding", {})
    state.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C", "logoDataUrl": "", "logoFileName": "", "supportEmail": "", "supportUrl": "", "supportPhone": "", "welcomeMessage": "", "reportFooter": "", "confidentialityLabel": "Internal use only"})
    state.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
    state.setdefault("changes", [])
    state.setdefault("contacts", [])
    state.setdefault("contactResponsibilities", [])
    with LOCK:
        DB.clear(); DB.update(state); save_db(DB)
    return {"message": "Portable backup imported successfully.", "companies": len(DB["companies"]), "assets": len(DB["assets"]), "restoredFrom": document.get("createdAt", "an unknown date"), "warning": preview["warning"]}
DB = load_db()
DB.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C", "logoDataUrl": "", "logoFileName": "", "supportEmail": "", "supportUrl": "", "supportPhone": "", "welcomeMessage": "", "reportFooter": "", "confidentialityLabel": "Internal use only"})
DB.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
DB.setdefault("changes", [])
DB.setdefault("contacts", [])
DB.setdefault("contactResponsibilities", [])
ROLE_TEMPLATES = [
    {"id": "platform_admin", "name": "Platform admin", "scope": "Root", "system": True, "permissions": ["Manage customers, groups and users", "Manage MSP integrations and branding", "Manage RBAC and database configuration", "Full customer CMDB access"]},
    {"id": "msp_operator", "name": "MSP operator", "scope": "Assigned customer groups", "system": True, "permissions": ["View and manage assigned customer CIs", "Manage customer users", "Run approved integration syncs", "No RBAC or database configuration"]},
    {"id": "client_reader", "name": "Customer reader", "scope": "One customer", "system": True, "permissions": ["View permitted customer CIs and relationships", "No configuration changes", "No MSP tools"]},
]
def allowed(user: dict | None, company_id: str) -> bool:
    return bool(user and (user["role"] == "platform_admin" or "*" in user["companyIds"] or company_id in user["companyIds"]))
def public_user(user: dict) -> dict: return {k: user[k] for k in ("id", "email", "role", "companyIds")}
def visible_user(user: dict) -> dict: return {**public_user(user), "accountType": user.get("accountType", "root" if user["role"] in {"platform_admin", "msp_operator"} else "customer")}
def can_manage(user: dict, company_id: str) -> bool: return bool(user and user["role"] in {"platform_admin", "msp_operator"} and allowed(user, company_id))
def known_company(company_id: str | None) -> bool: return bool(company_id and any(company["id"] == company_id for company in DB["companies"]))
METADATA_DEFAULTS = {
    "lifecycle": "in_service", "operationalStatus": "unknown", "criticality": "medium",
    "environment": "production", "site": "", "serviceOwner": "", "technicalOwner": "",
    "custodian": "", "vendor": "", "model": "", "purchaseDate": "", "warrantyEnd": "",
    "renewalDate": "", "endOfLifeDate": "", "reviewDate": "", "businessOwner": "",
    "signoffDelegate": "", "department": "", "userPopulation": "", "rtoHours": "",
    "rpoHours": "", "supportHours": "", "dataClassification": "internal",
    "customerFacing": "no", "signoffRequired": "yes", "aliases": "", "businessDescription": "",
    "virtualizationPlatform": "", "clusterName": "", "haEnabled": "no",
    "minimumHosts": "", "capacityStatus": "unknown", "mobility": "automatic",
    "powerState": "unknown", "guestOs": "", "cpuCount": "", "memoryGb": "",
    "storageGb": "", "maintenanceMode": "no", "protectionStatus": "unknown",
    "displayLayer": "", "networkZone": "", "vlanId": "", "subnet": "",
    "ipAddress": "", "networkRole": "", "redundancyGroup": "", "redundancyRole": "",
}
LIFECYCLE_STATES = {"planned", "ordered", "received", "in_stock", "in_service", "maintenance", "retired", "disposed"}
OPERATIONAL_STATES = {"unknown", "healthy", "warning", "critical", "offline"}
CRITICALITY_LEVELS = {"low", "medium", "high", "critical"}
ENVIRONMENTS = {"production", "pre_production", "test", "development", "disaster_recovery", "other"}
DATA_CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
YES_NO_VALUES = {"yes", "no"}
CAPACITY_STATES = {"unknown", "sufficient", "constrained", "insufficient"}
MOBILITY_STATES = {"automatic", "manual", "pinned"}
POWER_STATES = {"unknown", "running", "stopped", "suspended", "offline"}
PROTECTION_STATES = {"unknown", "protected", "degraded", "unprotected"}
DISPLAY_LAYERS = {"", "business", "application", "compute", "virtualization", "storage", "network", "foundation"}
RELATIONSHIP_TYPES = {
    "connected_to", "depends_on", "installed_on", "licensed_to", "used_by", "related_to",
    "hosts", "backs_up", "managed_by", "member_of", "stored_on", "provided_by", "protected_by",
}
IMPACT_POLICIES = {"required", "degraded", "redundant", "informational"}
SYMMETRIC_RELATIONSHIP_TYPES = {"connected_to", "related_to"}

def relationship_exists(relationships: list[dict], from_id: str, to_id: str, relationship_type: str) -> bool:
    """Reject duplicate edges, including reversed duplicates for symmetric relationship types."""
    return any(
        item.get("type") == relationship_type
        and (
            (item.get("fromId") == from_id and item.get("toId") == to_id)
            or (
                relationship_type in SYMMETRIC_RELATIONSHIP_TYPES
                and item.get("fromId") == to_id
                and item.get("toId") == from_id
            )
        )
        for item in relationships
    )

def would_create_dependency_cycle(relationships: list[dict], from_id: str, to_id: str) -> bool:
    """A stored `A depends_on B` edge is invalid when B already reaches A."""
    adjacency: dict[str, list[str]] = {}
    for item in relationships:
        if item.get("type") == "depends_on":
            adjacency.setdefault(item["fromId"], []).append(item["toId"])
    pending, visited = [to_id], set()
    while pending:
        current = pending.pop()
        if current == from_id: return True
        if current in visited: continue
        visited.add(current); pending.extend(adjacency.get(current, []))
    return False
def asset_metadata(asset: dict) -> dict:
    metadata = {**METADATA_DEFAULTS, **(asset.get("metadata") or {})}
    if not asset.get("metadata") and asset.get("status") == "Retired": metadata["lifecycle"] = "retired"
    elif not asset.get("metadata") and asset.get("status") == "Planned": metadata["lifecycle"] = "planned"
    elif not asset.get("metadata") and asset.get("status") == "Active": metadata["operationalStatus"] = "healthy"
    return metadata
def normalise_metadata(value: dict | None, status: str | None = None) -> dict:
    if value is not None and not isinstance(value, dict): raise ValueError("Asset metadata must be an object")
    metadata = {**METADATA_DEFAULTS, **(value or {})}
    metadata = {key: str(item).strip()[:160] for key, item in metadata.items() if key in METADATA_DEFAULTS and item is not None}
    if metadata["lifecycle"] not in LIFECYCLE_STATES: raise ValueError("Choose a valid lifecycle state")
    if metadata["operationalStatus"] not in OPERATIONAL_STATES: raise ValueError("Choose a valid operational status")
    if metadata["criticality"] not in CRITICALITY_LEVELS: raise ValueError("Choose a valid criticality")
    if metadata["environment"] not in ENVIRONMENTS: raise ValueError("Choose a valid environment")
    if metadata["dataClassification"] not in DATA_CLASSIFICATIONS: raise ValueError("Choose a valid data classification")
    if metadata["customerFacing"] not in YES_NO_VALUES: raise ValueError("Choose whether the service is customer-facing")
    if metadata["signoffRequired"] not in YES_NO_VALUES: raise ValueError("Choose whether business signoff is required")
    if metadata["haEnabled"] not in YES_NO_VALUES: raise ValueError("Choose whether high availability is enabled")
    if metadata["maintenanceMode"] not in YES_NO_VALUES: raise ValueError("Choose whether maintenance mode is enabled")
    if metadata["capacityStatus"] not in CAPACITY_STATES: raise ValueError("Choose a valid cluster capacity state")
    if metadata["mobility"] not in MOBILITY_STATES: raise ValueError("Choose a valid virtual machine mobility mode")
    if metadata["powerState"] not in POWER_STATES: raise ValueError("Choose a valid power state")
    if metadata["protectionStatus"] not in PROTECTION_STATES: raise ValueError("Choose a valid protection status")
    if metadata["displayLayer"] not in DISPLAY_LAYERS: raise ValueError("Choose a valid relationship display layer")
    if status == "Retired": metadata["lifecycle"] = "retired"
    return metadata
def asset_view(asset: dict) -> dict: return {**asset, "metadata": asset_metadata(asset)}
def attention_items(assets: list[dict], companies: list[dict], today: date | None = None, watch_days: int = 90) -> list[dict]:
    today = today or date.today(); company_names = {company["id"]: company["name"] for company in companies}; items = []
    for asset in assets:
        metadata = asset_metadata(asset)
        if metadata["lifecycle"] in {"retired", "disposed"}: continue
        for kind, field in (("Renewal", "renewalDate"), ("End of life", "endOfLifeDate")):
            value = metadata.get(field)
            if not value: continue
            try: days = (date.fromisoformat(value) - today).days
            except ValueError: continue
            if days <= watch_days:
                items.append({"assetId": asset["id"], "assetName": asset["name"], "assetType": asset["type"], "companyId": asset["companyId"], "companyName": company_names.get(asset["companyId"], asset["companyId"]), "kind": kind, "date": value, "days": days, "criticality": metadata["criticality"], "owner": metadata["technicalOwner"] or metadata["serviceOwner"] or "No owner recorded"})
    return sorted(items, key=lambda item: (item["days"], item["companyName"], item["assetName"]))
def customer_overview(companies: list[dict], assets: list[dict]) -> list[dict]:
    attention = attention_items(assets, companies); by_company = {company["id"]: [] for company in companies}
    for asset in assets: by_company.setdefault(asset["companyId"], []).append(asset)
    result = []
    for company in companies:
        company_assets = by_company.get(company["id"], []); items = [item for item in attention if item["companyId"] == company["id"]]
        result.append({"companyId": company["id"], "companyName": company["name"], "assetCount": len(company_assets), "criticalCount": sum(asset_metadata(asset)["criticality"] == "critical" for asset in company_assets), "attentionCount": len(items), "overdueCount": sum(item["days"] < 0 for item in items)})
    return result

_DASHBOARD_LAYER_LABELS = {
    "foundation": "Physical / cloud foundation",
    "network": "Network and security",
    "storage": "Storage and backup",
    "virtualization": "Virtualization",
    "compute": "Compute and operating systems",
    "application": "Applications and data",
    "business": "Business systems",
}
_DASHBOARD_IMPACT_RULES = {
    "connected_to": (False, True), "depends_on": (True, True), "installed_on": (True, True),
    "licensed_to": (False, True), "used_by": (False, True), "related_to": (False, False),
    "hosts": (False, True), "backs_up": (False, False), "managed_by": (True, True),
    "member_of": (False, False), "stored_on": (True, True), "provided_by": (True, True),
    "protected_by": (True, True),
}

def dashboard_display_layer(asset: dict) -> str:
    metadata = asset_metadata(asset); explicit = metadata.get("displayLayer")
    if explicit in _DASHBOARD_LAYER_LABELS: return explicit
    asset_type = str(asset.get("type", "")).lower()
    signal = f'{asset_type} {asset.get("name", "")} {metadata.get("vendor", "")}'.lower()
    if asset_type == "business system" or "credential" in asset_type or "person" in asset_type: return "business"
    if any(value in asset_type for value in ("software", "service", "licence", "license")): return "storage" if any(value in signal for value in ("backup", "veeam", "repository")) else "application"
    if asset_type in {"datastore", "storage array"}: return "storage"
    if "virtualization" in asset_type or asset_type in {"hypervisor host", "virtual network"}: return "virtualization"
    if "network" in asset_type or "vpn" in asset_type or any(value in signal for value in ("firewall", "switch", "router", "wan", "internet")): return "foundation" if any(value in signal for value in ("wan", "internet")) else "network"
    if asset_type == "virtual machine" or any(value in asset_type for value in ("server", "workstation")) or asset_type == "device": return "compute"
    return "foundation"

def _dashboard_owner(asset: dict) -> str:
    metadata = asset_metadata(asset)
    if asset.get("type") == "Business system" and metadata.get("businessOwner"): return metadata["businessOwner"]
    return metadata.get("technicalOwner") or metadata.get("serviceOwner") or metadata.get("custodian") or ""

def _dashboard_stale(asset: dict, today: date) -> bool:
    if asset.get("source") == "manual" or not asset.get("lastSeen"): return False
    try: seen = datetime.fromisoformat(str(asset["lastSeen"]).replace("Z", "+00:00")).date()
    except ValueError: return True
    return (today - seen).days > 7

def _dashboard_business_scope(system_id: str, relationships: list[dict]) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for relationship in relationships:
        reverse_for_impact, traverses = _DASHBOARD_IMPACT_RULES.get(relationship.get("type"), (False, False))
        if not traverses or relationship.get("impactPolicy") == "informational": continue
        source, target = relationship.get("fromId"), relationship.get("toId")
        if reverse_for_impact: source, target = target, source
        adjacency.setdefault(target, []).append(source)
    visited, queue = {system_id}, [system_id]
    while queue:
        current = queue.pop(0)
        for next_id in adjacency.get(current, []):
            if next_id not in visited: visited.add(next_id); queue.append(next_id)
    return visited

def dashboard_snapshot(companies: list[dict], assets: list[dict], relationships: list[dict], changes: list[dict] | None = None, integrations: list[dict] | None = None, sync_runs: list[dict] | None = None, company_id: str | None = None, today: date | None = None) -> dict:
    """Build the action-oriented dashboard contract for MSP or customer scope."""
    today = today or date.today(); changes = changes or []; integrations = integrations or []; sync_runs = sync_runs or []
    company_names = {company["id"]: company["name"] for company in companies}
    visible_assets = [asset_view(asset) for asset in assets if not company_id or asset.get("companyId") == company_id]
    asset_ids = {asset["id"] for asset in visible_assets}
    visible_relationships = [item for item in relationships if item.get("fromId") in asset_ids and item.get("toId") in asset_ids]
    relationship_counts = {asset_id: 0 for asset_id in asset_ids}
    for relationship in visible_relationships:
        relationship_counts[relationship["fromId"]] += 1; relationship_counts[relationship["toId"]] += 1
    lifecycle_by_asset: dict[str, list[dict]] = {}
    for item in attention_items(visible_assets, companies, today=today): lifecycle_by_asset.setdefault(item["assetId"], []).append(item)

    attention = []
    for asset in visible_assets:
        metadata = asset["metadata"]; reasons, score = [], 0
        operational = metadata.get("operationalStatus", "unknown")
        if operational in {"offline", "degraded"}: reasons.append(f"Operational status: {operational}"); score += 6 if operational == "offline" else 4
        for item in lifecycle_by_asset.get(asset["id"], []):
            reasons.append(f'{item["kind"]}: {abs(item["days"])} days overdue' if item["days"] < 0 else f'{item["kind"]}: {item["days"]} days'); score += 5 if item["days"] < 0 else 3
        if not _dashboard_owner(asset): reasons.append("Accountable owner missing"); score += 2
        if relationship_counts.get(asset["id"], 0) == 0: reasons.append("No relationships recorded"); score += 1
        if _dashboard_stale(asset, today): reasons.append("Integration data is stale"); score += 2
        if reasons:
            attention.append({"assetId": asset["id"], "assetName": asset["name"], "assetType": asset["type"], "companyId": asset["companyId"], "companyName": company_names.get(asset["companyId"], asset["companyId"]), "owner": _dashboard_owner(asset) or "Unassigned", "reasons": reasons, "score": score, "severity": "critical" if score >= 6 else "warning" if score >= 3 else "info"})
    attention.sort(key=lambda item: (-item["score"], item["companyName"], item["assetName"]))

    business_systems = []
    memberships: dict[str, set[str]] = {}
    assets_by_id = {asset["id"]: asset for asset in visible_assets}
    for system in [asset for asset in visible_assets if asset.get("type") == "Business system"]:
        scope = _dashboard_business_scope(system["id"], visible_relationships) & asset_ids
        for asset_id in scope: memberships.setdefault(asset_id, set()).add(system["id"])
        support = [assets_by_id[item] for item in scope if item != system["id"]]
        support_attention = [item for item in attention if item["assetId"] in scope]
        metadata = system["metadata"]
        business_systems.append({"id": system["id"], "name": system["name"], "companyId": system["companyId"], "companyName": company_names.get(system["companyId"], system["companyId"]), "owner": metadata.get("businessOwner") or "Unassigned", "serviceOwner": metadata.get("serviceOwner") or "", "operationalStatus": metadata.get("operationalStatus") or "unknown", "criticality": metadata.get("criticality") or "medium", "rtoHours": metadata.get("rtoHours") or "", "rpoHours": metadata.get("rpoHours") or "", "supportCount": len(support), "attentionCount": len(support_attention)})
    business_systems.sort(key=lambda item: (-item["attentionCount"], item["name"]))

    layer_coverage = []
    for layer, label in _DASHBOARD_LAYER_LABELS.items():
        layer_assets = [asset for asset in visible_assets if dashboard_display_layer(asset) == layer]
        layer_coverage.append({"id": layer, "label": label, "count": len(layer_assets), "attentionCount": sum(any(item["assetId"] == asset["id"] for item in attention) for asset in layer_assets)})

    customer_rows = []
    for company in companies:
        company_assets = [asset for asset in visible_assets if asset["companyId"] == company["id"]]
        company_attention = [item for item in attention if item["companyId"] == company["id"]]
        company_lifecycle = [item for items in lifecycle_by_asset.values() for item in items if item["companyId"] == company["id"]]
        customer_rows.append({"companyId": company["id"], "companyName": company["name"], "assetCount": len(company_assets), "businessSystemCount": sum(asset["type"] == "Business system" for asset in company_assets), "unhealthyCount": sum(asset["metadata"].get("operationalStatus") in {"offline", "degraded"} for asset in company_assets), "attentionCount": len(company_attention), "overdueCount": sum(item["days"] < 0 for item in company_lifecycle), "missingOwnerCount": sum(not _dashboard_owner(asset) for asset in company_assets), "staleCount": sum(_dashboard_stale(asset, today) for asset in company_assets), "riskScore": sum(item["score"] for item in company_attention)})
    customer_rows.sort(key=lambda item: (-item["riskScore"], item["companyName"]))

    latest_runs: dict[str, dict] = {}
    for run in sorted(sync_runs, key=lambda item: item.get("startedAt") or item.get("finishedAt") or "", reverse=True): latest_runs.setdefault(run.get("type", ""), run)
    integration_rows = []
    for item in integrations:
        configured_in_environment = configured(item.get("type", ""))
        status = "Ready" if configured_in_environment and item.get("status") == "Not configured" else item.get("status", "Unknown")
        last_run = latest_runs.get(item.get("type", ""))
        integration_rows.append({**item, "enabled": configured_in_environment or item.get("enabled", False), "status": status, "lastRunStatus": last_run.get("status") if last_run else "never", "lastRunAt": (last_run.get("finishedAt") or last_run.get("startedAt")) if last_run else item.get("lastSync")})

    upcoming_changes = []
    for change in changes:
        if company_id and change.get("companyId") != company_id: continue
        planned = change.get("plannedStart")
        if not planned: continue
        try: planned_date = datetime.fromisoformat(str(planned).replace("Z", "+00:00")).date()
        except ValueError: continue
        if planned_date < today or str(change.get("status", "")).lower() in {"completed", "cancelled"}: continue
        upcoming_changes.append({"id": change["id"], "number": change.get("number", ""), "title": change.get("title", "Untitled change"), "companyId": change.get("companyId"), "companyName": company_names.get(change.get("companyId"), change.get("companyId")), "plannedStart": planned, "riskLevel": change.get("riskLevel", "medium"), "impactCount": len(change.get("impactSnapshot") or [])})
    upcoming_changes.sort(key=lambda item: item["plannedStart"])

    unhealthy = sum(asset["metadata"].get("operationalStatus") in {"offline", "degraded"} for asset in visible_assets)
    missing_owner = sum(not _dashboard_owner(asset) for asset in visible_assets)
    unlinked = sum(relationship_counts.get(asset["id"], 0) == 0 for asset in visible_assets)
    stale = sum(_dashboard_stale(asset, today) for asset in visible_assets)
    return {
        "scope": "customer" if company_id else "msp",
        "summary": {"customers": len({asset["companyId"] for asset in visible_assets}) if company_id else len(companies), "assets": len(visible_assets), "businessSystems": len(business_systems), "unhealthy": unhealthy, "attention": len(attention), "missingOwners": missing_owner, "unlinked": unlinked, "stale": stale, "sharedDependencies": sum(len(system_ids) > 1 for system_ids in memberships.values()), "integrationIssues": sum(str(item.get("status", "")).lower() not in {"ready", "connected", "success", "healthy"} or item.get("lastRunStatus") in {"failed", "blocked"} for item in integration_rows)},
        "attention": attention[:20], "customers": customer_rows, "businessSystems": business_systems, "layers": layer_coverage,
        "integrations": integration_rows, "upcomingChanges": upcoming_changes[:8],
    }
def configured(kind: str) -> bool:
    if kind == "connectwise": return all(os.getenv(x) for x in ("CW_BASE_URL", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY"))
    key = kind.upper(); return bool(os.getenv(f"{key}_BASE_URL") and os.getenv(f"{key}_API_TOKEN"))

DEMO_ASSETS = [
    {"id":"demo-fw","companyId":"acme","name":"ACME-EDGE-FW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Edge firewall"}}, {"id":"demo-core","companyId":"acme","name":"ACME-CORE-SW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Core switch"}}, {"id":"demo-access","companyId":"acme","name":"ACME-ACCESS-SW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Access switch"}}, {"id":"demo-db","companyId":"acme","name":"ACME-DB01","type":"Virtual machine","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Database server"},"metadata":{"virtualizationPlatform":"VMware vSphere","clusterName":"ACME-PROD-CL01","haEnabled":"yes","mobility":"automatic","powerState":"running","guestOs":"Windows Server 2022","cpuCount":"8","memoryGb":"32","storageGb":"500","protectionStatus":"protected"}}, {"id":"demo-app","companyId":"acme","name":"ACME-APP01","type":"Virtual machine","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Application server"},"metadata":{"virtualizationPlatform":"VMware vSphere","clusterName":"ACME-PROD-CL01","haEnabled":"yes","mobility":"automatic","powerState":"running","guestOs":"Windows Server 2022","cpuCount":"4","memoryGb":"16","storageGb":"180","protectionStatus":"protected"}}, {"id":"demo-files","companyId":"acme","name":"ACME-FILES01","type":"Virtual machine","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"File server"},"metadata":{"virtualizationPlatform":"VMware vSphere","clusterName":"ACME-PROD-CL01","haEnabled":"yes","mobility":"automatic","powerState":"running","guestOs":"Windows Server 2022","cpuCount":"4","memoryGb":"16","storageGb":"2000","protectionStatus":"protected"}}, {"id":"demo-ws","companyId":"acme","name":"ACME-FIN-WS01","type":"Workstation","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"owner":"Finance"}}, {"id":"demo-printer","companyId":"acme","name":"ACME-PRN-FIN01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Finance printer"}}, {"id":"demo-crm","companyId":"acme","name":"CRM Web Service","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"service":"Customer portal"}}, {"id":"demo-dbapp","companyId":"acme","name":"Finance PostgreSQL","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"database":"finance"}}, {"id":"demo-license","companyId":"acme","name":"PostgreSQL Enterprise Licence","type":"Licence","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"renewal":"2027-06-30"}}, {"id":"demo-backup","companyId":"acme","name":"Veeam Backup Agent","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"policy":"Nightly"}},
    {"id":"demo-cluster","companyId":"acme","name":"ACME-PROD-CL01","type":"Virtualization cluster","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Production compute cluster"},"metadata":{"virtualizationPlatform":"VMware vSphere","haEnabled":"yes","minimumHosts":"1","capacityStatus":"sufficient","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"critical"}},
    {"id":"demo-esx1","companyId":"acme","name":"ACME-ESX01","type":"Hypervisor host","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Current host for finance workloads"},"metadata":{"vendor":"VMware","model":"ESXi 8","virtualizationPlatform":"VMware vSphere","clusterName":"ACME-PROD-CL01","powerState":"running","maintenanceMode":"no","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"high"}},
    {"id":"demo-esx2","companyId":"acme","name":"ACME-ESX02","type":"Hypervisor host","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"HA failover host"},"metadata":{"vendor":"VMware","model":"ESXi 8","virtualizationPlatform":"VMware vSphere","clusterName":"ACME-PROD-CL01","powerState":"running","maintenanceMode":"no","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"high"}},
    {"id":"demo-ds1","companyId":"acme","name":"ACME-SAN-DS01","type":"Datastore","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"capacity":"8 TB","free":"3.2 TB"},"metadata":{"vendor":"VMware","virtualizationPlatform":"VMware vSphere","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"critical"}},
    {"id":"demo-san","companyId":"acme","name":"ACME-SAN01","type":"Storage array","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Shared production storage"},"metadata":{"vendor":"Dell","model":"PowerVault","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"critical"}},
    {"id":"demo-vcenter","companyId":"acme","name":"ACME-VC01","type":"Virtualization manager","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"vCenter Server"},"metadata":{"vendor":"VMware","model":"vCenter 8","virtualizationPlatform":"VMware vSphere","technicalOwner":"Platform Team","operationalStatus":"healthy","criticality":"high"}},
    {"id":"demo-sage200","companyId":"acme","name":"Sage 200","type":"Business system","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"service":"Finance and accounting"},"metadata":{"lifecycle":"in_service","operationalStatus":"healthy","criticality":"critical","environment":"production","businessOwner":"Finance Director","serviceOwner":"Finance Applications","technicalOwner":"Managed Services","signoffDelegate":"Financial Controller","department":"Finance","userPopulation":"42 finance users","rtoHours":"4","rpoHours":"1","supportHours":"Business hours + month end","dataClassification":"confidential","customerFacing":"no","signoffRequired":"yes","aliases":"Sage, Finance ERP","businessDescription":"Finance, purchasing, stock and management reporting."}},
    {"id":"demo-reporting","companyId":"acme","name":"Executive Reporting","type":"Business system","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"service":"Management reporting"},"metadata":{"lifecycle":"in_service","operationalStatus":"healthy","criticality":"high","environment":"production","businessOwner":"Chief Financial Officer","serviceOwner":"Business Intelligence","technicalOwner":"Managed Services","signoffDelegate":"Finance Director","department":"Executive","userPopulation":"12 decision makers","rtoHours":"8","rpoHours":"4","supportHours":"Business hours","dataClassification":"confidential","customerFacing":"no","signoffRequired":"yes","aliases":"BI Reports","businessDescription":"Executive dashboards and scheduled management packs."}},
    {"id":"demo-wan","companyId":"acme","name":"Internet / MPLS WAN","type":"Network service","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Upstream connectivity"},"metadata":{"displayLayer":"foundation","networkZone":"WAN","networkRole":"internet_wan","technicalOwner":"Network Operations","operationalStatus":"healthy","criticality":"critical"}},
]
DEMO_NETWORK_METADATA = {
    "demo-fw": {"displayLayer":"network","site":"Head Office","networkZone":"Edge","ipAddress":"203.0.113.10","networkRole":"firewall","redundancyGroup":"ACME-EDGE","redundancyRole":"active","vendor":"Fortinet","model":"FortiGate"},
    "demo-core": {"displayLayer":"network","site":"Head Office","networkZone":"Core","ipAddress":"10.10.0.1","networkRole":"core_switch","vlanId":"trunk","vendor":"Cisco"},
    "demo-access": {"displayLayer":"network","site":"Head Office","networkZone":"Access","ipAddress":"10.10.0.11","networkRole":"access_switch","vlanId":"110","subnet":"10.10.110.0/24","vendor":"Cisco"},
    "demo-ws": {"displayLayer":"compute","site":"Head Office","networkZone":"Finance","vlanId":"110","subnet":"10.10.110.0/24","ipAddress":"10.10.110.21"},
    "demo-printer": {"displayLayer":"network","site":"Head Office","networkZone":"Finance","vlanId":"110","subnet":"10.10.110.0/24","ipAddress":"10.10.110.40","networkRole":"endpoint"},
    "demo-crm": {"displayLayer":"application"}, "demo-dbapp": {"displayLayer":"application"},
    "demo-license": {"displayLayer":"application"}, "demo-backup": {"displayLayer":"storage"},
    "demo-db": {"displayLayer":"compute"}, "demo-app": {"displayLayer":"compute"}, "demo-files": {"displayLayer":"compute"},
    "demo-cluster": {"displayLayer":"virtualization"}, "demo-esx1": {"displayLayer":"virtualization"}, "demo-esx2": {"displayLayer":"virtualization"},
    "demo-ds1": {"displayLayer":"storage"}, "demo-san": {"displayLayer":"storage"}, "demo-vcenter": {"displayLayer":"virtualization"},
    "demo-sage200": {"displayLayer":"business"}, "demo-reporting": {"displayLayer":"business"},
}
for _demo_asset in DEMO_ASSETS:
    if _demo_asset["id"] in DEMO_NETWORK_METADATA:
        _demo_asset["metadata"] = {**(_demo_asset.get("metadata") or {}), **DEMO_NETWORK_METADATA[_demo_asset["id"]]}
DEMO_RELATIONSHIPS = [{"id":"demo-r1","fromId":"demo-fw","toId":"demo-core","type":"connected_to"},{"id":"demo-r2","fromId":"demo-core","toId":"demo-access","type":"connected_to"},{"id":"demo-r3","fromId":"demo-core","toId":"demo-db","type":"connected_to"},{"id":"demo-r4","fromId":"demo-core","toId":"demo-app","type":"connected_to"},{"id":"demo-r5","fromId":"demo-core","toId":"demo-files","type":"connected_to"},{"id":"demo-r6","fromId":"demo-access","toId":"demo-ws","type":"connected_to"},{"id":"demo-r7","fromId":"demo-access","toId":"demo-printer","type":"connected_to"},{"id":"demo-r8","fromId":"demo-crm","toId":"demo-app","type":"installed_on"},{"id":"demo-r9","fromId":"demo-dbapp","toId":"demo-db","type":"installed_on"},{"id":"demo-r10","fromId":"demo-license","toId":"demo-db","type":"licensed_to"},{"id":"demo-r11","fromId":"demo-backup","toId":"demo-files","type":"installed_on"},{"id":"demo-r12","fromId":"demo-app","toId":"demo-db","type":"depends_on"},{"id":"demo-r13","fromId":"demo-ws","toId":"demo-crm","type":"depends_on"},{"id":"demo-r14","fromId":"demo-printer","toId":"demo-files","type":"depends_on"},{"id":"demo-r15","fromId":"demo-sage200","toId":"demo-crm","type":"depends_on","impactPolicy":"required"},{"id":"demo-r16","fromId":"demo-sage200","toId":"demo-dbapp","type":"depends_on","impactPolicy":"required"},{"id":"demo-r17","fromId":"demo-reporting","toId":"demo-dbapp","type":"depends_on","impactPolicy":"required"},{"id":"demo-r18","fromId":"demo-reporting","toId":"demo-crm","type":"depends_on","impactPolicy":"degraded"},
    {"id":"demo-r19","fromId":"demo-esx1","toId":"demo-cluster","type":"member_of","impactPolicy":"informational"},{"id":"demo-r20","fromId":"demo-esx2","toId":"demo-cluster","type":"member_of","impactPolicy":"informational"},
    {"id":"demo-r21","fromId":"demo-db","toId":"demo-cluster","type":"member_of","impactPolicy":"informational"},{"id":"demo-r22","fromId":"demo-app","toId":"demo-cluster","type":"member_of","impactPolicy":"informational"},{"id":"demo-r23","fromId":"demo-files","toId":"demo-cluster","type":"member_of","impactPolicy":"informational"},
    {"id":"demo-r24","fromId":"demo-esx1","toId":"demo-db","type":"hosts","impactPolicy":"required"},{"id":"demo-r25","fromId":"demo-esx1","toId":"demo-app","type":"hosts","impactPolicy":"required"},{"id":"demo-r26","fromId":"demo-esx2","toId":"demo-files","type":"hosts","impactPolicy":"required"},
    {"id":"demo-r27","fromId":"demo-db","toId":"demo-ds1","type":"stored_on","impactPolicy":"required"},{"id":"demo-r28","fromId":"demo-app","toId":"demo-ds1","type":"stored_on","impactPolicy":"required"},{"id":"demo-r29","fromId":"demo-files","toId":"demo-ds1","type":"stored_on","impactPolicy":"required"},
    {"id":"demo-r30","fromId":"demo-ds1","toId":"demo-san","type":"provided_by","impactPolicy":"required"},{"id":"demo-r31","fromId":"demo-cluster","toId":"demo-vcenter","type":"managed_by","impactPolicy":"degraded"},
    {"id":"demo-r32","fromId":"demo-db","toId":"demo-cluster","type":"protected_by","impactPolicy":"required"},{"id":"demo-r33","fromId":"demo-app","toId":"demo-cluster","type":"protected_by","impactPolicy":"required"},{"id":"demo-r34","fromId":"demo-files","toId":"demo-cluster","type":"protected_by","impactPolicy":"required"},{"id":"demo-r35","fromId":"demo-cluster","toId":"demo-ds1","type":"depends_on","impactPolicy":"required"},{"id":"demo-r36","fromId":"demo-wan","toId":"demo-fw","type":"connected_to","impactPolicy":"required"}]
def add_demo_data() -> dict:
    asset_ids, relation_ids = {item["id"] for item in DB["assets"]}, {item["id"] for item in DB["relationships"]}
    assets, relations = [item for item in DEMO_ASSETS if item["id"] not in asset_ids], [item for item in DEMO_RELATIONSHIPS if item["id"] not in relation_ids]
    DB["assets"].extend(assets); DB["relationships"].extend(relations); save_db(DB)
    return {"assetsAdded": len(assets), "relationshipsAdded": len(relations)}

def execute_sync(kind: str) -> dict:
    run = {"id": str(uuid.uuid4()), "type": kind, "startedAt": now(), "status": "success", "discovered": 0, "imported": 0, "message": ""}
    if not configured(kind): run.update(status="blocked", message="Integration variables are not configured. See .env.example.")
    elif kind == "connectwise":
        base = os.environ["CW_BASE_URL"].rstrip("/")
        raw = f"{os.environ['CW_COMPANY_ID']}+{os.environ['CW_PUBLIC_KEY']}:{os.environ['CW_PRIVATE_KEY']}"
        headers = {"Authorization": "Basic " + base64.b64encode(raw.encode()).decode(), "clientId": os.getenv("CW_CLIENT_ID", "msp-cmdb-hub")}
        try:
            with urlopen(Request(f"{base}/company/companies?pageSize=100", headers=headers), timeout=20) as response:
                run["discovered"] = len(json.loads(response.read())); run["message"] = "Connection verified. Company import is deliberately review-gated."
        except HTTPError as error: run.update(status="failed", message=f"ConnectWise returned {error.code}")
        except URLError as error: run.update(status="failed", message=f"ConnectWise connection failed: {error.reason}")
    else: run["message"] = f"{kind} adapter configuration detected; endpoint mapping remains review-gated."
    run["finishedAt"] = now()
    return run

def sync(kind: str) -> dict:
    """Local JSON fallback wrapper for the provider connection check."""
    run = execute_sync(kind)
    with LOCK:
        DB["syncRuns"] = [run] + DB["syncRuns"][:49]
        integration = next((x for x in DB["integrations"] if x["type"] == kind), None)
        if integration: integration.update(lastSync=run["finishedAt"], status="Healthy" if run["status"] == "success" else run["status"], enabled=configured(kind))
        save_db(DB)
    return run
