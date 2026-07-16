import unittest
from io import BytesIO

from pypdf import PdfReader

from src.cmdb.change_control import build_impact_snapshot, create_change_record, preview_change_impact, render_change_pdf


ASSETS = [
    {"id": "db", "companyId": "acme", "name": "DB01", "type": "Server", "source": "ncentral", "metadata": {"criticality": "critical", "environment": "production", "technicalOwner": "Platform Team", "site": "HQ"}},
    {"id": "app", "companyId": "acme", "name": "APP01", "type": "Server", "source": "ncentral", "metadata": {"criticality": "high", "environment": "production", "serviceOwner": "Applications"}},
    {"id": "portal", "companyId": "acme", "name": "Customer Portal", "type": "Software", "source": "manual", "metadata": {"criticality": "high", "environment": "production"}},
    {"id": "sage", "companyId": "acme", "name": "Sage 200", "type": "Business system", "source": "manual", "metadata": {"criticality": "critical", "environment": "production", "businessOwner": "Finance Director", "signoffDelegate": "Financial Controller", "signoffRequired": "yes", "department": "Finance", "userPopulation": "42 users", "rtoHours": "4", "rpoHours": "1"}},
    {"id": "other", "companyId": "northwind", "name": "OTHER", "type": "Server", "source": "ncentral", "metadata": {}},
]

RELATIONSHIPS = [
    {"fromId": "app", "toId": "db", "type": "depends_on"},
    {"fromId": "portal", "toId": "app", "type": "installed_on"},
    {"fromId": "sage", "toId": "app", "type": "depends_on", "impactPolicy": "required"},
]


