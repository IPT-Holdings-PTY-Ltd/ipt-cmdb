import unittest

from src.cmdb.repository import (
    PostgresCmdbRepository,
    StateRepository,
    canonical_uuid,
    hash_password,
    verify_password,
)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "companies": [{"id": "acme", "name": "Acme", "externalIds": {}}],
            "users": [
                {
                    "id": "admin",
                    "email": "admin@example.com",
                    "password": "ChangeMe!",
                    "role": "platform_admin",
                    "companyIds": ["*"],
                }
            ],
            "accessGroups": [],
            "integrations": [
                {
                    "id": "connectwise",
                    "name": "ConnectWise Manage",
                    "type": "connectwise",
                    "enabled": False,
                    "status": "Not configured",
                }
            ],
            "syncRuns": [],
            "assets": [],
            "relationships": [],
        }
        self.saved = []
        self.repository = StateRepository(self.state, lambda value: self.saved.append(value))

    def test_string_ids_migrate_to_stable_canonical_uuids(self):
        first = canonical_uuid("configuration_item", "asset-1")
        second = canonical_uuid("configuration_item", "asset-1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36)

    def test_existing_uuid_is_preserved(self):
        current = "175925a0-73a4-5a44-aee0-af21152589d8"
        self.assertEqual(canonical_uuid("configuration_item", current), current)

    def test_postgres_change_methods_do_not_fall_through_to_state(self):
        self.assertIs(PostgresCmdbRepository.list_changes, StateRepository._postgres_list_changes)
        self.assertIs(
            PostgresCmdbRepository.create_change,
            StateRepository._postgres_create_change,
        )
        self.assertIs(
            PostgresCmdbRepository.update_change,
            StateRepository._postgres_update_change,
        )

    def test_password_hash_verifies_without_storing_plaintext(self):
        encoded = hash_password("VerySecret!42")
        self.assertNotIn("VerySecret!42", encoded)
        self.assertTrue(verify_password("VerySecret!42", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_plaintext_password_is_migrated_after_successful_login(self):
        user = self.repository.authenticate("admin@example.com", "ChangeMe!")
        self.assertIsNotNone(user)
        self.assertNotIn("password", self.state["users"][0])
        self.assertTrue(verify_password("ChangeMe!", self.state["users"][0]["passwordHash"]))

    def test_created_user_only_stores_a_password_hash(self):
        created = self.repository.create_user(
            {
                "id": "reader",
                "email": "reader@example.com",
                "role": "client_reader",
                "companyIds": ["acme"],
            },
            "VerySecret!42",
            "admin",
        )
        self.assertNotIn("password", created)
        stored = next(item for item in self.state["users"] if item["id"] == "reader")
        self.assertNotIn("password", stored)
        self.assertTrue(verify_password("VerySecret!42", stored["passwordHash"]))

    def test_access_group_changes_are_audited(self):
        group = {
            "id": "managed",
            "name": "Managed",
            "companyIds": ["acme"],
            "system": False,
        }
        self.repository.create_access_group(group, "admin")
        self.repository.update_access_group("managed", {"name": "Managed customers"}, "admin")
        self.assertTrue(self.repository.delete_access_group("managed", "admin"))
        self.assertEqual(
            [item["action"] for item in self.state["auditEvents"][:3]],
            ["deleted", "updated", "created"],
        )

    def test_change_package_is_numbered_persisted_and_audited(self):
        change = {
            "id": "change-1",
            "number": self.repository.next_change_number(2026),
            "companyId": "acme",
            "title": "Patch database",
            "impactSnapshot": [],
        }
        stored = self.repository.create_change(change, "admin")
        self.assertEqual(stored["number"], "CHG-2026-0001")
        self.assertEqual(self.repository.get_change("change-1")["title"], "Patch database")
        self.assertEqual(self.repository.list_changes(), [change])
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "change_request")

    def test_change_revision_update_is_persisted_and_audited(self):
        change = {
            "id": "change-1",
            "number": "CHG-2026-0001",
            "companyId": "acme",
            "title": "Patch database",
            "revision": 1,
            "impactSnapshot": [],
        }
        self.repository.create_change(change, "admin")
        updated = {**change, "title": "Patch database safely", "revision": 2}
        stored = self.repository.update_change(
            "change-1", updated, "admin", action="updated", reason="Plan revised"
        )
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(self.repository.get_change("change-1")["title"], "Patch database safely")
        self.assertEqual(self.state["auditEvents"][0]["reason"], "Plan revised")

    def test_sync_run_updates_connection_history_and_audit(self):
        run = {
            "id": "run-1",
            "type": "connectwise",
            "status": "blocked",
            "startedAt": "2026-07-13T10:00:00Z",
            "finishedAt": "2026-07-13T10:00:01Z",
            "discovered": 0,
            "imported": 0,
            "message": "Credentials are not configured",
        }
        stored = self.repository.record_sync_run("connectwise", run, False, "admin")
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(self.repository.list_sync_runs()[0]["id"], "run-1")
        self.assertEqual(self.repository.list_integrations()[0]["lastSync"], run["finishedAt"])
        self.assertEqual(self.state["auditEvents"][0]["action"], "sync_completed")

    def test_msp_branding_is_normalized_persisted_and_audited(self):
        self.assertEqual(self.repository.get_msp_branding()["secondaryAccent"], "#7997ff")
        stored = self.repository.update_msp_branding(
            {
                "name": "IPT CMDB",
                "logoText": "IPT",
                "accent": "#4ed477",
                "secondaryAccent": "#5b7cfa",
                "logoDataUrl": "data:image/png;base64,abc",
                "logoFileName": "ipt.png",
                "reportFooter": "IPT Holdings | Controlled document",
            },
            "admin",
        )
        self.assertEqual(stored["name"], "IPT CMDB")
        self.assertEqual(self.state["mspBranding"]["logoFileName"], "ipt.png")
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "msp_branding")
        self.assertNotIn("data:image", self.state["auditEvents"][0]["after"]["logoDataUrl"])

    def test_customer_branding_is_scoped_persisted_and_audited(self):
        self.assertEqual(self.repository.get_company_branding("acme")["name"], "Acme")
        stored = self.repository.update_company_branding(
            "acme",
            {
                "name": "Acme Portal",
                "logoText": "AC",
                "accent": "#123456",
                "secondaryAccent": "#654321",
                "logoDataUrl": "",
                "logoFileName": "",
            },
            "admin",
        )
        self.assertEqual(stored["name"], "Acme Portal")
        self.assertEqual(self.repository.list_company_branding()["acme"]["accent"], "#123456")
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "company_branding")
        self.assertEqual(self.state["auditEvents"][0]["companyId"], "acme")

    def test_asset_write_creates_an_audit_event(self):
        asset = {
            "id": "asset-1",
            "companyId": "acme",
            "name": "APP01",
            "type": "Server",
        }
        self.repository.create_asset(asset, "admin")
        self.assertEqual(self.state["auditEvents"][0]["action"], "created")
        self.assertEqual(self.state["auditEvents"][0]["entityId"], "asset-1")
        self.assertEqual(len(self.saved), 1)

    def test_contacts_and_responsibility_history_are_tenant_scoped_and_audited(self):
        self.repository.create_asset(
            {
                "id": "asset-1",
                "companyId": "acme",
                "name": "Sage 200",
                "type": "Business system",
                "metadata": {},
            },
            "admin",
        )
        contact = self.repository.create_contact(
            {
                "id": "contact-1",
                "companyId": "acme",
                "displayName": "Jane Owner",
                "email": "jane@acme.example",
                "status": "active",
                "source": "manual",
                "syncStatus": "not_synced",
                "attributes": {},
            },
            "admin",
        )
        self.assertEqual(contact["responsibilityCount"], 0)
        assigned = self.repository.replace_asset_responsibilities(
            "asset-1",
            [
                {
                    "contactId": "contact-1",
                    "role": "business_owner",
                    "isPrimary": True,
                    "escalationOrder": 1,
                }
            ],
            "acme",
            "admin",
            reason="Owner confirmed",
        )
        self.assertEqual(assigned[0]["contactName"], "Jane Owner")
        self.assertEqual(
            self.repository.get_asset("asset-1")["responsibilities"][0]["role"],
            "business_owner",
        )
        self.assertEqual(self.repository.get_contact("contact-1")["responsibilityCount"], 1)
        self.repository.replace_asset_responsibilities(
            "asset-1", [], "acme", "admin", reason="Owner departed"
        )
        history = self.repository.list_contact_responsibilities(
            "acme", contact_id="contact-1", include_inactive=True
        )
        self.assertIsNotNone(history[0]["effectiveUntil"])
        self.assertEqual(self.state["auditEvents"][0]["reason"], "Owner departed")

    def test_contact_profile_changes_capture_field_level_history(self):
        self.repository.create_contact(
            {
                "id": "contact-1",
                "companyId": "acme",
                "displayName": "Jane Owner",
                "email": "jane@acme.example",
                "status": "active",
                "source": "manual",
                "syncStatus": "not_synced",
                "attributes": {},
            },
            "admin",
        )
        updated = self.repository.update_contact(
            "contact-1",
            {"jobTitle": "Finance Director", "status": "on_leave"},
            "admin",
            reason="Extended leave",
        )
        self.assertEqual(updated["jobTitle"], "Finance Director")
        event = self.state["auditEvents"][0]
        self.assertEqual(event["entityType"], "contact")
        self.assertEqual(event["reason"], "Extended leave")
        self.assertIn("jobTitle", {item["field"] for item in event["changes"]})

    def test_relationship_retirement_is_audited(self):
        relationship = {"id": "rel-1", "fromId": "a", "toId": "b", "type": "depends_on"}
        self.repository.create_relationship(relationship, "acme", "admin")
        self.assertTrue(self.repository.delete_relationship("rel-1", "acme", "admin"))
        self.assertEqual(self.state["relationships"], [])
        self.assertEqual(self.state["auditEvents"][0]["action"], "retired")

    def test_user_company_and_asset_lifecycle_handles_missing_and_disabled_records(self):
        self.assertIsNone(self.repository.authenticate("missing@example.com", "secret"))
        self.assertIsNone(self.repository.authenticate("admin@example.com", "wrong"))
        self.assertFalse(self.repository.set_user_status("missing", "disabled", "admin"))

        self.repository.create_user(
            {
                "id": "reader",
                "email": "reader@example.com",
                "role": "client_reader",
                "companyIds": ["acme"],
            },
            "VerySecret!42",
            "admin",
        )
        self.assertTrue(
            self.repository.set_user_status("reader", "disabled", "admin", reason="Access review")
        )
        self.assertNotIn("reader", {item["id"] for item in self.repository.list_users()})
        self.assertIsNone(self.repository.authenticate("reader@example.com", "VerySecret!42"))

        company = self.repository.create_company(
            {"id": "northwind", "name": "Northwind Traders", "externalIds": {}}, "admin"
        )
        self.assertEqual(company["id"], "northwind")
        asset = self.repository.create_asset(
            {"id": "asset-1", "companyId": "acme", "name": "APP01", "type": "Server"},
            "admin",
        )
        updated = self.repository.update_asset(asset["id"], {"status": "Retired"}, "admin")
        self.assertEqual(updated["status"], "Retired")
        self.assertIsNone(self.repository.update_asset("missing", {"status": "Retired"}, "admin"))
        self.assertFalse(self.repository.delete_relationship("missing", "acme", "admin"))

    def test_data_quality_exceptions_are_upserted_scoped_and_resolved(self):
        exception = {
            "id": "owner-gap:asset-1",
            "companyId": "acme",
            "ruleKey": "owner-gap",
            "entityId": "asset-1",
            "reason": "Temporary project ownership",
        }
        created = self.repository.create_data_quality_exception(exception, "admin")
        exception["reason"] = "Ownership review scheduled"
        updated = self.repository.create_data_quality_exception(exception, "admin")
        self.assertEqual(created["id"], updated["id"])
        self.assertEqual(len(self.repository.list_data_quality_exceptions("acme")), 1)
        self.assertEqual(updated["reason"], "Ownership review scheduled")

        resolved = self.repository.resolve_data_quality_exception(updated["id"], "admin")
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["resolvedBy"], "admin")
        self.assertIsNone(self.repository.resolve_data_quality_exception("missing", "admin"))
        self.assertEqual(self.repository.list_data_quality_exceptions("northwind"), [])

    def test_reconciliation_and_field_authority_decisions_are_governed(self):
        self.state["reconciliationCandidates"] = [
            {"id": "candidate-1", "companyId": "acme", "state": "pending"},
            {"id": "candidate-2", "companyId": "northwind", "state": "pending"},
        ]
        self.assertEqual(
            [item["id"] for item in self.repository.list_reconciliation_candidates("acme")],
            ["candidate-1"],
        )
        decision = self.repository.resolve_reconciliation_candidate(
            "candidate-1", "use_existing", "Serial number confirmed", "asset-1", "admin"
        )
        self.assertEqual(decision["state"], "approved")
        self.assertEqual(decision["targetAssetId"], "asset-1")
        self.assertIsNone(
            self.repository.resolve_reconciliation_candidate(
                "missing", "ignore", "Not relevant", None, "admin"
            )
        )

        rule = {
            "companyId": "acme",
            "ciType": "Server",
            "fieldName": "name",
            "provider": "ncentral",
            "priority": 100,
        }
        self.repository.upsert_field_authority(rule, "admin")
        rule["priority"] = 10
        updated = self.repository.upsert_field_authority(rule, "admin")
        self.assertEqual(updated["priority"], 10)
        self.assertEqual(len(self.repository.list_field_authority("acme")), 1)
        self.assertTrue(
            self.repository.delete_field_authority("acme", "Server", "name", "ncentral", "admin")
        )
        self.assertFalse(
            self.repository.delete_field_authority("acme", "Server", "name", "ncentral", "admin")
        )

    def test_audit_search_filters_and_redaction_preserve_tenant_boundaries(self):
        self.repository.record_audit_event(
            "acme",
            "admin",
            "configuration_item",
            "asset-1",
            "updated",
            before={"name": "APP01", "apiToken": "old-secret"},
            after={"name": "APP02", "apiToken": "new-secret"},
            reason="Rename approved",
            metadata={"password": "hidden", "ticket": "CHG-1"},
        )
        self.repository.record_audit_event(
            "northwind",
            None,
            "database",
            "database-1",
            "restored",
            severity="warning",
            actor_type="service",
            source_system="recovery",
        )
        acme = self.repository.list_audit_events(
            "acme",
            actor_id="admin",
            category="data",
            action="updated",
            entity_type="configuration_item",
            entity_id="asset-1",
            outcome="success",
            search="APP02",
            limit=1,
        )
        self.assertEqual(len(acme), 1)
        self.assertEqual(acme[0]["before"]["apiToken"], "[redacted]")
        self.assertEqual(acme[0]["metadata"]["password"], "[redacted]")
        self.assertEqual(self.repository.list_audit_events("acme", search="not-found"), [])
        self.assertEqual(len(self.repository.list_audit_events("northwind")), 1)

    def test_state_export_import_is_deep_copied_and_reinitializes_optional_collections(self):
        exported = self.repository.export_state()
        exported["companies"][0]["name"] = "Changed outside repository"
        self.assertEqual(self.state["companies"][0]["name"], "Acme")
        with self.assertRaisesRegex(ValueError, "Customer not found"):
            self.repository.get_company_branding("missing")

        result = self.repository.import_state(
            {
                "companies": [{"id": "new", "name": "New Customer"}],
                "users": [],
                "accessGroups": [],
                "assets": [{"id": "new-asset", "companyId": "new", "name": "CI"}],
                "relationships": [],
                "integrations": [],
                "syncRuns": [],
            },
            "admin",
        )
        self.assertEqual(result, {"companies": 1, "assets": 1, "relationships": 0})
        self.assertEqual(self.repository.list_contacts(), [])
        self.assertIn("auditEvents", self.state)


if __name__ == "__main__":
    unittest.main()
