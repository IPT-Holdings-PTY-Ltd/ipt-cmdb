import unittest
from datetime import date
from app import allowed, asset_metadata, attention_items, backup_document, can_manage, customer_overview, normalise_metadata, restore_backup

class CompanyScopeTests(unittest.TestCase):
    def test_client_is_limited_to_its_assigned_company(self):
        client = {"role": "client_reader", "companyIds": ["acme"]}
        self.assertTrue(allowed(client, "acme"))
        self.assertFalse(allowed(client, "northwind"))

    def test_platform_admin_has_global_scope(self):
        self.assertTrue(allowed({"role": "platform_admin", "companyIds": []}, "northwind"))

    def test_client_cannot_manage_their_company(self):
        self.assertFalse(can_manage({"role": "client_reader", "companyIds": ["acme"]}, "acme"))

    def test_legacy_asset_receives_safe_itil_metadata_defaults(self):
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
        self.assertEqual(backup["version"], 1)
        self.assertIn("assets", backup["state"])

    def test_restore_rejects_backup_without_platform_admin(self):
        invalid = {"format": "cmdb-hub-backup", "version": 1, "state": {"companies": [], "users": [], "assets": [], "relationships": [], "integrations": [], "syncRuns": []}}
        with self.assertRaises(ValueError): restore_backup(invalid)

if __name__ == "__main__": unittest.main()
