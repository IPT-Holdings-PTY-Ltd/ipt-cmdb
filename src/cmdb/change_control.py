"""Change-control impact snapshots and PDF rendering.

Change packages use canonical CI identifiers, immutable PostgreSQL revisions,
and a provider-neutral external-reference envelope so a future ConnectWise
publisher does not need to change the frontend contract.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from html import escape
from io import BytesIO
from typing import Any

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TEMPLATE_TOKEN = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}", re.IGNORECASE)
CLOSURE_RESULTS = {
    "successful",
    "successful_with_issues",
    "partially_implemented",
    "failed",
    "backed_out",
}
CLOSURE_SERVICE_STATES = {"restored", "degraded", "unavailable"}
CLOSURE_TEST_RESULTS = {"pending", "passed", "failed", "not_run"}
CLOSURE_FOLLOW_UP_STATES = {"open", "completed"}
CLOSURE_CONFIRMATION_STATES = {"not_required", "confirmed"}

CHANGE_TYPES = {"standard", "normal", "emergency"}
CHANGE_CATEGORIES = {
    "infrastructure",
    "network",
    "software",
    "database",
    "security",
    "cloud",
    "other",
}
CHANGE_PRIORITIES = {"low", "medium", "high", "critical"}
RISK_LEVELS = {"low", "medium", "high", "critical"}
COMMUNICATION_STATES = {"required", "not_required", "completed"}
CHANGE_STATUSES = {
    "draft",
    "impact_review",
    "awaiting_approval",
    "approved",
    "declined",
    "scheduled",
    "implementing",
    "completed",
    "failed",
    "backed_out",
    "post_implementation_review",
    "cancelled",
    "closed",
}
CHANGE_TRANSITIONS = {
    "draft": {"impact_review", "cancelled"},
    "impact_review": {"draft", "awaiting_approval", "cancelled"},
    "awaiting_approval": {"impact_review", "approved", "declined", "cancelled"},
    "approved": {"impact_review", "scheduled", "cancelled"},
    "declined": {"draft", "cancelled"},
    "scheduled": {"approved", "implementing", "cancelled"},
    "implementing": {"completed", "failed"},
    "completed": {"post_implementation_review"},
    "failed": {"backed_out", "post_implementation_review"},
    "backed_out": {"post_implementation_review"},
    "post_implementation_review": {"closed"},
    "cancelled": set(),
    "closed": set(),
}
FULL_EDIT_STATUSES = {"draft", "impact_review"}
SCHEDULE_EDIT_STATUSES = {"approved", "scheduled"}
SCHEDULE_EDIT_FIELDS = {
    "plannedStart",
    "plannedEnd",
    "communicationStatus",
    "communicationPlan",
    "notes",
}
ASSIGNABLE_CHANGE_STATUSES = CHANGE_STATUSES - {"cancelled", "closed"}
REASON_REQUIRED_TRANSITIONS = {
    "declined",
    "cancelled",
    "failed",
    "backed_out",
    "closed",
}
NON_PROPAGATING_RELATIONSHIPS = {"related_to", "member_of", "backs_up"}
REVERSED_IMPACT_RELATIONSHIPS = {
    "depends_on",
    "installed_on",
    "stored_on",
    "provided_by",
    "managed_by",
    "protected_by",
}
VIRTUAL_MACHINE_TYPES = {"virtual machine"}
HYPERVISOR_HOST_TYPES = {"hypervisor host"}


def utc_now() -> str:
    """Return the current UTC timestamp in the API's canonical format."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _impact_edge(relationship: dict) -> tuple[str, str] | None:
    """Return the supporting-to-affected direction used by outage analysis."""
    relationship_type = relationship.get("type", "related_to")
    if (
        relationship_type in NON_PROPAGATING_RELATIONSHIPS
        or relationship.get("impactPolicy") == "informational"
    ):
        return None
    if relationship_type in REVERSED_IMPACT_RELATIONSHIPS:
        return relationship.get("toId", ""), relationship.get("fromId", "")
    return relationship.get("fromId", ""), relationship.get("toId", "")


def _impact_severity(impact_policy_path: list[str]) -> str:
    # A protected or degraded hop limits the effect that can continue further
    # down that path. A required application dependency after an HA failover
    # does not turn a protected VM into an outage.
    if "redundant" in impact_policy_path:
        return "protected"
    if "degraded" in impact_policy_path:
        return "degraded"
    if "required" in impact_policy_path:
        return "outage"
    return "scope"


def _virtualization_policy(
    relationship: dict,
    assets_by_id: dict[str, dict],
    relationships: list[dict],
    scope_ids: set[str],
) -> tuple[str, str]:
    """Resolve a host-to-VM edge using current cluster HA and capacity evidence."""
    configured = relationship.get("impactPolicy", "required")
    if relationship.get("type") != "hosts":
        return configured, ""
    from_id = relationship.get("fromId")
    to_id = relationship.get("toId")
    host = assets_by_id.get(from_id) if isinstance(from_id, str) else None
    vm = assets_by_id.get(to_id) if isinstance(to_id, str) else None
    if (
        not host
        or not vm
        or host.get("type", "").lower() not in HYPERVISOR_HOST_TYPES
        or vm.get("type", "").lower() not in VIRTUAL_MACHINE_TYPES
    ):
        return configured, ""

    vm_metadata = vm.get("metadata") or {}
    if (
        vm_metadata.get("haEnabled") != "yes"
        or vm_metadata.get("mobility", "automatic") != "automatic"
    ):
        return (
            "required",
            "HA restart unavailable: the VM is not automatically movable.",
        )
    if vm_metadata.get("protectionStatus") == "unprotected":
        return "required", "HA restart unavailable: the VM is marked unprotected."

    memberships: dict[str, set[str]] = {}
    for item in relationships:
        if item.get("type") == "member_of":
            memberships.setdefault(item.get("fromId", ""), set()).add(item.get("toId", ""))
    common_clusters = memberships.get(host["id"], set()) & memberships.get(vm["id"], set())
    if not common_clusters:
        return (
            "required",
            "HA restart unavailable: host and VM have no common cluster membership.",
        )
    cluster_id = sorted(common_clusters)[0]
    cluster = assets_by_id.get(cluster_id) or {}
    cluster_metadata = cluster.get("metadata") or {}
    candidate_hosts = []
    for candidate_id, candidate_clusters in memberships.items():
        candidate = assets_by_id.get(candidate_id)
        candidate_metadata = (candidate or {}).get("metadata") or {}
        if (
            candidate
            and candidate.get("type", "").lower() in HYPERVISOR_HOST_TYPES
            and cluster_id in candidate_clusters
            and candidate_id != host["id"]
            and candidate_id not in scope_ids
            and candidate_metadata.get("powerState", "running") == "running"
            and candidate_metadata.get("maintenanceMode", "no") != "yes"
            and candidate_metadata.get("operationalStatus", "unknown")
            not in {"critical", "offline"}
        ):
            candidate_hosts.append(candidate)
    try:
        minimum_hosts = max(1, int(cluster_metadata.get("minimumHosts") or 1))
    except (TypeError, ValueError):
        minimum_hosts = 1
    capacity = cluster_metadata.get("capacityStatus", "unknown")
    cluster_name = cluster.get("name") or vm_metadata.get("clusterName") or "virtualization cluster"
    if len(candidate_hosts) < minimum_hosts or capacity == "insufficient":
        return (
            "required",
            f"HA restart unavailable in {cluster_name}: surviving host capacity is insufficient.",
        )
    if capacity in {"constrained", "unknown"} or vm_metadata.get("protectionStatus") == "degraded":
        return (
            "degraded",
            f"HA restart is possible in {cluster_name}, but remaining capacity is constrained or unverified.",
        )
    return (
        "redundant",
        f"Protected by {cluster_name}: {len(candidate_hosts)} eligible host(s) remain with sufficient capacity.",
    )


