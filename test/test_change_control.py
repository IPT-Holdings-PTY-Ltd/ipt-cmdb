import unittest
from io import BytesIO

from pypdf import PdfReader

from src.cmdb.change_control import build_impact_snapshot, create_change_record, preview_change_impact, render_change_pdf


ASSETS = [
    {"id": "db", "companyId": "acme", "name": "DB01", "type": "Server", "source": "ncentral", "metadata": {"criticality": "critical", "environment": "production", "technicalOwner": "Platform Team", "site": "HQ"}},
    {"id": "app", "companyId": "acme", "name": "APP01", "type": "Server", "source": "ncentral", "metadata": {"criticality": "high", "environment": "production", "serviceOwner": "Applications"}},
    {"id": "portal", "companyId": "acme", "name": "Customer Portal", "type": "Software", "source": "manual", "metadata": {"criticality": "high", "environment": "production"}},
    {"id": "other", "companyId": "northwind", "name": "OTHER", "type": "Server", "source": "ncentral", "metadata": {}},
]

RELATIONSHIPS = [
    {"fromId": "app", "toId": "db", "type": "depends_on"},
    {"fromId": "portal", "toId": "app", "type": "installed_on"},
]


class ChangeControlTests(unittest.TestCase):
    def test_impact_snapshot_follows_supporting_to_affected_semantics(self):
        snapshot = build_impact_snapshot("acme", ["db"], ASSETS, RELATIONSHIPS)
        self.assertEqual([item["assetId"] for item in snapshot], ["db", "app", "portal"])
        self.assertEqual(snapshot[1]["role"], "Direct impact")
        self.assertEqual(snapshot[2]["role"], "Downstream impact")
        self.assertEqual(snapshot[2]["relationshipPath"], ["depends_on", "installed_on"])

    def test_impact_preview_flags_risk_and_missing_owners(self):
        preview = preview_change_impact("acme", ["db"], True, ASSETS, RELATIONSHIPS)
        self.assertEqual(preview["summary"]["scopeCount"], 1)
        self.assertEqual(preview["summary"]["missingOwnerCount"], 1)
        self.assertIn(preview["summary"]["suggestedRisk"]["level"], {"high", "critical"})

    def test_cross_customer_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            build_impact_snapshot("acme", ["other"], ASSETS, RELATIONSHIPS)

    def test_change_record_has_connectwise_ready_external_envelope(self):
        change = self._change()
        self.assertEqual(change["integrationState"]["connectwise"]["status"], "not_published")
        self.assertEqual(change["revision"], 1)
        self.assertEqual(len(change["impactSnapshot"]), 3)

    def test_pdf_contains_change_and_impact_details(self):
        change = self._change()
        pdf = render_change_pdf(change, {"id": "acme", "name": "Acme Manufacturing"}, {"name": "CMDB Hub", "accent": "#4cc7b1"})
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
        self.assertIn("Database maintenance", text)
        self.assertIn("DB01", text)
        self.assertIn("APP01", text)
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


if __name__ == "__main__":
    unittest.main()
