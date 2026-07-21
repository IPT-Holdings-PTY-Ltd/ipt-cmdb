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
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import sql

from src.cmdb.audit import (
    current_audit_context,
    entity_name,
    event_category,
    field_changes,
    sanitize_audit_value,
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
        self.state.setdefault("apiTokens", [])
        self.state.setdefault("mfaCredentials", [])
        self.state.setdefault("mfaRecoveryCodes", [])
        self.state.setdefault("loginChallenges", [])
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
        if not user:
            return None
        if user.get("status", "active") != "active":
            return None
        password_hash = user.get("passwordHash")
        if password_hash and verify_password(password, password_hash):
            return self._effective_user(user)
        plaintext = user.get("password")
        if plaintext and hmac.compare_digest(plaintext, password):
            user["passwordHash"] = hash_password(password)
            user.pop("password", None)
            self.save_state(self.state)
            return self._effective_user(user)
        return None

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

        self.state["loginChallenges"].append(deepcopy(challenge))
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
        """Increment the failed-or-consumed verification attempt count."""

        challenge = next(
            (item for item in self.state["loginChallenges"] if item["tokenHash"] == token_hash),
            None,
        )
        if not challenge:
            return 0
        challenge["attempts"] = challenge.get("attempts", 0) + 1
        self.save_state(self.state)
        return challenge["attempts"]

    def consume_login_challenge(self, token_hash: str) -> None:
        """Mark a successful login transaction as single-use."""

        challenge = next(
            (item for item in self.state["loginChallenges"] if item["tokenHash"] == token_hash),
            None,
        )
        if challenge:
            challenge["consumedAt"] = utc_now()
            self.save_state(self.state)

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
        return state

    def import_state(self, state: dict, actor_id: str | None = None) -> dict[str, int]:
        self.state.clear()
        self.state.update(deepcopy(state))
        self.state.setdefault("auditEvents", [])
        self.state.setdefault("contacts", [])
        self.state.setdefault("contactResponsibilities", [])
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
        self.state.setdefault("passwordResets", [])
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
        if not row or not verify_password(password, row[1]):
            return None
        user_id = str(row[0])
        return next((item for item in self.list_users() if item["id"] == user_id), None)

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
        """Increment the failed-or-consumed verification attempt count."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE auth_login_challenges SET attempts = attempts + 1 WHERE token_hash = %s RETURNING attempts",
                (token_hash,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def consume_login_challenge(self, token_hash: str) -> None:
        """Mark a successful login transaction as single-use."""

        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE auth_login_challenges SET consumed_at = now() WHERE token_hash = %s AND consumed_at IS NULL",
                (token_hash,),
            )

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

    def export_state(self) -> dict:
        """Assemble a portable document from canonical tables, never a JSON mirror."""
        return {
            "companies": self.list_companies(),
            "users": self.list_users(include_credentials=True),
            "contacts": self.list_contacts(),
            "contactResponsibilities": self.list_contact_responsibilities(include_inactive=True),
            "notificationRules": self.list_notification_rules(),
            "notificationTemplates": self.list_notification_templates(),
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