def _asset_snapshot(
    asset: dict,
    role: str,
    depth: int,
    path: list[str],
    relationship_path: list[str],
    impact_policy_path: list[str],
    impact_notes: list[str],
) -> dict:
    metadata = asset.get("metadata") or {}
    service_owner = str(metadata.get("serviceOwner") or "").strip()
    technical_owner = str(metadata.get("technicalOwner") or "").strip()
    custodian = str(metadata.get("custodian") or "").strip()
    responsibilities = []
    for responsibility in asset.get("responsibilities") or []:
        role_name = str(responsibility.get("role") or "").strip().lower()
        contact_email = str(responsibility.get("contactEmail") or "").strip().lower()
        if not role_name:
            continue
        responsibilities.append(
            {
                "contactId": responsibility.get("contactId"),
                "contactName": str(responsibility.get("contactName") or "").strip(),
                "contactEmail": contact_email,
                "role": role_name,
                "isPrimary": bool(responsibility.get("isPrimary", True)),
                "effectiveUntil": responsibility.get("effectiveUntil"),
            }
        )
    return {
        "assetId": asset["id"],
        "name": asset.get("name", asset["id"]),
        "type": asset.get("type", "Configuration item"),
        "role": role,
        "depth": depth,
        "pathAssetIds": path,
        "relationshipPath": relationship_path,
        "impactPolicyPath": impact_policy_path,
        "impactSeverity": _impact_severity(impact_policy_path),
        "impactNotes": [value for value in impact_notes if value],
        "criticality": metadata.get("criticality", "medium"),
        "environment": metadata.get("environment", "production"),
        "site": metadata.get("site", ""),
        "lifecycle": metadata.get("lifecycle", "in_service"),
        "operationalStatus": metadata.get("operationalStatus", "unknown"),
        "serviceOwner": service_owner,
        "technicalOwner": technical_owner,
        "custodian": custodian,
        "businessOwner": str(metadata.get("businessOwner") or "").strip(),
        "signoffDelegate": str(metadata.get("signoffDelegate") or "").strip(),
        "signoffRequired": str(metadata.get("signoffRequired") or "yes").strip(),
        "responsibilities": responsibilities,
        "department": str(metadata.get("department") or "").strip(),
        "userPopulation": str(metadata.get("userPopulation") or "").strip(),
        "rtoHours": str(metadata.get("rtoHours") or "").strip(),
        "rpoHours": str(metadata.get("rpoHours") or "").strip(),
        "virtualizationPlatform": str(metadata.get("virtualizationPlatform") or "").strip(),
        "clusterName": str(metadata.get("clusterName") or "").strip(),
        "haEnabled": str(metadata.get("haEnabled") or "no").strip(),
        "capacityStatus": str(metadata.get("capacityStatus") or "unknown").strip(),
        "mobility": str(metadata.get("mobility") or "automatic").strip(),
        "powerState": str(metadata.get("powerState") or "unknown").strip(),
        "protectionStatus": str(metadata.get("protectionStatus") or "unknown").strip(),
        "virtualizationDecision": next((value for value in reversed(impact_notes) if value), ""),
        "owner": technical_owner
        or service_owner
        or str(metadata.get("businessOwner") or "").strip()
        or custodian
        or "No owner recorded",
        "source": asset.get("source", "manual"),
        "externalId": asset.get("externalId"),
    }


def build_impact_snapshot(
    company_id: str,
    scope_asset_ids: list[str],
    assets: list[dict],
    relationships: list[dict],
) -> list[dict]:
    """Freeze selected CIs and their downstream outage-impact paths."""
    company_assets = {
        asset["id"]: asset for asset in assets if asset.get("companyId") == company_id
    }
    unique_scope = list(dict.fromkeys(scope_asset_ids))
    if not unique_scope:
        raise ValueError("Choose at least one configuration item")
    missing = [asset_id for asset_id in unique_scope if asset_id not in company_assets]
    if missing:
        raise ValueError(
            "One or more selected configuration items are unavailable in this customer"
        )

    scope_set = set(unique_scope)
    adjacency: dict[str, list[tuple[str, str, str, str]]] = {}
    for relationship in relationships:
        edge = _impact_edge(relationship)
        if not edge or edge[0] not in company_assets or edge[1] not in company_assets:
            continue
        impact_policy, impact_note = _virtualization_policy(
            relationship, company_assets, relationships, scope_set
        )
        adjacency.setdefault(edge[0], []).append(
            (
                edge[1],
                relationship.get("type", "related_to"),
                impact_policy,
                impact_note,
            )
        )

    paths: dict[str, tuple[int, list[str], list[str], list[str], list[str]]] = {}
    queue: deque[tuple[str, int, list[str], list[str], list[str], list[str]]] = deque()
    for asset_id in unique_scope:
        paths[asset_id] = (0, [asset_id], [], [], [])
        queue.append((asset_id, 0, [asset_id], [], [], []))

    while queue:
        current, depth, path, relationship_path, impact_policy_path, impact_notes = queue.popleft()
        for target, relationship_type, impact_policy, impact_note in adjacency.get(current, []):
            if target in paths:
                continue
            next_path = [*path, target]
            next_relationships = [*relationship_path, relationship_type]
            next_policies = [*impact_policy_path, impact_policy]
            next_notes = [*impact_notes, impact_note]
            paths[target] = (
                depth + 1,
                next_path,
                next_relationships,
                next_policies,
                next_notes,
            )
            queue.append(
                (
                    target,
                    depth + 1,
                    next_path,
                    next_relationships,
                    next_policies,
                    next_notes,
                )
            )

    snapshots = []
    for asset_id, (
        depth,
        path,
        relationship_path,
        impact_policy_path,
        impact_notes,
    ) in paths.items():
        role = (
            "Scope"
            if asset_id in scope_set
            else "Direct impact"
            if depth == 1
            else "Downstream impact"
        )
        snapshots.append(
            _asset_snapshot(
                company_assets[asset_id],
                role,
                depth,
                path,
                relationship_path,
                impact_policy_path,
                impact_notes,
            )
        )
    return sorted(snapshots, key=lambda item: (item["depth"], item["name"].lower()))


