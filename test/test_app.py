import unittest
from datetime import date
from app import allowed, asset_metadata, attention_items, backup_document, build_seed_state, can_manage, customer_overview, normalise_metadata, preview_backup, relationship_exists, restore_backup, would_create_dependency_cycle

class CompanyScopeTests(unittest.TestCase):
    def test_client_is_limited_to_its_assigned_company(self):
        client = {"role": "client_reader", "companyIds": ["acme"]}
        self.assertTrue(allowed(client, "acme"))
        self.assertFalse(allowed(client, "northwind"))

    def test_platform_admin_has_global_scope(self):
        self.assertTrue(allowed({"role": "platform_admin", "companyIds": []}, "northwind"))

    def test_client_cannot_manage_their_company(self):
        self.assertFalse(can_manage({"role": "client_reader", "companyIds": ["acme"]}, "acme"))

    def test_asset_without_metadata_receives_safe_itil_defaults(self):
        metadata = asset_metadata({"status": "Active"})
        self.assertEqual(metadata["lifecycle"], "in_service")
        self.assertEqual(metadata["operationalStatus"], "healthy")

    def test_retired_status_forces_retired_lifecycle(self):
        metadata = normalise_metadata({"lifecycle": "in_service"}, "Retired")
        self.assertEqual(metadata["lifecycle"], "retired")

    def test_metadata_rejects_non_object_values(self):
        with self.assertRaises(ValueError): normalise_metadata("not a metadata object")

    def test_metadata_includes_a_distinct_subscription_renewal_date(self):
        metadata = normalise_metadata({"renewalDate": "2027-07-10"})
        self.assertEqual(metadata["renewalDate"], "2027-07-10")

    def test_metadata_includes_vendor_end_of_life_date(self):
        metadata = normalise_metadata({"endOfLifeDate": "2028-01-01"})
        self.assertEqual(metadata["endOfLifeDate"], "2028-01-01")

    def test_attention_items_include_overdue_and_customer_context(self):
        assets = [{"id": "ci-1", "companyId": "acme", "name": "Renewal CI", "type": "Software", "metadata": {"renewalDate": "2026-07-01", "technicalOwner": "MSP Licensing"}}]
        items = attention_items(assets, [{"id": "acme", "name": "Acme Manufacturing"}], today=date(2026, 7, 10))
        self.assertEqual(items[0]["days"], -9)
        self.assertEqual(items[0]["companyName"], "Acme Manufacturing")

    def test_customer_overview_includes_customers_without_attention_items(self):
        companies = [{"id": "acme", "name": "Acme"}, {"id": "northwind", "name": "Northwind"}]
        overview = customer_overview(companies, [{"id": "ci-1", "companyId": "acme", "name": "CI", "type": "Server", "metadata": {"criticality": "critical"}}])
        self.assertEqual(overview[0]["assetCount"], 1)
        self.assertEqual(overview[0]["criticalCount"], 1)
        self.assertEqual(overview[1]["assetCount"], 0)

    def test_backup_document_has_versioned_portable_shape(self):
        backup = backup_document()
        self.assertEqual(backup["format"], "cmdb-hub-backup")
        self.assertEqual(backup["version"], 2)
        self.assertIn("assets", backup["state"])
        self.assertTrue(backup["checksum"].startswith("sha256:"))
        self.assertIn("canonical audit history", backup["excluded"])

    def test_empty_seed_keeps_platform_admin_without_demo_tenants(self):
        state = build_seed_state("empty")
        self.assertEqual(state["companies"], [])
        self.assertEqual(state["assets"], [])
        self.assertTrue(any(user["role"] == "platform_admin" for user in state["users"]))

    def test_portable_backup_preview_detects_tampering(self):
        backup = backup_document()
        preview = preview_backup(backup)
        self.assertTrue(preview["valid"])
        backup["state"]["assets"].append({"id": "tampered"})
        with self.assertRaises(ValueError):
            preview_backup(backup)

    def test_restore_rejects_backup_without_platform_admin(self):
        invalid = {"format": "cmdb-hub-backup", "version": 1, "state": {"companies": [], "users": [], "assets": [], "relationships": [], "integrations": [], "syncRuns": []}}
        with self.assertRaises(ValueError): restore_backup(invalid)

    def test_symmetric_relationship_rejects_reverse_duplicate(self):
        relationships = [{"fromId": "switch", "toId": "server", "type": "connected_to"}]
        self.assertTrue(relationship_exists(relationships, "server", "switch", "connected_to"))

    def test_directional_relationship_allows_reverse_edge(self):
        relationships = [{"fromId": "app", "toId": "database", "type": "depends_on"}]
        self.assertFalse(relationship_exists(relationships, "database", "app", "depends_on"))

    def test_dependency_cycle_is_detected(self):
        relationships = [
            {"fromId": "app", "toId": "database", "type": "depends_on"},
            {"fromId": "portal", "toId": "app", "type": "depends_on"},
        ]
        self.assertTrue(would_create_dependency_cycle(relationships, "database", "portal"))
        self.assertFalse(would_create_dependency_cycle(relationships, "reporting", "database"))

if __name__ == "__main__": unittest.main()
