"""CMDB Hub: dependency-free Python web/API foundation for an MSP CMDB."""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timezone, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_FILE = DATA_DIR / "cmdb.json"
DATABASE_CONFIG_FILE = DATA_DIR / "database-config.json"
PUBLIC = ROOT / "public"
DATABASE_URL: str | None = None
DATABASE_SOURCE = "not configured"
DATABASE_ERROR: str | None = None
DATABASE_MODE = "local JSON fallback"
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
LOCK = threading.Lock()

SEED = {
    "companies": [
        {"id": "acme", "name": "Acme Manufacturing", "externalIds": {"connectwise": "125"}},
        {"id": "northwind", "name": "Northwind Traders", "externalIds": {"connectwise": "222"}},
    ],
    "users": [
        {"id": "admin", "email": "admin@example.com", "password": "ChangeMe!", "role": "platform_admin", "companyIds": ["*"]},
        {"id": "msp", "email": "msp@example.com", "password": "ChangeMe!", "role": "msp_operator", "companyIds": ["acme", "northwind"]},
        {"id": "client", "email": "client@acme.example", "password": "ChangeMe!", "role": "client_reader", "companyIds": ["acme"]},
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
        cursor.execute(schema)

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
    return {"configured": bool(DATABASE_URL), "available": DATABASE_MODE == "PostgreSQL", "mode": DATABASE_MODE, "source": DATABASE_SOURCE, "error": DATABASE_ERROR, "settings": safe_database_settings()}

def test_database_url(database_url: str) -> dict:
    try:
        with postgres_connection(database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), version()")
            database, version = cursor.fetchone()
        return {"ok": True, "database": database, "server": version.split(",")[0]}
    except Exception as error:
        return {"ok": False, "error": str(error).split("\n", 1)[0][:240]}

def save_database_settings(data: dict) -> dict:
    global DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE
    database_url = database_url_from_settings(data)
    test = test_database_url(database_url)
    if not test["ok"]: return test
    apply_postgres_schema(database_url)
    write_postgres_state(DB, database_url)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_CONFIG_FILE.write_text(json.dumps({"url": database_url}), encoding="utf-8")
    try: os.chmod(DATABASE_CONFIG_FILE, 0o600)
    except OSError: pass
    DATABASE_URL, DATABASE_SOURCE, DATABASE_ERROR, DATABASE_MODE = database_url, "saved local configuration", None, "PostgreSQL"
    return {"ok": True, "message": "PostgreSQL configured and current CMDB state migrated.", **database_status()}

def load_db() -> dict:
    global DATABASE_ERROR, DATABASE_MODE
    if DATABASE_URL:
        try:
            with postgres_connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT state FROM application_state WHERE state_key = %s", ("cmdb_api",))
                row = cursor.fetchone()
                if row:
                    DATABASE_MODE = "PostgreSQL"
                    return row[0]
                cursor.execute("INSERT INTO application_state (state_key, state) VALUES (%s, %s::jsonb)", ("cmdb_api", json.dumps(SEED)))
                DATABASE_MODE = "PostgreSQL"
                return json.loads(json.dumps(SEED))
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
    return {"format": "cmdb-hub-backup", "version": 1, "createdAt": now(), "databaseMode": DATABASE_MODE, "state": json.loads(json.dumps(DB))}

def restore_backup(document: dict) -> dict:
    if document.get("format") != "cmdb-hub-backup" or document.get("version") != 1:
        raise ValueError("Choose a CMDB Hub backup file (format version 1).")
    state = document.get("state")
    required_lists = ("companies", "users", "assets", "relationships", "integrations", "syncRuns")
    if not isinstance(state, dict) or any(not isinstance(state.get(key), list) for key in required_lists):
        raise ValueError("The backup is missing required CMDB collections.")
    if not any(user.get("role") == "platform_admin" and user.get("id") for user in state["users"] if isinstance(user, dict)):
        raise ValueError("The backup must retain at least one platform admin.")
    state.setdefault("branding", {})
    state.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "logoText": "C"})
    state.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
    with LOCK:
        DB.clear(); DB.update(state); save_db(DB)
    return {"message": "Backup restored successfully.", "companies": len(DB["companies"]), "assets": len(DB["assets"]), "restoredFrom": document.get("createdAt", "an unknown date")}
