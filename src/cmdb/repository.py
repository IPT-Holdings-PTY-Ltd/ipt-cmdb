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
import secrets
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from psycopg import sql

from src.cmdb.audit import (
    current_audit_context,
    entity_name,
    event_category,
    field_changes,
    sanitize_audit_value,
)

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
OWNER_RESPONSIBILITY_ROLES = {
    "business_owner",
    "service_owner",
    "technical_owner",
    "custodian",
    "change_approver",
    "signoff_delegate",
    "support_contact",
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
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        self.state.setdefault("dataQualityExceptions", [])
        self.state.setdefault("reconciliationCandidates", [])
        self.state.setdefault("fieldAuthority", [])
        self.state.setdefault("contacts", [])
        self.state.setdefault("contactResponsibilities", [])

    def list_companies(self) -> list[dict]:
        return deepcopy(self.state["companies"])

    def list_users(self) -> list[dict]:
        return deepcopy(
            [item for item in self.state["users"] if item.get("status", "active") != "disabled"]
        )

    def authenticate(self, email: str, password: str) -> dict | None:
        user = next(
            (item for item in self.state["users"] if item["email"].lower() == email.lower()),
            None,
        )
        if not user:
            return None
        if user.get("status", "active") == "disabled":
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
        return deepcopy(stored)

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
        return deepcopy(self.state["accessGroups"])

    def create_access_group(self, group: dict, actor_id: str | None = None) -> dict:
        self.state["accessGroups"].append(deepcopy(group))
        self._audit(None, actor_id, "access_group", group["id"], "created", None, group)
        self.save_state(self.state)
        return deepcopy(group)

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
        group.update(deepcopy(changes))
        self._audit(None, actor_id, "access_group", group_id, "updated", before, group)
        self.save_state(self.state)
        return deepcopy(group)

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
        self.state["relationships"].append(deepcopy(relationship))
        self._audit(
            company_id,
            actor_id,
            "relationship",
            relationship["id"],
            "created",
            None,
            relationship,
        )
        self.save_state(self.state)
        return deepcopy(relationship)

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

    def _postgres_list_changes(self) -> list[dict]:
        with self.connection_factory() as connection, connection.cursor() as cursor:  # type: ignore[attr-defined]  # Bound to PostgreSQL subclass below.
            cursor.execute(
                self._change_select_sql() + " ORDER BY cr.created_at DESC, cr.change_number DESC"
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
    def _change_select_sql() -> str:
        return """
            SELECT cr.id, c.slug, c.name, cr.change_number, cr.title, cr.status,
                   cr.change_type, cr.category, cr.priority, cr.risk_level, cr.risk_source,
                   cr.outage_expected, cr.planned_start, cr.planned_end, cr.reason,
                   cr.business_impact, cr.implementation_plan, cr.validation_plan,
                   cr.rollback_plan, cr.communication_status, cr.communication_plan,
                   cr.assigned_technician, cr.approver, cr.notes, cr.impact_summary,
                   cr.risk_assessment, cr.revision, cr.actual_start, cr.actual_end,
                   cr.actual_outage_minutes, cr.outcome, cr.failure_reason,
                   cr.validation_result, cr.rollback_executed, cr.rollback_result,
                   cr.closure_notes, cr.approvals, cr.status_history, u.id, u.email::text,
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
            assigned_technician,
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
            "assignedTechnician": assigned_technician or "",
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
        cursor.execute(
            """
            INSERT INTO change_requests (
                id, company_id, change_number, title, status, change_type, category,
                priority, risk_level, risk_source, outage_expected, planned_start,
                planned_end, reason, business_impact, implementation_plan, validation_plan,
                rollback_plan, communication_status, communication_plan, assigned_technician,
                approver, notes, impact_summary, risk_assessment, revision,
                actual_start, actual_end, actual_outage_minutes, outcome, failure_reason,
                validation_result, rollback_executed, rollback_result, closure_notes,
                approvals, status_history, created_by,
                created_at, updated_at
            ) VALUES (
                %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::timestamp, %s::timestamp, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                %s::timestamptz, %s::timestamptz, %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::uuid,
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
                actual_start = EXCLUDED.actual_start, actual_end = EXCLUDED.actual_end,
                actual_outage_minutes = EXCLUDED.actual_outage_minutes, outcome = EXCLUDED.outcome,
                failure_reason = EXCLUDED.failure_reason, validation_result = EXCLUDED.validation_result,
                rollback_executed = EXCLUDED.rollback_executed, rollback_result = EXCLUDED.rollback_result,
                closure_notes = EXCLUDED.closure_notes, approvals = EXCLUDED.approvals,
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
                change.get("assignedTechnician") or None,
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

    def list_changes(self) -> list[dict]:
        return deepcopy(self.state.get("changes", []))

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

    def list_integrations(self) -> list[dict]:
        return deepcopy(self.state.get("integrations", []))

    def list_sync_runs(self) -> list[dict]:
        return deepcopy(self.state.get("syncRuns", []))

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
        self.state["syncRuns"] = [stored, *self.state.get("syncRuns", [])[:49]]
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
                enabled=configured,
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
        return deepcopy(stored)

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

    def export_state(self) -> dict:
        return deepcopy(self.state)

    def import_state(self, state: dict, actor_id: str | None = None) -> dict[str, int]:
        self.state.clear()
        self.state.update(deepcopy(state))
        self.state.setdefault("auditEvents", [])
        self.state.setdefault("contacts", [])
        self.state.setdefault("contactResponsibilities", [])
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
            "actorType": actor_type if actor_id else "system",
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
            for user in self.state.get("users", []):
                user_uuid = canonical_uuid("user", user["id"])
                attributes = {
                    "legacyId": user.get("id"),
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
                    for company_slug in user.get("companyIds", []):
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
            for (
                user_id,
                email,
                attributes,
                password_hash,
                is_admin,
                company_ids,
                group_ids,
                is_msp,
            ) in cursor.fetchall():
                role = (
                    "platform_admin" if is_admin else "msp_operator" if is_msp else "client_reader"
                )
                record = {
                    "id": str(user_id),
                    "email": email,
                    "role": role,
                    "companyIds": ["*"] if is_admin else list(company_ids or []),
                    "groupIds": list(group_ids or []),
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
        if not row or not verify_password(password, row[1]):
            return None
        user_id = str(row[0])
        return next((item for item in self.list_users() if item["id"] == user_id), None)

    def create_user(self, user: dict, password: str, actor_id: str | None = None) -> dict:
        user_uuid = canonical_uuid("user", user["id"])
        attributes = {
            "legacyId": user.get("id"),
            "accountType": user.get("accountType", "customer"),
        }
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (id, email, display_name, status, attributes)
                VALUES (%s::uuid, %s, %s, 'active', %s::jsonb)
                """,
                (
                    user_uuid,
                    user["email"],
                    user["email"].split("@", 1)[0],
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

    def set_user_status(
        self,
        user_id: str,
        status: str,
        actor_id: str | None = None,
        *,
        reason: str = "",
    ) -> bool:
        active = next((item for item in self.list_users() if item["id"] == user_id), None)
        before = active or {"id": user_id, "status": "disabled"}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE users SET status = %s WHERE id = %s::uuid", (status, user_id))
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
                {
                    "id": slug,
                    "name": name,
                    "companyIds": list(company_ids or []),
                    "system": bool(system),
                }
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
        return group

    def update_access_group(
        self, group_id: str, changes: dict, actor_id: str | None = None
    ) -> dict | None:
        before = next((item for item in self.list_access_groups() if item["id"] == group_id), None)
        if not before:
            return None
        after = {**before, **deepcopy(changes)}
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM access_groups WHERE slug = %s", (group_id,))
            group_uuid = str(cursor.fetchone()[0])
            cursor.execute(
                "UPDATE access_groups SET name = %s, updated_at = now() WHERE id = %s::uuid",
                (after["name"], group_uuid),
            )
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
        return after

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
                SELECT id, from_ci_id, to_ci_id, relationship_type, impact_policy
                FROM ci_relationships
                WHERE retired_at IS NULL
                ORDER BY created_at, id
                """
            )
            return [
                {
                    "id": str(item_id),
                    "fromId": str(from_id),
                    "toId": str(to_id),
                    "type": relationship_type,
                    "impactPolicy": impact_policy,
                }
                for item_id, from_id, to_id, relationship_type, impact_policy in cursor.fetchall()
            ]

    def create_relationship(
        self, relationship: dict, company_id: str, actor_id: str | None = None
    ) -> dict:
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
            stored = {
                **relationship,
                "id": stored_id,
                "impactPolicy": relationship.get("impactPolicy", "required"),
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
                        "status": status_labels.get(
                            latest_status, "Ready" if enabled else "Not configured"
                        ),
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

    def export_state(self) -> dict:
        """Assemble a portable document from canonical tables, never a JSON mirror."""
        return {
            "companies": self.list_companies(),
            "users": self.list_users(include_credentials=True),
            "contacts": self.list_contacts(),
            "contactResponsibilities": self.list_contact_responsibilities(include_inactive=True),
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
        status_to_db = {
            "success": "succeeded",
            "blocked": "blocked",
            "failed": "failed",
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
                    json.dumps({"apiStatus": run.get("status")}),
                ),
            )
            cursor.execute(
                "UPDATE integration_connections SET enabled = %s, updated_at = now() WHERE id = %s::uuid",
                (configured, integration_uuid),
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
        self.state["integrations"] = self.list_integrations()
        self.state["syncRuns"] = self.list_sync_runs()
        self.state["branding"] = self.list_company_branding()
        self.state["mspBranding"] = self.get_msp_branding()

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
                actor_type if actor_uuid else "system",
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