def calculate_risk(snapshot: list[dict], outage_expected: bool) -> dict:
    """Score a change from its affected CIs, criticality, and outage intent."""

    score = 0
    factors: list[str] = []
    active_impact = [
        item
        for item in snapshot
        if item.get("role") == "Scope" or item.get("impactSeverity") != "protected"
    ]
    criticalities = {item.get("criticality") for item in active_impact}
    if "critical" in criticalities:
        score += 4
        factors.append("Critical CI in scope or impact path")
    elif "high" in criticalities:
        score += 2
        factors.append("High-criticality CI in scope or impact path")
    downstream_count = sum(
        item.get("role") != "Scope" and item.get("impactSeverity") != "protected"
        for item in snapshot
    )
    if downstream_count:
        addition = min(4, 1 + downstream_count // 4)
        score += addition
        factors.append(f"{downstream_count} downstream configuration item(s)")
    if any(item.get("environment") == "production" for item in snapshot):
        score += 2
        factors.append("Production environment affected")
    if outage_expected:
        score += 2
        factors.append("Service interruption expected")
    missing_owners = sum(item.get("owner") == "No owner recorded" for item in active_impact)
    if missing_owners:
        score += 2
        factors.append(f"{missing_owners} impacted item(s) have no recorded owner")
    critical_business_systems = sum(
        item.get("type") == "Business system"
        and item.get("criticality") in {"high", "critical"}
        and item.get("impactSeverity") != "protected"
        for item in snapshot
    )
    if critical_business_systems:
        score += min(3, critical_business_systems)
        factors.append(f"{critical_business_systems} high-criticality business system(s) affected")
    missing_business_owners = sum(
        item.get("type") == "Business system"
        and item.get("impactSeverity") != "protected"
        and not item.get("businessOwner")
        for item in snapshot
    )
    if missing_business_owners:
        score += 2
        factors.append(
            f"{missing_business_owners} business system(s) have no recorded business owner"
        )
    protected_vms = sum(
        item.get("type", "").lower() in VIRTUAL_MACHINE_TYPES
        and item.get("impactSeverity") == "protected"
        for item in snapshot
    )
    if protected_vms:
        factors.append(f"{protected_vms} virtual machine(s) protected by verified HA capacity")
    level = (
        "critical" if score >= 10 else "high" if score >= 7 else "medium" if score >= 4 else "low"
    )
    return {
        "score": score,
        "level": level,
        "factors": factors or ["No elevated CMDB risk factors detected"],
    }


def impact_summary(snapshot: list[dict], risk: dict) -> dict:
    """Aggregate impacted owners, services, and infrastructure for review."""

    owners = sorted({item["owner"] for item in snapshot if item["owner"] != "No owner recorded"})
    business_systems = [
        {
            key: item.get(key, "")
            for key in (
                "assetId",
                "name",
                "criticality",
                "operationalStatus",
                "businessOwner",
                "serviceOwner",
                "signoffDelegate",
                "signoffRequired",
                "department",
                "userPopulation",
                "rtoHours",
                "rpoHours",
                "impactSeverity",
                "responsibilities",
            )
        }
        for item in snapshot
        if item.get("type") == "Business system"
    ]
    business_owners = sorted(
        {str(item["businessOwner"]) for item in business_systems if item.get("businessOwner")}
    )
    virtualization = [
        {
            key: item.get(key, "")
            for key in (
                "assetId",
                "name",
                "role",
                "impactSeverity",
                "virtualizationPlatform",
                "clusterName",
                "haEnabled",
                "powerState",
                "protectionStatus",
                "virtualizationDecision",
            )
        }
        for item in snapshot
        if item.get("type", "").lower() in VIRTUAL_MACHINE_TYPES
    ]
    return {
        "scopeCount": sum(item["role"] == "Scope" for item in snapshot),
        "directCount": sum(item["role"] == "Direct impact" for item in snapshot),
        "downstreamCount": sum(item["role"] == "Downstream impact" for item in snapshot),
        "criticalCount": sum(item["criticality"] == "critical" for item in snapshot),
        "missingOwnerCount": sum(item["owner"] == "No owner recorded" for item in snapshot),
        "owners": owners,
        "businessSystemCount": len(business_systems),
        "businessSystems": business_systems,
        "businessOwners": business_owners,
        "missingBusinessOwnerCount": sum(
            not item.get("businessOwner") for item in business_systems
        ),
        "virtualizationAssessments": virtualization,
        "protectedVmCount": sum(
            item.get("impactSeverity") == "protected" for item in virtualization
        ),
        "degradedVmCount": sum(item.get("impactSeverity") == "degraded" for item in virtualization),
        "outageVmCount": sum(item.get("impactSeverity") == "outage" for item in virtualization),
        "suggestedRisk": risk,
    }


def derive_change_approvers(change: dict) -> dict:
    """Resolve approval recipients from the frozen change-impact snapshot.

    Structured, effective-dated CI responsibilities are authoritative. Legacy
    free-text owner fields are accepted only when their value is itself an
    email address, preventing a display name from being treated as a delivery
    destination.
    """

    resolved: dict[str, dict] = {}
    missing: list[dict] = []

    def add_approver(
        email: object,
        name: object,
        role: str,
        scope: dict,
        contact_id: object = None,
    ) -> bool:
        address = str(email or "").strip().lower()
        if not EMAIL_PATTERN.fullmatch(address):
            return False
        record = resolved.setdefault(
            address,
            {
                "approverEmail": address,
                "approverName": str(name or address).strip() or address,
                "approverContactId": contact_id,
                "responsibilityRoles": [],
                "scope": [],
            },
        )
        if role not in record["responsibilityRoles"]:
            record["responsibilityRoles"].append(role)
        if scope not in record["scope"]:
            record["scope"].append(scope)
        if not record.get("approverContactId") and contact_id:
            record["approverContactId"] = contact_id
        return True

    configured_approver = str(change.get("approver") or "").strip()
    if EMAIL_PATTERN.fullmatch(configured_approver.lower()):
        add_approver(
            configured_approver,
            configured_approver,
            "change_approver",
            {"type": "change", "id": change.get("id"), "name": change.get("number")},
        )

    for system in (change.get("impactSummary") or {}).get("businessSystems") or []:
        signoff_required = str(system.get("signoffRequired") or "yes").strip().lower()
        if signoff_required in {"no", "false", "0", "not_required"}:
            continue
        system_scope = {
            "type": "business_system",
            "id": system.get("assetId"),
            "name": system.get("name") or "Business system",
        }
        responsibilities = [
            item
            for item in system.get("responsibilities") or []
            if not item.get("effectiveUntil")
            and item.get("role") in {"signoff_delegate", "business_owner"}
        ]
        responsibilities.sort(
            key=lambda item: (
                0 if item.get("role") == "signoff_delegate" else 1,
                0 if item.get("isPrimary", True) else 1,
            )
        )
        selected = next(
            (
                item
                for item in responsibilities
                if EMAIL_PATTERN.fullmatch(str(item.get("contactEmail") or "").strip().lower())
            ),
            None,
        )
        if selected:
            add_approver(
                selected.get("contactEmail"),
                selected.get("contactName"),
                str(selected.get("role")),
                system_scope,
                selected.get("contactId"),
            )
            continue
        fallback_role = "signoff_delegate" if system.get("signoffDelegate") else "business_owner"
        fallback_value = system.get("signoffDelegate") or system.get("businessOwner")
        if not add_approver(fallback_value, fallback_value, fallback_role, system_scope):
            missing.append(
                {
                    "assetId": system.get("assetId"),
                    "name": system.get("name") or "Business system",
                    "reason": "No active sign-off delegate or business owner with an email address",
                }
            )

    approvers = sorted(resolved.values(), key=lambda item: item["approverEmail"])
    for item in approvers:
        item["responsibilityRoles"].sort()
    return {"approvers": approvers, "missing": missing}


def record_external_approval(
    change: dict,
    approval_request: dict,
    decision: str,
    comments: str,
    all_approved: bool,
) -> dict:
    """Append external decision evidence and apply its lifecycle consequence."""

    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision not in {"approved", "declined"}:
        raise ValueError("Choose approved or declined")
    if change.get("status") != "awaiting_approval":
        raise ValueError("This change is no longer awaiting approval")
    timestamp = str(approval_request.get("decidedAt") or utc_now())
    updated = deepcopy(change)
    updated["updatedAt"] = timestamp
    updated["revision"] = int(change.get("revision") or 1) + 1
    evidence = {
        "id": str(uuid.uuid4()),
        "requestId": approval_request.get("id"),
        "batchId": approval_request.get("batchId"),
        "decision": normalized_decision,
        "comments": str(comments or "").strip()[:8000],
        "actorId": approval_request.get("approverUserId"),
        "actorEmail": approval_request.get("approverEmail", ""),
        "approverName": approval_request.get("approverName", ""),
        "approverContactId": approval_request.get("approverContactId"),
        "responsibilityRole": approval_request.get("responsibilityRole", ""),
        "scope": deepcopy(approval_request.get("scope") or []),
        "createdAt": timestamp,
    }
    updated["approvals"] = [*(updated.get("approvals") or []), evidence]
    target = (
        "declined" if normalized_decision == "declined" else "approved" if all_approved else None
    )
    if target:
        updated["status"] = target
        reason = evidence["comments"] or (
            "External approver declined the change"
            if target == "declined"
            else "All required external approvals received"
        )
        updated["statusHistory"] = [
            *(updated.get("statusHistory") or []),
            {
                "id": str(uuid.uuid4()),
                "fromStatus": "awaiting_approval",
                "toStatus": target,
                "reason": reason,
                "actorId": approval_request.get("approverUserId"),
                "actorEmail": approval_request.get("approverEmail", ""),
                "createdAt": timestamp,
            },
        ]
    return updated


def preview_change_impact(
    company_id: str,
    scope_asset_ids: list[str],
    outage_expected: bool,
    assets: list[dict],
    relationships: list[dict],
) -> dict:
    """Build a non-persistent impact preview for a proposed change scope."""

    snapshot = build_impact_snapshot(company_id, scope_asset_ids, assets, relationships)
    risk = calculate_risk(snapshot, outage_expected)
    return {"items": snapshot, "summary": impact_summary(snapshot, risk)}


def normalise_change_payload(payload: dict) -> dict:
    """Validate and normalize technician-supplied change-control fields."""

    required_text = {
        "title": "Enter a change title",
        "reason": "Enter the reason for the change",
        "implementationPlan": "Enter the implementation plan",
        "validationPlan": "Enter the validation plan",
        "rollbackPlan": "Enter the rollback plan",
    }
    result = dict(payload)
    for field, message in required_text.items():
        value = str(result.get(field) or "").strip()
        if not value:
            raise ValueError(message)
        result[field] = value[:8000]
    enum_fields = {
        "changeType": (CHANGE_TYPES, "normal"),
        "category": (CHANGE_CATEGORIES, "infrastructure"),
        "priority": (CHANGE_PRIORITIES, "medium"),
        "communicationStatus": (COMMUNICATION_STATES, "required"),
    }
    for field, (allowed, default) in enum_fields.items():
        value = str(result.get(field) or default).lower()
        if value not in allowed:
            raise ValueError(f"Choose a valid {field}")
        result[field] = value
    risk_level = str(result.get("riskLevel") or "").lower()
    if risk_level and risk_level not in RISK_LEVELS:
        raise ValueError("Choose a valid risk level")
    result["riskLevel"] = risk_level
    result["scopeAssetIds"] = [str(value) for value in result.get("scopeAssetIds") or []]
    result["outageExpected"] = bool(result.get("outageExpected"))
    for field in (
        "plannedStart",
        "plannedEnd",
        "businessImpact",
        "communicationPlan",
        "assignedTechnician",
        "approver",
        "notes",
    ):
        result[field] = str(result.get(field) or "").strip()[:8000]
    result["assignedUserId"] = str(result.get("assignedUserId") or "").strip() or None
    result["templateId"] = str(result.get("templateId") or "").strip() or None
    result["templateVersion"] = (
        int(result.get("templateVersion") or 0) if result["templateId"] else None
    )
    result["templateSnapshot"] = (
        deepcopy(result.get("templateSnapshot"))
        if isinstance(result.get("templateSnapshot"), dict)
        else None
    )
    result["templateParameters"] = (
        deepcopy(result.get("templateParameters"))
        if isinstance(result.get("templateParameters"), dict)
        else {}
    )
    if (
        result["plannedStart"]
        and result["plannedEnd"]
        and result["plannedEnd"] <= result["plannedStart"]
    ):
        raise ValueError("Planned end must be after planned start")
    return result


def _render_closure_template(template: str, values: dict[str, Any]) -> str:
    """Render a safe closure-test string from a pinned template context."""

    def replacement(match: re.Match[str]) -> str:
        value = values.get(match.group(1).casefold())
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return "" if value is None else str(value)

    return TEMPLATE_TOKEN.sub(replacement, str(template or "")).strip()


def initial_closure_assessment(change: dict) -> dict:
    """Build the structured closure draft frozen to one change revision."""

    impact = list(change.get("impactSnapshot") or [])
    primary = next(
        (item for item in impact if item.get("role") == "Scope"),
        impact[0] if impact else {},
    )
    business_system: dict[str, Any] = next(
        (
            item
            for item in impact
            if str(item.get("type") or "").strip().casefold()
            in {"business system", "business application", "business service"}
        ),
        {},
    )
    context: dict[str, Any] = {
        "company_name": change.get("companyName", ""),
        "asset_name": primary.get("name", ""),
        "business_system_name": business_system.get("name", ""),
        **(change.get("templateParameters") or {}),
    }
    snapshot = change.get("templateSnapshot") or {}
    content = snapshot.get("content") or {}
    definitions = content.get("closureTests") or []
    tests = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        key = str(definition.get("key") or "").strip().lower()
        if not key:
            continue
        tests.append(
            {
                "key": key,
                "label": _render_closure_template(definition.get("label", ""), context)
                or key.replace("_", " ").title(),
                "expectedResult": _render_closure_template(
                    definition.get("expectedResultTemplate", ""),
                    context,
                ),
                "required": bool(definition.get("required", True)),
                "evidenceRequired": bool(definition.get("evidenceRequired", False)),
                "result": "pending",
                "actualResult": "",
                "evidence": "",
                "testedBy": "",
                "testedAt": "",
            }
        )
    if not tests:
        tests.append(
            {
                "key": "validation_plan",
                "label": "Validation plan",
                "expectedResult": str(change.get("validationPlan") or "").strip(),
                "required": True,
                "evidenceRequired": False,
                "result": "pending",
                "actualResult": "",
                "evidence": "",
                "testedBy": "",
                "testedAt": "",
            }
        )
    outcome = str(change.get("outcome") or "pending")
    implementation_result = outcome if outcome in CLOSURE_RESULTS else "successful"
    assessment = {
        "implementationResult": implementation_result,
        "serviceStatus": "restored",
        "deviations": "",
        "unexpectedImpact": "",
        "tests": tests,
        "pirRequired": False,
        "pirCompleted": False,
        "lessonsLearned": "",
        "followUpActions": [],
        "stakeholderConfirmation": "not_required",
        "closureSummary": "",
        "closedBy": "",
        "closedAt": "",
    }
    assessment["pirRequired"] = _closure_requires_pir(change, assessment)
    return assessment


def _closure_requires_pir(change: dict, assessment: dict) -> bool:
    """Return whether governance rules require a completed PIR."""

    return any(
        (
            str(change.get("changeType") or "") == "emergency",
            str(change.get("riskLevel") or "") in {"high", "critical"},
            bool(change.get("rollbackExecuted")),
            assessment.get("implementationResult") != "successful",
            assessment.get("serviceStatus") != "restored",
            bool(str(assessment.get("unexpectedImpact") or "").strip()),
            any(
                test.get("result") == "failed"
                for test in assessment.get("tests") or []
                if isinstance(test, dict)
            ),
        )
    )


def normalize_closure_assessment(change: dict, payload: object, actor: dict) -> dict:
    """Validate closure evidence and enforce final-review readiness gates."""

    if not isinstance(payload, dict):
        raise ValueError("Complete the post-change review before closing this change")
    draft = initial_closure_assessment(change)
    implementation_result = str(
        payload.get("implementationResult") or draft["implementationResult"]
    ).strip()
    if implementation_result not in CLOSURE_RESULTS:
        raise ValueError("Choose a valid implementation result")
    service_status = str(payload.get("serviceStatus") or "").strip()
    if service_status not in CLOSURE_SERVICE_STATES:
        raise ValueError("Choose the current service status")

    supplied_tests = payload.get("tests")
    if not isinstance(supplied_tests, list) or not supplied_tests or len(supplied_tests) > 30:
        raise ValueError("Record between 1 and 30 post-change tests")
    definitions = {item["key"]: item for item in draft["tests"]}
    tests: list[dict[str, Any]] = []
    seen: set[str] = set()
    timestamp = utc_now()
    for raw in supplied_tests:
        if not isinstance(raw, dict):
            raise ValueError("Each post-change test must be an object")
        key = str(raw.get("key") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key) or key in seen:
            raise ValueError("Post-change test keys must be unique stable identifiers")
        seen.add(key)
        definition = definitions.get(key)
        label = str((definition or {}).get("label") or raw.get("label") or "").strip()[:160]
        expected = str(
            (definition or {}).get("expectedResult") or raw.get("expectedResult") or ""
        ).strip()[:2000]
        if not label or not expected:
            raise ValueError(f"Complete the name and expected result for test {key}")
        required = bool((definition or {}).get("required", raw.get("required", True)))
        evidence_required = bool(
            (definition or {}).get(
                "evidenceRequired",
                raw.get("evidenceRequired", False),
            )
        )
        result = str(raw.get("result") or "pending").strip()
        if result not in CLOSURE_TEST_RESULTS:
            raise ValueError(f"Choose a valid result for {label}")
        actual_result = str(raw.get("actualResult") or "").strip()[:4000]
        evidence = str(raw.get("evidence") or "").strip()[:4000]
        if required and result in {"pending", "not_run"}:
            raise ValueError(f"Complete the required test: {label}")
        if result in {"passed", "failed"} and len(actual_result) < 4:
            raise ValueError(f"Record the actual result for {label}")
        if evidence_required and result in {"passed", "failed"} and len(evidence) < 3:
            raise ValueError(f"Record evidence for {label}")
        tests.append(
            {
                "key": key,
                "label": label,
                "expectedResult": expected,
                "required": required,
                "evidenceRequired": evidence_required,
                "result": result,
                "actualResult": actual_result,
                "evidence": evidence,
                "testedBy": actor.get("email", ""),
                "testedAt": timestamp if result in {"passed", "failed", "not_run"} else "",
            }
        )
    missing_required = [
        item["label"]
        for key, item in definitions.items()
        if item.get("required") and key not in seen
    ]
    if missing_required:
        raise ValueError(f"Required tests are missing: {', '.join(missing_required)}")

    deviations = str(payload.get("deviations") or "").strip()[:8000]
    unexpected_impact = str(payload.get("unexpectedImpact") or "").strip()[:8000]
    closure_summary = str(payload.get("closureSummary") or "").strip()[:8000]
    if len(closure_summary) < 4:
        raise ValueError("Enter a concise closure summary")
    if implementation_result == "successful" and any(
        test["required"] and test["result"] != "passed" for test in tests
    ):
        raise ValueError("A successful change requires every required test to pass")
    if implementation_result in {"successful", "successful_with_issues"} and (
        service_status != "restored"
    ):
        raise ValueError("A successful result requires service to be restored")
    if service_status != "restored" and len(unexpected_impact) < 4:
        raise ValueError("Describe the remaining degraded or unavailable service impact")

    raw_actions = payload.get("followUpActions") or []
    if not isinstance(raw_actions, list) or len(raw_actions) > 30:
        raise ValueError("A closure may contain up to 30 follow-up actions")
    follow_up_actions = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ValueError("Each follow-up action must be an object")
        description = str(raw.get("description") or "").strip()[:1000]
        if not description:
            continue
        status = str(raw.get("status") or "open").strip()
        if status not in CLOSURE_FOLLOW_UP_STATES:
            raise ValueError("Choose a valid follow-up action status")
        owner = str(raw.get("owner") or "").strip()[:240]
        due_date = str(raw.get("dueDate") or "").strip()[:10]
        if status == "open" and (not owner or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_date)):
            raise ValueError("Every open follow-up action needs an owner and due date")
        follow_up_actions.append(
            {
                "id": str(raw.get("id") or uuid.uuid4()),
                "description": description,
                "owner": owner,
                "dueDate": due_date,
                "status": status,
            }
        )

    assessment = {
        "implementationResult": implementation_result,
        "serviceStatus": service_status,
        "deviations": deviations,
        "unexpectedImpact": unexpected_impact,
        "tests": tests,
        "pirRequired": False,
        "pirCompleted": bool(payload.get("pirCompleted")),
        "lessonsLearned": str(payload.get("lessonsLearned") or "").strip()[:8000],
        "followUpActions": follow_up_actions,
        "stakeholderConfirmation": str(
            payload.get("stakeholderConfirmation") or "not_required"
        ).strip(),
        "closureSummary": closure_summary,
        "closedBy": actor.get("email", ""),
        "closedAt": timestamp,
    }
    assessment["pirRequired"] = _closure_requires_pir(change, assessment)
    if assessment["pirRequired"] and not assessment["pirCompleted"]:
        raise ValueError("Complete the required post-implementation review")
    if assessment["pirRequired"] and len(assessment["lessonsLearned"]) < 4:
        raise ValueError("Record lessons learned for the required post-implementation review")
    if assessment["stakeholderConfirmation"] not in CLOSURE_CONFIRMATION_STATES:
        raise ValueError("Choose a valid stakeholder confirmation state")
    business_systems = int((change.get("impactSummary") or {}).get("businessSystemCount") or 0)
    if (
        business_systems
        and str(change.get("riskLevel") or "") in {"high", "critical"}
        and assessment["stakeholderConfirmation"] != "confirmed"
    ):
        raise ValueError("Confirm stakeholder acceptance for this high-risk business impact")
    return assessment


