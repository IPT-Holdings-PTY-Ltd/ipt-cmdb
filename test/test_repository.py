import unittest

from src.cmdb.repository import PostgresCmdbRepository, StateRepository, canonical_uuid, hash_password, verify_password


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "companies": [{"id": "acme", "name": "Acme", "externalIds": {}}],
            "users": [{"id": "admin", "email": "admin@example.com", "password": "ChangeMe!", "role": "platform_admin", "companyIds": ["*"]}],
            "accessGroups": [],
            "integrations": [{"id": "connectwise", "name": "ConnectWise Manage", "type": "connectwise", "enabled": False, "status": "Not configured"}],
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
        self.assertIs(PostgresCmdbRepository.create_change, StateRepository._postgres_create_change)

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
            {"id": "reader", "email": "reader@example.com", "role": "client_reader", "companyIds": ["acme"]},
            "VerySecret!42",
            "admin",
        )
        self.assertNotIn("password", created)
        stored = next(item for item in self.state["users"] if item["id"] == "reader")
        self.assertNotIn("password", stored)
        self.assertTrue(verify_password("VerySecret!42", stored["passwordHash"]))

    def test_access_group_changes_are_audited(self):
        group = {"id": "managed", "name": "Managed", "companyIds": ["acme"], "system": False}
        self.repository.create_access_group(group, "admin")
        self.repository.update_access_group("managed", {"name": "Managed customers"}, "admin")
        self.assertTrue(self.repository.delete_access_group("managed", "admin"))
        self.assertEqual([item["action"] for item in self.state["auditEvents"][:3]], ["deleted", "updated", "created"])

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

    def test_asset_write_creates_an_audit_event(self):
        asset = {"id": "asset-1", "companyId": "acme", "name": "APP01", "type": "Server"}
        self.repository.create_asset(asset, "admin")
        self.assertEqual(self.state["auditEvents"][0]["action"], "created")
        self.assertEqual(self.state["auditEvents"][0]["entityId"], "asset-1")
        self.assertEqual(len(self.saved), 1)

    def test_relationship_retirement_is_audited(self):
        relationship = {"id": "rel-1", "fromId": "a", "toId": "b", "type": "depends_on"}
        self.repository.create_relationship(relationship, "acme", "admin")
        self.assertTrue(self.repository.delete_relationship("rel-1", "acme", "admin"))
        self.assertEqual(self.state["relationships"], [])
        self.assertEqual(self.state["auditEvents"][0]["action"], "retired")


if __name__ == "__main__":
    unittest.main()
