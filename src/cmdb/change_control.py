"""Change-control impact snapshots and PDF rendering.

Change packages use canonical CI identifiers, immutable PostgreSQL revisions,
and a provider-neutral external-reference envelope so a future ConnectWise
publisher does not need to change the frontend contract.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from html import escape
from io import BytesIO
import base64
import re


CHANGE_TYPES = {"standard", "normal", "emergency"}
CHANGE_CATEGORIES = {"infrastructure", "network", "software", "database", "security", "cloud", "other"}
CHANGE_PRIORITIES = {"low", "medium", "high", "critical"}
RISK_LEVELS = {"low", "medium", "high", "critical"}
COMMUNICATION_STATES = {"required", "not_required", "completed"}
NON_PROPAGATING_RELATIONSHIPS = {"related_to"}
REVERSED_IMPACT_RELATIONSHIPS = {"depends_on", "installed_on"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _impact_edge(relationship: dict) -> tuple[str, str] | None:
    """Return the supporting-to-affected direction used by outage analysis."""
    relationship_type = relationship.get("type", "related_to")
    if relationship_type in NON_PROPAGATING_RELATIONSHIPS:
        return None
    if relationship_type in REVERSED_IMPACT_RELATIONSHIPS:
        return relationship.get("toId", ""), relationship.get("fromId", "")
    return relationship.get("fromId", ""), relationship.get("toId", "")


def _asset_snapshot(asset: dict, role: str, depth: int, path: list[str], relationship_path: list[str]) -> dict:
    metadata = asset.get("metadata") or {}
    service_owner = str(metadata.get("serviceOwner") or "").strip()
    technical_owner = str(metadata.get("technicalOwner") or "").strip()
    custodian = str(metadata.get("custodian") or "").strip()
    return {
        "assetId": asset["id"],
        "name": asset.get("name", asset["id"]),
        "type": asset.get("type", "Configuration item"),
        "role": role,
        "depth": depth,
        "pathAssetIds": path,
        "relationshipPath": relationship_path,
        "criticality": metadata.get("criticality", "medium"),
        "environment": metadata.get("environment", "production"),
        "site": metadata.get("site", ""),
        "lifecycle": metadata.get("lifecycle", "in_service"),
        "operationalStatus": metadata.get("operationalStatus", "unknown"),
        "serviceOwner": service_owner,
        "technicalOwner": technical_owner,
        "custodian": custodian,
        "owner": technical_owner or service_owner or custodian or "No owner recorded",
        "source": asset.get("source", "manual"),
        "externalId": asset.get("externalId"),
    }


def build_impact_snapshot(company_id: str, scope_asset_ids: list[str], assets: list[dict], relationships: list[dict]) -> list[dict]:
    """Freeze selected CIs and their downstream outage-impact paths."""
    company_assets = {asset["id"]: asset for asset in assets if asset.get("companyId") == company_id}
    unique_scope = list(dict.fromkeys(scope_asset_ids))
    if not unique_scope:
        raise ValueError("Choose at least one configuration item")
    missing = [asset_id for asset_id in unique_scope if asset_id not in company_assets]
    if missing:
        raise ValueError("One or more selected configuration items are unavailable in this customer")

    adjacency: dict[str, list[tuple[str, str]]] = {}
    for relationship in relationships:
        edge = _impact_edge(relationship)
        if not edge or edge[0] not in company_assets or edge[1] not in company_assets:
            continue
        adjacency.setdefault(edge[0], []).append((edge[1], relationship.get("type", "related_to")))

    paths: dict[str, tuple[int, list[str], list[str]]] = {}
    queue: deque[tuple[str, int, list[str], list[str]]] = deque()
    for asset_id in unique_scope:
        paths[asset_id] = (0, [asset_id], [])
        queue.append((asset_id, 0, [asset_id], []))

    while queue:
        current, depth, path, relationship_path = queue.popleft()
        for target, relationship_type in adjacency.get(current, []):
            if target in paths:
                continue
            next_path = [*path, target]
            next_relationships = [*relationship_path, relationship_type]
            paths[target] = (depth + 1, next_path, next_relationships)
            queue.append((target, depth + 1, next_path, next_relationships))

    snapshots = []
    scope_set = set(unique_scope)
    for asset_id, (depth, path, relationship_path) in paths.items():
        role = "Scope" if asset_id in scope_set else "Direct impact" if depth == 1 else "Downstream impact"
        snapshots.append(_asset_snapshot(company_assets[asset_id], role, depth, path, relationship_path))
    return sorted(snapshots, key=lambda item: (item["depth"], item["name"].lower()))


def calculate_risk(snapshot: list[dict], outage_expected: bool) -> dict:
    score = 0
    factors: list[str] = []
    criticalities = {item.get("criticality") for item in snapshot}
    if "critical" in criticalities:
        score += 4
        factors.append("Critical CI in scope or impact path")
    elif "high" in criticalities:
        score += 2
        factors.append("High-criticality CI in scope or impact path")
    downstream_count = sum(item.get("role") != "Scope" for item in snapshot)
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
    missing_owners = sum(item.get("owner") == "No owner recorded" for item in snapshot)
    if missing_owners:
        score += 2
        factors.append(f"{missing_owners} impacted item(s) have no recorded owner")
    level = "critical" if score >= 10 else "high" if score >= 7 else "medium" if score >= 4 else "low"
    return {"score": score, "level": level, "factors": factors or ["No elevated CMDB risk factors detected"]}


def impact_summary(snapshot: list[dict], risk: dict) -> dict:
    owners = sorted({item["owner"] for item in snapshot if item["owner"] != "No owner recorded"})
    return {
        "scopeCount": sum(item["role"] == "Scope" for item in snapshot),
        "directCount": sum(item["role"] == "Direct impact" for item in snapshot),
        "downstreamCount": sum(item["role"] == "Downstream impact" for item in snapshot),
        "criticalCount": sum(item["criticality"] == "critical" for item in snapshot),
        "missingOwnerCount": sum(item["owner"] == "No owner recorded" for item in snapshot),
        "owners": owners,
        "suggestedRisk": risk,
    }


def preview_change_impact(company_id: str, scope_asset_ids: list[str], outage_expected: bool, assets: list[dict], relationships: list[dict]) -> dict:
    snapshot = build_impact_snapshot(company_id, scope_asset_ids, assets, relationships)
    risk = calculate_risk(snapshot, outage_expected)
    return {"items": snapshot, "summary": impact_summary(snapshot, risk)}


def normalise_change_payload(payload: dict) -> dict:
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
    for field in ("plannedStart", "plannedEnd", "businessImpact", "communicationPlan", "assignedTechnician", "approver", "notes"):
        result[field] = str(result.get(field) or "").strip()[:8000]
    if result["plannedStart"] and result["plannedEnd"] and result["plannedEnd"] <= result["plannedStart"]:
        raise ValueError("Planned end must be after planned start")
    return result


def create_change_record(payload: dict, number: str, change_id: str, company: dict, actor: dict, assets: list[dict], relationships: list[dict]) -> dict:
    values = normalise_change_payload(payload)
    preview = preview_change_impact(company["id"], values["scopeAssetIds"], values["outageExpected"], assets, relationships)
    suggested_risk = preview["summary"]["suggestedRisk"]
    timestamp = utc_now()
    return {
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
        "assignedTechnician": values["assignedTechnician"] or actor.get("email", ""),
        "approver": values["approver"],
        "notes": values["notes"],
        "scopeAssetIds": values["scopeAssetIds"],
        "impactSnapshot": preview["items"],
        "impactSummary": preview["summary"],
        "createdBy": {"id": actor.get("id"), "email": actor.get("email")},
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "revision": 1,
        "externalReferences": [],
        "integrationState": {
            "connectwise": {"status": "not_published", "ticketId": None, "ticketUrl": None, "lastAttemptAt": None, "error": None}
        },
    }


def _safe(value: object) -> str:
    text = str(value or "").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    return escape(text)


def _label(value: object) -> str:
    return str(value or "Not provided").replace("_", " ").title()


def change_pdf_filename(change: dict) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", change.get("title", "change")).strip("-")[:50] or "change"
    return f"{change.get('number', 'CHG')}-{slug}.pdf"


def render_change_pdf(change: dict, company: dict, branding: dict) -> bytes:
    """Render a stable, printable change-control package."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
    styles.add(ParagraphStyle(name="DocTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.white, alignment=TA_LEFT, spaceAfter=3 * mm))
    styles.add(ParagraphStyle(name="DocSub", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#d7e5ea")))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=navy, spaceBefore=5 * mm, spaceAfter=2.5 * mm))
    styles.add(ParagraphStyle(name="BodySmall", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.2, leading=11, textColor=ink))
    styles.add(ParagraphStyle(name="Cell", parent=styles["BodyText"], fontName="Helvetica", fontSize=7.4, leading=9.2, textColor=ink))
    styles.add(ParagraphStyle(name="CellHead", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=7.4, leading=9.2, textColor=colors.white, alignment=TA_LEFT))
    styles.add(ParagraphStyle(name="Plan", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=13, textColor=ink, spaceAfter=2 * mm))

    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=15 * mm, bottomMargin=18 * mm, title=f"{change['number']} - {change['title']}", author=brand_name)

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
            logo_flowable._restrictSize(32 * mm, 14 * mm)
        except Exception:
            logo_flowable = None
    brand_mark = logo_flowable or Paragraph(
        f"<b>{_safe(branding.get('logoText') or brand_name[:2].upper())}</b>",
        styles["DocSub"],
    )
    brand_identity = Table([[brand_mark, Paragraph(_safe(brand_name), styles["DocSub"])]], colWidths=[36 * mm, 89 * mm])
    brand_identity.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm)]))
    title_block = Table([[brand_identity], [Paragraph(_safe(change["title"]), styles["DocTitle"])]], colWidths=[125 * mm])
    title_block.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    header = Table([[
        title_block,
        Paragraph(f"<b>{_safe(change['number'])}</b><br/>{_safe(company['name'])}", styles["DocSub"]),
    ]], colWidths=[125 * mm, 53 * mm])
    header.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), navy), ("BOX", (0, 0), (-1, -1), 0, navy), ("LEFTPADDING", (0, 0), (-1, -1), 7 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 6 * mm), ("TOPPADDING", (0, 0), (-1, -1), 7 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 7 * mm), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.extend([header, Spacer(1, 4 * mm)])

    summary_data = [
        ["Change type", _label(change.get("changeType")), "Status", _label(change.get("status"))],
        ["Category", _label(change.get("category")), "Priority", _label(change.get("priority"))],
        ["Risk", f"{_label(change.get('riskLevel'))} ({change.get('riskAssessment', {}).get('score', 0)})", "Expected outage", "Yes" if change.get("outageExpected") else "No"],
        ["Planned start", change.get("plannedStart") or "Not scheduled", "Planned end", change.get("plannedEnd") or "Not scheduled"],
        ["Assigned technician", change.get("assignedTechnician") or "Not assigned", "Approver", change.get("approver") or "Not assigned"],
        ["Customer communication", _label(change.get("communicationStatus")), "Revision", str(change.get("revision", 1))],
    ]
    summary_table = Table([[Paragraph(f"<b>{_safe(a)}</b>", styles["Cell"]), Paragraph(_safe(b), styles["Cell"]), Paragraph(f"<b>{_safe(c)}</b>", styles["Cell"]), Paragraph(_safe(d), styles["Cell"])] for a, b, c, d in summary_data], colWidths=[32 * mm, 57 * mm, 32 * mm, 57 * mm])
    summary_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, line), ("BACKGROUND", (0, 0), (0, -1), pale), ("BACKGROUND", (2, 0), (2, -1), pale), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm), ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm)]))
    story.extend([Paragraph("Change summary", styles["Section"]), summary_table])

    story.append(Paragraph("Purpose and business impact", styles["Section"]))
    story.append(Paragraph(f"<b>Reason:</b> {_safe(change.get('reason'))}", styles["Plan"]))
    story.append(Paragraph(f"<b>Business impact:</b> {_safe(change.get('businessImpact') or 'Not provided')}", styles["Plan"]))

    impact = change.get("impactSnapshot") or []
    impact_summary_value = change.get("impactSummary") or {}
    story.append(Paragraph("CMDB impact snapshot", styles["Section"]))
    metrics = Table([
        [Paragraph(f"<b>{impact_summary_value.get('scopeCount', 0)}</b><br/>Scope", styles["BodySmall"]), Paragraph(f"<b>{impact_summary_value.get('directCount', 0)}</b><br/>Direct", styles["BodySmall"]), Paragraph(f"<b>{impact_summary_value.get('downstreamCount', 0)}</b><br/>Downstream", styles["BodySmall"]), Paragraph(f"<b>{impact_summary_value.get('missingOwnerCount', 0)}</b><br/>Missing owner", styles["BodySmall"])],
    ], colWidths=[44.5 * mm] * 4)
    metrics.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), pale), ("BOX", (0, 0), (-1, -1), 0.6, accent), ("INNERGRID", (0, 0), (-1, -1), 0.4, line), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm)]))
    story.extend([metrics, Spacer(1, 3 * mm)])

    impact_rows = [[Paragraph(value, styles["CellHead"]) for value in ("Impact", "Configuration item", "Type / environment", "Criticality", "Owner / site")]]
    for item in impact:
        impact_rows.append([
            Paragraph(_safe(item.get("role")), styles["Cell"]),
            Paragraph(f"<b>{_safe(item.get('name'))}</b><br/>Depth {item.get('depth', 0)}", styles["Cell"]),
            Paragraph(f"{_safe(item.get('type'))}<br/>{_safe(_label(item.get('environment')))}", styles["Cell"]),
            Paragraph(_safe(_label(item.get("criticality"))), styles["Cell"]),
            Paragraph(f"{_safe(item.get('owner'))}<br/>{_safe(item.get('site') or 'No site recorded')}", styles["Cell"]),
        ])
    impact_table = Table(impact_rows, repeatRows=1, colWidths=[25 * mm, 45 * mm, 38 * mm, 25 * mm, 45 * mm])
    impact_style = [("BACKGROUND", (0, 0), (-1, 0), accent), ("GRID", (0, 0), (-1, -1), 0.35, line), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm), ("TOPPADDING", (0, 0), (-1, -1), 2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm)]
    for row_number, item in enumerate(impact, start=1):
        if item.get("role") == "Scope":
            impact_style.append(("BACKGROUND", (0, row_number), (-1, row_number), colors.HexColor("#e7f5f2")))
        elif item.get("criticality") == "critical":
            impact_style.append(("BACKGROUND", (0, row_number), (-1, row_number), colors.HexColor("#fff0ee")))
    impact_table.setStyle(TableStyle(impact_style))
    story.append(impact_table)

    story.append(PageBreak())
    story.append(Paragraph("Risk assessment", styles["Section"]))
    risk_factors = change.get("riskAssessment", {}).get("factors") or []
    risk_table = Table([[Paragraph(f"<b>Selected risk:</b> {_safe(_label(change.get('riskLevel')))}", styles["Plan"])], [Paragraph("<b>CMDB-derived factors</b><br/>" + "<br/>".join(f"- {_safe(item)}" for item in risk_factors), styles["Plan"]) ]], colWidths=[178 * mm])
    risk_table.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, accent), ("BACKGROUND", (0, 0), (-1, 0), pale), ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm), ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm)]))
    story.append(risk_table)

    plan_sections = [
        ("Implementation plan", change.get("implementationPlan")),
        ("Validation and success criteria", change.get("validationPlan")),
        ("Rollback plan", change.get("rollbackPlan")),
        ("Communication plan", change.get("communicationPlan") or "No additional communication steps recorded."),
        ("Additional notes", change.get("notes") or "No additional notes recorded."),
    ]
    for heading, text in plan_sections:
        story.append(KeepTogether([Paragraph(heading, styles["Section"]), Paragraph(_safe(text).replace("\n", "<br/>"), styles["Plan"])]))

    story.append(Paragraph("Record and integration details", styles["Section"]))
    cw_state = change.get("integrationState", {}).get("connectwise", {})
    record_data = [
        ["Created by", change.get("createdBy", {}).get("email") or "Unknown"],
        ["Created at", change.get("createdAt") or "Unknown"],
        ["Impact snapshot", f"{len(impact)} configuration item(s), frozen at creation"],
        ["ConnectWise status", _label(cw_state.get("status") or "not_published")],
        ["ConnectWise ticket", cw_state.get("ticketId") or "Not created"],
    ]
    record_table = Table([[Paragraph(f"<b>{_safe(label)}</b>", styles["Cell"]), Paragraph(_safe(value), styles["Cell"])] for label, value in record_data], colWidths=[48 * mm, 130 * mm])
    record_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, line), ("BACKGROUND", (0, 0), (0, -1), pale), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm), ("TOPPADDING", (0, 0), (-1, -1), 2.3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3 * mm)]))
    story.append(record_table)

    story.extend([Spacer(1, 8 * mm), Paragraph("Approval", styles["Section"])])
    approval = Table([["Approver", "Signature", "Decision", "Date"], [change.get("approver") or "", "", "Approved / Rejected", ""]], colWidths=[50 * mm, 48 * mm, 48 * mm, 32 * mm], rowHeights=[8 * mm, 16 * mm])
    approval.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, 1), (-1, 1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.5, line), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm)]))
    story.append(approval)

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
