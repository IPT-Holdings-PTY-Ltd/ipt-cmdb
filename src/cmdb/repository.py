"""Resource repositories for the canonical CMDB data model.

The local implementation exists only for setup and lightweight development.
The PostgreSQL implementation is the sole operational source of truth when a
database is configured; portable exports are assembled from canonical tables.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import secrets
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable


CMDB_NAMESPACE = uuid.UUID("a12d44c4-64a7-4d6f-b829-3a8b691f0fa4")
PROVIDER_TO_DB = {
    "connectwise": "connectwise_manage",
    "ncentral": "ncentral",
    "passportal": "passportal",
}
PROVIDER_FROM_DB = {value: key for key, value in PROVIDER_TO_DB.items()}
CREDENTIAL_REFERENCES = {
    "connectwise": "env://CW_BASE_URL,CW_COMPANY_ID,CW_PUBLIC_KEY,CW_PRIVATE_KEY,CW_CLIENT_ID",
    "ncentral": "env://NCENTRAL_BASE_URL,NCENTRAL_API_TOKEN",
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_uuid(kind: str, current_id: str) -> str:
    """Keep existing UUIDs and deterministically migrate prototype string IDs."""
    try:
        return str(uuid.UUID(str(current_id)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(CMDB_NAMESPACE, f"{kind}:{current_id}"))


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
        self.state.setdefault("auditEvents", [])

    def list_companies(self) -> list[dict]:
        return deepcopy(self.state["companies"])

    def list_users(self) -> list[dict]:
        return deepcopy(self.state["users"])

    def authenticate(self, email: str, password: str) -> dict | None:
        user = next((item for item in self.state["users"] if item["email"].lower() == email.lower()), None)
        if not user:
            return None
        password_hash = user.get("passwordHash")
        if password_hash and verify_password(password, password_hash):
            return deepcopy(user)
        plaintext = user.get("password")
        if plaintext and hmac.compare_digest(plaintext, password):
            user["passwordHash"] = hash_password(password)
            user.pop("password", None)
            self.save_state(self.state)
            return deepcopy(user)
        return None

    def create_user(self, user: dict, password: str, actor_id: str | None = None) -> dict:
        stored = {**deepcopy(user), "passwordHash": hash_password(password)}
        stored.pop("password", None)
        self.state["users"].append(stored)
        company_id = stored.get("companyIds", [None])[0] if stored.get("companyIds") else None
        self._audit(company_id, actor_id, "user", stored["id"], "created", None, {key: value for key, value in stored.items() if key != "passwordHash"})
        self.save_state(self.state)
        return deepcopy(stored)

    def list_access_groups(self) -> list[dict]:
        return deepcopy(self.state["accessGroups"])

    def create_access_group(self, group: dict, actor_id: str | None = None) -> dict:
        self.state["accessGroups"].append(deepcopy(group))
        self._audit(None, actor_id, "access_group", group["id"], "created", None, group)
        self.save_state(self.state)
        return deepcopy(group)

    def update_access_group(self, group_id: str, changes: dict, actor_id: str | None = None) -> dict | None:
        group = next((item for item in self.state["accessGroups"] if item["id"] == group_id), None)
        if not group:
            return None
        before = deepcopy(group)
        group.update(deepcopy(changes))
        self._audit(None, actor_id, "access_group", group_id, "updated", before, group)
        self.save_state(self.state)
        return deepcopy(group)

    def delete_access_group(self, group_id: str, actor_id: str | None = None) -> bool:
        group = next((item for item in self.state["accessGroups"] if item["id"] == group_id), None)
        if not group:
            return False
        self.state["accessGroups"] = [item for item in self.state["accessGroups"] if item["id"] != group_id]
        self._audit(None, actor_id, "access_group", group_id, "deleted", group, None)
        self.save_state(self.state)
        return True

    def create_company(self, company: dict, actor_id: str | None = None) -> dict:
        self.state["companies"].append(deepcopy(company))
        self._audit(company["id"], actor_id, "company", company["id"], "created", None, company)
        self.save_state(self.state)
        return deepcopy(company)

    def list_assets(self) -> list[dict]:
        return deepcopy(self.state["assets"])

    def get_asset(self, asset_id: str) -> dict | None:
        value = next((item for item in self.state["assets"] if item["id"] == asset_id), None)
        return deepcopy(value) if value else None

    def create_asset(self, asset: dict, actor_id: str | None = None) -> dict:
        self.state["assets"].append(deepcopy(asset))
        self._audit(asset["companyId"], actor_id, "configuration_item", asset["id"], "created", None, asset)
        self.save_state(self.state)
        return deepcopy(asset)

    def update_asset(self, asset_id: str, changes: dict, actor_id: str | None = None) -> dict | None:
        asset = next((item for item in self.state["assets"] if item["id"] == asset_id), None)
        if not asset:
            return None
        before = deepcopy(asset)
        asset.update(deepcopy(changes))
        asset["updatedAt"] = utc_now()
        self._audit(asset["companyId"], actor_id, "configuration_item", asset_id, "updated", before, asset)
        self.save_state(self.state)
        return deepcopy(asset)

    def list_relationships(self) -> list[dict]:
        return deepcopy(self.state["relationships"])

    def create_relationship(self, relationship: dict, company_id: str, actor_id: str | None = None) -> dict:
        self.state["relationships"].append(deepcopy(relationship))
        self._audit(company_id, actor_id, "relationship", relationship["id"], "created", None, relationship)
        self.save_state(self.state)
        return deepcopy(relationship)

    def delete_relationship(self, relationship_id: str, company_id: str, actor_id: str | None = None) -> bool:
        relationship = next((item for item in self.state["relationships"] if item["id"] == relationship_id), None)
        if not relationship:
            return False
        self.state["relationships"] = [item for item in self.state["relationships"] if item["id"] != relationship_id]
        self._audit(company_id, actor_id, "relationship", relationship_id, "retired", relationship, None)
        self.save_state(self.state)
        return True

    def _postgres_list_changes(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(self._change_select_sql() + " ORDER BY cr.created_at DESC, cr.change_number DESC")
            return [self._change_from_row(cursor, row) for row in cursor.fetchall()]

    def _postgres_get_change(self, change_id: str) -> dict | None:
        try:
            parsed_id = str(uuid.UUID(change_id))
        except (ValueError, TypeError):
            return None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(self._change_select_sql() + " WHERE cr.id = %s::uuid", (parsed_id,))
            row = cursor.fetchone()
            return self._change_from_row(cursor, row) if row else None

    def _postgres_next_change_number(self, year: int) -> str:
        prefix = f"CHG-{year}-"
        with self.connection_factory() as connection, connection.cursor() as cursor:
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
        stored = {**deepcopy(change), "id": canonical_uuid("change_request", change["id"])}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            self._write_change(cursor, stored)
            self._insert_audit(cursor, stored["companyId"], actor_id, "change_request", stored["id"], "created", None, stored)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_change(stored["id"])

    @staticmethod
    def _change_select_sql() -> str:
        return """
            SELECT cr.id, c.slug, c.name, cr.change_number, cr.title, cr.status,
                   cr.change_type, cr.category, cr.priority, cr.risk_level, cr.risk_source,
                   cr.outage_expected, cr.planned_start, cr.planned_end, cr.reason,
                   cr.business_impact, cr.implementation_plan, cr.validation_plan,
                   cr.rollback_plan, cr.communication_status, cr.communication_plan,
                   cr.assigned_technician, cr.approver, cr.notes, cr.impact_summary,
                   cr.risk_assessment, cr.revision, u.id, u.email::text,
                   cr.created_at, cr.updated_at
            FROM change_requests cr
            JOIN companies c ON c.id = cr.company_id
            LEFT JOIN users u ON u.id = cr.created_by
        """

    def _change_from_row(self, cursor: Any, row: tuple) -> dict:
        (
            change_id, company_slug, company_name, number, title, status, change_type,
            category, priority, risk_level, risk_source, outage_expected, planned_start,
            planned_end, reason, business_impact, implementation_plan, validation_plan,
            rollback_plan, communication_status, communication_plan, assigned_technician,
            approver, notes, impact_summary, risk_assessment, revision, creator_id,
            creator_email, created_at, updated_at,
        ) = row
        change_id_text = str(change_id)
        cursor.execute(
            "SELECT ci_id, ci_snapshot FROM change_scope_items WHERE change_id = %s::uuid ORDER BY ordinal, id",
            (change_id_text,),
        )
        scope_rows = cursor.fetchall()
        scope_asset_ids = [str(ci_id) if ci_id else snapshot.get("assetId") for ci_id, snapshot in scope_rows]
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
        role_names = {"scope": "Scope", "direct": "Direct impact", "downstream": "Downstream impact"}
        for ci_id, impact_role, depth, path_value, snapshot in cursor.fetchall():
            item = dict(snapshot or {})
            path_value = path_value or {}
            if isinstance(path_value, list):
                relationship_types, path_asset_ids = path_value, item.get("pathAssetIds", [])
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
        for provider, external_id, external_url, sync_status, last_attempt_at, last_error in external_rows:
            integration_state[provider] = {
                "status": sync_status,
                "ticketId": external_id,
                "ticketUrl": external_url,
                "lastAttemptAt": self._timestamp(last_attempt_at) or None,
                "error": last_error,
            }
            if external_id or external_url:
                external_references.append({"provider": provider, "externalId": external_id, "url": external_url})
        integration_state.setdefault(
            "connectwise",
            {"status": "not_published", "ticketId": None, "ticketUrl": None, "lastAttemptAt": None, "error": None},
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
            "assignedTechnician": assigned_technician or "",
            "approver": approver or "",
            "notes": notes or "",
            "scopeAssetIds": [item for item in scope_asset_ids if item],
            "impactSnapshot": impact_snapshot,
            "impactSummary": impact_summary or {},
            "createdBy": {"id": str(creator_id) if creator_id else None, "email": creator_email or ""},
            "createdAt": self._timestamp(created_at),
            "updatedAt": self._timestamp(updated_at),
            "revision": revision,
            "externalReferences": external_references,
            "integrationState": integration_state,
        }

    def _write_change(self, cursor: Any, change: dict) -> None:
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
        cursor.execute(
            """
            INSERT INTO change_requests (
                id, company_id, change_number, title, status, change_type, category,
                priority, risk_level, risk_source, outage_expected, planned_start,
                planned_end, reason, business_impact, implementation_plan, validation_plan,
                rollback_plan, communication_status, communication_plan, assigned_technician,
                approver, notes, impact_summary, risk_assessment, revision, created_by,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::timestamp, %s::timestamp, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::uuid,
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
                assigned_technician = EXCLUDED.assigned_technician, approver = EXCLUDED.approver,
                notes = EXCLUDED.notes, impact_summary = EXCLUDED.impact_summary,
                risk_assessment = EXCLUDED.risk_assessment, revision = EXCLUDED.revision,
                created_by = EXCLUDED.created_by, updated_at = EXCLUDED.updated_at
            """,
            (
                change_uuid, company_uuid, change["number"], change["title"], change.get("status", "draft"),
                change.get("changeType", "normal"), change.get("category", "infrastructure"),
                change.get("priority", "medium"), change.get("riskLevel", "medium"),
                change.get("riskSource", "cmdb_suggestion"), bool(change.get("outageExpected")),
                change.get("plannedStart") or None, change.get("plannedEnd") or None,
                change.get("reason", ""), change.get("businessImpact") or None,
                change.get("implementationPlan", ""), change.get("validationPlan", ""),
                change.get("rollbackPlan", ""), change.get("communicationStatus", "required"),
                change.get("communicationPlan") or None, change.get("assignedTechnician") or None,
                change.get("approver") or None, change.get("notes") or None,
                json.dumps(change.get("impactSummary") or {}), json.dumps(change.get("riskAssessment") or {}),
                int(change.get("revision") or 1), creator_uuid,
                change.get("createdAt") or None, change.get("updatedAt") or None,
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
        cursor.execute("DELETE FROM change_impact_snapshots WHERE change_id = %s::uuid", (change_uuid,))
        role_values = {"Scope": "scope", "Direct impact": "direct", "Downstream impact": "downstream"}
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
                    change_uuid, ci_uuid, role_values.get(item.get("role"), "downstream"),
                    int(item.get("depth") or 0), json.dumps(relationship_path), json.dumps(item), ordinal,
                ),
            )
        revision = int(change.get("revision") or 1)
        cursor.execute(
            """
            INSERT INTO change_revisions (change_id, revision, document, created_by)
            VALUES (%s::uuid, %s, %s::jsonb, %s::uuid)
            ON CONFLICT (change_id, revision) DO UPDATE SET document = EXCLUDED.document
            """,
            (change_uuid, revision, json.dumps(change), creator_uuid),
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
                change_uuid, connectwise.get("ticketId"), connectwise.get("ticketUrl"), sync_status,
                f"change:{change_uuid}:connectwise:ticket", connectwise.get("lastAttemptAt") or None, connectwise.get("error"),
            ),
        )

    @staticmethod
    def _timestamp(value: Any) -> str:
        if not value:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat().replace("+00:00", "Z")
        return str(value)

    def list_changes(self) -> list[dict]:
        return deepcopy(self.state.get("changes", []))

    def get_change(self, change_id: str) -> dict | None:
        change = next((item for item in self.state.get("changes", []) if item["id"] == change_id), None)
        return deepcopy(change) if change else None

    def next_change_number(self, year: int) -> str:
        sequence = 1 + sum(str(item.get("number", "")).startswith(f"CHG-{year}-") for item in self.state.get("changes", []))
        return f"CHG-{year}-{sequence:04d}"

    def create_change(self, change: dict, actor_id: str | None = None) -> dict:
        self.state.setdefault("changes", []).append(deepcopy(change))
        self._audit(change["companyId"], actor_id, "change_request", change["id"], "created", None, change)
        self.save_state(self.state)
        return deepcopy(change)

    def list_integrations(self) -> list[dict]:
        return deepcopy(self.state.get("integrations", []))

    def list_sync_runs(self) -> list[dict]:
        return deepcopy(self.state.get("syncRuns", []))

    def record_sync_run(
        self,
        kind: str,
        run: dict,
        configured: bool,
        actor_id: str | None = None,
    ) -> dict:
        stored = deepcopy(run)
        self.state["syncRuns"] = [stored, *self.state.get("syncRuns", [])[:49]]
        integration = next((item for item in self.state.get("integrations", []) if item["type"] == kind), None)
        if integration:
            before = deepcopy(integration)
            integration.update(
                lastSync=stored.get("finishedAt"),
                status="Healthy" if stored.get("status") == "success" else stored.get("status", "unknown"),
                enabled=configured,
            )
            self._audit(integration.get("companyId"), actor_id, "integration_connection", integration["id"], "sync_completed", before, integration)
        self.save_state(self.state)
        return deepcopy(stored)

    def get_msp_branding(self) -> dict:
        return {**DEFAULT_MSP_BRANDING, **deepcopy(self.state.get("mspBranding") or {})}

    def update_msp_branding(self, brand: dict, actor_id: str | None = None) -> dict:
        before = self.get_msp_branding()
        stored = {**DEFAULT_MSP_BRANDING, **deepcopy(brand)}
        self.state["mspBranding"] = stored
        self._audit(None, actor_id, "msp_branding", "msp", "updated", branding_audit_value(before), branding_audit_value(stored))
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
        return {company["id"]: self.get_company_branding(company["id"]) for company in self.state["companies"]}

    def update_company_branding(self, company_id: str, brand: dict, actor_id: str | None = None) -> dict:
        before = self.get_company_branding(company_id)
        stored = {**before, **deepcopy(brand)}
        self.state.setdefault("branding", {})[company_id] = stored
        self._audit(company_id, actor_id, "company_branding", company_id, "updated", branding_audit_value(before), branding_audit_value(stored))
        self.save_state(self.state)
        return deepcopy(stored)

    def export_state(self) -> dict:
        return deepcopy(self.state)

    def import_state(self, state: dict, actor_id: str | None = None) -> dict[str, int]:
        self.state.clear()
        self.state.update(deepcopy(state))
        self.state.setdefault("auditEvents", [])
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
    ) -> None:
        self.state["auditEvents"] = [
            {
                "id": str(uuid.uuid4()),
                "companyId": company_id,
                "actorId": actor_id,
                "entityType": entity_type,
                "entityId": entity_id,
                "action": action,
                "before": deepcopy(before),
                "after": deepcopy(after),
                "createdAt": utc_now(),
            },
            *self.state["auditEvents"][:999],
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

    def __init__(
        self,
        state: dict,
        save_state: Callable[[dict], None],
        connection_factory: Callable[[], Any],
    ):
        super().__init__(state, save_state)
        self.connection_factory = connection_factory

    def is_initialized(self) -> bool:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM users WHERE status <> 'disabled')")
            return bool(cursor.fetchone()[0])

    def migrate_legacy_company_branding(self) -> int:
        """Import only the last non-canonical field, then retire the old document."""
        imported = 0
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT slug, id, name FROM companies WHERE status <> 'inactive'")
            companies = {slug: (str(company_id), name) for slug, company_id, name in cursor.fetchall()}
            for company_slug, configured_brand in self.state.get("branding", {}).items():
                company = companies.get(company_slug)
                if not company:
                    continue
                company_uuid, company_name = company
                brand = {**default_company_branding(company_name), **(configured_brand or {})}
                cursor.execute(
                    """
                    INSERT INTO company_branding (
                        company_id, display_name, logo_text, accent_color,
                        secondary_color, logo_data_url, logo_file_name, updated_at
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (company_id) DO NOTHING
                    """,
                    (
                        company_uuid, brand["name"], brand["logoText"], brand["accent"],
                        brand["secondaryAccent"], brand.get("logoDataUrl") or None,
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
                company_uuid = company_ids.get(company_slug)
                company = next((item for item in self.state["companies"] if item["id"] == company_slug), None)
                if not company_uuid or not company:
                    continue
                brand = {**default_company_branding(company["name"]), **(configured_brand or {})}
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
                        company_uuid, brand["name"], brand["logoText"], brand["accent"],
                        brand["secondaryAccent"], brand.get("logoDataUrl") or None,
                        brand.get("logoFileName") or None,
                    ),
                )

            for group in self.state.get("accessGroups", []):
                group_uuid = canonical_uuid("access_group", group["id"])
                cursor.execute(
                    """
                    INSERT INTO access_groups (id, slug, name, system, updated_at)
                    VALUES (%s::uuid, %s, %s, %s, now())
                    ON CONFLICT (slug) DO UPDATE SET
                        name = EXCLUDED.name,
                        system = EXCLUDED.system,
                        updated_at = now()
                    RETURNING id
                    """,
                    (group_uuid, group["id"], group["name"], bool(group.get("system"))),
                )
                group_uuid = str(cursor.fetchone()[0])
                cursor.execute("DELETE FROM access_group_companies WHERE access_group_id = %s::uuid", (group_uuid,))
                selected_companies = company_ids.values() if "*" in group.get("companyIds", []) else (
                    company_ids[item] for item in group.get("companyIds", []) if item in company_ids
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
            for user in self.state.get("users", []):
                user_uuid = canonical_uuid("user", user["id"])
                attributes = {
                    "legacyId": user.get("id"),
                    "accountType": user.get(
                        "accountType",
                        "root" if user.get("role") in {"platform_admin", "msp_operator"} else "customer",
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
                    (user_uuid, user["email"], user.get("displayName") or user["email"].split("@", 1)[0], json.dumps(attributes)),
                )
                user_uuid = str(cursor.fetchone()[0])
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
                cursor.execute("DELETE FROM user_platform_roles WHERE user_id = %s::uuid", (user_uuid,))
                cursor.execute("DELETE FROM user_company_roles WHERE user_id = %s::uuid", (user_uuid,))
                cursor.execute("DELETE FROM user_access_groups WHERE user_id = %s::uuid", (user_uuid,))
                if user.get("role") == "platform_admin":
                    cursor.execute(
                        "INSERT INTO user_platform_roles (user_id, role) VALUES (%s::uuid, 'platform_admin') ON CONFLICT DO NOTHING",
                        (user_uuid,),
                    )
                else:
                    database_role = "msp_operator" if user.get("role") == "msp_operator" else "customer_reader"
                    for company_slug in user.get("companyIds", []):
                        if company_slug == "*":
                            selected_role_companies = company_ids.values()
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
                        integration_uuid, integration["id"], company_slug, provider,
                        integration["name"], CREDENTIAL_REFERENCES.get(kind, "keyvault://future"),
                        json.dumps(configuration), bool(integration.get("enabled")),
                    ),
                )
                integration_ids[kind] = str(cursor.fetchone()[0])

            status_to_db = {"success": "succeeded", "blocked": "blocked", "failed": "failed"}
            for run in self.state.get("syncRuns", []):
                kind = run.get("type")
                integration_uuid = integration_ids.get(kind)
                if not integration_uuid:
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
                        run_uuid, integration_uuid, status, run.get("startedAt") or None, run.get("finishedAt") or None,
                        int(run.get("discovered") or 0), int(run.get("imported") or 0),
                        int(run.get("updated") or 0), int(run.get("review") or 0),
                        run.get("message") if status == "failed" else None,
                        run.get("message") or "", json.dumps({"legacyStatus": run.get("status")}),
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
                    brand["name"], brand["logoText"], brand["accent"], brand["secondaryAccent"],
                    brand["logoDataUrl"] or None, brand["logoFileName"] or None,
                    brand["supportEmail"] or None, brand["supportUrl"] or None,
                    brand["supportPhone"] or None, brand["welcomeMessage"] or None,
                    brand["reportFooter"] or None, brand["confidentialityLabel"] or None,
                ),
            )

            for asset in self.state["assets"]:
                company_uuid = company_ids.get(asset["companyId"])
                if not company_uuid:
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
                        company_uuid,
                        asset["type"],
                        asset["name"],
                        normalized_name(asset["name"]),
                        lifecycle,
                        operational,
                        json.dumps(attributes),
                    ),
                )

            for relationship in self.state["relationships"]:
                from_id = asset_ids.get(relationship["fromId"], canonical_uuid("configuration_item", relationship["fromId"]))
                to_id = asset_ids.get(relationship["toId"], canonical_uuid("configuration_item", relationship["toId"]))
                cursor.execute("SELECT company_id FROM configuration_items WHERE id = %s::uuid", (from_id,))
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
            cursor.execute("SELECT slug, name, attributes FROM companies WHERE status <> 'inactive' ORDER BY name")
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
                (company_uuid, company["id"], company["name"], json.dumps({"externalIds": company.get("externalIds", {})})),
            )
            cursor.execute(
                """
                INSERT INTO access_group_companies (access_group_id, company_id)
                SELECT id, %s::uuid FROM access_groups WHERE system = true
                ON CONFLICT DO NOTHING
                """,
                (company_uuid,),
            )
            self._insert_audit(cursor, company["id"], actor_id, "company", company_uuid, "created", None, company)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return company

    def list_users(self, include_credentials: bool = False) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.id, u.email::text, u.attributes, lac.password_hash,
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
                       EXISTS (SELECT 1 FROM user_company_roles ucr WHERE ucr.user_id = u.id AND ucr.role = 'msp_operator')
                FROM users u
                LEFT JOIN local_auth_credentials lac ON lac.user_id = u.id
                WHERE u.status <> 'disabled'
                ORDER BY u.email
                """
            )
            records = []
            for user_id, email, attributes, password_hash, is_admin, company_ids, group_ids, is_msp in cursor.fetchall():
                role = "platform_admin" if is_admin else "msp_operator" if is_msp else "client_reader"
                record = {
                    "id": str(user_id),
                    "email": email,
                    "role": role,
                    "companyIds": ["*"] if is_admin else list(company_ids or []),
                    "groupIds": list(group_ids or []),
                    "accountType": (attributes or {}).get("accountType", "root" if role != "client_reader" else "customer"),
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
        if not row or not verify_password(password, row[1]):
            return None
        user_id = str(row[0])
        return next((item for item in self.list_users() if item["id"] == user_id), None)

    def create_user(self, user: dict, password: str, actor_id: str | None = None) -> dict:
        user_uuid = canonical_uuid("user", user["id"])
        attributes = {"legacyId": user.get("id"), "accountType": user.get("accountType", "customer")}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (id, email, display_name, status, attributes)
                VALUES (%s::uuid, %s, %s, 'active', %s::jsonb)
                """,
                (user_uuid, user["email"], user["email"].split("@", 1)[0], json.dumps(attributes)),
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
                database_role = "msp_operator" if user["role"] == "msp_operator" else "customer_reader"
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

    def list_access_groups(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ag.slug, ag.name, ag.system,
                       ARRAY(SELECT c.slug FROM access_group_companies agc
                             JOIN companies c ON c.id = agc.company_id
                             WHERE agc.access_group_id = ag.id AND c.status <> 'inactive'
                             ORDER BY c.name)
                FROM access_groups ag
                ORDER BY ag.system DESC, ag.name
                """
            )
            return [
                {"id": slug, "name": name, "companyIds": list(company_ids or []), "system": bool(system)}
                for slug, name, system, company_ids in cursor.fetchall()
            ]

    def create_access_group(self, group: dict, actor_id: str | None = None) -> dict:
        group_uuid = canonical_uuid("access_group", group["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO access_groups (id, slug, name, system) VALUES (%s::uuid, %s, %s, %s)",
                (group_uuid, group["id"], group["name"], bool(group.get("system"))),
            )
            self._replace_group_companies(cursor, group_uuid, group["companyIds"])
            self._insert_audit(cursor, None, actor_id, "access_group", group_uuid, "created", None, group)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return group

    def update_access_group(self, group_id: str, changes: dict, actor_id: str | None = None) -> dict | None:
        before = next((item for item in self.list_access_groups() if item["id"] == group_id), None)
        if not before:
            return None
        after = {**before, **deepcopy(changes)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM access_groups WHERE slug = %s", (group_id,))
            group_uuid = str(cursor.fetchone()[0])
            cursor.execute("UPDATE access_groups SET name = %s, updated_at = now() WHERE id = %s::uuid", (after["name"], group_uuid))
            self._replace_group_companies(cursor, group_uuid, after["companyIds"])
            self._insert_audit(cursor, None, actor_id, "access_group", group_uuid, "updated", before, after)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return after

    def delete_access_group(self, group_id: str, actor_id: str | None = None) -> bool:
        before = next((item for item in self.list_access_groups() if item["id"] == group_id), None)
        if not before:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM access_groups WHERE slug = %s", (group_id,))
            group_uuid = str(cursor.fetchone()[0])
            self._insert_audit(cursor, None, actor_id, "access_group", group_uuid, "deleted", before, None)
            cursor.execute("DELETE FROM access_groups WHERE id = %s::uuid", (group_uuid,))
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    @staticmethod
    def _replace_group_companies(cursor: Any, group_uuid: str, company_ids: list[str]) -> None:
        cursor.execute("DELETE FROM access_group_companies WHERE access_group_id = %s::uuid", (group_uuid,))
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
            return [self._asset_from_row(row) for row in cursor.fetchall()]

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
            return self._asset_from_row(row) if row else None

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
            self._insert_audit(cursor, asset["companyId"], actor_id, "configuration_item", asset_uuid, "created", None, stored)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return stored

    def update_asset(self, asset_id: str, changes: dict, actor_id: str | None = None) -> dict | None:
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
            self._insert_audit(cursor, updated["companyId"], actor_id, "configuration_item", asset_id, "updated", before, updated)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_asset(asset_id)

    def list_relationships(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, from_ci_id, to_ci_id, relationship_type, impact_policy
                FROM ci_relationships
                WHERE retired_at IS NULL
                ORDER BY created_at, id
                """
            )
            return [
                {
                    "id": str(item_id), "fromId": str(from_id), "toId": str(to_id),
                    "type": relationship_type, "impactPolicy": impact_policy,
                }
                for item_id, from_id, to_id, relationship_type, impact_policy in cursor.fetchall()
            ]

    def create_relationship(self, relationship: dict, company_id: str, actor_id: str | None = None) -> dict:
        relationship_uuid = canonical_uuid("relationship", relationship["id"])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_id,))
            company_row = cursor.fetchone()
            if not company_row:
                raise ValueError("Customer not found")
            cursor.execute(
                """
                INSERT INTO ci_relationships (id, company_id, from_ci_id, to_ci_id, relationship_type, impact_policy)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s)
                ON CONFLICT (from_ci_id, to_ci_id, relationship_type) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    impact_policy = EXCLUDED.impact_policy,
                    retired_at = NULL
                RETURNING id
                """,
                (
                    relationship_uuid,
                    str(company_row[0]),
                    relationship["fromId"],
                    relationship["toId"],
                    relationship["type"],
                    relationship.get("impactPolicy", "required"),
                ),
            )
            stored_id = str(cursor.fetchone()[0])
            reactivated = stored_id != relationship_uuid
            stored = {**relationship, "id": stored_id, "impactPolicy": relationship.get("impactPolicy", "required")}
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

    def delete_relationship(self, relationship_id: str, company_id: str, actor_id: str | None = None) -> bool:
        relationship = next((item for item in self.list_relationships() if item["id"] == relationship_id), None)
        if not relationship:
            return False
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ci_relationships SET retired_at = now() WHERE id = %s::uuid AND retired_at IS NULL",
                (relationship_id,),
            )
            self._insert_audit(cursor, company_id, actor_id, "relationship", relationship_id, "retired", relationship, None)
        self._refresh_state_mirror()
        self.save_state(self.state)
        return True

    def list_integrations(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT ic.id, ic.slug, c.slug, ic.provider, ic.name, ic.configuration,
                       ic.enabled, latest.status, latest.finished_at
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
            for _item_id, slug, company_slug, provider, name, configuration, enabled, latest_status, finished_at in cursor.fetchall():
                configuration = configuration or {}
                records.append(
                    {
                        "id": slug,
                        "name": name,
                        "type": PROVIDER_FROM_DB.get(provider, provider),
                        "enabled": bool(enabled),
                        "mode": configuration.get("mode", "configured_by_environment"),
                        "lastSync": self._timestamp(finished_at) or None,
                        "status": status_labels.get(latest_status, "Ready" if enabled else "Not configured"),
                        "scope": configuration.get("scope", "customer" if company_slug else "msp"),
                        "companyId": company_slug,
                    }
                )
            return records

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
                    stored["name"], stored["logoText"], stored["accent"], stored["secondaryAccent"],
                    stored["logoDataUrl"] or None, stored["logoFileName"] or None,
                    stored["supportEmail"] or None, stored["supportUrl"] or None,
                    stored["supportPhone"] or None, stored["welcomeMessage"] or None,
                    stored["reportFooter"] or None, stored["confidentialityLabel"] or None,
                    actor_id,
                ),
            )
            self._insert_audit(
                cursor, None, actor_id, "msp_branding",
                canonical_uuid("msp_branding", "msp"), "updated", branding_audit_value(before), branding_audit_value(stored),
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
        company_name, display_name, logo_text, accent, secondary, logo_data_url, logo_file_name = row
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
        return {company["id"]: self.get_company_branding(company["id"]) for company in self.list_companies()}

    def update_company_branding(self, company_id: str, brand: dict, actor_id: str | None = None) -> dict:
        before = self.get_company_branding(company_id)
        stored = {**before, **deepcopy(brand)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = %s AND status <> 'inactive'", (company_id,))
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
                    company_uuid, stored["name"], stored["logoText"], stored["accent"],
                    stored["secondaryAccent"], stored.get("logoDataUrl") or None,
                    stored.get("logoFileName") or None, actor_id,
                ),
            )
            self._insert_audit(
                cursor, company_id, actor_id, "company_branding", company_uuid,
                "updated", branding_audit_value(before), branding_audit_value(stored),
            )
        self._refresh_state_mirror()
        self.save_state(self.state)
        return self.get_company_branding(company_id)

    def export_state(self) -> dict:
        """Assemble a portable document from canonical tables, never a JSON mirror."""
        return {
            "companies": self.list_companies(),
            "users": self.list_users(include_credentials=True),
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

    def list_sync_runs(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sr.id, ic.provider, sr.status, sr.message, sr.started_at,
                       sr.finished_at, sr.discovered_count, sr.created_count,
                       sr.updated_count, sr.review_count
                FROM sync_runs sr
                JOIN integration_connections ic ON ic.id = sr.integration_connection_id
                ORDER BY sr.started_at DESC, sr.id DESC
                LIMIT 50
                """
            )
            status_labels = {"succeeded": "success"}
            return [
                {
                    "id": str(run_id),
                    "type": PROVIDER_FROM_DB.get(provider, provider),
                    "status": status_labels.get(status, status),
                    "message": message or "",
                    "startedAt": self._timestamp(started_at),
                    "finishedAt": self._timestamp(finished_at),
                    "discovered": discovered,
                    "imported": created,
                    "updated": updated,
                    "review": review,
                }
                for run_id, provider, status, message, started_at, finished_at, discovered, created, updated, review in cursor.fetchall()
            ]

    def record_sync_run(
        self,
        kind: str,
        run: dict,
        configured: bool,
        actor_id: str | None = None,
    ) -> dict:
        status_to_db = {"success": "succeeded", "blocked": "blocked", "failed": "failed"}
        database_status = status_to_db.get(run.get("status"), "review_required")
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
                    run_uuid, integration_uuid, database_status, run.get("startedAt") or None, run.get("finishedAt") or None,
                    int(run.get("discovered") or 0), int(run.get("imported") or 0),
                    int(run.get("updated") or 0), int(run.get("review") or 0),
                    run.get("message") if database_status == "failed" else None,
                    run.get("message") or "", json.dumps({"apiStatus": run.get("status")}),
                ),
            )
            cursor.execute(
                "UPDATE integration_connections SET enabled = %s, updated_at = now() WHERE id = %s::uuid",
                (configured, integration_uuid),
            )
            company_slug = None
            if company_uuid:
                cursor.execute("SELECT slug FROM companies WHERE id = %s::uuid", (str(company_uuid),))
                company_slug = cursor.fetchone()[0]
            stored = {**deepcopy(run), "id": run_uuid}
            self._insert_audit(
                cursor, company_slug, actor_id, "integration_connection", integration_uuid,
                "sync_completed", None, stored,
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
        item_id, company_slug, name, ci_type, lifecycle, operational, attributes, updated_at = row
        attributes = attributes or {}
        metadata = {**(attributes.get("metadata") or {}), "lifecycle": lifecycle, "operationalStatus": operational}
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
        self.state["accessGroups"] = self.list_access_groups()
        self.state["assets"] = self.list_assets()
        self.state["relationships"] = self.list_relationships()
        self.state["changes"] = self.list_changes()
        self.state["integrations"] = self.list_integrations()
        self.state["syncRuns"] = self.list_sync_runs()
        self.state["branding"] = self.list_company_branding()
        self.state["mspBranding"] = self.get_msp_branding()

    def _rewrite_change_ids(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        for change in self.state.get("changes", []):
            if "scopeAssetIds" in change:
                change["scopeAssetIds"] = [mapping.get(item, item) for item in change["scopeAssetIds"]]
            for impact in change.get("impactSnapshot", []):
                impact["assetId"] = mapping.get(impact.get("assetId"), impact.get("assetId"))
                impact["pathAssetIds"] = [mapping.get(item, item) for item in impact.get("pathAssetIds", [])]

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
    ) -> None:
        company_uuid = None
        if company_slug:
            cursor.execute("SELECT id FROM companies WHERE slug = %s", (company_slug,))
            row = cursor.fetchone()
            company_uuid = str(row[0]) if row else None
        after_value = deepcopy(after) if after else None
        if after_value is not None and actor_id:
            after_value["actorApiId"] = actor_id
        cursor.execute(
            """
            INSERT INTO audit_events (
                company_id, actor_user_id, entity_type, entity_id, action,
                before_value, after_value
            ) VALUES (%s::uuid, NULL, %s, %s::uuid, %s, %s::jsonb, %s::jsonb)
            """,
            (
                company_uuid,
                entity_type,
                entity_id,
                action,
                json.dumps(before) if before is not None else None,
                json.dumps(after_value) if after_value is not None else None,
            ),
        )
