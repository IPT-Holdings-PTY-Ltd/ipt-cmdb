"""Tenant-aware notification candidate, recipient, and template helpers."""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any

from src.cmdb.email_delivery import valid_email_address

TOKEN = re.compile(r"{{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*}}")


DEFAULT_NOTIFICATION_RULES = (
    {
        "key": "asset-renewal",
        "name": "Asset and subscription renewals",
        "eventType": "asset_renewal",
        "leadDays": 90,
        "cadence": "daily",
        "recipientRoles": ["business_owner", "service_owner", "technical_owner", "custodian"],
        "templateKey": "asset_renewal",
        "enabled": True,
    },
    {
        "key": "asset-end-of-life",
        "name": "Asset end of life",
        "eventType": "asset_eol",
        "leadDays": 180,
        "cadence": "daily",
        "recipientRoles": ["business_owner", "service_owner", "technical_owner", "custodian"],
        "templateKey": "asset_eol",
        "enabled": True,
    },
    {
        "key": "change-approval",
        "name": "Change approval required",
        "eventType": "change_approval",
        "leadDays": 0,
        "cadence": "immediate",
        "recipientRoles": ["change_approver", "signoff_delegate", "business_owner"],
        "templateKey": "change_approval",
        "enabled": True,
    },
    {
        "key": "missing-owner",
        "name": "Missing CI owner",
        "eventType": "missing_owner",
        "leadDays": 0,
        "cadence": "weekly",
        "recipientRoles": ["support_contact"],
        "templateKey": "missing_owner",
        "enabled": True,
    },
)


DEFAULT_NOTIFICATION_TEMPLATES = (
    {
        "key": "asset_renewal",
        "name": "Asset renewal",
        "subjectTemplate": "{{company_name}}: {{asset_name}} renews in {{days_label}}",
        "htmlTemplate": "<h2>Renewal attention required</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) has a renewal date of {{event_date}}.</p><p>Time remaining: {{days_label}}.</p><p>Open the CMDB to review ownership, commercial details and dependencies.</p>",
        "textTemplate": "{{asset_name}} ({{asset_type}}) renews on {{event_date}}. Time remaining: {{days_label}}.",
    },
    {
        "key": "asset_eol",
        "name": "Asset end of life",
        "subjectTemplate": "{{company_name}}: {{asset_name}} reaches end of life in {{days_label}}",
        "htmlTemplate": "<h2>End-of-life attention required</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) reaches end of life on {{event_date}}.</p><p>Time remaining: {{days_label}}.</p><p>Review replacement, risk acceptance and affected business services in the CMDB.</p>",
        "textTemplate": "{{asset_name}} ({{asset_type}}) reaches end of life on {{event_date}}. Time remaining: {{days_label}}.",
    },
    {
        "key": "change_approval",
        "name": "Change approval required",
        "subjectTemplate": "Approval required: {{change_number}} - {{change_title}}",
        "htmlTemplate": "<h2>Change approval required</h2><p><strong>{{change_number}} - {{change_title}}</strong> is awaiting approval.</p><p>Risk: {{risk_level}}. Planned start: {{planned_start}}.</p><p>Review the impact, implementation and rollback plans in the CMDB before recording a decision.</p>",
        "textTemplate": "{{change_number}} - {{change_title}} is awaiting approval. Risk: {{risk_level}}. Planned start: {{planned_start}}.",
    },
    {
        "key": "missing_owner",
        "name": "Missing CI owner",
        "subjectTemplate": "{{company_name}}: owner missing for {{asset_name}}",
        "htmlTemplate": "<h2>CMDB ownership gap</h2><p><strong>{{asset_name}}</strong> ({{asset_type}}) has no active structured owner assignment.</p><p>Assign a business, service, technical owner or custodian so operational notifications reach the right people.</p>",
        "textTemplate": "{{asset_name}} ({{asset_type}}) has no active structured owner assignment.",
    },
)


def _days_label(days: int) -> str:
    if days < 0:
        return f"{abs(days)} days overdue"
    if days == 0:
        return "today"
    return f"{days} days"


