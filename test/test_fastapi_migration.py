import base64
import json
import os
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
        self.original_database_mode = core.DATABASE_MODE
        core.DATABASE_MODE = "local development state"
        core.DB = {
            "companies": [
                {"id": "acme", "name": "Acme Manufacturing", "externalIds": {}},
                {"id": "northwind", "name": "Northwind Traders", "externalIds": {}},
            ],
            "users": [
                {
                    "id": "admin",
                    "email": "admin@example.com",
                    "password": "ChangeMe!",
                    "role": "platform_admin",
                    "companyIds": ["*"],
                },
                {
                    "id": "client",
                    "email": "client@acme.example",
                    "password": "ChangeMe!",
                    "role": "client_reader",
                    "companyIds": ["acme"],
                },
                {
                    "id": "operator",
                    "email": "operator@example.com",
                    "password": "ChangeMe!",
                    "role": "msp_operator",
                    "companyIds": ["acme"],
                },
            ],
            "assets": [
                {
                    "id": "asset-1",
                    "companyId": "acme",
                    "name": "ACME-DC01",
                    "type": "Server",
                    "status": "Active",
                    "source": "manual",
                    "fields": {},
                },
                {
                    "id": "asset-2",
                    "companyId": "northwind",
                    "name": "NW-DC01",
                    "type": "Server",
                    "status": "Active",
                    "source": "manual",
                    "fields": {},
                },
            ],
            "relationships": [],
            "changes": [],
            "branding": {},
            "mspBranding": {"name": "CMDB Hub", "accent": "#50d5b9", "logoText": "C"},
            "accessGroups": [
                {
                    "id": "all-managed-customers",
                    "name": "All managed customers",
                    "companyIds": ["*"],
                    "system": True,
                }
            ],
            "integrations": [
                {
                    "id": "connectwise",
                    "name": "ConnectWise Manage",
                    "type": "connectwise",
                    "enabled": False,
                    "mode": "configured_by_environment",
                    "lastSync": None,
                    "status": "Not configured",
                    "scope": "msp",
                }
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
        core.DATABASE_MODE = self.original_database_mode
        core.SESSIONS.clear()

    def _login(self, email: str, password: str = "ChangeMe!") -> str:
        response = self.client.post("/api/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def _headers(self, email: str, password: str = "ChangeMe!") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._login(email, password)}"}

    def test_authorization_matrix_enforces_customer_read_scope(self):
        routes = [
            "/api/dashboard?companyId={company}",
            "/api/assets?companyId={company}",
            "/api/contacts?companyId={company}",
            "/api/relationships?companyId={company}",
            "/api/changes?companyId={company}",
            "/api/data-quality?companyId={company}",
            "/api/reconciliation-candidates?companyId={company}",
            "/api/field-authority?companyId={company}",
            "/api/audit-events?companyId={company}",
            "/api/reports/catalog?companyId={company}",
        ]
        roles = {
            "platform_admin": ("admin@example.com", 200, 200),
            "msp_operator": ("operator@example.com", 200, 403),
            "client_reader": ("client@acme.example", 200, 403),
        }

        for role, (email, own_status, other_status) in roles.items():
            headers = self._headers(email)
            for route in routes:
                with self.subTest(role=role, route=route, company="acme"):
                    self.assertEqual(
                        self.client.get(route.format(company="acme"), headers=headers).status_code,
                        own_status,
                    )
                with self.subTest(role=role, route=route, company="northwind"):
                    self.assertEqual(
                        self.client.get(
                            route.format(company="northwind"), headers=headers
                        ).status_code,
                        other_status,
                    )

        for email, expected_ids in {
            "admin@example.com": {"asset-1", "asset-2"},
            "operator@example.com": {"asset-1"},
            "client@acme.example": {"asset-1"},
        }.items():
            with self.subTest(email=email, route="unfiltered asset inventory"):
                response = self.client.get("/api/assets", headers=self._headers(email))
                self.assertEqual(response.status_code, 200)
                self.assertEqual({item["id"] for item in response.json()}, expected_ids)

    def test_authorization_matrix_enforces_customer_write_scope(self):
        cases = [
            ("admin@example.com", "acme", 201),
            ("admin@example.com", "northwind", 201),
            ("operator@example.com", "acme", 201),
            ("operator@example.com", "northwind", 403),
            ("client@acme.example", "acme", 403),
            ("client@acme.example", "northwind", 403),
        ]
        for index, (email, company_id, expected_status) in enumerate(cases):
            with self.subTest(email=email, company=company_id):
                response = self.client.post(
                    "/api/assets",
                    headers=self._headers(email),
                    json={
                        "companyId": company_id,
                        "name": f"AUTH-MATRIX-{index}",
                        "type": "Server",
                    },
                )
                self.assertEqual(response.status_code, expected_status)

        for email in ("operator@example.com", "client@acme.example"):
            with self.subTest(email=email, route="cross-customer asset detail"):
                response = self.client.get("/api/assets/asset-2", headers=self._headers(email))
                self.assertEqual(response.status_code, 403)

    def test_authorization_matrix_separates_root_and_platform_admin_routes(self):
        root_routes = [
            "/api/dashboard",
            "/api/root-overview",
            "/api/root-attention",
            "/api/contacts",
            "/api/access-groups",
            "/api/users",
            "/api/integrations",
            "/api/sync-runs",
            "/api/audit-events",
            "/api/reports/catalog",
        ]
        for role, email, expected_status in (
            ("platform_admin", "admin@example.com", 200),
            ("msp_operator", "operator@example.com", 200),
            ("client_reader", "client@acme.example", 403),
        ):
            headers = self._headers(email)
            for route in root_routes:
                with self.subTest(role=role, route=route):
                    self.assertEqual(
                        self.client.get(route, headers=headers).status_code,
                        expected_status,
                    )

        admin_only_routes = ["/api/database/status", "/api/rbac/roles"]
        for role, email, expected_status in (
            ("platform_admin", "admin@example.com", 200),
            ("msp_operator", "operator@example.com", 403),
            ("client_reader", "client@acme.example", 403),
        ):
            headers = self._headers(email)
            for route in admin_only_routes:
                with self.subTest(role=role, route=route):
                    self.assertEqual(
                        self.client.get(route, headers=headers).status_code,
                        expected_status,
                    )

        operator_groups = self.client.get(
            "/api/access-groups", headers=self._headers("operator@example.com")
        )
        self.assertEqual(operator_groups.status_code, 200)
        self.assertEqual(operator_groups.json()[0]["companyIds"], ["acme"])

    def test_protected_mutations_authenticate_before_resource_lookup(self):
        create = self.client.post(
            "/api/relationships",
            json={"fromId": "missing-a", "toId": "missing-b", "type": "depends_on"},
        )
        delete = self.client.delete("/api/relationships/missing")
        self.assertEqual(create.status_code, 401)
        self.assertEqual(delete.status_code, 401)

    def test_authorization_matrix_controls_customer_and_user_administration(self):
        admin_headers = self._headers("admin@example.com")
        root_user = self.client.post(
            "/api/users",
            headers=admin_headers,
            json={
                "email": "all-customers@example.com",
                "password": "Temporary!42",
                "accountType": "root",
                "groupIds": ["all-managed-customers"],
            },
        )
        self.assertEqual(root_user.status_code, 201)
        self.assertEqual(root_user.json()["role"], "msp_operator")
        self.assertEqual(root_user.json()["companyIds"], ["acme", "northwind"])

        created_company = self.client.post(
            "/api/companies",
            headers=admin_headers,
            json={"name": "Contoso Services", "slug": "contoso"},
        )
        self.assertEqual(created_company.status_code, 201)
        dynamic_scope = self.client.get(
            "/api/companies",
            headers=self._headers("all-customers@example.com", "Temporary!42"),
        )
        self.assertEqual(
            {item["id"] for item in dynamic_scope.json()}, {"acme", "contoso", "northwind"}
        )

        for email in ("operator@example.com", "client@acme.example"):
            with self.subTest(email=email, action="create customer"):
                denied = self.client.post(
                    "/api/companies",
                    headers=self._headers(email),
                    json={"name": f"Blocked {email}", "slug": f"blocked-{email.split('@')[0]}"},
                )
                self.assertEqual(denied.status_code, 403)

        operator_headers = self._headers("operator@example.com")
        own_customer_user = self.client.post(
            "/api/users",
            headers=operator_headers,
            json={
                "email": "new-reader@acme.example",
                "password": "Temporary!42",
                "accountType": "customer",
                "companyId": "acme",
            },
        )
        self.assertEqual(own_customer_user.status_code, 201)
        self.assertEqual(own_customer_user.json()["companyIds"], ["acme"])
        denied_other_customer = self.client.post(
            "/api/users",
            headers=operator_headers,
            json={
                "email": "blocked-reader@northwind.example",
                "password": "Temporary!42",
                "accountType": "customer",
                "companyId": "northwind",
            },
        )
        self.assertEqual(denied_other_customer.status_code, 403)

        expected_companies = {
            "admin@example.com": {"acme", "contoso", "northwind"},
            "operator@example.com": {"acme"},
            "client@acme.example": {"acme"},
        }
        for email, expected_ids in expected_companies.items():
            with self.subTest(email=email, action="list permitted customers"):
                response = self.client.get("/api/companies", headers=self._headers(email))
                self.assertEqual(response.status_code, 200)
                self.assertEqual({item["id"] for item in response.json()}, expected_ids)

    def test_complete_api_is_native_fastapi_without_proxy_route(self):
        paths = {route.path for route in api.routes}
        expected = {
            "/api/login",
            "/api/me",
            "/api/companies",
            "/api/users",
            "/api/contacts",
            "/api/assets",
            "/api/assets/{asset_id}",
            "/api/relationships",
            "/api/integrations",
            "/api/changes",
            "/api/changes/{change_id}/pdf",
            "/api/dashboard",
            "/api/database/restore/preview",
        }
        self.assertTrue(expected.issubset(paths))
        self.assertNotIn("/api/{path:path}", paths)

    def test_dashboard_is_role_and_customer_scoped(self):
        client_token = self._login("client@acme.example")
        client_headers = {"Authorization": f"Bearer {client_token}"}
        root_forbidden = self.client.get("/api/dashboard", headers=client_headers)
        self.assertEqual(root_forbidden.status_code, 403)
        customer = self.client.get("/api/dashboard?companyId=acme", headers=client_headers)
        self.assertEqual(customer.status_code, 200)
        self.assertEqual(customer.json()["scope"], "customer")
        self.assertEqual(customer.json()["summary"]["assets"], 1)

        admin_token = self._login("admin@example.com")
        root = self.client.get("/api/dashboard", headers={"Authorization": f"Bearer {admin_token}"})
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["scope"], "msp")
        self.assertEqual(root.json()["summary"]["customers"], 2)

    def test_openapi_identifies_the_direct_fastapi_surface(self):
        schema = api.openapi()
        self.assertEqual(schema["info"]["title"], "CMDB Hub API")
        self.assertEqual(schema["info"]["version"], "0.4.0")
        self.assertIn("/api/assets", schema["paths"])
        self.assertIn("/api/contacts", schema["paths"])
        self.assertIn("/api/relationships", schema["paths"])
        self.assertIn("/api/changes/{change_id}/pdf", schema["paths"])
        self.assertIn("/api/audit-events", schema["paths"])
        self.assertIn("/api/reports/{report_id}/download", schema["paths"])
        self.assertIn("/api/data-quality", schema["paths"])

    def test_audit_center_is_tenant_scoped_and_contains_attribution(self):
        admin = self._login("admin@example.com")
        headers = {
            "Authorization": f"Bearer {admin}",
            "X-Correlation-ID": "change-4451",
        }
        created = self.client.post(
            "/api/assets",
            headers=headers,
            json={
                "companyId": "acme",
                "name": "ACME-AUDIT-01",
                "type": "Server",
                "status": "Active",
            },
        )
        self.assertEqual(created.status_code, 201)
        events = self.client.get(
            f"/api/audit-events?companyId=acme&entityId={created.json()['id']}",
            headers=headers,
        )
        self.assertEqual(events.status_code, 200)
        event = events.json()[0]
        self.assertEqual(event["actorLabel"], "admin@example.com")
        self.assertEqual(event["correlationId"], "change-4451")
        self.assertEqual(event["entityName"], "ACME-AUDIT-01")
        self.assertTrue(any(change["field"] == "name" for change in event["changes"]))

        client = self._login("client@acme.example")
        client_headers = {"Authorization": f"Bearer {client}"}
        visible = self.client.get("/api/audit-events?companyId=acme", headers=client_headers)
        self.assertEqual(visible.status_code, 200)
        self.assertTrue(all(item.get("companyId") == "acme" for item in visible.json()))
        self.assertEqual(
            self.client.get(
                "/api/audit-events?companyId=northwind", headers=client_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/audit-events", headers=client_headers).status_code,
            403,
        )

    def test_authentication_events_never_capture_passwords(self):
        failed = self.client.post(
            "/api/login",
            json={"email": "admin@example.com", "password": "incorrect-secret"},
        )
        self.assertEqual(failed.status_code, 401)
        event = core.DB["auditEvents"][0]
        self.assertEqual(event["action"], "login_failed")
        self.assertEqual(event["outcome"], "failed")
        self.assertNotIn("incorrect-secret", json.dumps(event))

    def test_governance_reports_preview_and_download_in_customer_scope(self):
        client = self._login("client@acme.example")
        headers = {"Authorization": f"Bearer {client}"}
        catalog = self.client.get("/api/reports/catalog?companyId=acme", headers=headers)
        self.assertEqual(catalog.status_code, 200)
        self.assertIn("asset-register", {item["id"] for item in catalog.json()})
        self.assertNotIn("access-review", {item["id"] for item in catalog.json()})
        preview = self.client.get(
            "/api/reports/asset-register/preview?companyId=acme", headers=headers
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["summary"]["rowCount"], 1)
        self.assertEqual(preview.json()["rows"][0]["customer"], "Acme Manufacturing")
        export = self.client.get(
            "/api/reports/asset-register/download?companyId=acme&format=csv",
            headers=headers,
        )
        self.assertEqual(export.status_code, 200)
        self.assertIn("text/csv", export.headers["content-type"])
        self.assertIn("ACME-DC01", export.content.decode("utf-8-sig"))
        xlsx = self.client.get(
            "/api/reports/asset-register/download?companyId=acme&format=xlsx",
            headers=headers,
        )
        self.assertEqual(xlsx.status_code, 200)
        self.assertTrue(xlsx.content.startswith(b"PK"))
        pdf = self.client.get(
            "/api/reports/asset-register/download?companyId=acme&format=pdf",
            headers=headers,
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_data_quality_is_tenant_scoped_and_exceptions_are_audited(self):
        client = self._login("client@acme.example")
        client_headers = {"Authorization": f"Bearer {client}"}
        visible = self.client.get("/api/data-quality?companyId=acme", headers=client_headers)
        self.assertEqual(visible.status_code, 200)
        self.assertEqual({item["companyId"] for item in visible.json()["findings"]}, {"acme"})
        self.assertEqual(
            self.client.get(
                "/api/data-quality?companyId=northwind", headers=client_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/data-quality", headers=client_headers).status_code,
            403,
        )
        denied = self.client.post(
            "/api/data-quality/exceptions",
            headers=client_headers,
            json={
                "companyId": "acme",
                "ruleKey": "missing_owner",
                "entityId": "asset-1",
                "reason": "Approved temporary exception",
            },
        )
        self.assertEqual(denied.status_code, 403)

        admin = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin}"}
        created = self.client.post(
            "/api/data-quality/exceptions",
            headers=headers,
            json={
                "companyId": "acme",
                "ruleKey": "missing_owner",
                "entityId": "asset-1",
                "reason": "Owner assignment is in progress",
            },
        )
        self.assertEqual(created.status_code, 201)
        refreshed = self.client.get("/api/data-quality?companyId=acme", headers=headers)
        self.assertNotIn(
            "missing_owner", {item["ruleKey"] for item in refreshed.json()["findings"]}
        )
        self.assertEqual(core.DB["auditEvents"][0]["entityType"], "data_quality_exception")

    def test_source_authority_is_customer_scoped_and_audited(self):
        token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        saved = self.client.put(
            "/api/field-authority",
            headers=headers,
            json={
                "companyId": "acme",
                "ciType": "Server",
                "fieldName": "display_name",
                "provider": "ncentral",
                "priority": 10,
            },
        )
        self.assertEqual(saved.status_code, 200)
        rules = self.client.get("/api/field-authority?companyId=acme", headers=headers)
        self.assertEqual(rules.json()[0]["priority"], 10)
        self.assertEqual(core.DB["auditEvents"][0]["entityType"], "field_authority")

    def test_easy_auth_is_container_configured_and_does_not_fall_back_to_passwords(
        self,
    ):
        claims = {
            "claims": [
                {"typ": "preferred_username", "val": "admin@example.com"},
            ]
        }
        principal = base64.b64encode(json.dumps(claims).encode("utf-8")).decode("ascii")
        with patch.dict(os.environ, {"AUTH_MODE": "easy_auth", "ALLOW_LOCAL_BREAK_GLASS": "false"}):
            config = self.client.get("/api/auth/config")
            self.assertEqual(config.status_code, 200)
            self.assertTrue(config.json()["external"])
            self.assertFalse(config.json()["localLoginEnabled"])
            me = self.client.get("/api/me", headers={"x-ms-client-principal": principal})
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["email"], "admin@example.com")
            local = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            )
            self.assertEqual(local.status_code, 403)

    def test_easy_auth_principal_name_header_is_configurable(self):
        with patch.dict(
            os.environ,
            {
                "AUTH_MODE": "easy_auth",
                "ALLOW_LOCAL_BREAK_GLASS": "false",
                "ENTRA_PRINCIPAL_NAME_HEADER": "x-authenticated-email",
            },
        ):
            response = self.client.get(
                "/api/me", headers={"x-authenticated-email": "admin@example.com"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["role"], "platform_admin")

    def test_portable_backup_can_be_previewed_before_import(self):
        token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        backup = self.client.get("/api/database/backup", headers=headers)
        self.assertEqual(backup.status_code, 200)
        self.assertEqual(backup.json()["version"], 2)
        preview = self.client.post(
            "/api/database/restore/preview", headers=headers, json=backup.json()
        )
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

    def test_customer_branding_uses_repository_and_respects_customer_scope(self):
        admin = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin}"}
        updated = self.client.put(
            "/api/branding",
            headers=headers,
            json={
                "scope": "customer",
                "companyId": "acme",
                "name": "Acme Portal",
                "logoText": "AC",
                "accent": "#123456",
                "secondaryAccent": "#654321",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["secondaryAccent"], "#654321")

        client = self._login("client@acme.example")
        visible = self.client.get(
            "/api/branding?companyId=acme",
            headers={"Authorization": f"Bearer {client}"},
        )
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json()["name"], "Acme Portal")
        blocked = self.client.get(
            "/api/branding?companyId=northwind",
            headers={"Authorization": f"Bearer {client}"},
        )
        self.assertEqual(blocked.status_code, 403)

    def test_customer_asset_list_is_tenant_scoped(self):
        token = self._login("client@acme.example")
        response = self.client.get("/api/assets", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()], ["asset-1"])

    def test_contacts_are_separate_from_logins_and_drive_structured_asset_ownership(
        self,
    ):
        admin = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin}"}
        created = self.client.post(
            "/api/contacts",
            headers=headers,
            json={
                "companyId": "acme",
                "displayName": "Jane Owner",
                "firstName": "Jane",
                "lastName": "Owner",
                "email": "jane.owner@acme.example",
                "jobTitle": "Finance Director",
                "department": "Finance",
                "status": "active",
            },
        )
        self.assertEqual(created.status_code, 201)
        contact_id = created.json()["id"]
        self.assertIsNone(created.json()["portalUser"])

        assigned = self.client.patch(
            "/api/assets/asset-1",
            headers=headers,
            json={"responsibilities": [{"contactId": contact_id, "role": "technical_owner"}]},
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["metadata"]["technicalOwner"], "Jane Owner")
        self.assertEqual(assigned.json()["responsibilities"][0]["contactId"], contact_id)

        portal = self.client.post(
            f"/api/contacts/{contact_id}/portal-user",
            headers=headers,
            json={"password": "Temporary!42"},
        )
        self.assertEqual(portal.status_code, 201)
        self.assertEqual(portal.json()["portalUser"]["email"], "jane.owner@acme.example")

        replacement = self.client.post(
            "/api/contacts",
            headers=headers,
            json={
                "companyId": "acme",
                "displayName": "Alex Replacement",
                "email": "alex@acme.example",
            },
        )
        self.assertEqual(replacement.status_code, 201)
        transferred = self.client.post(
            f"/api/contacts/{contact_id}/reassign",
            headers=headers,
            json={
                "replacementContactId": replacement.json()["id"],
                "reason": "Responsibility handover",
            },
        )
        self.assertEqual(transferred.status_code, 200)
        self.assertEqual(transferred.json()["transferred"], 1)
        offboarded = self.client.patch(
            f"/api/contacts/{contact_id}",
            headers=headers,
            json={"status": "left_company", "reason": "Employee departed"},
        )
        self.assertEqual(offboarded.status_code, 200)
        self.assertEqual(offboarded.json()["portalUser"]["status"], "disabled")
        self.assertNotIn(
            "jane.owner@acme.example",
            [user["email"] for user in backend_main.REPOSITORY.list_users()],
        )

        client = self._login("client@acme.example")
        client_headers = {"Authorization": f"Bearer {client}"}
        visible = self.client.get("/api/contacts?companyId=acme", headers=client_headers)
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(
            {item["displayName"] for item in visible.json()},
            {"Jane Owner", "Alex Replacement"},
        )
        self.assertEqual(
            self.client.get(
                "/api/contacts?companyId=northwind", headers=client_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/contacts",
                headers=client_headers,
                json={"companyId": "acme", "displayName": "Blocked"},
            ).status_code,
            403,
        )

        detail = self.client.get(f"/api/contacts/{contact_id}", headers=headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["responsibilities"][0]["assetName"], "ACME-DC01")
        self.assertIsNotNone(detail.json()["responsibilities"][0]["effectiveUntil"])
        self.assertIn("contact", {item["entityType"] for item in core.DB["auditEvents"]})

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
            json={
                "companyId": "acme",
                "name": "ACME-APP02",
                "type": "Server",
                "fields": {"ip": "10.0.0.22"},
            },
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
            {
                "id": "asset-3",
                "companyId": "acme",
                "name": "ACME-APP01",
                "type": "Server",
                "status": "Active",
                "source": "manual",
                "fields": {},
            }
        )
        created = self.client.post(
            "/api/relationships",
            headers=headers,
            json={"fromId": "asset-3", "toId": "asset-1", "type": "depends_on"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["impactPolicy"], "required")
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

        updated = self.client.patch(
            f"/api/changes/{created.json()['id']}",
            headers=headers,
            json={"expectedRevision": 1, "title": "Patch domain controller safely"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertEqual(updated.json()["title"], "Patch domain controller safely")
        stale = self.client.patch(
            f"/api/changes/{created.json()['id']}",
            headers=headers,
            json={"expectedRevision": 1, "title": "Overwrite a newer revision"},
        )
        self.assertEqual(stale.status_code, 409)

        current = updated.json()
        for status in ("impact_review", "awaiting_approval", "declined"):
            transitioned = self.client.post(
                f"/api/changes/{current['id']}/transition",
                headers=headers,
                json={
                    "status": status,
                    "expectedRevision": current["revision"],
                    "reason": f"Lifecycle decision: {status}",
                },
            )
            self.assertEqual(transitioned.status_code, 200)
            current = transitioned.json()
        self.assertEqual(current["status"], "declined")
        self.assertEqual(current["approvals"][-1]["decision"], "declined")
        self.assertTrue(any(item["toStatus"] == "declined" for item in current["statusHistory"]))

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

    def test_user_profile_password_and_status_lifecycle_revokes_sessions(self):
        admin_headers = self._headers("admin@example.com")
        client_session = self._login("client@acme.example")
        updated = self.client.patch(
            "/api/users/client",
            headers=admin_headers,
            json={
                "email": "owner@acme.example",
                "displayName": "Acme Service Owner",
                "role": "client_reader",
                "companyId": "acme",
                "companyIds": [],
                "groupIds": [],
                "apiAccessEnabled": False,
                "reason": "Correct owner profile",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["displayName"], "Acme Service Owner")
        self.assertEqual(updated.json()["email"], "owner@acme.example")
        self.assertEqual(
            self.client.get(
                "/api/assets?companyId=acme",
                headers={"Authorization": f"Bearer {client_session}"},
            ).status_code,
            401,
        )

        owner_session = self._login("owner@acme.example")
        changed = self.client.put(
            "/api/users/client/password",
            headers=admin_headers,
            json={"password": "New-Local-Password-2026!"},
        )
        self.assertEqual(changed.status_code, 204)
        self.assertEqual(
            self.client.get(
                "/api/assets?companyId=acme",
                headers={"Authorization": f"Bearer {owner_session}"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"email": "owner@acme.example", "password": "ChangeMe!"},
            ).status_code,
            401,
        )
        self._login("owner@acme.example", "New-Local-Password-2026!")

        disabled = self.client.post(
            "/api/users/client/status",
            headers=admin_headers,
            json={"status": "disabled", "reason": "User left the customer"},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(disabled.json()["status"], "disabled")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={
                    "email": "owner@acme.example",
                    "password": "New-Local-Password-2026!",
                },
            ).status_code,
            401,
        )
        enabled = self.client.post(
            "/api/users/client/status",
            headers=admin_headers,
            json={"status": "active", "reason": "Access restored by administrator"},
        )
        self.assertEqual(enabled.status_code, 200)
        self._login("owner@acme.example", "New-Local-Password-2026!")

    def test_user_retirement_preserves_record_and_protects_final_admin(self):
        admin_headers = self._headers("admin@example.com")
        self.assertEqual(
            self.client.post(
                "/api/users/admin/status",
                headers=admin_headers,
                json={"status": "disabled", "reason": "Unsafe self-service action"},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.delete("/api/users/admin", headers=admin_headers).status_code,
            409,
        )
        archived = self.client.delete("/api/users/client", headers=admin_headers)
        self.assertEqual(archived.status_code, 200)
        directory = self.client.get("/api/users", headers=admin_headers)
        client_user = next(item for item in directory.json() if item["id"] == "client")
        self.assertEqual(client_user["status"], "archived")
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"email": "client@acme.example", "password": "ChangeMe!"},
            ).status_code,
            401,
        )

    def test_personal_api_token_is_one_time_scoped_and_revocable(self):
        admin_headers = self._headers("admin@example.com")
        enabled = self.client.patch(
            "/api/users/client",
            headers=admin_headers,
            json={
                "email": "client@acme.example",
                "displayName": "Acme API Reader",
                "role": "client_reader",
                "companyId": "acme",
                "companyIds": [],
                "groupIds": [],
                "apiAccessEnabled": True,
                "reason": "Enable read-only CMDB automation",
            },
        )
        self.assertEqual(enabled.status_code, 200)
        created = self.client.post(
            "/api/users/client/tokens",
            headers=admin_headers,
            json={
                "name": "Asset inventory export",
                "scopes": ["cmdb:read"],
                "companyIds": ["acme"],
                "expiresInDays": 30,
            },
        )
        self.assertEqual(created.status_code, 201)
        raw_token = created.json()["token"]
        self.assertTrue(raw_token.startswith("cmdb_pat_"))
        stored_token = core.DB["apiTokens"][0]
        self.assertNotIn("token", stored_token)
        self.assertNotEqual(stored_token["tokenHash"], raw_token)
        token_headers = {"Authorization": f"Bearer {raw_token}"}
        self.assertEqual(
            self.client.get("/api/assets?companyId=acme", headers=token_headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/api/assets?companyId=northwind", headers=token_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/assets",
                headers=token_headers,
                json={"companyId": "acme", "name": "PAT-WRITE", "type": "Server"},
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/api/users", headers=token_headers).status_code, 403)
        listed = self.client.get("/api/users/client/tokens", headers=admin_headers)
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn("token", listed.json()[0])
        self.assertNotIn("tokenHash", listed.json()[0])
        revoked = self.client.delete(
            f"/api/users/client/tokens/{created.json()['id']}", headers=admin_headers
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(
            self.client.get("/api/assets?companyId=acme", headers=token_headers).status_code,
            401,
        )

    def test_msp_operator_can_manage_only_customer_users_in_scope(self):
        operator_headers = self._headers("operator@example.com")
        payload = {
            "email": "client@acme.example",
            "displayName": "Acme Customer User",
            "role": "client_reader",
            "companyId": "acme",
            "companyIds": [],
            "groupIds": [],
            "apiAccessEnabled": False,
            "reason": "Customer user profile maintenance",
        }
        allowed = self.client.patch("/api/users/client", headers=operator_headers, json=payload)
        self.assertEqual(allowed.status_code, 200)
        forbidden = self.client.patch(
            "/api/users/admin",
            headers=operator_headers,
            json={**payload, "email": "admin@example.com"},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_customer_groups_are_governed_versioned_and_delete_impact_is_explicit(self):
        admin_headers = self._headers("admin@example.com")
        created = self.client.post(
            "/api/access-groups",
            headers=admin_headers,
            json={
                "name": "Managed services",
                "description": "Customers receiving the managed infrastructure service.",
                "companyIds": ["acme", "northwind"],
                "ownerUserId": "admin",
                "membershipMode": "manual",
                "membershipRules": {},
            },
        )
        self.assertEqual(created.status_code, 201)
        group = created.json()
        self.assertEqual(group["revision"], 1)
        self.assertEqual(group["ownerLabel"], "admin@example.com")
        self.assertEqual(group["assignedUserCount"], 0)

        assigned = self.client.patch(
            "/api/users/operator",
            headers=admin_headers,
            json={
                "email": "operator@example.com",
                "displayName": "Managed Services Operator",
                "role": "msp_operator",
                "companyIds": ["acme"],
                "groupIds": [group["id"]],
                "apiAccessEnabled": False,
                "reason": "Assign managed services customer group",
            },
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(set(assigned.json()["companyIds"]), {"acme", "northwind"})

        listed = self.client.get("/api/access-groups", headers=admin_headers)
        listed_group = next(item for item in listed.json() if item["id"] == group["id"])
        self.assertEqual(listed_group["assignedUserCount"], 1)

        update_payload = {
            "name": "Managed infrastructure",
            "description": "Reviewed reusable customer scope.",
            "companyIds": ["acme", "northwind"],
            "ownerUserId": "admin",
            "membershipMode": "manual",
            "membershipRules": {},
            "expectedRevision": 1,
        }
        updated = self.client.put(
            "/api/access-groups/" + group["id"],
            headers=admin_headers,
            json=update_payload,
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["revision"], 2)
        stale = self.client.put(
            "/api/access-groups/" + group["id"],
            headers=admin_headers,
            json=update_payload,
        )
        self.assertEqual(stale.status_code, 409)

        impact = self.client.get(
            "/api/access-groups/" + group["id"] + "/impact",
            headers=admin_headers,
        )
        self.assertEqual(impact.status_code, 200)
        self.assertEqual(impact.json()["assignedUserCount"], 1)
        self.assertEqual(impact.json()["usersLosingAccess"], 1)
        self.assertEqual(impact.json()["users"][0]["lostCustomers"], ["Northwind Traders"])
        self.assertEqual(
            self.client.get(
                "/api/access-groups/" + group["id"] + "/impact",
                headers=self._headers("operator@example.com"),
            ).status_code,
            403,
        )

        deleted = self.client.delete("/api/access-groups/" + group["id"], headers=admin_headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["impact"]["lostCustomerAssignments"], 1)
        operator = next(
            item
            for item in self.client.get("/api/users", headers=admin_headers).json()
            if item["id"] == "operator"
        )
        self.assertEqual(operator["groupIds"], [])
        self.assertEqual(operator["companyIds"], ["acme"])


if __name__ == "__main__":
    unittest.main()
