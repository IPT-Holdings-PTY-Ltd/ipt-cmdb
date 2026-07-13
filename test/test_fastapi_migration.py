import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as core
import backend.main as backend_main
from src.cmdb.repository import StateRepository


api = backend_main.api


class FastApiMigrationTests(unittest.TestCase):
    def setUp(self):
        self.original_db = core.DB
        core.DB = {
            "companies": [
                {"id": "acme", "name": "Acme Manufacturing", "externalIds": {}},
                {"id": "northwind", "name": "Northwind Traders", "externalIds": {}},
            ],
            "users": [
                {"id": "admin", "email": "admin@example.com", "password": "ChangeMe!", "role": "platform_admin", "companyIds": ["*"]},
                {"id": "client", "email": "client@acme.example", "password": "ChangeMe!", "role": "client_reader", "companyIds": ["acme"]},
            ],
            "assets": [
                {"id": "asset-1", "companyId": "acme", "name": "ACME-DC01", "type": "Server", "status": "Active", "source": "manual", "fields": {}},
                {"id": "asset-2", "companyId": "northwind", "name": "NW-DC01", "type": "Server", "status": "Active", "source": "manual", "fields": {}},
            ],
            "relationships": [],
            "changes": [],
            "branding": {},
            "mspBranding": {"name": "CMDB Hub", "accent": "#50d5b9", "logoText": "C"},
            "accessGroups": [{"id": "all-managed-customers", "name": "All managed customers", "companyIds": ["*"], "system": True}],
            "integrations": [
                {"id": "connectwise", "name": "ConnectWise Manage", "type": "connectwise", "enabled": False, "mode": "configured_by_environment", "lastSync": None, "status": "Not configured", "scope": "msp"}
            ],
            "syncRuns": [],
        }
        core.SESSIONS.clear()
        self.save_patcher = patch.object(core, "save_db")
        self.save_patcher.start()
        self.original_repository = backend_main.REPOSITORY
        backend_main.REPOSITORY = StateRepository(core.DB, lambda state: core.save_db(state))
        self.client = TestClient(api)

    def tearDown(self):
        self.save_patcher.stop()
        backend_main.REPOSITORY = self.original_repository
        core.DB = self.original_db
        core.SESSIONS.clear()

    def _login(self, email: str) -> str:
        response = self.client.post("/api/login", json={"email": email, "password": "ChangeMe!"})
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def test_complete_api_is_native_fastapi_without_proxy_route(self):
        paths = {route.path for route in api.routes}
        expected = {
            "/api/login",
            "/api/me",
            "/api/companies",
            "/api/users",
            "/api/assets",
            "/api/assets/{asset_id}",
            "/api/relationships",
            "/api/integrations",
            "/api/changes",
            "/api/changes/{change_id}/pdf",
            "/api/database/restore/preview",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertNotIn("/api/{path:path}", paths)

    def test_openapi_identifies_the_direct_fastapi_surface(self):
        schema = api.openapi()
        self.assertEqual(schema["info"]["title"], "CMDB Hub API")
        self.assertEqual(schema["info"]["version"], "0.3.0")
        self.assertIn("/api/assets", schema["paths"])
        self.assertIn("/api/relationships", schema["paths"])
        self.assertIn("/api/changes/{change_id}/pdf", schema["paths"])

    def test_portable_backup_can_be_previewed_before_import(self):
        token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        backup = self.client.get("/api/database/backup", headers=headers)
        self.assertEqual(backup.status_code, 200)
        self.assertEqual(backup.json()["version"], 2)
        preview = self.client.post("/api/database/restore/preview", headers=headers, json=backup.json())
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["valid"])
        self.assertEqual(preview.json()["companies"], 2)

    def test_msp_branding_has_a_public_identity_and_audited_admin_update(self):
        public = self.client.get("/api/branding/public")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.json()["name"], "CMDB Hub")
        self.assertEqual(public.json()["secondaryAccent"], "#7997ff")

        token = self._login("admin@example.com")
        updated = self.client.put(
            "/api/branding",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "scope": "msp",
                "name": "IPT CMDB",
                "logoText": "IPT",
                "accent": "#4ed477",
                "secondaryAccent": "#5b7cfa",
                "supportEmail": "support@example.com",
                "reportFooter": "IPT Holdings | Controlled document",
                "confidentialityLabel": "Customer confidential",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "IPT CMDB")
        self.assertEqual(core.DB["mspBranding"]["confidentialityLabel"], "Customer confidential")

    def test_customer_asset_list_is_tenant_scoped(self):
        token = self._login("client@acme.example")
        response = self.client.get("/api/assets", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], ["asset-1"])

    def test_customer_cannot_query_another_tenant(self):
        token = self._login("client@acme.example")
        response = self.client.get(
            "/api/assets?companyId=northwind",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_asset_create_and_patch_are_direct_typed_routes(self):
        token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        created = self.client.post(
            "/api/assets",
            headers=headers,
            json={"companyId": "acme", "name": "ACME-APP02", "type": "Server", "fields": {"ip": "10.0.0.22"}},
        )
        self.assertEqual(created.status_code, 201)
        updated = self.client.patch(
            f"/api/assets/{created.json()['id']}",
            headers=headers,
            json={"status": "Retired", "metadata": {"lifecycle": "retired"}},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["metadata"]["lifecycle"], "retired")

    def test_relationship_create_and_delete_are_direct_routes(self):
        token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        core.DB["assets"].append(
            {"id": "asset-3", "companyId": "acme", "name": "ACME-APP01", "type": "Server", "status": "Active", "source": "manual", "fields": {}}
        )
        created = self.client.post(
            "/api/relationships",
            headers=headers,
            json={"fromId": "asset-3", "toId": "asset-1", "type": "depends_on"},
        )
        self.assertEqual(created.status_code, 201)
        deleted = self.client.delete(f"/api/relationships/{created.json()['id']}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["deletedId"], created.json()["id"])

    def test_change_package_uses_repository_and_respects_customer_scope(self):
        client_token = self._login("client@acme.example")
        payload = {
            "companyId": "acme",
            "scopeAssetIds": ["asset-1"],
            "title": "Patch domain controller",
            "reason": "Apply security updates",
            "implementationPlan": "Validate backup and install updates",
            "validationPlan": "Check directory health",
            "rollbackPlan": "Restore the VM snapshot",
        }
        forbidden = self.client.post(
            "/api/changes",
            headers={"Authorization": f"Bearer {client_token}"},
            json=payload,
        )
        self.assertEqual(forbidden.status_code, 403)

        admin_token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin_token}"}
        created = self.client.post("/api/changes", headers=headers, json=payload)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["number"], "CHG-2026-0001")
        listed = self.client.get("/api/changes?companyId=acme", headers=headers)
        self.assertEqual([item["id"] for item in listed.json()], [created.json()["id"]])

    def test_integration_check_is_root_scoped_and_persisted_by_repository(self):
        client_token = self._login("client@acme.example")
        forbidden = self.client.get(
            "/api/integrations",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        self.assertEqual(forbidden.status_code, 403)

        admin_token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin_token}"}
        result = self.client.post("/api/integrations/connectwise/sync", headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["status"], "blocked")
        history = self.client.get("/api/sync-runs", headers=headers)
        self.assertEqual(history.json()[0]["type"], "connectwise")


if __name__ == "__main__":
    unittest.main()
