"""Tests for governed, versioned change procedures."""

import unittest
from copy import deepcopy

from src.cmdb.change_templates import (
    DEFAULT_CHANGE_TEMPLATES,
    normalize_template_content,
    template_public_snapshot,
    validate_template_parameters,
)
from src.cmdb.repository import StateRepository


class ChangeTemplateDomainTests(unittest.TestCase):
    """Protect validation and immutable provenance behavior."""

    def setUp(self) -> None:
        self.standard = deepcopy(DEFAULT_CHANGE_TEMPLATES[0])

    def test_standard_templates_are_valid_and_publishable(self) -> None:
        self.assertGreaterEqual(len(DEFAULT_CHANGE_TEMPLATES), 6)
        keys = [item["key"] for item in DEFAULT_CHANGE_TEMPLATES]
        self.assertEqual(len(keys), len(set(keys)))
        for template in DEFAULT_CHANGE_TEMPLATES:
            self.assertEqual(template["status"], "published")
            self.assertEqual(
                normalize_template_content(template["content"]),
                template["content"],
            )

    def test_undefined_variables_and_invalid_selects_are_rejected(self) -> None:
        content = deepcopy(self.standard["content"])
        content["reasonTemplate"] += " {{not_defined}}"
        with self.assertRaisesRegex(ValueError, "Undefined template variables"):
            normalize_template_content(content)

        content = deepcopy(self.standard["content"])
        content["parameters"].append(
            {
                "key": "deployment_ring",
                "label": "Deployment ring",
                "type": "select",
                "required": True,
                "options": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "requires options"):
            normalize_template_content(content)

    def test_parameter_values_are_typed_and_complete(self) -> None:
        content = deepcopy(self.standard["content"])
        content["parameters"] = [
            {
                "key": "batch_size",
                "label": "Batch size",
                "type": "number",
                "required": True,
                "options": [],
            },
            {
                "key": "confirmed",
                "label": "Confirmed",
                "type": "boolean",
                "required": True,
                "options": [],
            },
        ]
        content["titleTemplate"] = "Patch batch {{batch_size}}"
        content["reasonTemplate"] = "Approved: {{confirmed}}"
        content["businessImpactTemplate"] = "No material impact."
        content["implementationPlanTemplate"] = "Apply the approved batch."
        content["validationPlanTemplate"] = "Validate the service."
        content["rollbackPlanTemplate"] = "Restore the prior state."
        content["communicationPlanTemplate"] = "Notify the service owner."
        content["closureTests"] = []
        values = validate_template_parameters(
            content,
            {"batch_size": "12", "confirmed": False},
        )
        self.assertEqual(values, {"batch_size": 12, "confirmed": False})
        with self.assertRaisesRegex(ValueError, "Complete template parameter"):
            validate_template_parameters(content, {"confirmed": True})

    def test_closure_tests_are_templated_and_governed(self) -> None:
        content = deepcopy(self.standard["content"])
        content["closureTests"] = [
            {
                "key": "portal_login",
                "label": "Portal login",
                "expectedResultTemplate": "{{service_test}} succeeds on {{asset_name}}",
                "required": True,
                "evidenceRequired": True,
            }
        ]
        normalized = normalize_template_content(content)
        self.assertEqual(normalized["closureTests"][0]["key"], "portal_login")
        self.assertTrue(normalized["closureTests"][0]["evidenceRequired"])
        content["closureTests"].append(deepcopy(content["closureTests"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate closure test key"):
            normalize_template_content(content)

    def test_snapshot_is_detached_from_live_template(self) -> None:
        template = {
            **self.standard,
            "id": "template-id",
            "version": 3,
        }
        snapshot = template_public_snapshot(template)
        template["content"]["titleTemplate"] = "Changed later"
        self.assertNotEqual(snapshot["content"]["titleTemplate"], "Changed later")


class ChangeTemplateRepositoryTests(unittest.TestCase):
    """Protect identity, scope and immutable versions in the state adapter."""

    def setUp(self) -> None:
        self.state = {
            "companies": [{"id": "acme", "name": "Acme"}],
            "users": [
                {
                    "id": "admin",
                    "email": "admin@example.com",
                    "role": "platform_admin",
                    "companyIds": ["*"],
                }
            ],
            "assets": [],
            "relationships": [],
            "accessGroups": [],
            "integrations": [],
            "syncRuns": [],
        }
        self.repository = StateRepository(self.state, lambda _state: None)

    def test_defaults_and_customer_template_versions_are_available(self) -> None:
        self.assertGreaterEqual(len(self.repository.list_change_templates("acme")), 6)
        record = {
            **deepcopy(DEFAULT_CHANGE_TEMPLATES[0]),
            "companyId": "acme",
            "key": "acme_month_end",
            "name": "Acme month-end maintenance",
            "status": "draft",
            "system": False,
        }
        created = self.repository.create_change_template(record, "admin")
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["companyId"], "acme")

        changed_content = deepcopy(created["content"])
        changed_content["titleTemplate"] = "Month-end maintenance on {{asset_name}}"
        updated = self.repository.update_change_template(
            created["id"],
            {
                "name": created["name"],
                "description": created["description"],
                "tags": created["tags"],
                "status": "published",
                "content": changed_content,
            },
            1,
            "admin",
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["status"], "published")
        self.assertNotEqual(
            self.repository.get_change_template(created["id"], 1)["content"],
            self.repository.get_change_template(created["id"], 2)["content"],
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repository.update_change_template(
                created["id"],
                {"content": changed_content},
                1,
                "admin",
            )
        self.assertEqual(
            self.state["auditEvents"][0]["action"],
            "version_created",
        )

    def test_key_is_unique_per_scope_but_can_be_reused_by_customer(self) -> None:
        record = {
            **deepcopy(DEFAULT_CHANGE_TEMPLATES[0]),
            "companyId": "acme",
            "key": "windows_server_patching",
            "system": False,
        }
        self.repository.create_change_template(record, "admin")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.repository.create_change_template(record, "admin")


if __name__ == "__main__":
    unittest.main()
