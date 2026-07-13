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

ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_FILE = DATA_DIR / "cmdb.json"
DATABASE_CONFIG_FILE = DATA_DIR / "database-config.json"
DATABASE_URL: str | None = None
DATABASE_SOURCE = "not configured"
DATABASE_ERROR: str | None = None
DATABASE_MODE = "local JSON fallback"
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
LOCK = threading.Lock()
SCHEMA_VERSION = "2026.07.13.1"
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

def write_postgres_state(value: dict, database_url: str | None = None) -> None:
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO application_state (state_key, state, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (state_key) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            ("cmdb_api", json.dumps(value)),
        )

def apply_postgres_schema(database_url: str) -> None:
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cmdb_hub_schema'))")
        cursor.execute(schema)

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
    return state

def database_diagnostics(database_url: str | None = None) -> dict:
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), current_user, version(), "
            "has_schema_privilege(current_user, 'public', 'USAGE'), "
            "has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        database, current_user, version, can_use_schema, can_create_schema = cursor.fetchone()
        cursor.execute("SELECT to_regclass('public.schema_migrations'), to_regclass('public.application_state')")
        migration_table, state_table = cursor.fetchone()
        versions: list[str] = []
        initialized = False
        if migration_table:
            cursor.execute("SELECT version FROM schema_migrations ORDER BY applied_at, version")
            versions = [row[0] for row in cursor.fetchall()]
        if state_table:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM application_state WHERE state_key = 'cmdb_api')")
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
    global DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE
    if os.getenv("DATABASE_URL") and os.getenv("ALLOW_UI_DATABASE_CONFIG", "false").lower() != "true":
        return {"ok": False, "error": "This connection is managed by the environment. Update the Container App or Key Vault reference and restart the service."}
    database_url = database_url_from_settings(data)
    test = test_database_url(database_url)
    if not test["ok"]: return test
    apply_postgres_schema(database_url)
    seed_mode = data.get("seedMode") or "current"
    with postgres_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state FROM application_state WHERE state_key = 'cmdb_api'")
        row = cursor.fetchone()
    seeded = row is None
    target_state = build_seed_state(seed_mode, DB) if seeded else row[0]
    if seeded:
        write_postgres_state(target_state, database_url)
    DB.clear()
    DB.update(deepcopy(target_state))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_CONFIG_FILE.write_text(json.dumps({"url": database_url}), encoding="utf-8")
    try: os.chmod(DATABASE_CONFIG_FILE, 0o600)
    except OSError: pass
    DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE = database_url, "saved local configuration", None, "PostgreSQL"
    return {"ok": True, "message": "PostgreSQL configured and activated." if not seeded else f"PostgreSQL initialized with the {seed_mode} seed.", "seeded": seeded, "seedMode": seed_mode if seeded else "existing", **database_status()}

def load_db() -> dict:
    global DATABASE_ERROR, DATABASE_MODE
    if DATABASE_URL:
        try:
            apply_postgres_schema(DATABASE_URL)
            with postgres_connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT state FROM application_state WHERE state_key = %s", ("cmdb_api",))
                row = cursor.fetchone()
                if row:
                    DATABASE_MODE = "PostgreSQL"
                    return row[0]
                seed_mode = os.getenv("DATABASE_SEED_MODE", "current").lower()
                state = build_seed_state(seed_mode, SEED)
                cursor.execute("INSERT INTO application_state (state_key, state) VALUES (%s, %s::jsonb)", ("cmdb_api", json.dumps(state)))
                DATABASE_MODE = "PostgreSQL"
                return state
        except Exception as error:
            DATABASE_ERROR = str(error).split("\n", 1)[0][:240]
            DATABASE_MODE = "local JSON fallback"
    return load_local_db()
def save_db(value: dict) -> None:
    global DATABASE_ERROR, DATABASE_MODE
    if DATABASE_URL:
        try:
            write_postgres_state(value); DATABASE_MODE = "PostgreSQL"; return
        except Exception as error:
            DATABASE_ERROR = str(error).split("\n", 1)[0][:240]; DATABASE_MODE = "local JSON fallback"
    DATA_DIR.mkdir(parents=True, exist_ok=True); DATA_FILE.write_text(json.dumps(value, indent=2))

def backup_document() -> dict:
    """Create a portable application-state backup without transient sessions."""
    state = json.loads(json.dumps(DB))
    checksum = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "format": "cmdb-hub-backup",
        "version": PORTABLE_BACKUP_VERSION,
        "createdAt": now(),
        "databaseMode": DATABASE_MODE,
        "backupType": "portable_operational_state",
        "included": ["customers", "users and local credential hashes", "configuration items", "relationships", "changes", "integrations", "MSP branding"],
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
        "warning": "Portable import adds or updates operational records. It does not delete canonical records absent from the file and is not a PostgreSQL point-in-time restore.",
    }

