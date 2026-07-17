import io
import unittest

from openpyxl import load_workbook
from pypdf import PdfReader

from src.cmdb.reports import (
    build_report,
    render_csv,
    render_pdf,
    render_xlsx,
    report_catalog,
    report_filename,
)


class GovernanceReportTests(unittest.TestCase):
    def setUp(self):
        self.companies = [
            {"id": "acme", "name": "Acme Manufacturing"},
            {"id": "northwind", "name": "Northwind Traders"},
        ]
        self.assets = [
            {
                "id": "sage",
                "companyId": "acme",
                "name": "Sage 200",
                "type": "Business system",
                "status": "Active",
                "source": "manual",
                "lastSeen": "2026-07-17T08:00:00Z",
                "metadata": {
                    "displayLayer": "business",
                    "operationalStatus": "healthy",
                    "lifecycle": "in_service",
                    "criticality": "high",
                    "businessOwner": "Finance",
                    "serviceOwner": "Applications",
                    "technicalOwner": "Platform team",
                    "rtoHours": 4,
                    "rpoHours": 1,
                },
            },
            {
                "id": "database",
                "companyId": "acme",
                "name": "SQL01",
                "type": "Database",
                "status": "Active",
                "source": "ncentral",
                "metadata": {
                    "displayLayer": "data",
                    "operationalStatus": "healthy",
                    "lifecycle": "maintenance",
                    "criticality": "critical",
                    "serviceOwner": "Applications",
                    "technicalOwner": "Database team",
                    "renewalDate": "2026-08-01",
                    "warrantyEnd": "2026-09-01",
                    "endOfLifeDate": "2027-01-01",
                    "reviewDate": "2026-07-31",
                },
            },
            {
                "id": "orphan",
                "companyId": "acme",
                "name": "APP02",
                "type": "Server",
                "status": "Active",
                "source": "manual",
                "metadata": {"criticality": "medium", "lifecycle": "in_service"},
            },
            {
                "id": "northwind-server",
                "companyId": "northwind",
                "name": "NW-APP01",
                "type": "Server",
                "status": "Active",
                "source": "manual",
                "metadata": {
                    "serviceOwner": "Northwind IT",
                    "technicalOwner": "Northwind IT",
                },
            },
        ]
        self.relationships = [
            {"id": "rel-1", "fromId": "sage", "toId": "database", "type": "depends_on"}
        ]
        self.changes = [
            {
                "id": "change-1",
                "companyId": "acme",
                "number": "CHG-2026-0001",
                "title": "Patch SQL",
                "status": "approved",
                "riskLevel": "high",
                "plannedStart": "2026-07-20T18:00:00Z",
                "assignedTechnician": "Alex Tech",
                "approver": "Finance",
                "impactSnapshot": [{"assetId": "sage"}, {"assetId": "database"}],
            },
            {
                "id": "change-2",
                "companyId": "northwind",
                "number": "CHG-2026-0002",
                "title": "Other customer change",
                "impactSnapshot": [],
            },
        ]
        self.users = [
            {
                "email": "admin@example.com",
                "role": "platform_admin",
                "accountType": "root",
                "companyIds": ["*"],
                "groupIds": ["operations"],
            }
        ]
        self.integrations = [
            {
                "name": "ConnectWise Manage",
                "type": "connectwise",
                "enabled": True,
                "status": "Healthy",
            },
            {
                "name": "N-central",
                "type": "ncentral",
                "enabled": False,
                "status": "Not configured",
            },
        ]
        self.sync_runs = [
            {
                "type": "connectwise",
                "finishedAt": "2026-07-17T08:00:00Z",
                "status": "success",
                "message": "100 companies discovered",
            },
            {
                "type": "connectwise",
                "finishedAt": "2026-07-16T08:00:00Z",
                "status": "failed",
                "message": "Older failure",
            },
        ]
        self.audit_events = [
            {
                "companyId": "acme",
                "createdAt": "2026-07-17T08:00:00Z",
                "actorLabel": "admin@example.com",
                "category": "data",
                "action": "updated",
                "entityName": "SQL01",
                "outcome": "success",
                "sourceSystem": "web",
                "correlationId": "request-1",
            },
            {
                "companyId": "northwind",
                "createdAt": "2026-07-17T09:00:00Z",
                "actorLabel": "other@example.com",
                "category": "data",
                "action": "deleted",
                "entityName": "NW-APP01",
                "outcome": "success",
                "sourceSystem": "web",
                "correlationId": "request-2",
            },
        ]

    def build(self, report_id, company_id="acme"):
        return build_report(
            report_id,
            companies=self.companies,
            assets=self.assets,
            relationships=self.relationships,
            changes=self.changes,
            users=self.users,
            integrations=self.integrations,
            sync_runs=self.sync_runs,
            audit_events=self.audit_events,
            company_id=company_id,
        )

    def test_catalog_hides_root_only_reports_at_customer_scope(self):
        customer_ids = {item["id"] for item in report_catalog(False)}
        root_ids = {item["id"] for item in report_catalog(True)}
        self.assertNotIn("access-review", customer_ids)
        self.assertNotIn("integration-health", customer_ids)
        self.assertIn("access-review", root_ids)
        self.assertTrue(all("rootOnly" not in item for item in report_catalog(True)))

    def test_asset_lifecycle_and_ownership_reports_are_tenant_scoped(self):
        assets = self.build("asset-register")
        self.assertEqual(assets["summary"], {"rowCount": 3, "customerCount": 1})
        self.assertEqual(assets["rows"][0]["owner"], "Finance")
        self.assertNotIn("NW-APP01", {row["name"] for row in assets["rows"]})

        lifecycle = self.build("lifecycle-attention")
        self.assertEqual([row["name"] for row in lifecycle["rows"]], ["SQL01"])
        self.assertEqual(lifecycle["rows"][0]["renewal"], "2026-08-01")

        ownership = self.build("ownership-gaps")
        self.assertEqual([row["name"] for row in ownership["rows"]], ["APP02"])
        self.assertEqual(ownership["rows"][0]["missing"], ["service owner", "technical owner"])

    def test_business_change_and_audit_reports_calculate_impact(self):
        services = self.build("business-services")
        self.assertEqual(services["rows"][0]["supportingCIs"], 1)
        self.assertEqual(services["rows"][0]["rto"], 4)

        changes = self.build("change-register")
        self.assertEqual(changes["rows"][0]["impact"], 2)
        self.assertEqual(changes["rows"][0]["number"], "CHG-2026-0001")

        activity = self.build("audit-activity")
        self.assertEqual([row["entity"] for row in activity["rows"]], ["SQL01"])
        self.assertEqual(activity["rows"][0]["customer"], "Acme Manufacturing")

    def test_root_access_and_integration_reports_normalize_values(self):
        access = self.build("access-review", company_id=None)
        self.assertEqual(access["rows"][0]["customers"], "All customers")
        self.assertEqual(access["rows"][0]["groups"], ["operations"])

        health = self.build("integration-health", company_id=None)
        self.assertEqual(health["rows"][0]["runStatus"], "success")
        self.assertEqual(health["rows"][1]["runStatus"], "Never run")
        csv_text = render_csv(health).decode("utf-8-sig")
        self.assertIn("ConnectWise Manage,connectwise,Yes", csv_text)

    def test_unknown_report_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown report"):
            self.build("not-a-report")

    def test_csv_xlsx_and_pdf_exports_are_branded_and_readable(self):
        report = self.build("asset-register")
        branding = {"name": "IPT CMDB", "accent": "#50d5b9"}

        csv_bytes = render_csv(report)
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Acme Manufacturing,Sage 200", csv_bytes.decode("utf-8-sig"))

        workbook = load_workbook(io.BytesIO(render_xlsx(report, branding)))
        sheet = workbook.active
        self.assertEqual(sheet["A1"].value, "IPT CMDB")
        self.assertEqual(sheet["B1"].value, "Asset register")
        self.assertEqual(sheet.freeze_panes, "A5")
        self.assertEqual(sheet["A4"].value, "Customer")

        pdf = render_pdf(report, branding)
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn("IPT CMDB", pdf_text)
        self.assertIn("Asset register", pdf_text)
        self.assertEqual(
            report_filename(report, "pdf"),
            f"asset-register-{report['generatedAt'][:10]}.pdf",
        )


if __name__ == "__main__":
    unittest.main()