def create_change_record(
    payload: dict,
    number: str,
    change_id: str,
    company: dict,
    actor: dict,
    assets: list[dict],
    relationships: list[dict],
) -> dict:
    """Create a revision-one change record with a frozen impact snapshot."""

    values = normalise_change_payload(payload)
    preview = preview_change_impact(
        company["id"],
        values["scopeAssetIds"],
        values["outageExpected"],
        assets,
        relationships,
    )
    suggested_risk = preview["summary"]["suggestedRisk"]
    timestamp = utc_now()
    assigned_user_id = values.get("assignedUserId")
    assigned_technician = values["assignedTechnician"] or actor.get("email", "")
    record = {
        "id": change_id,
        "number": number,
        "companyId": company["id"],
        "companyName": company["name"],
        "title": values["title"],
        "status": "draft",
        "changeType": values["changeType"],
        "category": values["category"],
        "priority": values["priority"],
        "riskLevel": values["riskLevel"] or suggested_risk["level"],
        "riskSource": "technician" if values["riskLevel"] else "cmdb_suggestion",
        "riskAssessment": suggested_risk,
        "outageExpected": values["outageExpected"],
        "plannedStart": values["plannedStart"],
        "plannedEnd": values["plannedEnd"],
        "reason": values["reason"],
        "businessImpact": values["businessImpact"],
        "implementationPlan": values["implementationPlan"],
        "validationPlan": values["validationPlan"],
        "rollbackPlan": values["rollbackPlan"],
        "communicationStatus": values["communicationStatus"],
        "communicationPlan": values["communicationPlan"],
        "assignedUserId": assigned_user_id,
        "assignedTechnician": assigned_technician,
        "approver": values["approver"],
        "notes": values["notes"],
        "scopeAssetIds": values["scopeAssetIds"],
        "impactSnapshot": preview["items"],
        "impactSummary": preview["summary"],
        "createdBy": {"id": actor.get("id"), "email": actor.get("email")},
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "revision": 1,
        "actualStart": "",
        "actualEnd": "",
        "actualOutageMinutes": 0,
        "outcome": "pending",
        "failureReason": "",
        "validationResult": "",
        "rollbackExecuted": False,
        "rollbackResult": "",
        "closureNotes": "",
        "closureAssessment": {},
        "approvals": [],
        "assignmentHistory": [
            {
                "id": str(uuid.uuid4()),
                "previousUserId": None,
                "previousDisplayName": "",
                "assignedUserId": assigned_user_id,
                "assignedDisplayName": assigned_technician,
                "reason": "Assigned when change was created",
                "actorId": actor.get("id"),
                "actorEmail": actor.get("email", ""),
                "createdAt": timestamp,
            }
        ]
        if assigned_technician
        else [],
        "templateId": values["templateId"],
        "templateVersion": values["templateVersion"],
        "templateSnapshot": values["templateSnapshot"],
        "templateParameters": values["templateParameters"],
        "statusHistory": [
            {
                "id": str(uuid.uuid4()),
                "fromStatus": None,
                "toStatus": "draft",
                "reason": "Change created",
                "actorId": actor.get("id"),
                "actorEmail": actor.get("email", ""),
                "createdAt": timestamp,
            }
        ],
        "externalReferences": [],
        "integrationState": {
            "connectwise": {
                "status": "not_published",
                "ticketId": None,
                "ticketUrl": None,
                "lastAttemptAt": None,
                "error": None,
            }
        },
    }
    record["closureAssessment"] = initial_closure_assessment(record)
    return record