def restore_backup(document: dict) -> dict:
    preview = preview_backup(document)
    state = document.get("state")
    state.setdefault("branding", {})
    state.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C", "logoDataUrl": "", "logoFileName": "", "supportEmail": "", "supportUrl": "", "supportPhone": "", "welcomeMessage": "", "reportFooter": "", "confidentialityLabel": "Internal use only"})
    state.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
    state.setdefault("changes", [])
    with LOCK:
        DB.clear(); DB.update(state); save_db(DB)
    return {"message": "Portable backup imported successfully.", "companies": len(DB["companies"]), "assets": len(DB["assets"]), "restoredFrom": document.get("createdAt", "an unknown date"), "warning": preview["warning"]}
DB = load_db()
DB.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "secondaryAccent": "#7997ff", "logoText": "C", "logoDataUrl": "", "logoFileName": "", "supportEmail": "", "supportUrl": "", "supportPhone": "", "welcomeMessage": "", "reportFooter": "", "confidentialityLabel": "Internal use only"})
DB.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
DB.setdefault("changes", [])
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
METADATA_DEFAULTS = {"lifecycle": "in_service", "operationalStatus": "unknown", "criticality": "medium", "environment": "production", "site": "", "serviceOwner": "", "technicalOwner": "", "custodian": "", "vendor": "", "model": "", "purchaseDate": "", "warrantyEnd": "", "renewalDate": "", "endOfLifeDate": "", "reviewDate": ""}
LIFECYCLE_STATES = {"planned", "ordered", "received", "in_stock", "in_service", "maintenance", "retired", "disposed"}
OPERATIONAL_STATES = {"unknown", "healthy", "warning", "critical", "offline"}
CRITICALITY_LEVELS = {"low", "medium", "high", "critical"}
ENVIRONMENTS = {"production", "pre_production", "test", "development", "disaster_recovery", "other"}
RELATIONSHIP_TYPES = {"connected_to", "depends_on", "installed_on", "licensed_to", "used_by", "related_to"}
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
def configured(kind: str) -> bool:
    if kind == "connectwise": return all(os.getenv(x) for x in ("CW_BASE_URL", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY"))
    key = kind.upper(); return bool(os.getenv(f"{key}_BASE_URL") and os.getenv(f"{key}_API_TOKEN"))

DEMO_ASSETS = [
    {"id":"demo-fw","companyId":"acme","name":"ACME-EDGE-FW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Edge firewall"}}, {"id":"demo-core","companyId":"acme","name":"ACME-CORE-SW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Core switch"}}, {"id":"demo-access","companyId":"acme","name":"ACME-ACCESS-SW01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Access switch"}}, {"id":"demo-db","companyId":"acme","name":"ACME-DB01","type":"Server","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Database server"}}, {"id":"demo-app","companyId":"acme","name":"ACME-APP01","type":"Server","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Application server"}}, {"id":"demo-files","companyId":"acme","name":"ACME-FILES01","type":"Server","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"File server"}}, {"id":"demo-ws","companyId":"acme","name":"ACME-FIN-WS01","type":"Workstation","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"owner":"Finance"}}, {"id":"demo-printer","companyId":"acme","name":"ACME-PRN-FIN01","type":"Network device","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"role":"Finance printer"}}, {"id":"demo-crm","companyId":"acme","name":"CRM Web Service","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"service":"Customer portal"}}, {"id":"demo-dbapp","companyId":"acme","name":"Finance PostgreSQL","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"database":"finance"}}, {"id":"demo-license","companyId":"acme","name":"PostgreSQL Enterprise Licence","type":"Licence","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"renewal":"2027-06-30"}}, {"id":"demo-backup","companyId":"acme","name":"Veeam Backup Agent","type":"Software","status":"Active","source":"demo","externalId":None,"lastSeen":"2026-07-10T08:00:00Z","fields":{"policy":"Nightly"}},
]
DEMO_RELATIONSHIPS = [{"id":"demo-r1","fromId":"demo-fw","toId":"demo-core","type":"connected_to"},{"id":"demo-r2","fromId":"demo-core","toId":"demo-access","type":"connected_to"},{"id":"demo-r3","fromId":"demo-core","toId":"demo-db","type":"connected_to"},{"id":"demo-r4","fromId":"demo-core","toId":"demo-app","type":"connected_to"},{"id":"demo-r5","fromId":"demo-core","toId":"demo-files","type":"connected_to"},{"id":"demo-r6","fromId":"demo-access","toId":"demo-ws","type":"connected_to"},{"id":"demo-r7","fromId":"demo-access","toId":"demo-printer","type":"connected_to"},{"id":"demo-r8","fromId":"demo-crm","toId":"demo-app","type":"installed_on"},{"id":"demo-r9","fromId":"demo-dbapp","toId":"demo-db","type":"installed_on"},{"id":"demo-r10","fromId":"demo-license","toId":"demo-db","type":"licensed_to"},{"id":"demo-r11","fromId":"demo-backup","toId":"demo-files","type":"installed_on"},{"id":"demo-r12","fromId":"demo-app","toId":"demo-db","type":"depends_on"},{"id":"demo-r13","fromId":"demo-ws","toId":"demo-crm","type":"depends_on"},{"id":"demo-r14","fromId":"demo-printer","toId":"demo-files","type":"depends_on"}]
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
