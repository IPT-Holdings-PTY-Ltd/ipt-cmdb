"""Versioned, parameterized procedures for technician change records."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

TEMPLATE_STATUSES = {"draft", "published", "retired"}
TEMPLATE_PARAMETER_TYPES = {"text", "multiline", "number", "select", "boolean"}
TEMPLATE_TOKEN = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}", re.IGNORECASE)
TEMPLATE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
TEMPLATE_FIELDS = {
    "titleTemplate",
    "changeType",
    "category",
    "priority",
    "outageExpected",
    "expectedDurationMinutes",
    "reasonTemplate",
    "businessImpactTemplate",
    "implementationPlanTemplate",
    "validationPlanTemplate",
    "rollbackPlanTemplate",
    "communicationStatus",
    "communicationPlanTemplate",
    "suggestedApproverRole",
    "closureTests",
    "parameters",
}


def _template(
    key: str,
    name: str,
    description: str,
    tags: list[str],
    content: dict[str, Any],
) -> dict[str, Any]:
    """Return a published MSP standard-template seed."""

    content = deepcopy(content)
    content.setdefault(
        "closureTests",
        [
            {
                "key": "validation_plan",
                "label": "Planned validation",
                "expectedResultTemplate": content.get("validationPlanTemplate", ""),
                "required": True,
                "evidenceRequired": True,
            },
            {
                "key": "post_change_observation",
                "label": "Post-change observation",
                "expectedResultTemplate": (
                    "Monitoring and representative user-facing checks remain healthy "
                    "after the change."
                ),
                "required": True,
                "evidenceRequired": False,
            },
        ],
    )
    return {
        "key": key,
        "name": name,
        "description": description,
        "tags": tags,
        "companyId": None,
        "status": "published",
        "system": True,
        "ownerUserId": None,
        "reviewDueDate": None,
        "content": content,
    }


DEFAULT_CHANGE_TEMPLATES = [
    _template(
        "windows_server_patching",
        "Windows server patching",
        "Controlled operating-system patching with service checks and rollback criteria.",
        ["microsoft", "windows", "patching", "server"],
        {
            "titleTemplate": "Patch {{asset_name}} - {{patch_window}}",
            "changeType": "standard",
            "category": "infrastructure",
            "priority": "medium",
            "outageExpected": False,
            "expectedDurationMinutes": 120,
            "reasonTemplate": (
                "Install approved Windows updates on {{asset_name}} during {{patch_window}} "
                "to maintain security and vendor support."
            ),
            "businessImpactTemplate": (
                "{{business_impact}}. Services may restart while updates are installed."
            ),
            "implementationPlanTemplate": (
                "1. Confirm monitoring, backup and free-space checks are healthy.\n"
                "2. Confirm the approved update set and maintenance window.\n"
                "3. Notify affected stakeholders and place {{asset_name}} in maintenance mode.\n"
                "4. Install approved updates and restart when required.\n"
                "5. Confirm services start and remove maintenance mode."
            ),
            "validationPlanTemplate": (
                "Confirm {{asset_name}} is online, required services are running, monitoring is "
                "healthy and {{service_test}} succeeds. Record update and restart evidence."
            ),
            "rollbackPlanTemplate": (
                "Stop if startup, service or application checks fail. Remove the failed update "
                "where supported or restore the approved pre-change snapshot/backup. Escalate "
                "if service is not restored within {{rollback_limit}}."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Notify the service owner before work starts, at completion, and immediately if "
                "rollback is invoked."
            ),
            "suggestedApproverRole": "service_owner",
            "parameters": [
                {
                    "key": "asset_name",
                    "label": "Primary server",
                    "type": "text",
                    "required": True,
                    "source": "scope_primary_name",
                    "helpText": "Defaults to the first selected CI.",
                    "options": [],
                },
                {
                    "key": "patch_window",
                    "label": "Patch window",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "For example: July production maintenance window.",
                    "options": [],
                },
                {
                    "key": "business_impact",
                    "label": "Expected business impact",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "Describe user-facing interruption or state that none is expected.",
                    "options": [],
                },
                {
                    "key": "service_test",
                    "label": "Service validation",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "The application or transaction test the technician will run.",
                    "options": [],
                },
                {
                    "key": "rollback_limit",
                    "label": "Rollback decision limit",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "For example: 30 minutes.",
                    "options": [],
                },
            ],
        },
    ),
    _template(
        "network_firmware_upgrade",
        "Network firmware upgrade",
        "Firmware upgrade for a managed switch, router or firewall.",
        ["network", "firmware", "switch", "firewall"],
        {
            "titleTemplate": "Upgrade {{asset_name}} firmware to {{target_version}}",
            "changeType": "normal",
            "category": "network",
            "priority": "high",
            "outageExpected": True,
            "expectedDurationMinutes": 90,
            "reasonTemplate": (
                "Upgrade {{asset_name}} from {{current_version}} to {{target_version}} "
                "to address {{upgrade_reason}}."
            ),
            "businessImpactTemplate": (
                "{{business_impact}} during the network-device restart and convergence period."
            ),
            "implementationPlanTemplate": (
                "1. Verify the approved firmware path, release notes and hardware compatibility.\n"
                "2. Export and validate the current configuration.\n"
                "3. Confirm redundant paths and console/out-of-band access.\n"
                "4. Notify stakeholders and place monitoring in maintenance mode.\n"
                "5. Upload, validate and activate firmware {{target_version}}.\n"
                "6. Confirm configuration, interfaces, routing and security services."
            ),
            "validationPlanTemplate": (
                "Confirm device health, expected firmware, interface state, routing/neighbour "
                "sessions, monitoring and representative client connectivity."
            ),
            "rollbackPlanTemplate": (
                "If health or connectivity checks fail, boot the previous image or restore the "
                "saved configuration using console access. Escalate after {{rollback_limit}}."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Send start, service-restored and completion notices to the service owner and "
                "affected site contacts."
            ),
            "suggestedApproverRole": "technical_owner",
            "parameters": [
                {
                    "key": "asset_name",
                    "label": "Network device",
                    "type": "text",
                    "required": True,
                    "source": "scope_primary_name",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "current_version",
                    "label": "Current firmware",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "target_version",
                    "label": "Target firmware",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "upgrade_reason",
                    "label": "Upgrade reason",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "Security advisory, support requirement or defect reference.",
                    "options": [],
                },
                {
                    "key": "business_impact",
                    "label": "Expected business impact",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "rollback_limit",
                    "label": "Rollback decision limit",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
            ],
        },
    ),
    _template(
        "certificate_renewal",
        "Certificate renewal",
        "Renew and deploy a TLS certificate with expiry and trust validation.",
        ["certificate", "tls", "security", "renewal"],
        {
            "titleTemplate": "Renew {{certificate_name}} certificate",
            "changeType": "standard",
            "category": "security",
            "priority": "medium",
            "outageExpected": False,
            "expectedDurationMinutes": 60,
            "reasonTemplate": (
                "Renew {{certificate_name}} before {{expiry_date}} to prevent trust or "
                "service interruption."
            ),
            "businessImpactTemplate": (
                "No outage is expected. A brief service reload may occur for {{service_name}}."
            ),
            "implementationPlanTemplate": (
                "1. Confirm names, chain, key ownership and approved validity period.\n"
                "2. Back up the current certificate and binding configuration.\n"
                "3. Obtain and validate the replacement certificate.\n"
                "4. Install it in the required stores and update bindings.\n"
                "5. Reload the service where required and retain the prior certificate for rollback."
            ),
            "validationPlanTemplate": (
                "Validate the certificate chain, names, expiry and protocol from an external "
                "client. Confirm {{service_test}} succeeds without trust warnings."
            ),
            "rollbackPlanTemplate": (
                "Restore the previous certificate and bindings, reload the service and repeat "
                "the external trust and application tests."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Confirm completion and the new expiry date with the technical and service owners."
            ),
            "suggestedApproverRole": "technical_owner",
            "parameters": [
                {
                    "key": "certificate_name",
                    "label": "Certificate or common name",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "expiry_date",
                    "label": "Current expiry date",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "service_name",
                    "label": "Protected service",
                    "type": "text",
                    "required": True,
                    "source": "scope_primary_name",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "service_test",
                    "label": "Validation transaction",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
            ],
        },
    ),
    _template(
        "database_maintenance",
        "Database maintenance",
        "Controlled database patching, upgrade or maintenance with integrity checks.",
        ["database", "sql", "maintenance", "backup"],
        {
            "titleTemplate": "{{maintenance_action}} - {{asset_name}}",
            "changeType": "normal",
            "category": "database",
            "priority": "high",
            "outageExpected": True,
            "expectedDurationMinutes": 120,
            "reasonTemplate": (
                "Perform {{maintenance_action}} on {{asset_name}} to address {{maintenance_reason}}."
            ),
            "businessImpactTemplate": "{{business_impact}}.",
            "implementationPlanTemplate": (
                "1. Confirm database health, capacity, replication and a tested recent backup.\n"
                "2. Record current version and application connectivity baselines.\n"
                "3. Notify stakeholders and stop or drain dependent application services.\n"
                "4. Perform {{maintenance_action}} according to the approved vendor procedure.\n"
                "5. Start services and monitor database health and recovery."
            ),
            "validationPlanTemplate": (
                "Confirm database consistency, jobs, replication and monitoring. Run "
                "{{service_test}} through the dependent application."
            ),
            "rollbackPlanTemplate": (
                "Stop dependent services and restore the pre-change snapshot/backup or execute "
                "the vendor downgrade procedure. Validate recovery within {{rollback_limit}}."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Notify application and business owners before interruption, after service "
                "restoration and at final validation."
            ),
            "suggestedApproverRole": "business_owner",
            "parameters": [
                {
                    "key": "asset_name",
                    "label": "Database service",
                    "type": "text",
                    "required": True,
                    "source": "scope_primary_name",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "maintenance_action",
                    "label": "Maintenance action",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "For example: cumulative update or index maintenance.",
                    "options": [],
                },
                {
                    "key": "maintenance_reason",
                    "label": "Maintenance reason",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "business_impact",
                    "label": "Expected business impact",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "service_test",
                    "label": "Application transaction test",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "rollback_limit",
                    "label": "Recovery objective",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
            ],
        },
    ),
    _template(
        "firewall_rule_change",
        "Firewall rule change",
        "Create or modify a least-privilege firewall rule with connectivity evidence.",
        ["firewall", "security", "network", "rule"],
        {
            "titleTemplate": "Firewall rule - {{rule_purpose}}",
            "changeType": "normal",
            "category": "security",
            "priority": "medium",
            "outageExpected": False,
            "expectedDurationMinutes": 45,
            "reasonTemplate": "Implement a firewall rule for {{rule_purpose}} under {{request_reference}}.",
            "businessImpactTemplate": (
                "{{business_impact}}. Existing traffic should remain unaffected."
            ),
            "implementationPlanTemplate": (
                "1. Confirm approved source, destination, service, direction and expiry.\n"
                "2. Export the active policy and identify the least-privilege rule position.\n"
                "3. Create or update the rule with logging enabled.\n"
                "4. Validate policy compilation and commit the configuration.\n"
                "5. Record rule identifier and change evidence."
            ),
            "validationPlanTemplate": (
                "Confirm approved traffic succeeds, prohibited traffic remains blocked and logs "
                "show the expected source, destination and service."
            ),
            "rollbackPlanTemplate": (
                "Disable or remove the new rule, restore the previous policy when required and "
                "confirm the prior connectivity state."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Notify the requester and service owner after validation or immediately if the "
                "rule is rolled back."
            ),
            "suggestedApproverRole": "technical_owner",
            "parameters": [
                {
                    "key": "rule_purpose",
                    "label": "Rule purpose",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "request_reference",
                    "label": "Request or approval reference",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "business_impact",
                    "label": "Expected business impact",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
            ],
        },
    ),
    _template(
        "emergency_service_restoration",
        "Emergency service restoration",
        "Minimum controlled record for urgent restoration, with mandatory retrospective review.",
        ["emergency", "outage", "restoration", "incident"],
        {
            "titleTemplate": "Emergency restore {{service_name}} - {{incident_reference}}",
            "changeType": "emergency",
            "category": "application",
            "priority": "critical",
            "outageExpected": True,
            "expectedDurationMinutes": 60,
            "reasonTemplate": (
                "Restore {{service_name}} during {{incident_reference}}. Emergency authority: "
                "{{emergency_authority}}."
            ),
            "businessImpactTemplate": "{{business_impact}}.",
            "implementationPlanTemplate": (
                "1. Record current symptoms and emergency authorization.\n"
                "2. Preserve logs and available recovery evidence.\n"
                "3. Execute the least-risk restoration action: {{restoration_action}}.\n"
                "4. Monitor recovery and stop if impact increases.\n"
                "5. Record every deviation for retrospective review."
            ),
            "validationPlanTemplate": (
                "Confirm {{service_test}}, monitoring recovery and business-owner acceptance. "
                "Continue incident monitoring after restoration."
            ),
            "rollbackPlanTemplate": (
                "Reverse the restoration action if impact increases, return to the last known "
                "stable state and continue the incident escalation."
            ),
            "communicationStatus": "required",
            "communicationPlanTemplate": (
                "Maintain incident-channel updates, notify the business owner at restoration and "
                "schedule a post-implementation review."
            ),
            "suggestedApproverRole": "business_owner",
            "parameters": [
                {
                    "key": "service_name",
                    "label": "Affected service",
                    "type": "text",
                    "required": True,
                    "source": "scope_primary_name",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "incident_reference",
                    "label": "Incident reference",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "emergency_authority",
                    "label": "Emergency authority",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "Name or approved emergency-change role.",
                    "options": [],
                },
                {
                    "key": "business_impact",
                    "label": "Current business impact",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "restoration_action",
                    "label": "Restoration action",
                    "type": "multiline",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
                {
                    "key": "service_test",
                    "label": "Restoration validation",
                    "type": "text",
                    "required": True,
                    "source": "",
                    "helpText": "",
                    "options": [],
                },
            ],
        },
    ),
]


def normalize_template_content(content: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one immutable template-version payload."""

    if not isinstance(content, dict):
        raise ValueError("Template content must be an object")
    unknown = sorted(set(content) - TEMPLATE_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported template fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {
        "titleTemplate": str(content.get("titleTemplate") or "").strip(),
        "changeType": str(content.get("changeType") or "normal").strip().lower(),
        "category": str(content.get("category") or "infrastructure").strip().lower(),
        "priority": str(content.get("priority") or "medium").strip().lower(),
        "outageExpected": bool(content.get("outageExpected")),
        "expectedDurationMinutes": int(content.get("expectedDurationMinutes") or 60),
        "reasonTemplate": str(content.get("reasonTemplate") or "").strip(),
        "businessImpactTemplate": str(content.get("businessImpactTemplate") or "").strip(),
        "implementationPlanTemplate": str(content.get("implementationPlanTemplate") or "").strip(),
        "validationPlanTemplate": str(content.get("validationPlanTemplate") or "").strip(),
        "rollbackPlanTemplate": str(content.get("rollbackPlanTemplate") or "").strip(),
        "communicationStatus": str(content.get("communicationStatus") or "required").strip(),
        "communicationPlanTemplate": str(content.get("communicationPlanTemplate") or "").strip(),
        "suggestedApproverRole": str(content.get("suggestedApproverRole") or "").strip(),
        "closureTests": [],
        "parameters": [],
    }
    if normalized["changeType"] not in {"standard", "normal", "emergency"}:
        raise ValueError("Choose a valid template change type")
    if normalized["category"] not in {
        "infrastructure",
        "network",
        "software",
        "application",
        "database",
        "security",
        "cloud",
        "other",
    }:
        raise ValueError("Choose a valid template category")
    if normalized["priority"] not in {"low", "medium", "high", "critical"}:
        raise ValueError("Choose a valid template priority")
    if normalized["communicationStatus"] not in {
        "required",
        "not_required",
        "completed",
    }:
        raise ValueError("Choose a valid template communication state")
    if not 5 <= normalized["expectedDurationMinutes"] <= 10080:
        raise ValueError("Expected duration must be between 5 minutes and 7 days")
    required_text = {
        "titleTemplate": "Enter a title template",
        "reasonTemplate": "Enter a reason template",
        "implementationPlanTemplate": "Enter an implementation plan template",
        "validationPlanTemplate": "Enter validation criteria",
        "rollbackPlanTemplate": "Enter a rollback plan template",
    }
    for field, message in required_text.items():
        if not normalized[field]:
            raise ValueError(message)
    for field in (
        "titleTemplate",
        "reasonTemplate",
        "businessImpactTemplate",
        "implementationPlanTemplate",
        "validationPlanTemplate",
        "rollbackPlanTemplate",
        "communicationPlanTemplate",
    ):
        if len(normalized[field]) > (240 if field == "titleTemplate" else 8000):
            raise ValueError(f"{field} is too long")
    if len(normalized["suggestedApproverRole"]) > 80:
        raise ValueError("Suggested approver role is too long")

    closure_test_keys: set[str] = set()
    closure_tests = content.get("closureTests") or []
    if not isinstance(closure_tests, list) or len(closure_tests) > 30:
        raise ValueError("A template may define up to 30 closure tests")
    for raw in closure_tests:
        if not isinstance(raw, dict):
            raise ValueError("Each closure test must be an object")
        key = str(raw.get("key") or "").strip().lower()
        if not TEMPLATE_KEY.fullmatch(key):
            raise ValueError(f"Invalid closure test key: {key or '(blank)'}")
        if key in closure_test_keys:
            raise ValueError(f"Duplicate closure test key: {key}")
        closure_test_keys.add(key)
        label = str(raw.get("label") or key.replace("_", " ").title()).strip()
        expected = str(raw.get("expectedResultTemplate") or "").strip()
        if not label:
            raise ValueError(f"Enter a label for closure test {key}")
        if not expected:
            raise ValueError(f"Enter the expected result for closure test {key}")
        if len(label) > 160 or len(expected) > 2000:
            raise ValueError(f"Closure test {key} is too long")
        normalized["closureTests"].append(
            {
                "key": key,
                "label": label,
                "expectedResultTemplate": expected,
                "required": bool(raw.get("required", True)),
                "evidenceRequired": bool(raw.get("evidenceRequired", False)),
            }
        )

    keys: set[str] = set()
    parameters = content.get("parameters") or []
    if not isinstance(parameters, list) or len(parameters) > 30:
        raise ValueError("A template may define up to 30 parameters")
    for raw in parameters:
        if not isinstance(raw, dict):
            raise ValueError("Each template parameter must be an object")
        key = str(raw.get("key") or "").strip().lower()
        if not TEMPLATE_KEY.fullmatch(key):
            raise ValueError(f"Invalid template parameter key: {key or '(blank)'}")
        if key in keys:
            raise ValueError(f"Duplicate template parameter key: {key}")
        keys.add(key)
        parameter_type = str(raw.get("type") or "text").strip().lower()
        if parameter_type not in TEMPLATE_PARAMETER_TYPES:
            raise ValueError(f"Unsupported parameter type for {key}")
        raw_options = raw.get("options") or []
        if not isinstance(raw_options, list) or len(raw_options) > 50:
            raise ValueError(f"Parameter {key} may define up to 50 options")
        options = [str(item).strip()[:120] for item in raw_options if str(item).strip()]
        if parameter_type == "select" and not options:
            raise ValueError(f"Select parameter {key} requires options")
        source = str(raw.get("source") or "").strip()[:80]
        if source not in {"", "scope_primary_name"}:
            raise ValueError(f"Unsupported auto-fill source for {key}")
        normalized["parameters"].append(
            {
                "key": key,
                "label": str(raw.get("label") or key.replace("_", " ").title()).strip()[:120],
                "type": parameter_type,
                "required": bool(raw.get("required", True)),
                "source": source,
                "helpText": str(raw.get("helpText") or "").strip()[:500],
                "options": list(dict.fromkeys(options)),
            }
        )
    referenced: set[str] = set()
    for field, value in normalized.items():
        if field.endswith("Template") and isinstance(value, str):
            referenced.update(token.casefold() for token in TEMPLATE_TOKEN.findall(value))
    for closure_test in normalized["closureTests"]:
        referenced.update(
            token.casefold() for token in TEMPLATE_TOKEN.findall(closure_test["label"])
        )
        referenced.update(
            token.casefold()
            for token in TEMPLATE_TOKEN.findall(closure_test["expectedResultTemplate"])
        )
    implicit = {"company_name", "asset_name", "business_system_name"}
    undefined = sorted(referenced - keys - implicit)
    if undefined:
        raise ValueError(f"Undefined template variables: {', '.join(undefined)}")
    return normalized


def validate_template_parameters(
    content: dict[str, Any],
    parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate values supplied for one pinned template version."""

    normalized_content = normalize_template_content(content)
    supplied = parameters or {}
    if not isinstance(supplied, dict):
        raise ValueError("Template parameters must be an object")
    definitions = {item["key"]: item for item in normalized_content["parameters"]}
    unknown = sorted(set(supplied) - set(definitions))
    if unknown:
        raise ValueError(f"Unknown template parameters: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key, definition in definitions.items():
        raw = supplied.get(key)
        if definition["type"] == "boolean":
            if isinstance(raw, bool):
                value: Any = raw
            elif raw is None:
                value = False
            elif str(raw).strip().casefold() in {"true", "1", "yes"}:
                value = True
            elif str(raw).strip().casefold() in {"false", "0", "no"}:
                value = False
            else:
                raise ValueError(f"{definition['label']} must be yes or no")
        elif definition["type"] == "number":
            number = str(raw or "").strip()
            value = number
            if number:
                try:
                    parsed = float(number)
                except ValueError as error:
                    raise ValueError(f"{definition['label']} must be a number") from error
                value = int(parsed) if parsed.is_integer() else parsed
        else:
            value = str(raw or "").strip()
            if len(value) > 4000:
                raise ValueError(f"{definition['label']} is too long")
        if definition["required"] and (
            key not in supplied or (definition["type"] != "boolean" and value in {"", None})
        ):
            raise ValueError(f"Complete template parameter: {definition['label']}")
        if definition["type"] == "select" and value not in definition["options"]:
            raise ValueError(f"Choose a valid value for {definition['label']}")
        result[key] = value
    return result


def template_public_snapshot(template: dict[str, Any]) -> dict[str, Any]:
    """Return immutable, non-secret provenance stored with a change."""

    return {
        "id": template["id"],
        "key": template["key"],
        "name": template["name"],
        "companyId": template.get("companyId"),
        "version": int(template["version"]),
        "content": deepcopy(template["content"]),
    }