DB = load_db()
DB.setdefault("mspBranding", {"name": "CMDB Hub", "accent": "#50d5b9", "logoText": "C", "supportEmail": "", "supportUrl": "", "welcomeMessage": ""})
DB.setdefault("accessGroups", [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}])
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

def sync(kind: str) -> dict:
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
    with LOCK:
        DB["syncRuns"] = [run] + DB["syncRuns"][:49]
        integration = next((x for x in DB["integrations"] if x["type"] == kind), None)
        if integration: integration.update(lastSync=run["finishedAt"], status="Healthy" if run["status"] == "success" else run["status"], enabled=configured(kind))
        save_db(DB)
    return run

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(PUBLIC), **kwargs)
    def send_json(self, status: int, value: dict | list):
        raw = json.dumps(value).encode(); self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def payload(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
    def current_user(self) -> dict | None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer "); session = SESSIONS.get(token)
        if not session or session["expiresAt"] <= datetime.now(timezone.utc):
            SESSIONS.pop(token, None); return None
        return next((x for x in DB["users"] if x["id"] == session["userId"]), None)
    def require(self, company_id: str | None = None) -> dict | None:
        user = self.current_user()
        if not user: self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in required"}); return None
        if company_id and not allowed(user, company_id): self.send_json(HTTPStatus.FORBIDDEN, {"error": "You do not have access to this company"}); return None
        return user
    def do_POST(self):
        if self.path == "/api/login":
            data = self.payload(); user = next((x for x in DB["users"] if x["email"] == data.get("email") and hmac.compare_digest(x["password"], data.get("password", ""))), None)
            if not user: return self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid credentials"})
            token = secrets.token_urlsafe(32); expires_at = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS); SESSIONS[token] = {"userId": user["id"], "expiresAt": expires_at}
            return self.send_json(HTTPStatus.OK, {"token": token, "expiresAt": expires_at.isoformat().replace("+00:00", "Z"), "user": public_user(user)})
        if self.path == "/api/logout":
            SESSIONS.pop(self.headers.get("Authorization", "").removeprefix("Bearer "), None); return self.send_json(HTTPStatus.NO_CONTENT, {})
        if self.path == "/api/database/test":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Database configuration requires platform admin role"})
            try: result = test_database_url(database_url_from_settings(self.payload()))
            except (TypeError, ValueError) as error: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return self.send_json(HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST, result)
        if self.path == "/api/database/restore":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Backup restore requires platform admin role"})
            try: return self.send_json(HTTPStatus.OK, restore_backup(self.payload()))
            except ValueError as error: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        pieces = self.path.split("/")
        if self.path == "/api/demo-data":
            user = self.require("acme")
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Demo data requires platform admin role"})
            with LOCK: result = add_demo_data()
            return self.send_json(HTTPStatus.OK, result)
        if self.path == "/api/companies":
            data = self.payload(); actor = self.require()
            if not actor: return
            if actor["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Customer creation requires platform admin role"})
            name = str(data.get("name", "")).strip()[:100]
            slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("slug") or name).strip().lower()).strip("-")[:48]
            if not name or not slug: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Customer name is required"})
            if any(company["id"] == slug or company["name"].lower() == name.lower() for company in DB["companies"]): return self.send_json(HTTPStatus.CONFLICT, {"error": "A customer with that name or ID already exists"})
            company = {"id": slug, "name": name, "externalIds": {}}
            with LOCK:
                DB["companies"].append(company); DB["branding"][slug] = {"name": name, "accent": "#50d5b9", "logoText": name[0].upper()}; save_db(DB)
            return self.send_json(HTTPStatus.CREATED, company)
        if self.path == "/api/access-groups":
            data = self.payload(); actor = self.require()
            if not actor: return
            if actor["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Customer group management requires platform admin role"})
            name = str(data.get("name", "")).strip()[:80]; group_id = re.sub(r"[^a-z0-9]+", "-", str(data.get("id") or name).lower()).strip("-")[:48]
            company_ids = sorted(set(data.get("companyIds") or []))
            if not name or not group_id or not company_ids or not all(known_company(company_id) for company_id in company_ids): return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Provide a group name and at least one valid customer"})
            if any(group["id"] == group_id or group["name"].lower() == name.lower() for group in DB["accessGroups"]): return self.send_json(HTTPStatus.CONFLICT, {"error": "A group with that name or ID already exists"})
            group = {"id": group_id, "name": name, "companyIds": company_ids, "system": False}
            with LOCK: DB["accessGroups"].append(group); save_db(DB)
            return self.send_json(HTTPStatus.CREATED, group)
        if self.path == "/api/users":
            data = self.payload(); actor = self.require()
            if not actor: return
            account_type = data.get("accountType")
            email, password = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
            if not email or "@" not in email or len(password) < 8: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Email and a password of at least 8 characters are required"})
            if any(x["email"] == email for x in DB["users"]): return self.send_json(HTTPStatus.CONFLICT, {"error": "A user with this email already exists"})
            if account_type == "root":
                if actor["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Only platform admins can create root/MSP users"})
                company_ids = set(data.get("companyIds") or [])
                groups = {group["id"]: group for group in DB["accessGroups"]}
                for group_id in data.get("groupIds") or []:
                    group = groups.get(group_id)
                    if not group: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose valid MSP access groups"})
                    company_ids.update(company["id"] for company in DB["companies"] if "*" in group["companyIds"] or company["id"] in group["companyIds"])
                company_ids = sorted(company_ids)
                if not company_ids or not all(known_company(item) for item in company_ids): return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose at least one valid customer permission or access group"})
                role = "msp_operator"
            elif account_type == "customer":
                company_ids = [data.get("companyId")]
                if not known_company(company_ids[0]) or not can_manage(actor, company_ids[0]): return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Choose a customer you manage"})
                role = "client_reader"
            else: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose root or customer account type"})
            new_user = {"id": str(uuid.uuid4()), "email": email, "password": password, "role": role, "companyIds": company_ids, "accountType": account_type}
            with LOCK: DB["users"].append(new_user); save_db(DB)
            return self.send_json(HTTPStatus.CREATED, visible_user(new_user))
        if self.path == "/api/assets":
            data = self.payload(); company_id = data.get("companyId"); user = self.require(company_id)
            if not user: return
            if not can_manage(user, company_id): return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Asset changes require MSP operator or platform admin role"})
            required = [key for key in ("name", "type", "companyId") if not data.get(key)]
            if required: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": f"Missing required values: {', '.join(required)}"})
            if not known_company(company_id): return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose a valid customer before adding an asset"})
            try: metadata = normalise_metadata(data.get("metadata"), data.get("status"))
            except ValueError as error: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            asset = {"id": str(uuid.uuid4()), "companyId": company_id, "name": data["name"], "type": data["type"], "status": data.get("status", "Active"), "source": "manual", "externalId": None, "lastSeen": now(), "fields": data.get("fields", {}), "metadata": metadata}
            with LOCK: DB["assets"].append(asset); save_db(DB)
            return self.send_json(HTTPStatus.CREATED, asset)
        if self.path == "/api/relationships":
            data = self.payload(); from_asset = next((x for x in DB["assets"] if x["id"] == data.get("fromId")), None); to_asset = next((x for x in DB["assets"] if x["id"] == data.get("toId")), None)
            user = self.require(from_asset["companyId"] if from_asset else None)
            if not user: return
            if not from_asset or not to_asset or from_asset["companyId"] != to_asset["companyId"]: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Choose two assets in the same company"})
            if not can_manage(user, from_asset["companyId"]): return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Relationship changes require MSP operator or platform admin role"})
            relationship = {"id": str(uuid.uuid4()), "fromId": from_asset["id"], "toId": to_asset["id"], "type": data.get("type") or "related_to"}
            with LOCK: DB["relationships"].append(relationship); save_db(DB)
            return self.send_json(HTTPStatus.CREATED, relationship)
        if len(pieces) == 5 and pieces[:3] == ["", "api", "integrations"] and pieces[4] == "sync" and pieces[3] in {"connectwise", "ncentral", "passportal"}:
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Sync requires MSP operator or platform admin role"})
            return self.send_json(HTTPStatus.OK, sync(pieces[3]))
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
    def do_PUT(self):
        if self.path == "/api/database/config":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Database configuration requires platform admin role"})
            try:
                with LOCK: result = save_database_settings(self.payload())
            except (OSError, TypeError, ValueError) as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return self.send_json(HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST, result)
        pieces = self.path.split("/")
        if len(pieces) == 4 and pieces[:3] == ["", "api", "access-groups"]:
            data = self.payload(); actor = self.require()
            if not actor: return
            if actor["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Customer group management requires platform admin role"})
            group = next((item for item in DB["accessGroups"] if item["id"] == pieces[3]), None)
            if not group: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Customer group not found"})
            if group.get("system") or group["id"] == "all-managed-customers": return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "The All managed customers group is dynamic and cannot be edited"})
            name = str(data.get("name", "")).strip()[:80]; company_ids = sorted(set(data.get("companyIds") or []))
            if not name or not company_ids or not all(known_company(company_id) for company_id in company_ids): return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Provide a group name and at least one valid customer"})
            if any(item["id"] != group["id"] and item["name"].lower() == name.lower() for item in DB["accessGroups"]): return self.send_json(HTTPStatus.CONFLICT, {"error": "A group with that name already exists"})
            with LOCK: group.update(name=name, companyIds=company_ids); save_db(DB)
            return self.send_json(HTTPStatus.OK, group)
        if self.path != "/api/branding": return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        data = self.payload()
        if data.get("scope") == "msp":
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP branding requires root or MSP role"})
            brand = {"name": str(data.get("name") or "CMDB Hub")[:80], "accent": str(data.get("accent") or "#50d5b9")[:16], "logoText": str(data.get("logoText") or "C").upper()[:3], "supportEmail": str(data.get("supportEmail") or "")[:160], "supportUrl": str(data.get("supportUrl") or "")[:300], "welcomeMessage": str(data.get("welcomeMessage") or "")[:180]}
            with LOCK: DB["mspBranding"] = brand; save_db(DB)
            return self.send_json(HTTPStatus.OK, brand)
        company_id = data.get("companyId"); user = self.require(company_id)
        if not user: return
        if not can_manage(user, company_id): return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Branding changes require MSP operator or platform admin role"})
        company = next((x for x in DB["companies"] if x["id"] == company_id), None)
        if not company: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Company not found"})
        brand = {"name": str(data.get("name") or company["name"])[:80], "accent": str(data.get("accent") or "#50d5b9")[:16], "logoText": str(data.get("logoText") or company["name"][0]).upper()[:3]}
        with LOCK: DB["branding"][company_id] = brand; save_db(DB)
        self.send_json(HTTPStatus.OK, brand)
    def do_PATCH(self):
        pieces = self.path.split("/")
        if len(pieces) != 4 or pieces[:3] != ["", "api", "assets"]:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        asset = next((x for x in DB["assets"] if x["id"] == pieces[3]), None)
        if not asset: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Asset not found"})
        user = self.require(asset["companyId"])
        if not user: return
        if not can_manage(user, asset["companyId"]):
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Asset changes require MSP operator or platform admin role"})
        data = self.payload()
        for field in ("name", "type", "status", "fields"):
            if field in data: asset[field] = data[field]
        if "metadata" in data:
            try: asset["metadata"] = normalise_metadata(data["metadata"], asset.get("status"))
            except ValueError as error: return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        asset["updatedAt"] = now()
        with LOCK: save_db(DB)
        self.send_json(HTTPStatus.OK, asset)
    def do_DELETE(self):
        pieces = self.path.split("/")
        if len(pieces) == 4 and pieces[:3] == ["", "api", "access-groups"]:
            actor = self.require()
            if not actor: return
            if actor["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Customer group management requires platform admin role"})
            group = next((item for item in DB["accessGroups"] if item["id"] == pieces[3]), None)
            if not group: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Customer group not found"})
            if group.get("system") or group["id"] == "all-managed-customers": return self.send_json(HTTPStatus.BAD_REQUEST, {"error": "The All managed customers group is dynamic and cannot be deleted"})
            with LOCK: DB["accessGroups"] = [item for item in DB["accessGroups"] if item["id"] != group["id"]]; save_db(DB)
            return self.send_json(HTTPStatus.OK, {"deletedId": group["id"]})
        if len(pieces) != 4 or pieces[:3] != ["", "api", "relationships"]:
            return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        relationship = next((x for x in DB["relationships"] if x["id"] == pieces[3]), None)
        if not relationship: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "Relationship not found"})
        source = next((x for x in DB["assets"] if x["id"] == relationship["fromId"]), None)
        user = self.require(source["companyId"] if source else None)
        if not user: return
        if not source or not can_manage(user, source["companyId"]):
            return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Disconnect requires MSP operator or platform admin role"})
        with LOCK:
            DB["relationships"] = [x for x in DB["relationships"] if x["id"] != relationship["id"]]
            save_db(DB)
        self.send_json(HTTPStatus.OK, {"deletedId": relationship["id"]})
    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path); query = parse_qs(parsed.query); path = parsed.path
        if path == "/api/health": return self.send_json(HTTPStatus.OK, {"status": "ok"})
        if path == "/api/me":
            user = self.require()
            if user: self.send_json(HTTPStatus.OK, public_user(user))
            return
        if path == "/api/database/status":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Database configuration requires platform admin role"})
            return self.send_json(HTTPStatus.OK, database_status())
        if path == "/api/database/backup":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "Database backup requires platform admin role"})
            return self.send_json(HTTPStatus.OK, backup_document())
        if path == "/api/companies":
            user = self.require()
            if user: self.send_json(HTTPStatus.OK, [x for x in DB["companies"] if allowed(user, x["id"])])
            return
        if path == "/api/root-attention":
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP overview requires root or MSP role"})
            assets = [asset for asset in DB["assets"] if allowed(user, asset["companyId"])]
            return self.send_json(HTTPStatus.OK, attention_items(assets, DB["companies"]))
        if path == "/api/root-overview":
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP overview requires root or MSP role"})
            companies = [company for company in DB["companies"] if allowed(user, company["id"])]
            assets = [asset for asset in DB["assets"] if allowed(user, asset["companyId"])]
            return self.send_json(HTTPStatus.OK, customer_overview(companies, assets))
        if path == "/api/access-groups":
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP access groups require root or MSP role"})
            visible = []
            for group in DB["accessGroups"]:
                company_ids = [company["id"] for company in DB["companies"] if allowed(user, company["id"]) and ("*" in group["companyIds"] or company["id"] in group["companyIds"])]
                if company_ids: visible.append({"id": group["id"], "name": group["name"], "companyIds": company_ids, "system": bool(group.get("system") or group["id"] == "all-managed-customers")})
            return self.send_json(HTTPStatus.OK, visible)
        if path == "/api/rbac/roles":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "RBAC requires platform admin role"})
            return self.send_json(HTTPStatus.OK, ROLE_TEMPLATES)
        if path == "/api/rbac/effective":
            user = self.require()
            if not user: return
            if user["role"] != "platform_admin": return self.send_json(HTTPStatus.FORBIDDEN, {"error": "RBAC requires platform admin role"})
            target = next((item for item in DB["users"] if item["id"] == query.get("userId", [None])[0]), None)
            if not target: return self.send_json(HTTPStatus.NOT_FOUND, {"error": "User not found"})
            template = next((item for item in ROLE_TEMPLATES if item["id"] == target["role"]), None)
            customer_ids = [company["id"] for company in DB["companies"]] if target["role"] == "platform_admin" else target["companyIds"]
            customers = [company["name"] for company in DB["companies"] if company["id"] in customer_ids]
            return self.send_json(HTTPStatus.OK, {"user": visible_user(target), "role": template, "customers": customers, "scope": "All customers" if target["role"] == "platform_admin" else ", ".join(customers) or "No customer access"})
        if path == "/api/users":
            company_id = query.get("companyId", [None])[0]; user = self.require(company_id)
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "User management requires MSP operator or platform admin role"})
            records = [x for x in DB["users"] if user["role"] == "platform_admin" or any(allowed(user, c) for c in x["companyIds"])]
            if company_id: records = [x for x in records if company_id in x["companyIds"] or x["role"] == "platform_admin"]
            self.send_json(HTTPStatus.OK, [visible_user(x) for x in records])
            return
        if path == "/api/branding":
            if query.get("scope", [None])[0] == "msp":
                user = self.require()
                if not user: return
                if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP branding requires root or MSP role"})
                return self.send_json(HTTPStatus.OK, {"supportEmail": "", "supportUrl": "", "welcomeMessage": "", **DB["mspBranding"]})
            company_id = query.get("companyId", [None])[0]; user = self.require(company_id)
            if user:
                if company_id: self.send_json(HTTPStatus.OK, DB["branding"].get(company_id, {"name": next(x["name"] for x in DB["companies"] if x["id"] == company_id), "accent": "#50d5b9", "logoText": "C"}))
                else: self.send_json(HTTPStatus.OK, {key: value for key, value in DB["branding"].items() if allowed(user, key)})
            return
        if path == "/api/assets":
            company_id = query.get("companyId", [None])[0]; user = self.require(company_id)
            if user: self.send_json(HTTPStatus.OK, [asset_view(x) for x in DB["assets"] if (not company_id or x["companyId"] == company_id) and allowed(user, x["companyId"])])
            return
        if path == "/api/integrations":
            user = self.require()
            if not user: return
            if user["role"] not in {"platform_admin", "msp_operator"}: return self.send_json(HTTPStatus.FORBIDDEN, {"error": "MSP integration tools require root or MSP role"})
            self.send_json(HTTPStatus.OK, [{**x, "scope": x.get("scope", "msp"), "enabled": configured(x["type"]) or x["enabled"]} for x in DB["integrations"] if x.get("scope", "msp") == "msp"])
            return
            return
        if path == "/api/relationships":
            company_id = query.get("companyId", [None])[0]; user = self.require(company_id)
            if user:
                asset_ids = {x["id"] for x in DB["assets"] if allowed(user, x["companyId"]) and (not company_id or x["companyId"] == company_id)}
                self.send_json(HTTPStatus.OK, [x for x in DB["relationships"] if x["fromId"] in asset_ids and x["toId"] in asset_ids])
            return
        if path == "/api/sync-runs":
            if self.require(): self.send_json(HTTPStatus.OK, DB["syncRuns"])
            return
        super().do_GET()
    def log_message(self, format, *args): print(format % args)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000")); storage = "PostgreSQL" if DATABASE_URL else "local JSON demo"
    print(f"CMDB Hub running on :{port} ({storage})"); ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
