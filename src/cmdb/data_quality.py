"""Deterministic, tenant-safe CMDB data quality evaluation.

The evaluator deliberately works on the canonical resource dictionaries used by
both repositories.  Findings are calculated at read time so a rule improvement
does not require rebuilding a findings table; explicit exceptions are persisted
separately and remain auditable.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import Any

RULES = {
    "missing_owner": {
        "label": "Accountable owner missing",
        "category": "ownership",
        "severity": "high",
        "weight": 8,
    },
    "missing_business_metadata": {
        "label": "Business system metadata incomplete",
        "category": "completeness",
        "severity": "high",
        "weight": 7,
    },
    "unlinked_ci": {
        "label": "No relationships recorded",
        "category": "relationships",
        "severity": "medium",
        "weight": 4,
    },
    "stale_source": {
        "label": "Integration data is stale",
        "category": "freshness",
        "severity": "high",
        "weight": 7,
    },
    "unknown_operational_status": {
        "label": "Operational status is unknown",
        "category": "completeness",
        "severity": "medium",
        "weight": 3,
    },
    "missing_source_identity": {
        "label": "Source identity is missing",
        "category": "identity",
        "severity": "high",
        "weight": 6,
    },
    "possible_duplicate": {
        "label": "Possible duplicate CI",
        "category": "identity",
        "severity": "medium",
        "weight": 5,
    },
}


def _metadata(asset: dict) -> dict:
    return asset.get("metadata") or {}


def _owner(asset: dict) -> str:
    metadata = _metadata(asset)
    if str(asset.get("type", "")).casefold() == "business system":
        return (
            metadata.get("businessOwner")
            or metadata.get("serviceOwner")
            or metadata.get("technicalOwner")
            or ""
        )
    return (
        metadata.get("technicalOwner")
        or metadata.get("serviceOwner")
        or metadata.get("custodian")
        or ""
    )


def _stale(asset: dict, today: date, stale_days: int) -> tuple[bool, str]:
    if asset.get("source") in {None, "", "manual", "demo"}:
        return False, ""
    value = asset.get("lastSeen")
    if not value:
        return True, "No successful source observation has been recorded."
    try:
        seen = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return True, f"Last-seen value {value!r} is invalid."
    age = (today - seen).days
    return (
        age > stale_days,
        f"Last observed {age} days ago; policy is {stale_days} days.",
    )


def _finding(asset: dict, rule_key: str, evidence: str, recommendation: str) -> dict:
    rule = RULES[rule_key]
    return {
        "id": f"{rule_key}:{asset['id']}",
        "ruleKey": rule_key,
        "ruleLabel": rule["label"],
        "category": rule["category"],
        "severity": rule["severity"],
        "weight": rule["weight"],
        "companyId": asset["companyId"],
        "assetId": asset["id"],
        "assetName": asset["name"],
        "assetType": asset["type"],
        "source": asset.get("source") or "manual",
        "evidence": evidence,
        "recommendation": recommendation,
    }


def evaluate_data_quality(
    companies: list[dict],
    assets: list[dict],
    relationships: list[dict],
    exceptions: list[dict] | None = None,
    *,
    today: date | None = None,
    stale_days: int = 30,
) -> dict[str, Any]:
    """Return an actionable quality scorecard and unsuppressed findings."""
    today = today or datetime.now(UTC).date()
    exceptions = exceptions or []
    company_names = {item["id"]: item["name"] for item in companies}
    related: Counter[str] = Counter()
    for relationship in relationships:
        for asset_id in (relationship.get("fromId"), relationship.get("toId")):
            if isinstance(asset_id, str):
                related[asset_id] += 1

    duplicate_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    external_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for asset in assets:
        normalized = " ".join(str(asset.get("name", "")).casefold().split())
        duplicate_groups[(asset["companyId"], normalized)].append(asset)
        if asset.get("externalId"):
            external_groups[
                (
                    asset["companyId"],
                    str(asset.get("source", "")).casefold(),
                    str(asset["externalId"]).casefold(),
                )
            ].append(asset)

    findings: list[dict] = []
    for asset in assets:
        metadata = _metadata(asset)
        if not _owner(asset):
            findings.append(
                _finding(
                    asset,
                    "missing_owner",
                    "No business, service, technical or custodian owner is assigned.",
                    "Assign an accountable owner on the CI.",
                )
            )
        if str(asset.get("type", "")).casefold() == "business system":
            required = {
                "business owner": metadata.get("businessOwner"),
                "service owner": metadata.get("serviceOwner"),
                "RTO": metadata.get("rtoHours"),
                "RPO": metadata.get("rpoHours"),
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                findings.append(
                    _finding(
                        asset,
                        "missing_business_metadata",
                        f"Missing {', '.join(missing)}.",
                        "Complete ownership and recovery objectives for change impact and sign-off.",
                    )
                )
        if not related[asset["id"]] and metadata.get("lifecycle", "in_service") not in {
            "planned",
            "retired",
            "disposed",
        }:
            findings.append(
                _finding(
                    asset,
                    "unlinked_ci",
                    "The CI has no active incoming or outgoing relationship.",
                    "Relate it to the service, application, host or network component it supports.",
                )
            )
        is_stale, stale_evidence = _stale(asset, today, stale_days)
        if is_stale:
            findings.append(
                _finding(
                    asset,
                    "stale_source",
                    stale_evidence,
                    "Verify the integration and refresh or retire this CI.",
                )
            )
        if (
            metadata.get("operationalStatus", "unknown") == "unknown"
            and metadata.get("lifecycle", "in_service") == "in_service"
        ):
            findings.append(
                _finding(
                    asset,
                    "unknown_operational_status",
                    "The CI is in service but its operational state is unknown.",
                    "Set or synchronize the current operational status.",
                )
            )
        if asset.get("source") not in {None, "", "manual", "demo"} and not asset.get("externalId"):
            findings.append(
                _finding(
                    asset,
                    "missing_source_identity",
                    f"{asset.get('source')} supplied the CI without a stable external identifier.",
                    "Map the source record ID before allowing automated updates.",
                )
            )
        normalized = " ".join(str(asset.get("name", "")).casefold().split())
        name_matches = duplicate_groups[(asset["companyId"], normalized)]
        id_matches = (
            external_groups.get(
                (
                    asset["companyId"],
                    str(asset.get("source", "")).casefold(),
                    str(asset.get("externalId", "")).casefold(),
                ),
                [],
            )
            if asset.get("externalId")
            else []
        )
        matches = {
            item["id"]: item for item in name_matches + id_matches if item["id"] != asset["id"]
        }
        if matches:
            names = ", ".join(sorted(item["name"] for item in matches.values()))
            findings.append(
                _finding(
                    asset,
                    "possible_duplicate",
                    f"Identity signals overlap with {names}.",
                    "Review the records and merge or confirm that they are distinct.",
                )
            )

    active_exceptions = {
        (item.get("ruleKey"), item.get("entityId"))
        for item in exceptions
        if item.get("state", "active") == "active"
        and (not item.get("expiresAt") or str(item["expiresAt"])[:10] >= today.isoformat())
    }
    findings = [
        item for item in findings if (item["ruleKey"], item["assetId"]) not in active_exceptions
    ]
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(
        key=lambda item: (
            severity_rank[item["severity"]],
            company_names.get(item["companyId"], ""),
            item["assetName"],
            item["ruleKey"],
        )
    )

    company_rows = []
    for company in companies:
        company_assets = [item for item in assets if item["companyId"] == company["id"]]
        company_findings = [item for item in findings if item["companyId"] == company["id"]]
        denominator = max(1, len(company_assets) * 12)
        penalty = min(denominator, sum(item["weight"] for item in company_findings))
        company_rows.append(
            {
                "companyId": company["id"],
                "companyName": company["name"],
                "assetCount": len(company_assets),
                "findingCount": len(company_findings),
                "highCount": sum(item["severity"] == "high" for item in company_findings),
                "score": round(100 * (denominator - penalty) / denominator),
            }
        )
    denominator = max(1, len(assets) * 12)
    penalty = min(denominator, sum(item["weight"] for item in findings))
    return {
        "summary": {
            "score": round(100 * (denominator - penalty) / denominator),
            "assetCount": len(assets),
            "findingCount": len(findings),
            "highCount": sum(item["severity"] == "high" for item in findings),
            "exceptionCount": len(active_exceptions),
            "staleDays": stale_days,
            "byCategory": dict(Counter(item["category"] for item in findings)),
        },
        "customers": sorted(company_rows, key=lambda item: (item["score"], item["companyName"])),
        "findings": findings,
        "rules": [{"key": key, **value} for key, value in RULES.items()],
    }