def update_change_record(
    change: dict,
    payload: dict,
    actor: dict,
    assets: list[dict],
    relationships: list[dict],
) -> dict:
    """Create a new immutable revision while preserving the change identity."""
    current_status = str(change.get("status") or "draft")
    provided_fields = {
        key for key, value in payload.items() if value is not None and key != "expectedRevision"
    }
    if provided_fields & {"assignedTechnician", "assignedUserId"}:
        raise ValueError("Use the dedicated reassignment action to change the technician")
    if current_status in SCHEDULE_EDIT_STATUSES:
        unsupported = provided_fields - SCHEDULE_EDIT_FIELDS
        if unsupported:
            raise ValueError(
                "Approved changes only allow schedule, assignment, communication and notes updates"
            )
        updated = dict(change)
        for field in SCHEDULE_EDIT_FIELDS:
            if field in payload and payload[field] is not None:
                updated[field] = str(payload[field]).strip()[:8000]
        if (
            updated.get("plannedStart")
            and updated.get("plannedEnd")
            and updated["plannedEnd"] <= updated["plannedStart"]
        ):
            raise ValueError("Planned end must be after planned start")
    elif current_status in FULL_EDIT_STATUSES:
        merged = {
            **change,
            **{key: value for key, value in payload.items() if value is not None},
        }
        values = normalise_change_payload(merged)
        preview = preview_change_impact(
            change["companyId"],
            values["scopeAssetIds"],
            values["outageExpected"],
            assets,
            relationships,
        )
        updated = dict(change)
        for field in (
            "title",
            "changeType",
            "category",
            "priority",
            "outageExpected",
            "plannedStart",
            "plannedEnd",
            "reason",
            "businessImpact",
            "implementationPlan",
            "validationPlan",
            "rollbackPlan",
            "communicationStatus",
            "communicationPlan",
            "approver",
            "notes",
            "scopeAssetIds",
        ):
            updated[field] = values[field]
        suggested_risk = preview["summary"]["suggestedRisk"]
        updated["riskLevel"] = values["riskLevel"] or suggested_risk["level"]
        updated["riskSource"] = "technician" if values["riskLevel"] else "cmdb_suggestion"
        updated["riskAssessment"] = suggested_risk
        updated["impactSnapshot"] = preview["items"]
        updated["impactSummary"] = preview["summary"]
    else:
        raise ValueError(f"A change in {current_status.replace('_', ' ')} status cannot be edited")
    updated["revision"] = int(change.get("revision") or 1) + 1
    updated["updatedAt"] = utc_now()
    updated["lastUpdatedBy"] = {"id": actor.get("id"), "email": actor.get("email", "")}
    return updated


def reassign_change_record(
    change: dict,
    assignee: dict | None,
    reason: str,
    actor: dict,
) -> dict:
    """Create an auditable revision that changes only the technical assignee."""

    current_status = str(change.get("status") or "draft")
    if current_status not in ASSIGNABLE_CHANGE_STATUSES:
        raise ValueError(
            f"A change in {current_status.replace('_', ' ')} status cannot be reassigned"
        )
    normalized_reason = str(reason or "").strip()
    if len(normalized_reason) < 4:
        raise ValueError("Enter a reason for the reassignment")

    previous_user_id = change.get("assignedUserId") or None
    previous_display_name = str(change.get("assignedTechnician") or "")
    assigned_user_id = assignee.get("id") if assignee else None
    assigned_display_name = (
        str(assignee.get("displayName") or assignee.get("email") or "") if assignee else ""
    )
    if previous_user_id == assigned_user_id and (
        assigned_user_id is not None or not previous_display_name
    ):
        raise ValueError("Choose a different technician")

    timestamp = utc_now()
    updated = deepcopy(change)
    updated["assignedUserId"] = assigned_user_id
    updated["assignedTechnician"] = assigned_display_name
    history = list(updated.get("assignmentHistory") or [])
    history.append(
        {
            "id": str(uuid.uuid4()),
            "previousUserId": previous_user_id,
            "previousDisplayName": previous_display_name,
            "assignedUserId": assigned_user_id,
            "assignedDisplayName": assigned_display_name,
            "reason": normalized_reason,
            "actorId": actor.get("id"),
            "actorEmail": actor.get("email", ""),
            "createdAt": timestamp,
        }
    )
    updated["assignmentHistory"] = history
    updated["revision"] = int(change.get("revision") or 1) + 1
    updated["updatedAt"] = timestamp
    updated["lastUpdatedBy"] = {"id": actor.get("id"), "email": actor.get("email", "")}
    return updated