class ChangeControlTests(unittest.TestCase):
    def test_impact_snapshot_follows_supporting_to_affected_semantics(self):
        snapshot = build_impact_snapshot("acme", ["db"], ASSETS, RELATIONSHIPS)
        self.assertEqual([item["assetId"] for item in snapshot], ["db", "app", "portal", "sage"])
        self.assertEqual(snapshot[1]["role"], "Direct impact")
        self.assertEqual(snapshot[2]["role"], "Downstream impact")
        self.assertEqual(snapshot[2]["relationshipPath"], ["depends_on", "installed_on"])
        self.assertEqual(snapshot[3]["impactSeverity"], "outage")

    def test_impact_preview_flags_risk_and_missing_owners(self):
        preview = preview_change_impact("acme", ["db"], True, ASSETS, RELATIONSHIPS)
        self.assertEqual(preview["summary"]["scopeCount"], 1)
        self.assertEqual(preview["summary"]["missingOwnerCount"], 1)
        self.assertEqual(preview["summary"]["businessSystemCount"], 1)
        self.assertEqual(preview["summary"]["businessOwners"], ["Finance Director"])
        self.assertIn(preview["summary"]["suggestedRisk"]["level"], {"high", "critical"})

    def test_cross_customer_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            build_impact_snapshot("acme", ["other"], ASSETS, RELATIONSHIPS)

    def test_change_record_has_connectwise_ready_external_envelope(self):
        change = self._change()
        self.assertEqual(change["integrationState"]["connectwise"]["status"], "not_published")
        self.assertEqual(change["revision"], 1)
        self.assertEqual(len(change["impactSnapshot"]), 4)

    def test_informational_relationship_does_not_propagate_impact(self):
        relationships = [
            {"fromId": "sage", "toId": "db", "type": "depends_on", "impactPolicy": "informational"},
        ]
        snapshot = build_impact_snapshot("acme", ["db"], ASSETS, relationships)
        self.assertEqual([item["assetId"] for item in snapshot], ["db"])

    def test_single_host_change_uses_cluster_ha_capacity(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["host-1"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "protected")
        self.assertEqual(by_id["erp"]["impactSeverity"], "protected")
        self.assertIn("1 eligible host", by_id["vm-1"]["virtualizationDecision"])
        self.assertEqual(preview["summary"]["protectedVmCount"], 1)
        self.assertIn("protected by verified HA capacity", " ".join(preview["summary"]["suggestedRisk"]["factors"]))

    def test_multi_host_change_exhausts_cluster_failover(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["host-1", "host-2"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "outage")
        self.assertEqual(by_id["erp"]["impactSeverity"], "outage")
        self.assertEqual(preview["summary"]["outageVmCount"], 1)

    def test_shared_datastore_failure_bypasses_compute_ha(self):
        assets, relationships = self._virtualization_fixture()
        preview = preview_change_impact("acme", ["ds-1"], False, assets, relationships)
        by_id = {item["assetId"]: item for item in preview["items"]}
        self.assertEqual(by_id["vm-1"]["impactSeverity"], "outage")
        self.assertEqual(by_id["erp"]["impactSeverity"], "outage")

    def test_pdf_contains_change_and_impact_details(self):
        change = self._change()
        pdf = render_change_pdf(change, {"id": "acme", "name": "Acme Manufacturing"}, {"name": "CMDB Hub", "accent": "#4cc7b1"})
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
        self.assertIn("Database maintenance", text)
        self.assertIn("DB01", text)
        self.assertIn("APP01", text)
        self.assertIn("Sage 200", text)
        self.assertIn("Financial Controller", text)
        self.assertIn("Rollback plan", text)
        self.assertIn("ConnectWise status", text)

    def test_pdf_embeds_msp_logo_and_document_labels(self):
        change = self._change()
        branding = {
            "name": "IPT CMDB",
            "accent": "#4ed477",
            "logoDataUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
            "reportFooter": "IPT Holdings | Controlled document",
            "confidentialityLabel": "Customer confidential",
        }
        reader = PdfReader(BytesIO(render_change_pdf(change, {"id": "acme", "name": "Acme Manufacturing"}, branding)))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertGreater(len(list(reader.pages[0].images)), 0)
        self.assertIn("IPT Holdings | Controlled document", text)
        self.assertIn("Customer confidential", text)

    @staticmethod
    def _change():
        return create_change_record(
            {
                "companyId": "acme",
                "scopeAssetIds": ["db"],
                "title": "Database maintenance",
                "changeType": "normal",
                "category": "database",
                "priority": "high",
                "riskLevel": "",
                "outageExpected": True,
                "plannedStart": "2026-07-15T20:00",
                "plannedEnd": "2026-07-15T21:00",
                "reason": "Apply database security updates.",
                "businessImpact": "The customer portal may be unavailable.",
                "implementationPlan": "1. Validate backups.\n2. Apply updates.\n3. Restart services.",
                "validationPlan": "Confirm database health and complete a portal login test.",
                "rollbackPlan": "Restore the database VM snapshot and validate service recovery.",
                "communicationStatus": "required",
                "communicationPlan": "Notify the service owner before and after the window.",
                "assignedTechnician": "tech@example.com",
                "approver": "CAB",
                "notes": "Generated during automated verification.",
            },
            "CHG-2026-0001",
            "change-1",
            {"id": "acme", "name": "Acme Manufacturing"},
            {"id": "admin", "email": "admin@example.com"},
            ASSETS,
            RELATIONSHIPS,
        )

    @staticmethod
    def _virtualization_fixture():
        assets = [
            {"id": "cluster", "companyId": "acme", "name": "PROD-CL01", "type": "Virtualization cluster", "source": "manual", "metadata": {"haEnabled": "yes", "minimumHosts": "1", "capacityStatus": "sufficient", "operationalStatus": "healthy"}},
            {"id": "host-1", "companyId": "acme", "name": "ESX01", "type": "Hypervisor host", "source": "manual", "metadata": {"powerState": "running", "maintenanceMode": "no", "operationalStatus": "healthy"}},
            {"id": "host-2", "companyId": "acme", "name": "ESX02", "type": "Hypervisor host", "source": "manual", "metadata": {"powerState": "running", "maintenanceMode": "no", "operationalStatus": "healthy"}},
            {"id": "vm-1", "companyId": "acme", "name": "ERP01", "type": "Virtual machine", "source": "manual", "metadata": {"haEnabled": "yes", "mobility": "automatic", "protectionStatus": "protected", "virtualizationPlatform": "VMware vSphere", "clusterName": "PROD-CL01"}},
            {"id": "ds-1", "companyId": "acme", "name": "DATASTORE01", "type": "Datastore", "source": "manual", "metadata": {}},
            {"id": "erp", "companyId": "acme", "name": "ERP", "type": "Business system", "source": "manual", "metadata": {"businessOwner": "Finance"}},
        ]
        relationships = [
            {"fromId": "host-1", "toId": "cluster", "type": "member_of", "impactPolicy": "informational"},
            {"fromId": "host-2", "toId": "cluster", "type": "member_of", "impactPolicy": "informational"},
            {"fromId": "vm-1", "toId": "cluster", "type": "member_of", "impactPolicy": "informational"},
            {"fromId": "host-1", "toId": "vm-1", "type": "hosts", "impactPolicy": "required"},
            {"fromId": "vm-1", "toId": "ds-1", "type": "stored_on", "impactPolicy": "required"},
            {"fromId": "erp", "toId": "vm-1", "type": "depends_on", "impactPolicy": "required"},
        ]
        return assets, relationships


if __name__ == "__main__":
    unittest.main()
