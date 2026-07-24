"""Curated canonical-field authority catalogue and safe MSP policy presets."""

from __future__ import annotations

from copy import deepcopy

CANONICAL_FIELD_CATALOGUE = [
    {"key": "name", "label": "Display name", "group": "Identity"},
    {"key": "type", "label": "CI type", "group": "Identity"},
    {"key": "status", "label": "Operational status", "group": "Operations"},
    {"key": "fields.serialNumber", "label": "Serial number", "group": "Identity"},
    {"key": "fields.deviceIdentifier", "label": "Device identifier", "group": "Identity"},
    {"key": "fields.model", "label": "Model", "group": "Hardware"},
    {"key": "fields.vendor", "label": "Vendor", "group": "Hardware"},
    {"key": "fields.ipAddress", "label": "IP address", "group": "Network"},
    {"key": "fields.macAddress", "label": "MAC address", "group": "Network"},
    {"key": "fields.operatingSystem", "label": "Operating system", "group": "Software"},
    {"key": "metadata.lifecycle", "label": "Lifecycle", "group": "Governance"},
    {
        "key": "metadata.operationalStatus",
        "label": "Health state",
        "group": "Operations",
    },
    {"key": "metadata.criticality", "label": "Criticality", "group": "Governance"},
    {"key": "metadata.environment", "label": "Environment", "group": "Governance"},
]

FIELD_AUTHORITY_PRESETS = {
    "balanced_msp": {
        "name": "Balanced MSP baseline",
        "description": (
            "N-central owns live operational fields, ConnectWise owns commercial inventory "
            "identity, and CMDB-managed governance fields remain protected."
        ),
        "rules": [
            ("*", "status", "ncentral", 10),
            ("*", "metadata.operationalStatus", "ncentral", 10),
            ("*", "fields.ipAddress", "ncentral", 10),
            ("*", "fields.macAddress", "ncentral", 10),
            ("*", "fields.operatingSystem", "ncentral", 10),
            ("*", "name", "connectwise", 20),
            ("*", "type", "connectwise", 20),
            ("*", "fields.serialNumber", "connectwise", 20),
            ("*", "fields.model", "connectwise", 20),
            ("*", "fields.vendor", "connectwise", 20),
            ("*", "metadata.lifecycle", "cmdb", 5),
            ("*", "metadata.criticality", "cmdb", 5),
            ("*", "metadata.environment", "cmdb", 5),
        ],
    },
    "cmdb_protected": {
        "name": "CMDB governance protected",
        "description": "Keep ownership-adjacent lifecycle, criticality and environment fields local.",
        "rules": [
            ("*", "metadata.lifecycle", "cmdb", 5),
            ("*", "metadata.criticality", "cmdb", 5),
            ("*", "metadata.environment", "cmdb", 5),
        ],
    },
}


def authority_catalogue() -> dict:
    """Return a copy safe for API serialization."""

    return {
        "fields": deepcopy(CANONICAL_FIELD_CATALOGUE),
        "presets": [
            {"key": key, "name": value["name"], "description": value["description"]}
            for key, value in FIELD_AUTHORITY_PRESETS.items()
        ],
    }


def preset_rules(preset_key: str, company_id: str) -> list[dict]:
    """Expand one reviewed preset into repository field-authority rows."""

    preset = FIELD_AUTHORITY_PRESETS.get(preset_key)
    if not preset:
        raise ValueError("Unknown field-authority preset")
    return [
        {
            "companyId": company_id,
            "ciType": ci_type,
            "fieldName": field_name,
            "provider": provider,
            "priority": priority,
        }
        for ci_type, field_name, provider, priority in preset["rules"]
    ]