def transition_change_record(change: dict, target_status: str, payload: dict, actor: dict) -> dict:
    """Apply an explicit, auditable lifecycle transition."""
    current_status = str(change.get("status") or "draft")
    target = str(target_status or "").lower()
    if target not in CHANGE_STATUSES or target not in CHANGE_TRANSITIONS.get(current_status, set()):
        raise ValueError(
            f"Change cannot move from {current_status.replace('_', ' ')} to {target.replace('_', ' ')}"
        )
    reason = str(payload.get("reason") or "").strip()
    if target in REASON_REQUIRED_TRANSITIONS and len(reason) < 4:
        raise ValueError(f"Enter a reason for marking this change {target.replace('_', ' ')}")
    timestamp = utc_now()
    updated = deepcopy(change)
    updated["status"] = target
    updated["updatedAt"] = timestamp
    updated["revision"] = int(change.get("revision") or 1) + 1
    updated["lastUpdatedBy"] = {"id": actor.get("id"), "email": actor.get("email", "")}
    history = list(updated.get("statusHistory") or [])
    history.append(
        {
            "id": str(uuid.uuid4()),
            "fromStatus": current_status,
            "toStatus": target,
            "reason": reason,
            "actorId": actor.get("id"),
            "actorEmail": actor.get("email", ""),
            "createdAt": timestamp,
        }
    )
    updated["statusHistory"] = history
    if target in {"approved", "declined"}:
        approvals = list(updated.get("approvals") or [])
        approvals.append(
            {
                "id": str(uuid.uuid4()),
                "decision": target,
                "comments": reason,
                "actorId": actor.get("id"),
                "actorEmail": actor.get("email", ""),
                "createdAt": timestamp,
            }
        )
        updated["approvals"] = approvals
    if target == "implementing":
        updated["actualStart"] = str(payload.get("actualStart") or timestamp)
    if target in {"completed", "failed"}:
        updated["actualEnd"] = str(payload.get("actualEnd") or timestamp)
        try:
            updated["actualOutageMinutes"] = max(0, int(payload.get("actualOutageMinutes") or 0))
        except (TypeError, ValueError) as error:
            raise ValueError("Actual outage must be a whole number of minutes") from error
    if target == "completed":
        updated["outcome"] = "successful"
        updated["validationResult"] = str(payload.get("validationResult") or reason).strip()[:8000]
    elif target == "failed":
        updated["outcome"] = "failed"
        updated["failureReason"] = reason[:8000]
        updated["validationResult"] = str(payload.get("validationResult") or "").strip()[:8000]
    elif target == "backed_out":
        updated["outcome"] = "backed_out"
        updated["rollbackExecuted"] = True
        updated["rollbackResult"] = str(payload.get("rollbackResult") or reason).strip()[:8000]
    elif target == "cancelled":
        updated["outcome"] = "cancelled"
    elif target == "post_implementation_review":
        if not updated.get("closureAssessment"):
            updated["closureAssessment"] = initial_closure_assessment(updated)
    elif target == "closed":
        assessment = normalize_closure_assessment(
            updated,
            payload.get("closureAssessment"),
            actor,
        )
        updated["closureAssessment"] = assessment
        updated["outcome"] = assessment["implementationResult"]
        updated["closureNotes"] = assessment["closureSummary"]
    return updated


def _safe(value: object) -> str:
    text = str(value or "").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    return escape(text)


def _label(value: object) -> str:
    return str(value or "Not provided").replace("_", " ").title()


def change_pdf_filename(change: dict) -> str:
    """Return a filesystem-safe filename for a change-control PDF."""

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", change.get("title", "change")).strip("-")[:50] or "change"
    return f"{change.get('number', 'CHG')}-{slug}.pdf"