def notification_candidates(
    rule: dict[str, Any],
    assets: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic candidates for one notification rule."""

    today = today or date.today()
    company_names = {item["id"]: item["name"] for item in companies}
    event_type = str(rule.get("eventType") or "")
    company_filter = rule.get("companyId")
    candidates: list[dict[str, Any]] = []
    if event_type in {"asset_renewal", "asset_eol"}:
        field = "renewalDate" if event_type == "asset_renewal" else "endOfLifeDate"
        for asset in assets:
            if company_filter and asset.get("companyId") != company_filter:
                continue
            if asset.get("status") == "Retired":
                continue
            value = str((asset.get("metadata") or {}).get(field) or "")
            try:
                event_date = date.fromisoformat(value)
            except ValueError:
                continue
            days = (event_date - today).days
            if days > int(rule.get("leadDays") or 0):
                continue
            candidates.append(
                {
                    "companyId": asset["companyId"],
                    "eventType": event_type,
                    "entityType": "asset",
                    "entityId": asset["id"],
                    "entityName": asset["name"],
                    "assetIds": [asset["id"]],
                    "dedupeSuffix": value,
                    "context": {
                        "company_name": company_names.get(asset["companyId"], asset["companyId"]),
                        "asset_name": asset["name"],
                        "asset_type": asset.get("type", "Configuration item"),
                        "event_date": value,
                        "days": days,
                        "days_label": _days_label(days),
                    },
                }
            )
    elif event_type == "missing_owner":
        owner_roles = {"business_owner", "service_owner", "technical_owner", "custodian"}
        for asset in assets:
            if company_filter and asset.get("companyId") != company_filter:
                continue
            active_roles = {
                item.get("role")
                for item in asset.get("responsibilities", [])
                if not item.get("effectiveUntil")
            }
            if active_roles & owner_roles or asset.get("status") == "Retired":
                continue
            week = today.strftime("%G-W%V")
            candidates.append(
                {
                    "companyId": asset["companyId"],
                    "eventType": event_type,
                    "entityType": "asset",
                    "entityId": asset["id"],
                    "entityName": asset["name"],
                    "assetIds": [asset["id"]],
                    "dedupeSuffix": week,
                    "context": {
                        "company_name": company_names.get(asset["companyId"], asset["companyId"]),
                        "asset_name": asset["name"],
                        "asset_type": asset.get("type", "Configuration item"),
                    },
                }
            )
    elif event_type == "change_approval":
        for change in changes:
            if change.get("status") != "awaiting_approval":
                continue
            if company_filter and change.get("companyId") != company_filter:
                continue
            impacted_ids = [
                item.get("id") for item in change.get("impactSnapshot", []) if item.get("id")
            ]
            candidates.append(
                {
                    "companyId": change["companyId"],
                    "eventType": event_type,
                    "entityType": "change_request",
                    "entityId": change["id"],
                    "entityName": change.get("changeNumber") or change.get("title", "Change"),
                    "assetIds": sorted(set(change.get("scopeAssetIds", []) + impacted_ids)),
                    "dedupeSuffix": str(change.get("revision") or 1),
                    "context": {
                        "company_name": change.get("companyName")
                        or company_names.get(change["companyId"], change["companyId"]),
                        "change_number": change.get("changeNumber", "Change"),
                        "change_title": change.get("title", "Untitled change"),
                        "risk_level": change.get("riskLevel", "Not assessed"),
                        "planned_start": change.get("plannedStart") or "Not scheduled",
                    },
                }
            )
    return candidates


def resolve_notification_recipients(
    rule: dict[str, Any],
    candidate: dict[str, Any],
    responsibilities: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Resolve active role contacts and report roles with no usable recipient."""

    roles = list(rule.get("recipientRoles") or [])
    asset_ids = set(candidate.get("assetIds") or [])
    preference_by_contact = {item.get("contactId"): item for item in preferences}
    found_roles: set[str] = set()
    recipients: list[str] = []
    for item in responsibilities:
        role = item.get("role")
        if role not in roles or item.get("effectiveUntil"):
            continue
        if asset_ids and item.get("assetId") not in asset_ids:
            continue
        address = str(item.get("contactEmail") or "").strip().lower()
        preference = preference_by_contact.get(item.get("contactId"), {})
        allowed_events = preference.get("eventTypes") or ["*"]
        if preference.get("emailEnabled", True) is False:
            continue
        if "*" not in allowed_events and candidate.get("eventType") not in allowed_events:
            continue
        if valid_email_address(address):
            found_roles.add(str(role))
            if address not in recipients:
                recipients.append(address)
    for address in rule.get("fallbackAddresses") or []:
        normalized = str(address).strip().lower()
        if valid_email_address(normalized) and normalized not in recipients:
            recipients.append(normalized)
    return recipients, [role for role in roles if role not in found_roles]


def render_notification_template(
    template: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, str]:
    """Render a constrained mustache-like template with escaped context values."""

    text_values = {key: str(value) for key, value in context.items()}

    def render(value: str, *, escape: bool) -> str:
        return TOKEN.sub(
            lambda match: (
                html.escape(text_values.get(match.group(1), ""))
                if escape
                else text_values.get(match.group(1), "")
            ),
            value,
        )

    subject = (
        render(str(template.get("subjectTemplate") or "CMDB notification"), escape=False)
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return {
        "subject": subject[:998],
        "bodyHtml": render(str(template.get("htmlTemplate") or ""), escape=True),
        "bodyText": render(str(template.get("textTemplate") or ""), escape=False),
    }


def notification_dedupe_key(rule: dict[str, Any], candidate: dict[str, Any]) -> str:
    """Return a stable key preventing duplicate messages for one business event."""

    return ":".join(
        (
            str(rule.get("key") or rule.get("id")),
            str(candidate.get("entityType")),
            str(candidate.get("entityId")),
            str(candidate.get("dedupeSuffix")),
        )
    )