def render_change_pdf(change: dict, company: dict, branding: dict) -> bytes:
    """Render a stable, printable change-control package."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Image,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = BytesIO()
    accent_value = branding.get("accent") or "#4cc7b1"
    try:
        accent = colors.HexColor(accent_value)
    except ValueError:
        accent = colors.HexColor("#4cc7b1")
    navy = colors.HexColor("#0b1526")
    ink = colors.HexColor("#172033")
    muted = colors.HexColor("#5e6b80")
    pale = colors.HexColor("#edf4f6")
    line = colors.HexColor("#d8e1e8")
    brand_name = branding.get("name") or "CMDB Hub"
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="DocTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=colors.white,
            alignment=TA_LEFT,
            spaceAfter=3 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="DocSub",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#d7e5ea"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=navy,
            spaceBefore=5 * mm,
            spaceAfter=2.5 * mm,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BodySmall",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=8.2,
            leading=11,
            textColor=ink,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Cell",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=7.4,
            leading=9.2,
            textColor=ink,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CellHead",
            parent=styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=7.4,
            leading=9.2,
            textColor=colors.white,
            alignment=TA_LEFT,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Plan",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=ink,
            spaceAfter=2 * mm,
        )
    )

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=f"{change['number']} - {change['title']}",
        author=brand_name,
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.line(16 * mm, 11 * mm, A4[0] - 16 * mm, 11 * mm)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 7)
        footer_text = branding.get("reportFooter") or f"{brand_name} | Controlled change record"
        canvas.drawString(16 * mm, 7 * mm, f"{footer_text} | {change['number']}")
        confidentiality = branding.get("confidentialityLabel") or ""
        if confidentiality:
            canvas.drawCentredString(A4[0] / 2, 4 * mm, confidentiality)
        canvas.drawRightString(A4[0] - 16 * mm, 7 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story = []
    logo_flowable = None
    logo_data_url = branding.get("logoDataUrl") or ""
    if logo_data_url.startswith("data:image/") and ";base64," in logo_data_url:
        try:
            logo_bytes = base64.b64decode(logo_data_url.split(",", 1)[1], validate=True)
            logo_flowable = Image(BytesIO(logo_bytes))
            scale = min(
                (32 * mm) / logo_flowable.drawWidth,
                (14 * mm) / logo_flowable.drawHeight,
                1,
            )
            logo_flowable.drawWidth *= scale
            logo_flowable.drawHeight *= scale
        except Exception:
            logo_flowable = None
    brand_mark = logo_flowable or Paragraph(
        f"<b>{_safe(branding.get('logoText') or brand_name[:2].upper())}</b>",
        styles["DocSub"],
    )
    brand_identity = Table(
        [[brand_mark, Paragraph(_safe(brand_name), styles["DocSub"])]],
        colWidths=[36 * mm, 89 * mm],
    )
    brand_identity.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    title_block = Table(
        [[brand_identity], [Paragraph(_safe(change["title"]), styles["DocTitle"])]],
        colWidths=[125 * mm],
    )
    title_block.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    header = Table(
        [
            [
                title_block,
                Paragraph(
                    f"<b>{_safe(change['number'])}</b><br/>{_safe(company['name'])}",
                    styles["DocSub"],
                ),
            ]
        ],
        colWidths=[125 * mm, 53 * mm],
    )
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), navy),
                ("BOX", (0, 0), (-1, -1), 0, navy),
                ("LEFTPADDING", (0, 0), (-1, -1), 7 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 7 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7 * mm),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.extend([header, Spacer(1, 4 * mm)])

    summary_data = [
        [
            "Change type",
            _label(change.get("changeType")),
            "Status",
            _label(change.get("status")),
        ],
        [
            "Category",
            _label(change.get("category")),
            "Priority",
            _label(change.get("priority")),
        ],
        [
            "Risk",
            f"{_label(change.get('riskLevel'))} ({change.get('riskAssessment', {}).get('score', 0)})",
            "Expected outage",
            "Yes" if change.get("outageExpected") else "No",
        ],
        [
            "Planned start",
            change.get("plannedStart") or "Not scheduled",
            "Planned end",
            change.get("plannedEnd") or "Not scheduled",
        ],
        [
            "Assigned technician",
            change.get("assignedTechnician") or "Not assigned",
            "Approver",
            change.get("approver") or "Not assigned",
        ],
        [
            "Customer communication",
            _label(change.get("communicationStatus")),
            "Revision",
            str(change.get("revision", 1)),
        ],
        [
            "Actual start",
            change.get("actualStart") or "Not started",
            "Actual end",
            change.get("actualEnd") or "Not completed",
        ],
        [
            "Outcome",
            _label(change.get("outcome") or "pending"),
            "Actual outage",
            f"{int(change.get('actualOutageMinutes') or 0)} minute(s)",
        ],
    ]
    summary_table = Table(
        [
            [
                Paragraph(f"<b>{_safe(a)}</b>", styles["Cell"]),
                Paragraph(_safe(b), styles["Cell"]),
                Paragraph(f"<b>{_safe(c)}</b>", styles["Cell"]),
                Paragraph(_safe(d), styles["Cell"]),
            ]
            for a, b, c, d in summary_data
        ],
        colWidths=[32 * mm, 57 * mm, 32 * mm, 57 * mm],
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, line),
                ("BACKGROUND", (0, 0), (0, -1), pale),
                ("BACKGROUND", (2, 0), (2, -1), pale),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
            ]
        )
    )
    story.extend([Paragraph("Change summary", styles["Section"]), summary_table])

    story.append(Paragraph("Purpose and business impact", styles["Section"]))
    story.append(Paragraph(f"<b>Reason:</b> {_safe(change.get('reason'))}", styles["Plan"]))
    story.append(
        Paragraph(
            f"<b>Business impact:</b> {_safe(change.get('businessImpact') or 'Not provided')}",
            styles["Plan"],
        )
    )

    impact = change.get("impactSnapshot") or []
    impact_summary_value = change.get("impactSummary") or {}
    business_systems = impact_summary_value.get("businessSystems") or []
    if business_systems:
        story.append(Paragraph("Business systems and signoff", styles["Section"]))
        business_rows = [
            [
                Paragraph(value, styles["CellHead"])
                for value in (
                    "Business system",
                    "Impact",
                    "Business owner",
                    "Signoff",
                    "Recovery objective",
                )
            ]
        ]
        for item in business_systems:
            signoff = (
                item.get("signoffDelegate") or item.get("businessOwner") or "Owner not recorded"
            )
            if item.get("signoffRequired") == "no":
                signoff = "Not required"
            business_rows.append(
                [
                    Paragraph(
                        f"<b>{_safe(item.get('name'))}</b><br/>{_safe(item.get('department') or 'Department not recorded')}",
                        styles["Cell"],
                    ),
                    Paragraph(
                        f"{_safe(_label(item.get('impactSeverity')))}<br/>{_safe(_label(item.get('criticality')))} criticality",
                        styles["Cell"],
                    ),
                    Paragraph(
                        _safe(item.get("businessOwner") or "Not recorded"),
                        styles["Cell"],
                    ),
                    Paragraph(_safe(signoff), styles["Cell"]),
                    Paragraph(
                        f"RTO {_safe(item.get('rtoHours') or '?')}h<br/>RPO {_safe(item.get('rpoHours') or '?')}h",
                        styles["Cell"],
                    ),
                ]
            )
        business_table = Table(
            business_rows,
            repeatRows=1,
            colWidths=[43 * mm, 31 * mm, 39 * mm, 39 * mm, 26 * mm],
        )
        business_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), navy),
                    ("GRID", (0, 0), (-1, -1), 0.35, line),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ]
            )
        )
        story.extend([business_table, Spacer(1, 2 * mm)])
    virtualization = impact_summary_value.get("virtualizationAssessments") or []
    if virtualization:
        story.append(Paragraph("Virtualization resilience", styles["Section"]))
        virtualization_rows = [
            [
                Paragraph(value, styles["CellHead"])
                for value in (
                    "Virtual machine",
                    "Impact",
                    "Platform / cluster",
                    "HA decision",
                )
            ]
        ]
        for item in virtualization:
            virtualization_rows.append(
                [
                    Paragraph(
                        f"<b>{_safe(item.get('name'))}</b><br/>{_safe(_label(item.get('powerState')))}",
                        styles["Cell"],
                    ),
                    Paragraph(_safe(_label(item.get("impactSeverity"))), styles["Cell"]),
                    Paragraph(
                        f"{_safe(item.get('virtualizationPlatform') or 'Not recorded')}<br/>{_safe(item.get('clusterName') or 'No cluster recorded')}",
                        styles["Cell"],
                    ),
                    Paragraph(
                        _safe(
                            item.get("virtualizationDecision")
                            or "No HA decision was required for this path."
                        ),
                        styles["Cell"],
                    ),
                ]
            )
        virtualization_table = Table(
            virtualization_rows,
            repeatRows=1,
            colWidths=[38 * mm, 24 * mm, 42 * mm, 74 * mm],
        )
        virtualization_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), navy),
                    ("GRID", (0, 0), (-1, -1), 0.35, line),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ]
            )
        )
        story.extend([virtualization_table, Spacer(1, 2 * mm)])
    story.append(Paragraph("CMDB impact snapshot", styles["Section"]))
    metrics = Table(
        [
            [
                Paragraph(
                    f"<b>{impact_summary_value.get('scopeCount', 0)}</b><br/>Scope",
                    styles["BodySmall"],
                ),
                Paragraph(
                    f"<b>{impact_summary_value.get('directCount', 0)}</b><br/>Direct",
                    styles["BodySmall"],
                ),
                Paragraph(
                    f"<b>{impact_summary_value.get('downstreamCount', 0)}</b><br/>Downstream",
                    styles["BodySmall"],
                ),
                Paragraph(
                    f"<b>{impact_summary_value.get('missingOwnerCount', 0)}</b><br/>Missing owner",
                    styles["BodySmall"],
                ),
            ],
        ],
        colWidths=[44.5 * mm] * 4,
    )
    metrics.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), pale),
                ("BOX", (0, 0), (-1, -1), 0.6, accent),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, line),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend([metrics, Spacer(1, 3 * mm)])

    impact_rows = [
        [
            Paragraph(value, styles["CellHead"])
            for value in (
                "Impact",
                "Configuration item",
                "Type / environment",
                "Criticality",
                "Owner / site",
            )
        ]
    ]
    for item in impact:
        impact_rows.append(
            [
                Paragraph(
                    f"{_safe(item.get('role'))}<br/>{_safe(_label(item.get('impactSeverity')))}",
                    styles["Cell"],
                ),
                Paragraph(
                    f"<b>{_safe(item.get('name'))}</b><br/>Depth {item.get('depth', 0)}",
                    styles["Cell"],
                ),
                Paragraph(
                    f"{_safe(item.get('type'))}<br/>{_safe(_label(item.get('environment')))}",
                    styles["Cell"],
                ),
                Paragraph(_safe(_label(item.get("criticality"))), styles["Cell"]),
                Paragraph(
                    f"{_safe(item.get('owner'))}<br/>{_safe(item.get('site') or 'No site recorded')}",
                    styles["Cell"],
                ),
            ]
        )
    impact_table = Table(
        impact_rows,
        repeatRows=1,
        colWidths=[25 * mm, 45 * mm, 38 * mm, 25 * mm, 45 * mm],
    )
    impact_style = [
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("GRID", (0, 0), (-1, -1), 0.35, line),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]
    for row_number, item in enumerate(impact, start=1):
        if item.get("role") == "Scope":
            impact_style.append(
                (
                    "BACKGROUND",
                    (0, row_number),
                    (-1, row_number),
                    colors.HexColor("#e7f5f2"),
                )
            )
        elif item.get("criticality") == "critical":
            impact_style.append(
                (
                    "BACKGROUND",
                    (0, row_number),
                    (-1, row_number),
                    colors.HexColor("#fff0ee"),
                )
            )
    impact_table.setStyle(TableStyle(impact_style))  # type: ignore[arg-type]  # ReportLab accepts dynamic style tuples.
    story.append(impact_table)

    story.append(PageBreak())
    story.append(Paragraph("Risk assessment", styles["Section"]))
    risk_factors = change.get("riskAssessment", {}).get("factors") or []
    risk_table = Table(
        [
            [
                Paragraph(
                    f"<b>Selected risk:</b> {_safe(_label(change.get('riskLevel')))}",
                    styles["Plan"],
                )
            ],
            [
                Paragraph(
                    "<b>CMDB-derived factors</b><br/>"
                    + "<br/>".join(f"- {_safe(item)}" for item in risk_factors),
                    styles["Plan"],
                )
            ],
        ],
        colWidths=[178 * mm],
    )
    risk_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, accent),
                ("BACKGROUND", (0, 0), (-1, 0), pale),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.append(risk_table)

    plan_sections = [
        ("Implementation plan", change.get("implementationPlan")),
        ("Validation and success criteria", change.get("validationPlan")),
        ("Rollback plan", change.get("rollbackPlan")),
        (
            "Communication plan",
            change.get("communicationPlan") or "No additional communication steps recorded.",
        ),
        ("Additional notes", change.get("notes") or "No additional notes recorded."),
    ]
    for heading, text in plan_sections:
        story.append(
            KeepTogether(
                [
                    Paragraph(heading, styles["Section"]),
                    Paragraph(_safe(text).replace("\n", "<br/>"), styles["Plan"]),
                ]
            )
        )

    if (
        change.get("outcome") not in {None, "", "pending"}
        or change.get("validationResult")
        or change.get("failureReason")
    ):
        story.append(Paragraph("Implementation outcome", styles["Section"]))
        outcome_rows = [
            ["Outcome", _label(change.get("outcome") or "pending")],
            ["Validation result", change.get("validationResult") or "Not recorded"],
            ["Failure reason", change.get("failureReason") or "Not applicable"],
            [
                "Rollback",
                change.get("rollbackResult")
                or ("Executed" if change.get("rollbackExecuted") else "Not executed"),
            ],
            ["Closure notes", change.get("closureNotes") or "Not yet closed"],
        ]
        outcome_table = Table(
            [
                [
                    Paragraph(f"<b>{_safe(label)}</b>", styles["Cell"]),
                    Paragraph(_safe(value), styles["Cell"]),
                ]
                for label, value in outcome_rows
            ],
            colWidths=[48 * mm, 130 * mm],
        )
        outcome_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, line),
                    ("BACKGROUND", (0, 0), (0, -1), pale),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
                ]
            )
        )
        story.append(outcome_table)

    closure = change.get("closureAssessment") or {}
    closure_tests = [item for item in closure.get("tests") or [] if isinstance(item, dict)]
    if closure_tests:
        story.append(Paragraph("Post-change review and closure", styles["Section"]))
        closure_rows = [
            ["Implementation result", _label(closure.get("implementationResult"))],
            ["Service status", _label(closure.get("serviceStatus"))],
            [
                "PIR",
                (
                    "Completed"
                    if closure.get("pirCompleted")
                    else "Required"
                    if closure.get("pirRequired")
                    else "Not required"
                ),
            ],
            [
                "Stakeholder acceptance",
                _label(closure.get("stakeholderConfirmation") or "not_required"),
            ],
            ["Deviations", closure.get("deviations") or "None recorded"],
            ["Unexpected impact", closure.get("unexpectedImpact") or "None recorded"],
            ["Lessons learned", closure.get("lessonsLearned") or "None recorded"],
            ["Closure summary", closure.get("closureSummary") or "Not yet closed"],
            ["Closed by", closure.get("closedBy") or "Not yet closed"],
            ["Closed at", closure.get("closedAt") or "Not yet closed"],
        ]
        closure_table = Table(
            [
                [
                    Paragraph(f"<b>{_safe(label)}</b>", styles["Cell"]),
                    Paragraph(_safe(value).replace("\n", "<br/>"), styles["Cell"]),
                ]
                for label, value in closure_rows
            ],
            colWidths=[48 * mm, 130 * mm],
        )
        closure_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, line),
                    ("BACKGROUND", (0, 0), (0, -1), pale),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
                ]
            )
        )
        story.append(closure_table)
        story.append(Paragraph("Validation evidence", styles["Section"]))
        validation_rows: list[list[object]] = [
            [
                Paragraph("<b>Test</b>", styles["Cell"]),
                Paragraph("<b>Result</b>", styles["Cell"]),
                Paragraph("<b>Actual result and evidence</b>", styles["Cell"]),
            ]
        ]
        for test in closure_tests:
            details = test.get("actualResult") or "Not recorded"
            if test.get("evidence"):
                details = f"{details}\nEvidence: {test['evidence']}"
            if test.get("testedBy"):
                details = f"{details}\nTested by {test['testedBy']}" + (
                    f" at {test['testedAt']}" if test.get("testedAt") else ""
                )
            validation_rows.append(
                [
                    Paragraph(
                        f"<b>{_safe(test.get('label'))}</b><br/>"
                        f"<font color='#5e6b80'>{_safe(test.get('expectedResult'))}</font>",
                        styles["Cell"],
                    ),
                    Paragraph(_safe(_label(test.get("result"))), styles["Cell"]),
                    Paragraph(_safe(details).replace("\n", "<br/>"), styles["Cell"]),
                ]
            )
        validation_table = Table(
            validation_rows,
            colWidths=[62 * mm, 28 * mm, 88 * mm],
            repeatRows=1,
        )
        validation_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, line),
                    ("BACKGROUND", (0, 0), (-1, 0), navy),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2.3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2.3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ]
            )
        )
        story.append(validation_table)
        follow_ups = [
            item
            for item in closure.get("followUpActions") or []
            if isinstance(item, dict) and item.get("description")
        ]
        if follow_ups:
            story.append(Paragraph("Follow-up actions", styles["Section"]))
            follow_up_rows: list[list[object]] = [
                ["Action", "Owner", "Due", "Status"],
                *[
                    [
                        item.get("description"),
                        item.get("owner") or "Not assigned",
                        item.get("dueDate") or "Not set",
                        _label(item.get("status")),
                    ]
                    for item in follow_ups
                ],
            ]
            follow_up_table = Table(
                [
                    [Paragraph(_safe(value), styles["Cell"]) for value in row]
                    for row in follow_up_rows
                ],
                colWidths=[88 * mm, 44 * mm, 24 * mm, 22 * mm],
                repeatRows=1,
            )
            follow_up_table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, line),
                        ("BACKGROUND", (0, 0), (-1, 0), pale),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(follow_up_table)

    story.append(Paragraph("Record and integration details", styles["Section"]))
    cw_state = change.get("integrationState", {}).get("connectwise", {})
    record_data = [
        ["Created by", change.get("createdBy", {}).get("email") or "Unknown"],
        ["Created at", change.get("createdAt") or "Unknown"],
        ["Impact snapshot", f"{len(impact)} configuration item(s), frozen at creation"],
        ["ConnectWise status", _label(cw_state.get("status") or "not_published")],
        ["ConnectWise ticket", cw_state.get("ticketId") or "Not created"],
    ]
    record_table = Table(
        [
            [
                Paragraph(f"<b>{_safe(label)}</b>", styles["Cell"]),
                Paragraph(_safe(value), styles["Cell"]),
            ]
            for label, value in record_data
        ],
        colWidths=[48 * mm, 130 * mm],
    )
    record_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, line),
                ("BACKGROUND", (0, 0), (0, -1), pale),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm),
            ]
        )
    )
    story.append(record_table)

    story.extend([Spacer(1, 8 * mm), Paragraph("Approval", styles["Section"])])
    approval_rows: list[list[object]] = [["Approver", "Comments / signature", "Decision", "Date"]]
    recorded_approvals = change.get("approvals") or []
    if recorded_approvals:
        for decision in recorded_approvals:
            approver_identity = decision.get("approverName") or decision.get("actorEmail")
            if decision.get("approverName") and decision.get("actorEmail"):
                approver_identity = f"{decision['approverName']}\n{decision['actorEmail']}"
            role = _label(decision.get("responsibilityRole"))
            if decision.get("responsibilityRole"):
                approver_identity = f"{approver_identity}\n{role}"
            scope_names = ", ".join(
                str(item.get("name") or "")
                for item in decision.get("scope") or []
                if item.get("name")
            )
            comments = str(decision.get("comments") or "")
            if scope_names:
                comments = f"{comments}\nScope: {scope_names}".strip()
            approval_rows.append(
                [
                    Paragraph(_safe(approver_identity or "Recorded approver"), styles["Cell"]),
                    Paragraph(_safe(comments), styles["Cell"]),
                    Paragraph(_safe(_label(decision.get("decision"))), styles["Cell"]),
                    Paragraph(_safe(decision.get("createdAt") or ""), styles["Cell"]),
                ]
            )
    else:
        approval_rows.append(
            [change.get("approver") or "Change approver", "", "Approved / Declined", ""]
        )
    recorded_approvers = {str(change.get("approver") or "").strip().lower()}
    for item in business_systems:
        if item.get("signoffRequired") == "no":
            continue
        approver = str(
            item.get("signoffDelegate")
            or item.get("businessOwner")
            or "Business owner not recorded"
        ).strip()
        if approver.lower() in recorded_approvers:
            continue
        recorded_approvers.add(approver.lower())
        approval_rows.append([approver, "", f"{item.get('name')} signoff", ""])
    approval = Table(
        approval_rows,
        colWidths=[50 * mm, 48 * mm, 48 * mm, 32 * mm],
        rowHeights=[8 * mm] + [16 * mm] * (len(approval_rows) - 1),
    )
    approval.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), navy),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, line),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.append(approval)

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
