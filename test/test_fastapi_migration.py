import asyncio
import base64
import json
import os
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import ANY, patch

import pyotp
from fastapi.testclient import TestClient

import app as core
import backend.main as backend_main
from src.cmdb.email_delivery import DeliveryResult, EmailDeliveryError
from src.cmdb.repository import StateRepository
from src.cmdb.version import APPLICATION_VERSION

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
        self.save_patcher = patch.object(core, "save_db")
        self.save_patcher.start()
        self.original_repository = backend_main.REPOSITORY
        backend_main.REPOSITORY = StateRepository(core.DB, core.save_db)
        self.client = TestClient(api)

    def tearDown(self):
        self.save_patcher.stop()
        backend_main.REPOSITORY = self.original_repository
        core.DB = self.original_db
        core.DATABASE_MODE = self.original_database_mode

    def _login(self, email: str, password: str = "ChangeMe!") -> str:
        response = self.client.post("/api/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    def _headers(self, email: str, password: str = "ChangeMe!") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._login(email, password)}"}

    def _seed_provider_link_review(
        self,
        provider: str,
        external_id: str,
        *,
        connection_revision: int = 7,
    ) -> tuple[dict, dict, dict]:
        """Seed one exact manual-preview generation for explicit-link tests."""

        connection = next(
            (
                item
                for item in core.DB["integrations"]
                if item.get("type") == provider and item.get("companyId") is None
            ),
            None,
        )
        if connection is None:
            backend_main.REPOSITORY.ensure_integration_connection(
                provider,
                "N-central" if provider == "ncentral" else "ConnectWise Manage",
                "admin",
            )
            connection = next(
                item
                for item in core.DB["integrations"]
                if item.get("type") == provider and item.get("companyId") is None
            )
        connection.update(
            enabled=True,
            lifecycleStatus="active",
            revision=connection_revision,
        )
        provider_parent_id = "101" if provider == "ncentral" else "42"
        core.DB.setdefault("providerCompanyObservations", []).append(
            {
                "id": f"{provider}-company-observation",
                "provider": provider,
                "externalId": provider_parent_id,
                "name": "Acme Manufacturing",
                "active": True,
                "deleted": False,
            }
        )
        core.DB.setdefault("providerCompanyMappings", []).append(
            {
                "id": f"{provider}-company-mapping",
                "provider": provider,
                "externalId": provider_parent_id,
                "companyId": "acme",
                "active": True,
            }
        )
        policy = backend_main.REPOSITORY.update_ci_sync_policy(
            provider,
            "acme",
            provider_parent_id,
            backend_main.normalize_ci_policy({"syncMode": "manual", "enabled": False}),
            actor_id="admin",
        )
        run_id = f"{provider}-review-run-{external_id}"
        core.DB["syncRuns"].insert(
            0,
            {
                "id": run_id,
                "type": provider,
                "status": "success",
                "companyId": "acme",
                "providerCompanyId": provider_parent_id,
                "policyId": policy["id"],
                "attributes": {
                    "operation": (
                        "device_preview" if provider == "ncentral" else "configuration_preview"
                    ),
                    "companyId": "acme",
                    "providerCompanyId": provider_parent_id,
                    "policyId": policy["id"],
                    "policyRevision": policy["revision"],
                    "connectionRevision": connection_revision,
                    "readOnly": True,
                },
            },
        )
        record = {
            "externalId": external_id,
            "providerParentId": provider_parent_id,
            "name": f"{provider.upper()}-{external_id}",
            "type": "Server",
            "status": "Active",
            "fields": {},
            "metadata": {},
        }
        review = {
            "id": f"{provider}-review-{external_id}",
            "policyId": policy["id"],
            "companyId": "acme",
            "externalId": external_id,
            "externalName": record["name"],
            "providerRecord": record,
            "state": "pending",
            "contentHash": f"hash-{provider}-{external_id}",
            "lastRunId": run_id,
        }
        core.DB.setdefault("integrationCiReviewItems", []).append(review)
        return connection, policy, review

    def test_request_log_uses_route_template_without_query_or_authorization_values(self):
        sentinel = "must-not-appear-in-logs"
        with self.assertLogs("cmdb.api", level="WARNING") as captured:
            response = self.client.get(
                f"/api/assets/asset-1?token={sentinel}",
                headers={"Authorization": f"Bearer {sentinel}"},
            )

        self.assertEqual(response.status_code, 401)
        request_record = next(
            record
            for record in captured.records
            if getattr(record, "event", "") == "http_request_completed"
        )
        self.assertEqual(request_record.route, "/api/assets/{asset_id}")
        self.assertEqual(request_record.status_code, 401)
        self.assertNotIn(sentinel, " ".join(captured.output))

    def test_notification_worker_finishes_cleanup_during_shutdown(self):
        events: list[str] = []

        async def worker() -> None:
            events.append("started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("stopped")

        async def exercise_lifespan() -> None:
            with (
                patch.dict(os.environ, {"NOTIFICATION_WORKER_ENABLED": "true"}),
                patch.object(backend_main, "_notification_worker_loop", worker),
            ):
                async with backend_main.application_lifespan(api):
                    await asyncio.sleep(0)
                    self.assertEqual(events, ["started"])
            self.assertEqual(events, ["started", "stopped"])

        asyncio.run(exercise_lifespan())

    def test_integration_worker_finishes_cleanup_during_shutdown(self):
        events: list[str] = []

        async def worker() -> None:
            events.append("started")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("stopped")

        async def exercise_lifespan() -> None:
            with (
                patch.dict(os.environ, {"INTEGRATION_WORKER_ENABLED": "true"}),
                patch.object(backend_main, "_integration_worker_loop", worker),
            ):
                async with backend_main.application_lifespan(api):
                    await asyncio.sleep(0)
                    self.assertEqual(events, ["started"])
            self.assertEqual(events, ["started", "stopped"])

        asyncio.run(exercise_lifespan())

    def test_web_process_role_never_starts_embedded_workers(self):
        events: list[str] = []

        async def worker() -> None:
            events.append("started")

        async def exercise_lifespan() -> None:
            with (
                patch.dict(
                    os.environ,
                    {
                        "CMDB_PROCESS_ROLE": "web",
                        "NOTIFICATION_WORKER_ENABLED": "true",
                        "INTEGRATION_WORKER_ENABLED": "true",
                    },
                ),
                patch.object(backend_main, "_notification_worker_loop", worker),
                patch.object(backend_main, "_integration_worker_loop", worker),
            ):
                async with backend_main.application_lifespan(api):
                    await asyncio.sleep(0)
            self.assertEqual(events, [])

        asyncio.run(exercise_lifespan())

    def test_worker_status_surfaces_durable_heartbeat_and_provider_quota(self):
        headers = self._headers("admin@example.com")
        backend_main.REPOSITORY.record_worker_runtime(
            "integrations",
            "worker-status-test",
            "dedicated",
            60,
            "starting",
        )
        backend_main.REPOSITORY.record_worker_runtime(
            "integrations",
            "worker-status-test",
            "dedicated",
            60,
            "cycle_succeeded",
            processed=4,
        )
        backend_main.REPOSITORY.record_provider_rate_limit(
            "connectwise",
            {
                "httpStatus": 200,
                "limit": 1000,
                "remaining": 900,
                "requestPath": "/company/companies",
            },
        )

        with patch.dict(
            os.environ,
            {
                "CMDB_PROCESS_ROLE": "web",
                "INTEGRATION_WORKER_ENABLED": "true",
                "NOTIFICATION_WORKER_ENABLED": "true",
            },
        ):
            response = self.client.get(
                "/api/integrations/continuous-preview/status",
                headers=headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()
        self.assertTrue(status["workerConfigured"])
        self.assertTrue(status["workerEnabled"])
        self.assertTrue(status["workerHealthy"])
        self.assertTrue(status["notificationWorkerEnabled"])
        self.assertEqual(status["executionMode"], "dedicated")
        self.assertEqual(status["runtime"]["itemsProcessed"], 4)
        self.assertEqual(status["providerRateLimit"]["remaining"], 900)

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

    def test_root_reconciliation_workbench_is_tenant_safe_and_governed(self):
        """Root operators see only permitted tenants while admins can govern decisions."""

        queued_ids: dict[str, str] = {}
        for company_id, parent_id, external_id in (
            ("acme", "42", "501"),
            ("northwind", "84", "601"),
        ):
            policy = backend_main.REPOSITORY.update_ci_sync_policy(
                "connectwise",
                company_id,
                parent_id,
                {"syncMode": "continuous_preview", "enabled": True},
                expected_revision=0,
                actor_id="admin",
            )
            backend_main.REPOSITORY.replace_ci_review_items(
                policy["id"],
                company_id,
                f"run-{external_id}",
                [
                    {
                        "externalId": external_id,
                        "name": f"{company_id.upper()}-APP-01",
                        "action": "create",
                        "reason": "New immutable provider identity",
                        "record": {
                            "externalId": external_id,
                            "name": f"{company_id.upper()}-APP-01",
                            "type": "Server",
                            "status": "Active",
                        },
                    }
                ],
            )
            queued_ids[company_id] = backend_main.REPOSITORY.list_ci_review_items(
                "connectwise", company_id
            )[0]["id"]

        admin_headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        client_headers = self._headers("client@acme.example")
        admin_queue = self.client.get("/api/integration-reconciliation", headers=admin_headers)
        self.assertEqual(admin_queue.status_code, 200, admin_queue.text)
        self.assertEqual(admin_queue.json()["total"], 2)
        operator_queue = self.client.get(
            "/api/integration-reconciliation", headers=operator_headers
        )
        self.assertEqual(operator_queue.status_code, 200, operator_queue.text)
        self.assertEqual(operator_queue.json()["total"], 1)
        self.assertEqual(operator_queue.json()["items"][0]["companyId"], "acme")
        legacy_operator_queue = self.client.get(
            "/api/integrations/connectwise/configurations/review-queue",
            headers=operator_headers,
        )
        self.assertEqual(legacy_operator_queue.status_code, 200, legacy_operator_queue.text)
        self.assertEqual(legacy_operator_queue.json()["total"], 1)
        self.assertEqual(legacy_operator_queue.json()["items"][0]["companyId"], "acme")
        operator_status = self.client.get(
            "/api/integrations/continuous-preview/status",
            headers=operator_headers,
        )
        self.assertEqual(operator_status.status_code, 200, operator_status.text)
        self.assertEqual(operator_status.json()["pendingReviews"], 1)
        self.assertEqual(
            {item["companyId"] for item in operator_status.json()["policies"]},
            {"acme"},
        )
        admin_status = self.client.get(
            "/api/integrations/continuous-preview/status",
            headers=admin_headers,
        )
        self.assertEqual(admin_status.status_code, 200, admin_status.text)
        self.assertEqual(admin_status.json()["pendingReviews"], 2)
        self.assertEqual(
            {item["companyId"] for item in admin_status.json()["policies"]},
            {"acme", "northwind"},
        )
        self.assertEqual(
            self.client.get("/api/integration-reconciliation", headers=client_headers).status_code,
            403,
        )

        catalogue = self.client.get("/api/field-authority/catalogue", headers=admin_headers)
        self.assertEqual(catalogue.status_code, 200, catalogue.text)
        self.assertIn("cmdb", {item["key"] for item in catalogue.json()["providers"]})
        preset = self.client.post(
            "/api/field-authority/presets",
            headers=admin_headers,
            json={"companyId": "acme", "presetKey": "balanced_msp"},
        )
        self.assertEqual(preset.status_code, 200, preset.text)
        self.assertGreater(preset.json()["applied"], 0)
        self.assertEqual(
            self.client.post(
                "/api/field-authority/presets",
                headers=operator_headers,
                json={"companyId": "acme", "presetKey": "balanced_msp"},
            ).status_code,
            403,
        )

        dismissed = self.client.post(
            "/api/integration-reconciliation/dismiss",
            headers=admin_headers,
            json={"itemIds": [queued_ids["acme"]], "notes": "Confirmed duplicate source record"},
        )
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual(dismissed.json()["dismissed"], 1)
        self.assertEqual(
            backend_main.REPOSITORY.get_ci_review_item(queued_ids["acme"])["state"],
            "dismissed",
        )

        ignored = self.client.post(
            "/api/integration-reconciliation/ignore",
            headers=admin_headers,
            json={
                "itemIds": [queued_ids["northwind"]],
                "notes": "Outside the managed configuration scope",
            },
        )
        self.assertEqual(ignored.status_code, 200, ignored.text)
        self.assertEqual(ignored.json()["ignored"], 1)
        ignored_list = self.client.get(
            "/api/integration-reconciliation/ignored", headers=admin_headers
        )
        self.assertEqual(ignored_list.status_code, 200, ignored_list.text)
        self.assertEqual(ignored_list.json()["total"], 1)
        self.assertEqual(
            self.client.post(
                "/api/integration-reconciliation/ignore",
                headers=operator_headers,
                json={
                    "itemIds": [queued_ids["acme"]],
                    "notes": "Operator cannot persist exclusions",
                },
            ).status_code,
            403,
        )
        restored = self.client.post(
            (
                "/api/integration-reconciliation/ignored/"
                f"{ignored.json()['suppressionIds'][0]}/restore"
            ),
            headers=admin_headers,
            json={"notes": "Configuration is now managed"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertFalse(restored.json()["active"])
        self.assertEqual(
            backend_main.REPOSITORY.get_ci_review_item(queued_ids["northwind"])["state"],
            "pending",
        )

    def test_presence_lifecycle_queue_is_validated_paginated_and_tenant_scoped(self):
        """Only root readers may list evidence and operators stay within assigned customers."""

        candidates = [
            {
                "id": "presence-acme",
                "companyId": "acme",
                "companyName": "Acme Manufacturing",
                "provider": "ncentral",
                "externalId": "device-1",
                "externalName": "ACME-HV01",
                "state": "eligible",
                "revision": 2,
            },
            {
                "id": "presence-northwind",
                "companyId": "northwind",
                "companyName": "Northwind Traders",
                "provider": "ncentral",
                "externalId": "device-2",
                "externalName": "NW-HV01",
                "state": "monitoring",
                "revision": 1,
            },
        ]

        def scoped_candidates(**filters):
            rows = deepcopy(candidates)
            company_id = filters.get("company_id")
            company_ids = filters.get("company_ids")
            if company_id:
                rows = [item for item in rows if item["companyId"] == company_id]
            if company_ids is not None:
                rows = [item for item in rows if item["companyId"] in company_ids]
            state = filters.get("state")
            if state:
                rows = [item for item in rows if item["state"] == state]
            return {
                "items": rows,
                "total": len(rows),
                "summary": {
                    "observed": 0,
                    "monitoring": sum(item["state"] == "monitoring" for item in rows),
                    "eligible": sum(item["state"] == "eligible" for item in rows),
                    "notEvaluated": 0,
                    "retired": 0,
                    "restoreReady": 0,
                    "total": len(rows),
                },
            }

        with patch.object(
            backend_main.REPOSITORY,
            "list_ci_presence_lifecycle",
            side_effect=scoped_candidates,
            create=True,
        ) as listing:
            admin = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates"
                "?provider=ncentral&limit=100&offset=0",
                headers=self._headers("admin@example.com"),
            )
            operator = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?provider=ncentral",
                headers=self._headers("operator@example.com"),
            )
            client = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?provider=ncentral",
                headers=self._headers("client@acme.example"),
            )
            forbidden_tenant = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates"
                "?provider=ncentral&companyId=northwind",
                headers=self._headers("operator@example.com"),
            )
            invalid_provider = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?provider=unknown",
                headers=self._headers("admin@example.com"),
            )
            invalid_state = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?state=deleted",
                headers=self._headers("admin@example.com"),
            )
            invalid_limit = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?limit=501",
                headers=self._headers("admin@example.com"),
            )
            invalid_offset = self.client.get(
                "/api/integration-reconciliation/lifecycle-candidates?offset=25001",
                headers=self._headers("admin@example.com"),
            )

        self.assertEqual(admin.status_code, 200, admin.text)
        self.assertEqual(admin.json()["total"], 2)
        self.assertEqual(operator.status_code, 200, operator.text)
        self.assertEqual(operator.json()["total"], 1)
        self.assertEqual(operator.json()["items"][0]["companyId"], "acme")
        self.assertEqual(client.status_code, 403)
        self.assertEqual(forbidden_tenant.status_code, 403)
        self.assertEqual(invalid_provider.status_code, 422)
        self.assertEqual(invalid_state.status_code, 422)
        self.assertEqual(invalid_limit.status_code, 422)
        self.assertEqual(invalid_offset.status_code, 422)
        operator_call = listing.call_args_list[1]
        self.assertEqual(operator_call.kwargs["company_ids"], ["acme"])
        self.assertEqual(operator_call.kwargs["limit"], 50)
        self.assertEqual(operator_call.kwargs["offset"], 0)

    def test_presence_lifecycle_actions_are_admin_only_and_revision_safe(self):
        """Source retirement and restore require explicit admin notes and revisions."""

        admin_headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        retired = {"id": "presence-1", "state": "retired", "revision": 3}
        restored = {"id": "presence-1", "state": "observed", "revision": 4}
        with (
            patch.object(
                backend_main.REPOSITORY,
                "retire_ci_presence_mapping",
                return_value=retired,
                create=True,
            ) as retire,
            patch.object(
                backend_main.REPOSITORY,
                "restore_ci_presence_mapping",
                return_value=restored,
                create=True,
            ) as restore,
        ):
            retired_response = self.client.post(
                "/api/integration-reconciliation/lifecycle-candidates/presence-1/retire",
                headers=admin_headers,
                json={"expectedRevision": 2, "notes": "Provider source was decommissioned"},
            )
            denied = self.client.post(
                "/api/integration-reconciliation/lifecycle-candidates/presence-1/retire",
                headers=operator_headers,
                json={"expectedRevision": 2, "notes": "Operator cannot approve this"},
            )
            restored_response = self.client.post(
                "/api/integration-reconciliation/lifecycle-candidates/presence-1/restore",
                headers=admin_headers,
                json={"expectedRevision": 3, "notes": "Device identity was observed again"},
            )

        self.assertEqual(retired_response.status_code, 200, retired_response.text)
        self.assertEqual(retired_response.json()["state"], "retired")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(restored_response.status_code, 200, restored_response.text)
        retire.assert_called_once_with(
            "presence-1",
            2,
            "Provider source was decommissioned",
            "admin",
        )
        restore.assert_called_once_with(
            "presence-1",
            3,
            "Device identity was observed again",
            "admin",
        )

        with patch.object(
            backend_main.REPOSITORY,
            "retire_ci_presence_mapping",
            side_effect=ValueError("Presence evidence changed; refresh before deciding"),
            create=True,
        ):
            conflict = self.client.post(
                "/api/integration-reconciliation/lifecycle-candidates/presence-1/retire",
                headers=admin_headers,
                json={"expectedRevision": 2, "notes": "Provider source was decommissioned"},
            )
        with patch.object(
            backend_main.REPOSITORY,
            "restore_ci_presence_mapping",
            return_value=None,
            create=True,
        ):
            missing = self.client.post(
                "/api/integration-reconciliation/lifecycle-candidates/missing/restore",
                headers=admin_headers,
                json={"expectedRevision": 1, "notes": "Device identity was observed again"},
            )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(missing.status_code, 404)

    def test_ncentral_bulk_import_skips_restore_required_before_asset_update(self):
        """A retired N-central source must not partially update its canonical asset."""

        core.DB["providerCiMappings"] = [
            {
                "id": "retired-ncentral-mapping",
                "provider": "ncentral",
                "companyId": "acme",
                "providerParentId": "101",
                "externalId": "7001",
                "assetId": "asset-1",
                "active": False,
            }
        ]
        core.DB["integrationCiPresence"] = [
            {
                "id": "retired-ncentral-presence",
                "mappingId": "retired-ncentral-mapping",
                "state": "restore_ready",
            }
        ]
        preview = {
            "companyName": "Acme Manufacturing",
            "discovered": 1,
            "appliedPolicy": {"id": "policy-ncentral"},
            "items": [
                {
                    "externalId": "7001",
                    "name": "ACME-NC01",
                    "action": "update",
                    "assetId": "asset-1",
                    "changes": {"name": "MUST-NOT-BE-WRITTEN"},
                    "changedFields": ["name"],
                    "record": {
                        "externalId": "7001",
                        "providerParentId": "101",
                        "name": "ACME-NC01",
                        "type": "Server",
                        "status": "Active",
                        "fields": {},
                        "metadata": {},
                    },
                }
            ],
        }

        with patch.object(
            backend_main,
            "_ncentral_device_preview",
            return_value=deepcopy(preview),
        ):
            response = self.client.post(
                "/api/integrations/ncentral/devices/import",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "externalIds": ["7001"],
                    "decisionNotes": "Regression test for governed restoration",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["updated"], 0)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(response.json()["skippedItems"][0]["action"], "restore_required")
        self.assertEqual(core.DB["assets"][0]["name"], "ACME-DC01")
        self.assertFalse(core.DB["providerCiMappings"][0]["active"])

    def test_connectwise_bulk_import_skips_restore_required_before_asset_create(self):
        """A retired ConnectWise source must not leave an orphan canonical asset."""

        core.DB["providerCiMappings"] = [
            {
                "id": "retired-connectwise-mapping",
                "provider": "connectwise",
                "companyId": "acme",
                "providerParentId": "42",
                "externalId": "9001",
                "assetId": "asset-1",
                "active": False,
            }
        ]
        core.DB["integrationCiPresence"] = [
            {
                "id": "retired-connectwise-presence",
                "mappingId": "retired-connectwise-mapping",
                "state": "retired",
            }
        ]
        preview = {
            "companyName": "Acme Manufacturing",
            "discovered": 1,
            "appliedPolicy": {"id": "policy-connectwise"},
            "items": [
                {
                    "externalId": "9001",
                    "name": "CW-DEVICE-9001",
                    "action": "create",
                    "assetId": None,
                    "changes": {},
                    "changedFields": [],
                    "record": {
                        "externalId": "9001",
                        "providerParentId": "42",
                        "name": "CW-DEVICE-9001",
                        "type": "Server",
                        "status": "Active",
                        "fields": {},
                        "metadata": {},
                    },
                }
            ],
        }
        asset_count = len(core.DB["assets"])

        with patch.object(
            backend_main,
            "_connectwise_configuration_preview",
            return_value=deepcopy(preview),
        ):
            response = self.client.post(
                "/api/integrations/connectwise/configurations/import",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalIds": ["9001"],
                    "decisionNotes": "Regression test for governed restoration",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["created"], 0)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertEqual(response.json()["skippedItems"][0]["action"], "restore_required")
        self.assertEqual(len(core.DB["assets"]), asset_count)
        self.assertFalse(core.DB["providerCiMappings"][0]["active"])

    def test_provider_bulk_imports_roll_back_canonical_writes_on_atomic_failpoint(self):
        """Neither provider may retain an asset when its atomic mapping commit fails."""

        headers = self._headers("admin@example.com")
        cases = (
            (
                "ncentral",
                "/api/integrations/ncentral/devices/import",
                "_ncentral_device_preview",
                "101",
                "7001",
            ),
            (
                "connectwise",
                "/api/integrations/connectwise/configurations/import",
                "_connectwise_configuration_preview",
                "42",
                "9001",
            ),
        )
        for provider, route, preview_name, provider_parent_id, external_id in cases:
            with self.subTest(provider=provider):
                preview = {
                    "companyName": "Acme Manufacturing",
                    "discovered": 1,
                    "appliedPolicy": {"id": f"policy-{provider}"},
                    "items": [
                        {
                            "externalId": external_id,
                            "name": f"DEVICE-{external_id}",
                            "action": "create",
                            "assetId": None,
                            "changes": {},
                            "changedFields": [],
                            "record": {
                                "externalId": external_id,
                                "providerParentId": provider_parent_id,
                                "name": f"DEVICE-{external_id}",
                                "type": "Server",
                                "status": "Active",
                                "fields": {},
                                "metadata": {},
                            },
                        }
                    ],
                }
                asset_count = len(core.DB["assets"])
                with (
                    patch.object(backend_main, preview_name, return_value=deepcopy(preview)),
                    patch.object(
                        backend_main.REPOSITORY,
                        "_provider_import_failpoint",
                        side_effect=ValueError("simulated mapping failure"),
                    ),
                ):
                    response = self.client.post(
                        route,
                        headers=headers,
                        json={
                            "companyId": "acme",
                            "providerCompanyId": provider_parent_id,
                            "externalIds": [external_id],
                            "decisionNotes": "Atomic rollback regression test",
                        },
                    )

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(len(core.DB["assets"]), asset_count)
                self.assertFalse(
                    any(
                        item.get("provider") == provider and item.get("externalId") == external_id
                        for item in core.DB.get("providerCiMappings", [])
                    )
                )

    def test_provider_import_preflight_fails_closed_on_scope_mismatch(self):
        """Immutable identities cannot move across customer or provider-parent scope."""

        item = {
            "externalId": "7001",
            "name": "ACME-NC01",
            "record": {"externalId": "7001", "name": "ACME-NC01"},
        }
        with patch.object(
            backend_main.REPOSITORY,
            "classify_provider_ci_mapping_import",
            return_value={
                "decision": "allow",
                "mappingId": "mapping-1",
                "companyMatches": False,
                "providerParentMatches": True,
                "reason": "No governed retirement blocks this provider identity.",
            },
        ):
            allowed, skipped = backend_main._provider_import_preflight(
                "ncentral",
                "acme",
                "101",
                {"7001": item},
                {"7001"},
            )

        self.assertEqual(allowed, [])
        self.assertEqual(skipped[0]["action"], "scope_conflict")
        self.assertIn("another customer", skipped[0]["reason"])

    def test_single_item_link_routes_reject_cross_scope_provider_identities(self):
        """Explicit links cannot reassign an immutable ID across tenant boundaries."""

        core.DB["providerCiMappings"] = [
            {
                "id": "cross-scope-ncentral",
                "provider": "ncentral",
                "companyId": "northwind",
                "providerParentId": "202",
                "externalId": "7001",
                "assetId": "asset-2",
                "active": True,
            },
            {
                "id": "cross-scope-connectwise",
                "provider": "connectwise",
                "companyId": "northwind",
                "providerParentId": "84",
                "externalId": "9001",
                "assetId": "asset-2",
                "active": True,
            },
        ]
        ncentral_item = {
            "id": "review-ncentral-7001",
            "externalId": "7001",
            "externalName": "ACME-NC01",
            "policyId": "policy-ncentral",
            "providerRecord": {
                "externalId": "7001",
                "providerParentId": "101",
                "name": "ACME-NC01",
                "type": "Server",
                "status": "Active",
                "fields": {},
                "metadata": {},
            },
        }
        connectwise_preview = {
            "appliedPolicy": {"id": "policy-connectwise"},
            "items": [
                {
                    "externalId": "9001",
                    "name": "CW-DEVICE-9001",
                    "record": {
                        "externalId": "9001",
                        "providerParentId": "42",
                        "name": "CW-DEVICE-9001",
                    },
                }
            ],
        }

        with (
            patch.object(backend_main, "_ncentral_mapped_company", return_value={}),
            patch.object(
                backend_main.REPOSITORY,
                "get_ci_review_item_by_identity",
                return_value=ncentral_item,
            ),
            patch.object(
                backend_main,
                "_connectwise_configuration_preview",
                return_value=connectwise_preview,
            ),
            patch.object(
                backend_main.REPOSITORY,
                "record_provider_ci_mapping",
                wraps=backend_main.REPOSITORY.record_provider_ci_mapping,
            ) as record_mapping,
        ):
            ncentral = self.client.post(
                "/api/integrations/ncentral/devices/link",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "externalId": "7001",
                    "assetId": "asset-1",
                },
            )
            connectwise = self.client.post(
                "/api/integrations/connectwise/configurations/link",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalId": "9001",
                    "assetId": "asset-1",
                },
            )

        self.assertEqual(ncentral.status_code, 409, ncentral.text)
        self.assertEqual(connectwise.status_code, 409, connectwise.text)
        self.assertIn("scoped elsewhere", ncentral.json()["detail"])
        self.assertIn("scoped elsewhere", connectwise.json()["detail"])
        record_mapping.assert_not_called()

    def test_single_item_link_directs_retired_mapping_to_missing_devices(self):
        """Lifecycle retirement cannot be bypassed by an explicit link action."""

        core.DB["providerCiMappings"] = [
            {
                "id": "retired-connectwise-link",
                "provider": "connectwise",
                "companyId": "acme",
                "providerParentId": "42",
                "externalId": "9001",
                "assetId": "asset-1",
                "active": False,
            }
        ]
        core.DB["integrationCiPresence"] = [
            {
                "id": "retired-connectwise-link-presence",
                "mappingId": "retired-connectwise-link",
                "state": "retired",
            }
        ]
        preview = {
            "appliedPolicy": {"id": "policy-connectwise"},
            "items": [
                {
                    "externalId": "9001",
                    "name": "CW-DEVICE-9001",
                    "record": {"externalId": "9001", "name": "CW-DEVICE-9001"},
                }
            ],
        }

        with patch.object(
            backend_main,
            "_connectwise_configuration_preview",
            return_value=preview,
        ):
            response = self.client.post(
                "/api/integrations/connectwise/configurations/link",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalId": "9001",
                    "assetId": "asset-1",
                },
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Missing devices", response.json()["detail"])

    def test_single_item_link_rejects_inactive_mapping_without_lifecycle_row(self):
        """An ungoverned inactive identity is a clean conflict, never a server error."""

        core.DB["providerCiMappings"] = [
            {
                "id": "inactive-connectwise-link",
                "provider": "connectwise",
                "companyId": "acme",
                "providerParentId": "42",
                "externalId": "9001",
                "assetId": "asset-1",
                "active": False,
            }
        ]
        preview = {
            "appliedPolicy": {"id": "policy-connectwise"},
            "items": [
                {
                    "externalId": "9001",
                    "name": "CW-DEVICE-9001",
                    "record": {"externalId": "9001", "name": "CW-DEVICE-9001"},
                }
            ],
        }

        with patch.object(
            backend_main,
            "_connectwise_configuration_preview",
            return_value=preview,
        ):
            response = self.client.post(
                "/api/integrations/connectwise/configurations/link",
                headers=self._headers("admin@example.com"),
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalId": "9001",
                    "assetId": "asset-1",
                },
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("inactive without a governed restore action", response.json()["detail"])

    def test_manual_policy_review_generation_allows_explicit_links_for_both_providers(self):
        """Scheduler-disabled manual policies remain valid when every generation matches."""

        headers = self._headers("admin@example.com")
        routes = {
            "ncentral": "/api/integrations/ncentral/devices/link",
            "connectwise": "/api/integrations/connectwise/configurations/link",
        }
        parents = {"ncentral": "101", "connectwise": "42"}
        for index, provider in enumerate(("ncentral", "connectwise"), start=1):
            external_id = f"manual-{index}"
            _connection, policy, _review = self._seed_provider_link_review(
                provider,
                external_id,
            )
            self.assertFalse(policy["enabled"])
            response = self.client.post(
                routes[provider],
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": parents[provider],
                    "externalId": external_id,
                    "assetId": "asset-1",
                },
            )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["assetId"], "asset-1")

    def test_explicit_links_reject_stale_review_generations_for_both_providers(self):
        """A connection change after preview requires another reviewed provider read."""

        headers = self._headers("admin@example.com")
        routes = {
            "ncentral": "/api/integrations/ncentral/devices/link",
            "connectwise": "/api/integrations/connectwise/configurations/link",
        }
        parents = {"ncentral": "101", "connectwise": "42"}
        for index, provider in enumerate(("ncentral", "connectwise"), start=1):
            external_id = f"stale-{index}"
            connection, _policy, review = self._seed_provider_link_review(
                provider,
                external_id,
            )
            connection["revision"] += 1
            response = self.client.post(
                routes[provider],
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": parents[provider],
                    "externalId": external_id,
                    "assetId": "asset-1",
                },
            )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("Run preview again", response.json()["detail"])
            self.assertEqual(review["state"], "pending")
            self.assertFalse(
                any(
                    item.get("provider") == provider and item.get("externalId") == external_id
                    for item in core.DB.get("providerCiMappings", [])
                )
            )

    def test_explicit_link_rejects_legacy_review_without_source_generation(self):
        """Review observations from older releases cannot authorize a canonical write."""

        _connection, _policy, review = self._seed_provider_link_review(
            "ncentral",
            "legacy-generation",
        )
        review.pop("lastRunId")
        response = self.client.post(
            "/api/integrations/ncentral/devices/link",
            headers=self._headers("admin@example.com"),
            json={
                "companyId": "acme",
                "providerCompanyId": "101",
                "externalId": "legacy-generation",
                "assetId": "asset-1",
            },
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("older release", response.json()["detail"])
        self.assertEqual(review["state"], "pending")

    def test_explicit_links_reject_policy_changes_after_review_for_both_providers(self):
        """A saved-policy edit invalidates earlier link evidence for either provider."""

        headers = self._headers("admin@example.com")
        routes = {
            "ncentral": "/api/integrations/ncentral/devices/link",
            "connectwise": "/api/integrations/connectwise/configurations/link",
        }
        parents = {"ncentral": "101", "connectwise": "42"}
        for index, provider in enumerate(("ncentral", "connectwise"), start=1):
            external_id = f"policy-stale-{index}"
            _connection, policy, review = self._seed_provider_link_review(
                provider,
                external_id,
            )
            stored_policy = next(
                item for item in core.DB["integrationCiPolicies"] if item.get("id") == policy["id"]
            )
            stored_policy["revision"] += 1
            response = self.client.post(
                routes[provider],
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": parents[provider],
                    "externalId": external_id,
                    "assetId": "asset-1",
                },
            )

            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("Run preview again", response.json()["detail"])
            self.assertEqual(review["state"], "pending")

    def test_connectwise_company_observations_and_preview_samples_respect_scope(self):
        """Scoped operators must not see other or unmapped provider-company names."""

        backend_main.REPOSITORY.record_company_discovery(
            "connectwise",
            {
                "id": "company-discovery-scope",
                "type": "connectwise",
                "status": "success",
                "startedAt": "2026-07-27T10:00:00Z",
                "finishedAt": "2026-07-27T10:00:01Z",
                "discovered": 3,
                "imported": 0,
                "updated": 0,
                "review": 3,
                "message": "Three companies discovered",
            },
            [
                {
                    "externalId": "42",
                    "identifier": "ACME",
                    "name": "Acme Manufacturing",
                    "status": "Active",
                    "type": "Customer",
                    "site": "South",
                    "deleted": False,
                    "lastUpdated": "2026-07-27T09:00:00Z",
                },
                {
                    "externalId": "84",
                    "identifier": "NORTHWIND",
                    "name": "Northwind Traders",
                    "status": "Active",
                    "type": "Customer",
                    "site": "North",
                    "deleted": False,
                    "lastUpdated": "2026-07-27T09:00:00Z",
                },
                {
                    "externalId": "99",
                    "identifier": "UNMAPPED",
                    "name": "Unmapped Customer",
                    "status": "Active",
                    "type": "Customer",
                    "site": "West",
                    "deleted": False,
                    "lastUpdated": "2026-07-27T09:00:00Z",
                },
            ],
            "admin",
        )
        backend_main.REPOSITORY.map_provider_company("connectwise", "42", "acme", "admin")
        backend_main.REPOSITORY.map_provider_company(
            "connectwise",
            "84",
            "northwind",
            "admin",
        )

        operator_headers = self._headers("operator@example.com")
        operator_companies = self.client.get(
            "/api/integrations/connectwise/companies",
            headers=operator_headers,
        )
        self.assertEqual(operator_companies.status_code, 200, operator_companies.text)
        self.assertEqual(
            {item["externalId"] for item in operator_companies.json()},
            {"42"},
        )
        self.assertNotIn("Northwind Traders", operator_companies.text)
        self.assertNotIn("Unmapped Customer", operator_companies.text)

        admin_companies = self.client.get(
            "/api/integrations/connectwise/companies",
            headers=self._headers("admin@example.com"),
        )
        self.assertEqual(
            {item["externalId"] for item in admin_companies.json()},
            {"42", "84", "99"},
        )

        preview = {
            "readOnly": True,
            "writesAttempted": False,
            "appliedPolicy": {},
            "discovered": 2,
            "included": 2,
            "excluded": 0,
            "truncated": False,
            "exclusionReasons": {},
            "availableStatuses": ["Active"],
            "availableTypes": ["Customer"],
            "availableSites": ["North", "South"],
            "sampleIncluded": [
                {"externalId": "84", "name": "Northwind Traders"},
                {"externalId": "42", "name": "Acme Manufacturing"},
            ],
            "sampleExcluded": [],
        }
        with (
            patch.object(
                backend_main,
                "_connectwise_effective_configuration",
                return_value=({}, "saved"),
            ),
            patch.object(
                backend_main,
                "_connectwise_connection_public",
                return_value={"discoveryPolicy": {}},
            ),
            patch.object(backend_main, "_connectwise_adapter") as adapter_factory,
        ):
            adapter_factory.return_value.preview.return_value = preview
            operator_preview = self.client.post(
                "/api/integrations/connectwise/discovery-preview",
                headers=operator_headers,
            )
            admin_preview = self.client.post(
                "/api/integrations/connectwise/discovery-preview",
                headers=self._headers("admin@example.com"),
            )

        self.assertEqual(operator_preview.status_code, 200, operator_preview.text)
        self.assertEqual(operator_preview.json()["sampleIncluded"], [])
        self.assertEqual(operator_preview.json()["sampleExcluded"], [])
        self.assertNotIn("Northwind Traders", operator_preview.text)
        self.assertEqual(admin_preview.status_code, 200, admin_preview.text)
        self.assertEqual(admin_preview.json()["sampleIncluded"], preview["sampleIncluded"])

    def test_root_email_configuration_is_write_only_and_test_delivery_is_audited(self):
        admin_headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        key = base64.urlsafe_b64encode(b"e" * 32).decode("ascii")
        with patch.dict(os.environ, {"MFA_ENCRYPTION_KEY": key}, clear=False):
            response = self.client.put(
                "/api/email/config",
                headers=admin_headers,
                json={
                    "enabled": True,
                    "authMode": "client_secret",
                    "tenantId": "00000000-0000-0000-0000-000000000001",
                    "clientId": "00000000-0000-0000-0000-000000000002",
                    "servicePrincipalObjectId": "00000000-0000-0000-0000-000000000003",
                    "senderAddress": "cmdb@example.com",
                    "senderName": "CMDB Hub",
                    "clientSecret": "write-only-secret",
                    "expectedRevision": 1,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["hasClientSecret"])
            self.assertEqual(
                response.json()["servicePrincipalObjectId"],
                "00000000-0000-0000-0000-000000000003",
            )
            self.assertNotIn("clientSecretEncrypted", response.json())
            self.assertNotIn("write-only-secret", json.dumps(core.DB))
            self.assertEqual(
                self.client.get("/api/email/config", headers=operator_headers).status_code,
                403,
            )
            with patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                return_value=DeliveryResult(202, "graph-request"),
            ):
                sent = self.client.post(
                    "/api/email/test",
                    headers=admin_headers,
                    json={"recipient": "tech@example.com"},
                )
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertEqual(sent.json()["status"], "accepted")
            self.assertEqual(sent.json()["providerRequestId"], "graph-request")
            history = self.client.get("/api/email/outbox", headers=admin_headers)
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()[0]["status"], "accepted")
            self.assertNotIn("bodyHtml", history.json()[0])

            setup = self.client.post(
                "/api/email/setup-script",
                headers=admin_headers,
                json={
                    "authMode": "client_secret",
                    "tenantId": "00000000-0000-0000-0000-000000000001",
                    "clientId": "00000000-0000-0000-0000-000000000002",
                    "servicePrincipalObjectId": "00000000-0000-0000-0000-000000000003",
                    "senderAddress": "cmdb@example.com",
                    "senderName": "CMDB Hub",
                    "createSharedMailbox": True,
                },
            )
            self.assertEqual(setup.status_code, 200, setup.text)
            self.assertIn("$CreateSharedMailbox = $true", setup.text)
            self.assertIn("00000000-0000-0000-0000-000000000003", setup.text)
            self.assertNotIn("write-only-secret", setup.text)

            with patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                side_effect=EmailDeliveryError("Microsoft identity is unreachable"),
            ):
                failed = self.client.post(
                    "/api/email/test",
                    headers=admin_headers,
                    json={"recipient": "tech@example.com"},
                )
            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertEqual(failed.json()["detail"], "Microsoft identity is unreachable")
            history = self.client.get("/api/email/outbox", headers=admin_headers).json()
            self.assertEqual(history[0]["status"], "failed")
            failed_delivery_event = next(
                event for event in core.DB["auditEvents"] if event["action"] == "delivery_failed"
            )
            self.assertEqual(failed_delivery_event["outcome"], "failed")

    def test_integration_failure_alerts_are_durable_rate_limited_and_idempotent(self):
        """Unattended failures should alert operators without sending on every retry."""

        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "verified",
            },
            None,
        )
        policy = {
            "id": "policy-1",
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "lastRunAt": "2026-07-27T10:00:00Z",
        }
        with patch.dict(
            os.environ,
            {
                "NOTIFICATION_WORKER_ENABLED": "true",
                "INTEGRATION_ALERT_RECIPIENTS": "ops@example.com;admin@example.com",
            },
            clear=False,
        ):
            first = backend_main._queue_integration_alert(
                policy,
                event="failed",
                detail="Provider unavailable",
                consecutive_failures=1,
                retry_delay_minutes=15,
            )
            duplicate = backend_main._queue_integration_alert(
                policy,
                event="failed",
                detail="Provider unavailable",
                consecutive_failures=1,
                retry_delay_minutes=15,
            )
            suppressed = backend_main._queue_integration_alert(
                {**policy, "lastRunAt": "2026-07-27T11:00:00Z"},
                event="failed",
                detail="Provider unavailable",
                consecutive_failures=3,
                retry_delay_minutes=60,
            )
            fourth = backend_main._queue_integration_alert(
                {**policy, "lastRunAt": "2026-07-27T12:00:00Z"},
                event="failed",
                detail="Provider unavailable",
                consecutive_failures=4,
                retry_delay_minutes=120,
            )

        self.assertIsNotNone(first)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertIsNone(suppressed)
        self.assertIsNotNone(fourth)
        self.assertEqual(len(core.DB["emailOutbox"]), 2)
        self.assertEqual(
            core.DB["emailOutbox"][0]["to"],
            ["admin@example.com", "ops@example.com"],
        )
        self.assertIn("120 minute(s)", core.DB["emailOutbox"][0]["bodyText"])

    def test_integration_alerts_require_a_delivery_ready_email_connection(self):
        """Enabled but unconfigured email must not accumulate doomed alert messages."""

        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "not_configured",
            },
            None,
        )
        with patch.dict(
            os.environ,
            {
                "NOTIFICATION_WORKER_ENABLED": "true",
                "INTEGRATION_ALERT_RECIPIENTS": "ops@example.com",
            },
            clear=False,
        ):
            self.assertFalse(backend_main._integration_alert_delivery_ready())
            self.assertIsNone(
                backend_main._queue_integration_alert(
                    {
                        "id": "policy-1",
                        "companyId": "acme",
                        "companyName": "Acme Manufacturing",
                        "lastRunAt": "2026-07-27T10:00:00Z",
                    },
                    event="failed",
                    detail="Provider unavailable",
                    consecutive_failures=1,
                    retry_delay_minutes=15,
                )
            )
        self.assertEqual(core.DB.get("emailOutbox", []), [])

    def test_recovery_alert_uses_the_completed_sync_run_timestamp(self):
        """Recovery idempotency must identify the run that actually recovered."""

        preview = {
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "providerCompanyId": "42",
            "providerCompanyName": "Acme Manufacturing",
            "credentialSource": "saved",
            "readOnly": True,
            "writesAttempted": False,
            "discovered": 0,
            "included": 0,
            "excluded": 0,
            "exclusionReasons": {},
            "availableTypes": [],
            "availableStatuses": [],
            "typeMappingSummary": {},
            "appliedPolicy": {
                "id": "policy-1",
                "revision": 3,
                "lastRunAt": "2026-07-27T09:00:00Z",
                "consecutiveFailures": 2,
            },
            "counts": {
                "create": 0,
                "update": 0,
                "link": 0,
                "unchanged": 0,
                "conflict": 0,
            },
            "items": [],
        }
        with (
            patch.object(
                backend_main,
                "_connectwise_configuration_preview",
                return_value=preview,
            ),
            patch.object(
                backend_main.REPOSITORY,
                "renew_ci_sync_policy_run",
                return_value={"id": "policy-1"},
            ),
            patch.object(
                backend_main.REPOSITORY,
                "publish_and_complete_ci_policy_preview",
                return_value={
                    "run": {
                        "id": "run-recovered",
                        "finishedAt": "2026-07-27T10:05:00Z",
                    },
                    "queueSummary": {
                        "pending": 0,
                        "created": 0,
                        "updated": 0,
                        "resolved": 0,
                    },
                    "policy": {"id": "policy-1", "consecutiveFailures": 0},
                },
            ),
            patch.object(backend_main, "_queue_integration_alert") as queue_alert,
        ):
            backend_main._execute_connectwise_ci_preview(
                "acme",
                "42",
                actor_id=None,
                trigger="continuous_preview",
                policy_id="policy-1",
                lease_owner="scheduled-worker",
            )

        queue_alert.assert_called_once()
        self.assertEqual(
            queue_alert.call_args.args[0]["lastRunAt"],
            "2026-07-27T10:05:00Z",
        )

    def test_connectwise_failure_history_names_the_trigger_that_failed(self):
        """Sync history should distinguish scheduled failures from Sync now failures."""

        policy = {
            "id": "policy-1",
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "providerParentId": "42",
        }
        recorded_runs = []
        recorded_actors = []

        def record_failure(_kind, _policy_id, _lease_owner, run, _error, actor_id):
            stored = {**run}
            recorded_runs.append(stored)
            recorded_actors.append(actor_id)
            return {
                "run": stored,
                "policy": {"id": "policy-1", "consecutiveFailures": 0},
            }

        with patch.object(
            backend_main.REPOSITORY,
            "fail_and_complete_ci_policy_preview",
            side_effect=record_failure,
        ):
            backend_main._record_connectwise_ci_preview_failure(
                policy,
                backend_main.ConnectWiseRequestError("provider-secret-marker"),
                trigger="continuous_preview",
                lease_owner="scheduled-worker",
            )
            backend_main._record_connectwise_ci_preview_failure(
                policy,
                backend_main.ConnectWiseRequestError("provider-secret-marker"),
                trigger="manual_sync",
                actor_id="operator",
                lease_owner="manual-worker",
            )

        self.assertTrue(recorded_runs[0]["message"].startswith("Continuous preview failed"))
        self.assertTrue(recorded_runs[1]["message"].startswith("Sync now failed"))
        self.assertEqual(recorded_runs[0]["attributes"]["trigger"], "continuous_preview")
        self.assertEqual(recorded_runs[1]["attributes"]["trigger"], "manual_sync")
        self.assertEqual(recorded_actors, [None, "operator"])
        self.assertNotIn("provider-secret-marker", json.dumps(recorded_runs))

    def test_direct_preview_discards_results_when_atomic_publication_loses_lease(self):
        """A stale direct worker must not fall back to the legacy multi-commit writes."""

        preview = {
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "providerCompanyId": "42",
            "providerCompanyName": "Acme Manufacturing",
            "credentialSource": "stored",
            "readOnly": True,
            "writesAttempted": False,
            "discovered": 1,
            "included": 1,
            "excluded": 0,
            "exclusionReasons": {},
            "availableTypes": [],
            "availableStatuses": [],
            "typeMappingSummary": {},
            "appliedPolicy": {
                "id": "policy-1",
                "revision": 1,
                "consecutiveFailures": 0,
            },
            "counts": {
                "create": 1,
                "update": 0,
                "link": 0,
                "unchanged": 0,
                "conflict": 0,
            },
            "items": [{"externalId": "100", "action": "create"}],
        }
        with (
            patch.object(
                backend_main,
                "_connectwise_configuration_preview",
                return_value=preview,
            ),
            patch.object(
                backend_main.REPOSITORY,
                "renew_ci_sync_policy_run",
                return_value={"id": "policy-1"},
            ),
            patch.object(
                backend_main.REPOSITORY,
                "publish_and_complete_ci_policy_preview",
                return_value=None,
            ) as publish,
            patch.object(backend_main.REPOSITORY, "record_sync_run") as legacy_record,
            patch.object(backend_main.REPOSITORY, "replace_ci_review_items") as legacy_queue,
            self.assertRaises(backend_main._IntegrationPreviewLeaseLost),
        ):
            backend_main._execute_connectwise_ci_preview(
                "acme",
                "42",
                actor_id=None,
                trigger="continuous_preview",
                policy_id="policy-1",
                lease_owner="stale-worker",
            )

        publish.assert_called_once()
        legacy_record.assert_not_called()
        legacy_queue.assert_not_called()

    def test_scheduled_worker_treats_reclaimed_lease_as_neutral(self):
        """Lease takeover must not create a false failure or success result."""

        policy = {
            "id": "policy-1",
            "provider": "connectwise",
            "companyId": "acme",
            "providerParentId": "42",
        }
        with (
            patch.object(
                backend_main,
                "_process_queued_integration_previews",
                return_value={"processed": 0, "succeeded": 0, "failed": 0},
            ),
            patch.object(backend_main, "_worker_flag", return_value=True),
            patch.object(
                backend_main.REPOSITORY,
                "claim_due_ci_sync_policy",
                return_value=policy,
            ),
            patch.object(
                backend_main,
                "_execute_connectwise_ci_preview",
                side_effect=backend_main.ConnectWiseRequestError("provider failed"),
            ),
            patch.object(
                backend_main,
                "_record_connectwise_ci_preview_failure",
                side_effect=backend_main._IntegrationPreviewLeaseLost("reclaimed"),
            ) as record_failure,
        ):
            result = backend_main._run_due_integration_previews(limit=1)

        self.assertEqual(result, {"processed": 1, "succeeded": 0, "failed": 0})
        record_failure.assert_called_once()

    def test_sync_now_failure_is_attributed_to_the_authenticated_operator(self):
        """Operator-triggered failure evidence should retain its human actor."""

        policy = {
            "id": "policy-1",
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "providerParentId": "42",
        }
        failure = backend_main.ConnectWiseRequestError("provider-secret-marker")
        with (
            patch.object(backend_main, "_require_integration_active"),
            patch.object(
                backend_main.REPOSITORY,
                "list_ci_sync_policies",
                return_value=[policy],
            ),
            patch.object(
                backend_main.REPOSITORY,
                "claim_ci_sync_policy_now",
                return_value=policy,
            ),
            patch.object(
                backend_main,
                "_execute_connectwise_ci_preview",
                side_effect=failure,
            ),
            patch.object(
                backend_main,
                "_record_connectwise_ci_preview_failure",
            ) as record_failure,
        ):
            response = self.client.post(
                "/api/integrations/connectwise/configurations/policies/policy-1/sync-now",
                headers=self._headers("operator@example.com"),
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(
            response.json()["detail"],
            "ConnectWise could not complete the requested read operation. "
            "Verify connectivity, credentials and API permissions.",
        )
        record_failure.assert_called_once_with(
            policy,
            failure,
            trigger="manual_sync",
            actor_id="operator",
            lease_owner=ANY,
        )

    def test_local_password_recovery_is_generic_single_use_and_revokes_credentials(self):
        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "senderName": "CMDB Hub",
                "status": "verified",
            },
            None,
        )
        old_session = self._login("admin@example.com")
        backend_main.REPOSITORY.create_api_token(
            {
                "id": "00000000-0000-0000-0000-000000000099",
                "userId": "admin",
                "name": "Recovery test",
                "tokenPrefix": "cmdb_pat_test",
                "tokenHash": "f" * 64,
                "scopes": ["cmdb:read"],
                "companyIds": [],
                "expiresAt": "2099-01-01T00:00:00Z",
            },
            "admin",
        )
        with (
            patch.dict(
                os.environ,
                {"AUTH_MODE": "local", "PUBLIC_BASE_URL": "http://localhost:3000"},
                clear=False,
            ),
            patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                return_value=DeliveryResult(202, "graph-reset-request"),
            ) as send_mock,
        ):
            known = self.client.post(
                "/api/password-reset/request",
                json={"email": "admin@example.com"},
            )
            unknown = self.client.post(
                "/api/password-reset/request",
                json={"email": "missing@example.com"},
            )
            self.assertEqual(known.status_code, 202, known.text)
            self.assertEqual(known.json(), unknown.json())
            self.assertTrue(self.client.get("/api/auth/config").json()["passwordResetAvailable"])

            reset_message = send_mock.call_args_list[0].args[1]
            raw_token = reset_message["bodyText"].split("resetToken=", 1)[1].splitlines()[0]
            self.assertTrue(raw_token.startswith("cmdb_reset_"))
            self.assertNotIn(raw_token, json.dumps(core.DB["passwordResets"]))
            self.assertNotIn(raw_token, json.dumps(core.DB.get("emailOutbox", [])))
            self.assertTrue(
                self.client.get("/api/password-reset/validate", params={"token": raw_token}).json()[
                    "valid"
                ]
            )

            completed = self.client.post(
                "/api/password-reset/complete",
                json={
                    "token": raw_token,
                    "newPassword": "Recovered-Local-Password-2026!",
                    "revokeApiTokens": True,
                },
            )
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertGreaterEqual(completed.json()["revokedSessions"], 1)
            self.assertEqual(completed.json()["revokedApiTokens"], 1)

        self.assertEqual(
            self.client.get(
                "/api/assets", headers={"Authorization": f"Bearer {old_session}"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            ).status_code,
            401,
        )
        self._login("admin@example.com", "Recovered-Local-Password-2026!")
        self.assertFalse(
            self.client.get("/api/password-reset/validate", params={"token": raw_token}).json()[
                "valid"
            ]
        )
        reused = self.client.post(
            "/api/password-reset/complete",
            json={"token": raw_token, "newPassword": "Another-Local-Password-2026!"},
        )
        self.assertEqual(reused.status_code, 400)
        self.assertTrue(
            any(item["action"] == "password_reset_completed" for item in core.DB["auditEvents"])
        )

    def test_local_password_recovery_rate_limits_by_identifier(self):
        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "verified",
            },
            None,
        )
        with (
            patch.dict(os.environ, {"PUBLIC_BASE_URL": "http://localhost:3000"}, clear=False),
            patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                return_value=DeliveryResult(202, "graph-rate-limit"),
            ) as send_mock,
        ):
            responses = [
                self.client.post("/api/password-reset/request", json={"email": "admin@example.com"})
                for _index in range(4)
            ]
        self.assertTrue(all(item.status_code == 202 for item in responses))
        self.assertTrue(all(item.json() == responses[0].json() for item in responses))
        self.assertEqual(len(core.DB["passwordResets"]), 3)
        self.assertEqual(send_mock.call_count, 3)
        self.assertFalse(
            any(
                item.get("templateKey") == "local_password_reset" for item in core.DB["emailOutbox"]
            )
        )

    def test_local_password_recovery_requires_a_trusted_public_origin(self):
        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "verified",
            },
            None,
        )
        for public_url in ("http://cmdb.example.com", "https://cmdb.example.com/subpath"):
            with (
                self.subTest(public_url=public_url),
                patch.dict(os.environ, {"PUBLIC_BASE_URL": public_url}, clear=False),
            ):
                self.assertFalse(
                    self.client.get("/api/auth/config").json()["passwordResetAvailable"]
                )

    def test_authenticated_local_password_change_revokes_the_current_session(self):
        session = self._login("client@acme.example")
        headers = {"Authorization": f"Bearer {session}"}
        changed = self.client.put(
            "/api/me/password",
            headers=headers,
            json={
                "currentPassword": "ChangeMe!",
                "newPassword": "Self-Service-Password-2026!",
                "revokeApiTokens": False,
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertGreaterEqual(changed.json()["revokedSessions"], 1)
        self.assertEqual(self.client.get("/api/me", headers=headers).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"email": "client@acme.example", "password": "ChangeMe!"},
            ).status_code,
            401,
        )
        self._login("client@acme.example", "Self-Service-Password-2026!")
        self.assertTrue(
            any(item["action"] == "password_changed_by_user" for item in core.DB["auditEvents"])
        )

    def test_notification_rules_queue_owner_events_without_sending(self):
        admin_headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        core.DB["assets"][0]["metadata"] = {
            "renewalDate": (date.today() + timedelta(days=10)).isoformat()
        }
        rules_response = self.client.get("/api/notifications/rules", headers=admin_headers)
        self.assertEqual(rules_response.status_code, 200, rules_response.text)
        renewal_rule = next(
            item for item in rules_response.json() if item["eventType"] == "asset_renewal"
        )
        updated = self.client.patch(
            f"/api/notifications/rules/{renewal_rule['id']}",
            headers=admin_headers,
            json={
                "name": renewal_rule["name"],
                "enabled": True,
                "leadDays": 30,
                "cadence": "daily",
                "recipientRoles": renewal_rule["recipientRoles"],
                "fallbackAddresses": ["fallback@example.com"],
                "templateKey": renewal_rule["templateKey"],
                "maxAttempts": 4,
                "expectedRevision": renewal_rule["revision"],
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        evaluated = self.client.post("/api/notifications/run?companyId=acme", headers=admin_headers)
        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        self.assertGreaterEqual(evaluated.json()["queued"], 1)
        evidence = self.client.get(
            "/api/notifications/events?companyId=acme", headers=admin_headers
        )
        self.assertEqual(evidence.status_code, 200)
        renewal_event = next(
            item for item in evidence.json() if item["eventType"] == "asset_renewal"
        )
        self.assertEqual(renewal_event["status"], "queued")
        self.assertEqual(renewal_event["recipients"], ["fallback@example.com"])
        self.assertEqual(
            self.client.get("/api/notifications/rules", headers=operator_headers).status_code,
            403,
        )

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

    def test_legacy_v2_asset_read_alias_matches_the_canonical_resource(self):
        headers = self._headers("admin@example.com")

        canonical = self.client.get("/api/assets/asset-1", headers=headers)
        compatibility = self.client.get("/api/v2/assets/asset-1", headers=headers)

        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(compatibility.status_code, 200)
        self.assertEqual(compatibility.json(), canonical.json())

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

    def test_sync_history_dashboard_and_report_respect_msp_customer_scope(self):
        """Every sync-history consumer must enforce the operator's tenant boundary."""

        runs = (
            (
                "run-global",
                "success",
                "Global provider health",
                "2026-07-27T10:00:00Z",
                {"operation": "company_discovery"},
            ),
            (
                "run-acme",
                "success",
                "Acme permitted detail",
                "2026-07-27T11:00:00Z",
                {"operation": "configuration_preview", "companyId": "acme"},
            ),
            (
                "run-northwind",
                "failed",
                "Northwind restricted detail",
                "2026-07-27T12:00:00Z",
                {"operation": "configuration_preview", "companyId": "northwind"},
            ),
        )
        for run_id, status, message, started_at, attributes in runs:
            backend_main.REPOSITORY.record_sync_run(
                "connectwise",
                {
                    "id": run_id,
                    "type": "connectwise",
                    "status": status,
                    "startedAt": started_at,
                    "finishedAt": started_at,
                    "discovered": 0,
                    "imported": 0,
                    "updated": 0,
                    "review": 0,
                    "message": message,
                    "attributes": attributes,
                },
                False,
                "admin",
            )

        operator_headers = self._headers("operator@example.com")
        operator_history = self.client.get("/api/sync-runs", headers=operator_headers)
        self.assertEqual(operator_history.status_code, 200, operator_history.text)
        self.assertEqual(
            {item["id"] for item in operator_history.json()},
            {"run-global", "run-acme"},
        )
        self.assertNotIn("Northwind restricted detail", operator_history.text)

        admin_history = self.client.get(
            "/api/sync-runs",
            headers=self._headers("admin@example.com"),
        )
        self.assertEqual(
            {item["id"] for item in admin_history.json()},
            {"run-global", "run-acme", "run-northwind"},
        )

        operator_dashboard = self.client.get("/api/dashboard", headers=operator_headers)
        self.assertEqual(operator_dashboard.status_code, 200, operator_dashboard.text)
        self.assertEqual(
            operator_dashboard.json()["integrations"][0]["lastRunAt"],
            "2026-07-27T11:00:00Z",
        )
        self.assertNotIn("Northwind restricted detail", operator_dashboard.text)

        operator_report = self.client.get(
            "/api/reports/integration-health/preview",
            headers=operator_headers,
        )
        self.assertEqual(operator_report.status_code, 200, operator_report.text)
        self.assertEqual(
            operator_report.json()["rows"][0]["message"],
            "Acme permitted detail",
        )
        self.assertNotIn("Northwind restricted detail", operator_report.text)

    def test_openapi_identifies_the_direct_fastapi_surface(self):
        schema = api.openapi()
        self.assertEqual(schema["info"]["title"], "CMDB Hub API")
        self.assertEqual(schema["info"]["version"], APPLICATION_VERSION)
        self.assertIn("/api/assets", schema["paths"])
        self.assertNotIn("/api/v2/assets/{asset_id}", schema["paths"])
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

    def test_local_login_identifier_throttle_is_generic_and_audit_bounded(self):
        settings = {
            "LOCAL_LOGIN_IDENTIFIER_LIMIT": "2",
            "LOCAL_LOGIN_IDENTIFIER_WINDOW_SECONDS": "900",
            "LOCAL_LOGIN_SOURCE_LIMIT": "20",
            "LOCAL_LOGIN_SOURCE_WINDOW_SECONDS": "900",
        }
        with patch.dict(os.environ, settings):
            failures = [
                self.client.post(
                    "/api/login",
                    json={"email": "admin@example.com", "password": "incorrect-secret"},
                )
                for _ in range(2)
            ]
            limited = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "incorrect-secret"},
            )
            repeated = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "incorrect-secret"},
            )

        self.assertTrue(all(response.status_code == 401 for response in failures))
        self.assertEqual(
            {response.json()["detail"] for response in failures},
            {"Invalid credentials"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(
            limited.json()["detail"],
            "Too many sign-in attempts. Try again later.",
        )
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)
        self.assertEqual(repeated.status_code, 429)
        throttle_events = [
            event for event in core.DB["auditEvents"] if event["action"] == "login_throttled"
        ]
        self.assertEqual(len(throttle_events), 1)
        self.assertEqual(throttle_events[0]["actorType"], "anonymous")
        self.assertNotIn("admin@example.com", json.dumps(core.DB["loginAttempts"]))
        self.assertNotIn("admin@example.com", json.dumps(throttle_events))

    def test_forwarded_header_cannot_evade_source_throttle_without_trusted_peer(self):
        settings = {
            "LOCAL_LOGIN_IDENTIFIER_LIMIT": "100",
            "LOCAL_LOGIN_SOURCE_LIMIT": "3",
            "LOCAL_LOGIN_SOURCE_WINDOW_SECONDS": "900",
        }
        responses = []
        with patch.dict(os.environ, settings):
            for index in range(4):
                responses.append(
                    self.client.post(
                        "/api/login",
                        headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
                        json={
                            "email": f"missing-{index}@example.com",
                            "password": "incorrect-secret",
                        },
                    )
                )

        self.assertEqual([response.status_code for response in responses], [401, 401, 401, 429])
        requester_hashes = {
            item["requesterHash"]
            for item in core.DB["loginAttempts"]
            if item["outcome"] != "throttled"
        }
        self.assertEqual(len(requester_hashes), 1)

    def test_local_totp_enrollment_recovery_replay_and_admin_reset(self):
        mfa_key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        with patch.dict(
            os.environ,
            {"MFA_ENCRYPTION_KEY": mfa_key, "LOCAL_MFA_POLICY": "optional"},
        ):
            admin_token = self._login("admin@example.com")
            headers = {"Authorization": f"Bearer {admin_token}"}
            required = self.client.patch(
                "/api/users/admin",
                headers=headers,
                json={
                    "email": "admin@example.com",
                    "displayName": "Admin",
                    "role": "platform_admin",
                    "companyIds": [],
                    "groupIds": [],
                    "apiAccessEnabled": False,
                    "mfaRequired": True,
                    "reason": "Require MFA for the platform administrator",
                },
            )
            self.assertEqual(required.status_code, 200)
            self.client.post("/api/logout", headers=headers)

            first = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json()["mfaEnrollmentRequired"])
            challenge_token = first.json()["challengeToken"]
            enrollment = self.client.post(
                "/api/login/mfa/enrollment", json={"challengeToken": challenge_token}
            )
            self.assertEqual(enrollment.status_code, 200)
            self.assertTrue(enrollment.json()["qrCodeDataUri"].startswith("data:image/png"))
            secret = enrollment.json()["manualKey"]
            completed = self.client.post(
                "/api/login/mfa",
                json={
                    "challengeToken": challenge_token,
                    "code": pyotp.TOTP(secret).now(),
                },
            )
            self.assertEqual(completed.status_code, 200)
            self.assertEqual(len(completed.json()["recoveryCodes"]), 10)
            recovery = completed.json()["recoveryCodes"]
            session = completed.json()["token"]
            self.client.post("/api/logout", headers={"Authorization": f"Bearer {session}"})

            recovery_login = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            ).json()
            recovered = self.client.post(
                "/api/login/mfa",
                json={
                    "challengeToken": recovery_login["challengeToken"],
                    "code": recovery[0],
                },
            )
            self.assertEqual(recovered.status_code, 200)
            recovered_headers = {"Authorization": f"Bearer {recovered.json()['token']}"}
            status = self.client.get("/api/me/mfa", headers=recovered_headers)
            self.assertEqual(status.json()["recoveryCodesRemaining"], 9)
            self.client.post("/api/logout", headers=recovered_headers)

            replay_challenge = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            ).json()
            replay = self.client.post(
                "/api/login/mfa",
                json={
                    "challengeToken": replay_challenge["challengeToken"],
                    "code": recovery[0],
                },
            )
            self.assertEqual(replay.status_code, 401)

            reset_challenge = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            ).json()
            reset_session = self.client.post(
                "/api/login/mfa",
                json={
                    "challengeToken": reset_challenge["challengeToken"],
                    "code": recovery[1],
                },
            ).json()["token"]
            reset_payload = {
                "reason": "Lost authenticator during test",
                "ticketReference": "CHG-1042",
                "confirmation": "admin@example.com",
                "administratorPassword": "ChangeMe!",
                "administratorCode": recovery[2],
            }
            operator_reset = self.client.post(
                "/api/users/admin/mfa/reset",
                headers=self._headers("operator@example.com"),
                json=reset_payload,
            )
            self.assertEqual(operator_reset.status_code, 403)
            wrong_confirmation = self.client.post(
                "/api/users/admin/mfa/reset",
                headers={"Authorization": f"Bearer {reset_session}"},
                json={**reset_payload, "confirmation": "wrong@example.com"},
            )
            self.assertEqual(wrong_confirmation.status_code, 400)
            wrong_password = self.client.post(
                "/api/users/admin/mfa/reset",
                headers={"Authorization": f"Bearer {reset_session}"},
                json={**reset_payload, "administratorPassword": "incorrect"},
            )
            self.assertEqual(wrong_password.status_code, 401)
            reset = self.client.post(
                "/api/users/admin/mfa/reset",
                headers={"Authorization": f"Bearer {reset_session}"},
                json=reset_payload,
            )
            self.assertEqual(reset.status_code, 200)
            self.assertGreaterEqual(reset.json()["revokedSessions"], 1)
            self.assertEqual(
                self.client.get(
                    "/api/me", headers={"Authorization": f"Bearer {reset_session}"}
                ).status_code,
                401,
            )
            next_login = self.client.post(
                "/api/login",
                json={"email": "admin@example.com", "password": "ChangeMe!"},
            )
            self.assertTrue(next_login.json()["mfaEnrollmentRequired"])
            audit_text = json.dumps(core.DB["auditEvents"])
            reset_event = next(
                item for item in core.DB["auditEvents"] if item["action"] == "mfa_reset_by_admin"
            )
            self.assertEqual(reset_event["reason"], "Lost authenticator during test")
            self.assertEqual(reset_event["metadata"]["ticketReference"], "CHG-1042")
            self.assertEqual(
                reset_event["metadata"]["administratorVerification"],
                "password+recovery_code",
            )
            self.assertNotIn(secret, audit_text)
            self.assertNotIn(recovery[0], audit_text)
            self.assertNotIn(recovery[2], audit_text)

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

    def test_provider_relationship_candidates_require_review_and_preserve_provenance(self):
        connection, policy, _review = self._seed_provider_link_review(
            "ncentral", "relationship-context"
        )
        relationship_context = {
            "policy_id": policy["id"],
            "expected_policy_revision": policy["revision"],
            "expected_connection_revision": connection["revision"],
            "provider_parent_id": "101",
        }
        core.DB["assets"].append(
            {
                "id": "asset-3",
                "companyId": "acme",
                "name": "ACME-VM01",
                "type": "Virtual machine",
                "status": "Active",
                "source": "ncentral",
                "fields": {},
            }
        )
        host_mapping = backend_main.REPOSITORY.record_provider_ci_mapping(
            "ncentral",
            "acme",
            {
                "externalId": "7001",
                "name": "ACME-HV01",
                "providerVersion": "host-v1",
                "providerParentId": "101",
                "identifiers": {},
            },
            "asset-1",
            "admin",
        )
        backend_main.REPOSITORY.record_provider_ci_mapping(
            "ncentral",
            "acme",
            {
                "externalId": "7002",
                "name": "ACME-VM01",
                "providerVersion": "guest-v1",
                "providerParentId": "101",
                "identifiers": {},
            },
            "asset-3",
            "admin",
        )
        candidate_values = [
            {
                "fromCiId": "asset-1",
                "toCiId": "asset-3",
                "fromExternalIdentity": {
                    "provider": "ncentral",
                    "externalId": "7001",
                },
                "toExternalIdentity": {
                    "provider": "ncentral",
                    "externalId": "7002",
                },
                "relationshipType": "hosts",
                "confidence": 1,
                "evidence": {
                    "messages": ["Explicit N-central guest device ID"],
                    "impactPolicy": "required",
                },
            }
        ]
        candidates = backend_main.REPOSITORY.upsert_relationship_candidates(
            "ncentral",
            "acme",
            host_mapping["id"],
            candidate_values,
        )
        candidate_id = candidates[0]["id"]

        legacy = self.client.post(
            f"/api/relationship-candidates/{candidate_id}/decision",
            headers=self._headers("admin@example.com"),
            json={
                "decision": "approve",
                "notes": "Legacy evidence must be refreshed",
                "expectedRevision": candidates[0]["revision"],
            },
        )
        self.assertEqual(legacy.status_code, 409, legacy.text)
        self.assertIn("generation evidence is missing", legacy.text)
        candidates = backend_main.REPOSITORY.upsert_relationship_candidates(
            "ncentral",
            "acme",
            host_mapping["id"],
            candidate_values,
            **relationship_context,
        )
        candidate_id = candidates[0]["id"]

        client_headers = self._headers("client@acme.example")
        visible = self.client.get(
            "/api/relationship-candidates?companyId=acme&state=pending",
            headers=client_headers,
        )
        self.assertEqual(visible.status_code, 200, visible.text)
        self.assertEqual(visible.json()[0]["fromName"], "ACME-DC01")
        blocked = self.client.post(
            f"/api/relationship-candidates/{candidate_id}/decision",
            headers=client_headers,
            json={"decision": "approve", "notes": "Reviewed evidence"},
        )
        self.assertEqual(blocked.status_code, 403)

        stale = self.client.post(
            f"/api/relationship-candidates/{candidate_id}/decision",
            headers=self._headers("admin@example.com"),
            json={
                "decision": "approve",
                "notes": "Reviewed stale provider evidence",
                "expectedRevision": candidates[0]["revision"] + 1,
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("changed; refresh", stale.text)
        self.assertFalse(
            any(
                item.get("fromId") == "asset-1"
                and item.get("toId") == "asset-3"
                and item.get("type") == "hosts"
                for item in backend_main.REPOSITORY.list_relationships()
            )
        )

        approved = self.client.post(
            f"/api/relationship-candidates/{candidate_id}/decision",
            headers=self._headers("admin@example.com"),
            json={
                "decision": "approve",
                "notes": "Reviewed provider evidence",
                "expectedRevision": candidates[0]["revision"],
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        relationship = approved.json()["approvedRelationship"]
        self.assertEqual(relationship["type"], "hosts")
        self.assertEqual(relationship["provenance"], "provider")
        self.assertEqual(relationship["sourceMappingId"], host_mapping["id"])
        self.assertEqual(relationship["confidence"], 1)
        self.assertEqual(approved.json()["revision"], candidates[0]["revision"] + 1)
        # The legacy proposal was observed once and then refreshed with a
        # governed provider generation before approval.
        self.assertEqual(approved.json()["observationCount"], 2)
        self.assertEqual(
            approved.json()["approvedRelationshipId"],
            relationship["id"],
        )

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
        asset_history = self.client.get("/api/changes?assetId=asset-1", headers=headers)
        self.assertEqual(asset_history.status_code, 200)
        self.assertEqual(
            [item["id"] for item in asset_history.json()],
            [created.json()["id"]],
        )
        unrelated_history = self.client.get("/api/changes?assetId=asset-2", headers=headers)
        self.assertEqual(unrelated_history.status_code, 200)
        self.assertEqual(unrelated_history.json(), [])
        denied_history = self.client.get(
            "/api/changes?assetId=asset-2",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        self.assertEqual(denied_history.status_code, 403)
        mismatched_customer = self.client.get(
            "/api/changes?companyId=northwind&assetId=asset-1",
            headers=headers,
        )
        self.assertEqual(mismatched_customer.status_code, 400)

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

    def test_change_templates_are_scoped_versioned_and_pinned_to_changes(self):
        admin_headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        client_headers = self._headers("client@acme.example")

        standards = self.client.get(
            "/api/change-templates?companyId=acme",
            headers=client_headers,
        )
        self.assertEqual(standards.status_code, 200, standards.text)
        self.assertGreaterEqual(len(standards.json()), 6)
        self.assertTrue(all(item["status"] == "published" for item in standards.json()))

        root_blocked = self.client.get(
            "/api/change-templates?includeDraft=true",
            headers=client_headers,
        )
        self.assertEqual(root_blocked.status_code, 403)

        content = standards.json()[0]["content"]
        customer_template = self.client.post(
            "/api/change-templates",
            headers=operator_headers,
            json={
                "companyId": "acme",
                "key": "acme_patch_procedure",
                "name": "Acme patch procedure",
                "description": "Customer-specific approved patch procedure.",
                "tags": ["patching", "acme"],
                "status": "draft",
                "content": content,
            },
        )
        self.assertEqual(customer_template.status_code, 201, customer_template.text)
        template = customer_template.json()
        self.assertEqual(template["version"], 1)

        client_drafts = self.client.get(
            "/api/change-templates?companyId=acme&includeDraft=true",
            headers=client_headers,
        )
        self.assertNotIn(template["id"], {item["id"] for item in client_drafts.json()})

        published = self.client.put(
            f"/api/change-templates/{template['id']}",
            headers=operator_headers,
            json={
                "expectedVersion": 1,
                "name": template["name"],
                "description": template["description"],
                "tags": template["tags"],
                "status": "published",
                "content": template["content"],
            },
        )
        self.assertEqual(published.status_code, 200, published.text)
        template = published.json()
        self.assertEqual(template["version"], 2)
        stale = self.client.put(
            f"/api/change-templates/{template['id']}",
            headers=operator_headers,
            json={
                "expectedVersion": 1,
                "name": template["name"],
                "description": template["description"],
                "tags": template["tags"],
                "status": "published",
                "content": template["content"],
            },
        )
        self.assertEqual(stale.status_code, 409)

        parameter_values = {
            parameter["key"]: (
                False
                if parameter["type"] == "boolean"
                else 10
                if parameter["type"] == "number"
                else parameter["options"][0]
                if parameter["type"] == "select"
                else "Verified input"
            )
            for parameter in template["content"]["parameters"]
        }
        change = self.client.post(
            "/api/changes",
            headers=admin_headers,
            json={
                "companyId": "acme",
                "scopeAssetIds": ["asset-1"],
                "title": "Patch ACME-DC01",
                "reason": "Approved maintenance",
                "implementationPlan": "Install approved updates",
                "validationPlan": "Validate directory health",
                "rollbackPlan": "Restore the approved backup",
                "templateId": template["id"],
                "templateVersion": template["version"],
                "templateParameters": parameter_values,
            },
        )
        self.assertEqual(change.status_code, 201, change.text)
        self.assertEqual(change.json()["templateId"], template["id"])
        self.assertEqual(change.json()["templateVersion"], 2)
        self.assertEqual(change.json()["templateSnapshot"]["name"], template["name"])
        self.assertEqual(change.json()["templateParameters"], parameter_values)

        missing_parameters = self.client.post(
            "/api/changes",
            headers=admin_headers,
            json={
                "companyId": "acme",
                "scopeAssetIds": ["asset-1"],
                "title": "Invalid template use",
                "reason": "Missing inputs",
                "implementationPlan": "Not applicable",
                "validationPlan": "Not applicable",
                "rollbackPlan": "Not applicable",
                "templateId": template["id"],
                "templateVersion": template["version"],
            },
        )
        self.assertEqual(missing_parameters.status_code, 400)

        audit = self.client.get(
            "/api/audit-events?companyId=acme",
            headers=admin_headers,
        )
        self.assertEqual(audit.status_code, 200)
        self.assertIn(
            "change_template",
            {item["entityType"] for item in audit.json()},
        )

    def test_change_reassignment_is_scoped_audited_concurrent_and_notified(self):
        headers = self._headers("admin@example.com")
        created = self.client.post(
            "/api/changes",
            headers=headers,
            json={
                "companyId": "acme",
                "scopeAssetIds": ["asset-1"],
                "title": "Patch domain controller",
                "reason": "Apply security updates",
                "implementationPlan": "Validate backup and install updates",
                "validationPlan": "Check directory health",
                "rollbackPlan": "Restore the VM snapshot",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        change = created.json()
        self.assertEqual(change["assignedUserId"], "admin")
        self.assertEqual(change["assignmentHistory"][0]["assignedUserId"], "admin")

        assignees = self.client.get(
            "/api/change-assignees?companyId=acme",
            headers=headers,
        )
        self.assertEqual(assignees.status_code, 200)
        self.assertEqual(
            {item["email"] for item in assignees.json()},
            {"admin@example.com", "operator@example.com"},
        )

        forbidden = self.client.post(
            f"/api/changes/{change['id']}/assignment",
            headers=self._headers("client@acme.example"),
            json={
                "expectedRevision": change["revision"],
                "assignedUserId": "operator",
                "reason": "Customer readers cannot reassign changes",
            },
        )
        self.assertEqual(forbidden.status_code, 403)

        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "verified",
            },
            None,
        )
        with (
            patch.dict(
                os.environ,
                {"PUBLIC_BASE_URL": "http://localhost:3000"},
                clear=False,
            ),
            patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                return_value=DeliveryResult(202, "graph-change-assignment"),
            ) as send_mock,
        ):
            reassigned = self.client.post(
                f"/api/changes/{change['id']}/assignment",
                headers=headers,
                json={
                    "expectedRevision": change["revision"],
                    "assignedUserId": "operator",
                    "reason": "Move this work to the on-call technician",
                    "notify": True,
                },
            )
        self.assertEqual(reassigned.status_code, 200, reassigned.text)
        updated = reassigned.json()
        self.assertEqual(updated["assignedUserId"], "operator")
        self.assertEqual(updated["assignedTechnician"], "operator")
        self.assertEqual(updated["revision"], 2)
        self.assertTrue(updated["assignmentNotification"]["queued"])
        self.assertEqual(updated["assignmentHistory"][-1]["previousUserId"], "admin")
        self.assertEqual(updated["assignmentHistory"][-1]["assignedUserId"], "operator")
        self.assertIn(change["number"], send_mock.call_args.args[1]["subject"])
        self.assertIn(
            f"/#/changes?changeId={change['id']}",
            send_mock.call_args.args[1]["bodyText"],
        )

        stale = self.client.post(
            f"/api/changes/{change['id']}/assignment",
            headers=headers,
            json={
                "expectedRevision": 1,
                "assignedUserId": None,
                "reason": "Attempt to overwrite a newer assignment",
            },
        )
        self.assertEqual(stale.status_code, 409)
        bypass = self.client.patch(
            f"/api/changes/{change['id']}",
            headers=headers,
            json={
                "expectedRevision": updated["revision"],
                "assignedTechnician": "free-text@example.com",
            },
        )
        self.assertEqual(bypass.status_code, 400)

        unassigned = self.client.post(
            f"/api/changes/{change['id']}/assignment",
            headers=headers,
            json={
                "expectedRevision": updated["revision"],
                "assignedUserId": None,
                "reason": "Return this change to the dispatch queue",
                "notify": False,
            },
        )
        self.assertEqual(unassigned.status_code, 200, unassigned.text)
        self.assertIsNone(unassigned.json()["assignedUserId"])
        self.assertEqual(unassigned.json()["assignedTechnician"], "")
        self.assertEqual(
            [
                event["action"]
                for event in core.DB["auditEvents"]
                if event["entityId"] == change["id"]
            ],
            ["unassigned", "reassigned", "created"],
        )

    def test_change_approval_links_are_scoped_hashed_single_use_and_audited(self):
        headers = self._headers("admin@example.com")
        core.DB["assets"].append(
            {
                "id": "business-system-1",
                "companyId": "acme",
                "name": "Sage 200",
                "type": "Business system",
                "status": "Active",
                "source": "manual",
                "fields": {
                    "criticality": "critical",
                    "signoffRequired": "yes",
                    "businessOwner": "Finance Director",
                },
            }
        )
        core.DB["relationships"].append(
            {
                "id": "relationship-approval-test",
                "fromId": "business-system-1",
                "toId": "asset-1",
                "type": "depends_on",
                "impactPolicy": "required",
            }
        )
        core.DB["contacts"].append(
            {
                "id": "finance-approver",
                "companyId": "acme",
                "displayName": "Financial Controller",
                "email": "controller@acme.example",
                "status": "active",
            }
        )
        core.DB["contactResponsibilities"].append(
            {
                "id": "responsibility-approval-test",
                "companyId": "acme",
                "assetId": "business-system-1",
                "contactId": "finance-approver",
                "role": "signoff_delegate",
                "isPrimary": True,
                "effectiveUntil": None,
            }
        )
        backend_main.REPOSITORY.update_email_connection(
            {
                "enabled": True,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "verified",
            },
            None,
        )
        created = self.client.post(
            "/api/changes",
            headers=headers,
            json={
                "companyId": "acme",
                "scopeAssetIds": ["asset-1"],
                "title": "Patch domain controller",
                "reason": "Apply security updates",
                "implementationPlan": "Validate backup and install updates",
                "validationPlan": "Check directory and Sage health",
                "rollbackPlan": "Restore the VM snapshot",
            },
        ).json()
        current = created
        for status in ("impact_review", "awaiting_approval"):
            response = self.client.post(
                f"/api/changes/{current['id']}/transition",
                headers=headers,
                json={"status": status, "expectedRevision": current["revision"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            current = response.json()

        with (
            patch.dict(os.environ, {"PUBLIC_BASE_URL": "http://localhost:3000"}, clear=False),
            patch.object(
                backend_main.EMAIL_SENDER,
                "send",
                return_value=DeliveryResult(202, "graph-change-approval"),
            ) as send_mock,
        ):
            sent = self.client.post(
                f"/api/changes/{current['id']}/approval-requests",
                headers=headers,
                json={"expectedRevision": current["revision"], "expiresInHours": 24},
            )
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(sent.json()[0]["approverEmail"], "controller@acme.example")
        self.assertEqual(sent.json()[0]["deliveryStatus"], "accepted")
        self.assertNotIn("tokenHash", sent.json()[0])
        body_text = send_mock.call_args.args[1]["bodyText"]
        raw_token = body_text.split("token=", 1)[1].splitlines()[0]
        self.assertTrue(raw_token.startswith("cmdb_approval_"))
        self.assertNotIn(raw_token, json.dumps(core.DB))

        validated = self.client.post("/api/change-approvals/validate", json={"token": raw_token})
        self.assertEqual(validated.status_code, 200, validated.text)
        self.assertEqual(validated.json()["change"]["businessSystems"][0]["name"], "Sage 200")
        self.assertNotIn("approverEmail", validated.json())

        approved = self.client.post(
            "/api/change-approvals/respond",
            json={"token": raw_token, "decision": "approved", "comments": "Window approved"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["changeStatus"], "approved")
        reused = self.client.post(
            "/api/change-approvals/respond",
            json={"token": raw_token, "decision": "approved", "comments": "Use again"},
        )
        self.assertEqual(reused.status_code, 404)
        stored = backend_main.REPOSITORY.get_change(current["id"])
        self.assertEqual(stored["approvals"][-1]["actorEmail"], "controller@acme.example")
        self.assertTrue(
            any(event["action"] == "external_approved" for event in core.DB["auditEvents"])
        )

    def test_ncentral_inventory_is_root_scoped_review_gated_and_token_safe(self):
        """Exercise the public N-central setup, mapping and reviewed import boundary."""

        headers = self._headers("admin@example.com")
        encryption_key = base64.urlsafe_b64encode(b"n" * 32).decode("ascii")
        device_discovery_calls = []

        class FakeNcentralClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def test_connection(self):
                return {
                    "reachable": True,
                    "accessExpirySeconds": 3600,
                    "accessibleOrganizations": 1,
                    "sampleOrganization": {
                        "externalId": "101",
                        "name": "Acme Manufacturing",
                        "type": "CUSTOMER",
                    },
                }

            def discover_customers(self):
                return [
                    {
                        "externalId": "101",
                        "identifier": "ACME-NC",
                        "name": "Acme Manufacturing",
                        "type": "CUSTOMER",
                        "parentId": "50",
                        "metadata": {"externalId": "ACME"},
                    }
                ]

            def list_device_filters(self):
                return [
                    {
                        "id": "managed-servers",
                        "name": "Managed servers",
                        "description": "Production server devices",
                    }
                ]

            def probe_device_capabilities(
                self,
                org_unit_id,
                *,
                device_id="",
                sample_size=3,
            ):
                self.assert_scope = (org_unit_id, device_id, sample_size)
                return {
                    "provider": "ncentral",
                    "readOnly": True,
                    "sampleCount": 1,
                    "samples": [
                        {
                            "sample": 1,
                            "endpoints": {
                                "assets": {
                                    "accessStatus": "available",
                                    "shape": {
                                        "type": "object",
                                        "fields": [{"name": "computerSystem", "type": "object"}],
                                    },
                                }
                            },
                        }
                    ],
                    "organizationEndpoints": {"active_issues": {"accessStatus": "available"}},
                }

            def discover_devices(
                self,
                org_unit_id,
                *,
                filter_id="",
                enrich_limit=0,
                enrichment_offset=0,
                priority_external_ids=None,
                progress_callback=None,
                cancel_requested=None,
            ):
                device_discovery_calls.append(
                    {
                        "orgUnitId": org_unit_id,
                        "filterId": filter_id,
                        "enrichLimit": enrich_limit,
                        "enrichmentOffset": enrichment_offset,
                        "priorityExternalIds": sorted(priority_external_ids or []),
                    }
                )
                if org_unit_id != "101":
                    raise AssertionError("unexpected organization")
                if filter_id and filter_id != "managed-servers":
                    raise AssertionError("unexpected device filter")
                if cancel_requested and cancel_requested():
                    raise AssertionError("preview unexpectedly cancelled")
                if progress_callback:
                    progress_callback(
                        {
                            "phase": "discovering",
                            "current": 2,
                            "total": 2,
                        }
                    )
                return [
                    {
                        "externalId": "7001",
                        "name": "ACME-NC01",
                        "type": "Server",
                        "status": "Active",
                        "providerTypeId": "server",
                        "providerTypeName": "Server",
                        "providerStatusId": "normal",
                        "providerStatusName": "Normal",
                        "fields": {
                            "serialNumber": "NC-SERIAL-7001",
                            "ipAddress": "10.0.0.71",
                            "lastAgentCheckIn": "2026-07-28T09:45:00Z",
                        },
                        "metadata": {
                            "lifecycle": "in_service",
                            "serialNumber": "NC-SERIAL-7001",
                        },
                        "identifiers": {"serial_number": "NC-SERIAL-7001"},
                        "inventoryCollections": {
                            "hardware": {
                                "manufacturer": "Dell",
                                "model": "PowerEdge R650",
                            },
                            "network_interfaces": [
                                {
                                    "key": "nic-1",
                                    "name": "Ethernet",
                                    "macAddress": "00:11:22:33:44:55",
                                    "ipAddresses": ["10.0.0.71"],
                                }
                            ],
                            "virtualization": {
                                "kind": "hypervisor_host",
                                "platform": "Hyper-V",
                                "evidence": ["installed_server_feature"],
                            },
                        },
                        "providerVersion": "2026-07-28T10:00:00Z",
                    },
                    {
                        "externalId": "7002",
                        "name": "ACME-NC02",
                        "type": "Server",
                        "status": "Active",
                        "providerTypeId": "server",
                        "providerTypeName": "Server",
                        "providerStatusId": "normal",
                        "providerStatusName": "Normal",
                        "fields": {"ipAddress": "10.0.0.72"},
                        "metadata": {"lifecycle": "in_service"},
                        "identifiers": {},
                        "providerVersion": "2026-07-28T10:00:00Z",
                    },
                ]

        environment = {
            "MFA_ENCRYPTION_KEY": encryption_key,
            "NCENTRAL_BASE_URL": "",
            "NCENTRAL_USER_API_TOKEN": "",
            "NCENTRAL_USER_API_TOKEN_FILE": "",
            "NCENTRAL_API_TOKEN": "",
            "NCENTRAL_PAGE_SIZE": "",
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(backend_main, "NcentralClient", FakeNcentralClient),
        ):
            configured = self.client.put(
                "/api/integrations/ncentral/config",
                headers=headers,
                json={
                    "enabled": True,
                    "baseUrl": "https://ncentral.example.com",
                    "userApiToken": "permanent-user-api-token",
                    "pageSize": 250,
                    "expectedRevision": 1,
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            self.assertTrue(configured.json()["hasCredentials"])
            self.assertNotIn("userApiToken", configured.json())
            tested = self.client.post("/api/integrations/ncentral/test", headers=headers)
            self.assertEqual(tested.status_code, 200, tested.text)
            self.assertEqual(
                [stage["status"] for stage in tested.json()["stages"]],
                ["passed", "passed", "passed", "passed"],
            )
            self.assertFalse(tested.json()["writesAttempted"])

            discovered = self.client.post("/api/integrations/ncentral/discover", headers=headers)
            self.assertEqual(discovered.status_code, 200, discovered.text)
            self.assertEqual(discovered.json()["discovered"], 1)
            mapped = self.client.put(
                "/api/integrations/ncentral/organizations/101/mapping",
                headers=headers,
                json={"companyId": "acme"},
            )
            self.assertEqual(mapped.status_code, 200, mapped.text)
            capability = self.client.post(
                "/api/integrations/ncentral/capabilities",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "externalId": "7001",
                    "sampleLimit": 1,
                },
            )
            self.assertEqual(capability.status_code, 200, capability.text)
            self.assertTrue(capability.json()["readOnly"])
            self.assertEqual(
                capability.json()["samples"][0]["endpoints"]["assets"]["accessStatus"],
                "available",
            )
            self.assertNotIn("permanent-user-api-token", capability.text)

            options = self.client.post(
                "/api/integrations/ncentral/devices/options",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101"},
            )
            self.assertEqual(options.status_code, 200, options.text)
            self.assertEqual(options.json()["deviceFilters"][0]["id"], "managed-servers")
            policy = self.client.get(
                "/api/integrations/ncentral/devices/policy?companyId=acme&providerCompanyId=101",
                headers=headers,
            )
            saved_policy = self.client.put(
                "/api/integrations/ncentral/devices/policy",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "providerFilterId": "managed-servers",
                    "typeMode": "selected",
                    "includedTypeIds": ["server"],
                    "typeMappings": {"server": "Server"},
                    "statusMode": "selected",
                    "includedStatusIds": ["normal"],
                    "relationshipAutomationMode": "auto_explicit",
                    "relationshipAutoApproveTypes": ["hosts"],
                    "relationshipMinConfidence": 0.99,
                    "relationshipMinObservations": 3,
                    "relationshipMaxEvidenceAgeHours": 24,
                    "syncMode": "manual",
                    "enabled": False,
                    "expectedRevision": policy.json()["revision"],
                },
            )
            self.assertEqual(saved_policy.status_code, 200, saved_policy.text)
            self.assertEqual(saved_policy.json()["providerFilterId"], "managed-servers")
            self.assertEqual(saved_policy.json()["relationshipAutomationMode"], "auto_explicit")
            self.assertEqual(saved_policy.json()["relationshipAutoApproveTypes"], ["hosts"])

            invalid_automation = self.client.put(
                "/api/integrations/ncentral/devices/policy",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "relationshipAutomationMode": "auto_explicit",
                    "relationshipAutoApproveTypes": ["connected_to"],
                    "expectedRevision": saved_policy.json()["revision"],
                },
            )
            self.assertEqual(invalid_automation.status_code, 400, invalid_automation.text)

            preview = self.client.post(
                "/api/integrations/ncentral/devices/preview",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101"},
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["counts"]["create"], 2)
            self.assertTrue(preview.json()["readOnly"])
            imported = self.client.post(
                "/api/integrations/ncentral/devices/import",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "externalIds": ["7001"],
                    "decisionNotes": "Approved test import",
                },
            )
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertEqual(imported.json()["created"], 1)
            self.assertEqual(
                device_discovery_calls[-1]["priorityExternalIds"],
                ["7001"],
            )
            imported_asset = next(
                item
                for item in backend_main.REPOSITORY.list_assets()
                if item["name"] == "ACME-NC01"
            )
            self.assertEqual(imported_asset["source"], "ncentral")
            self.assertEqual(imported_asset["externalId"], "7001")
            self.assertEqual(imported_asset["lastSeen"], "2026-07-28T09:45:00Z")
            inventory = self.client.get(
                f"/api/assets/{imported_asset['id']}/inventory",
                headers=headers,
            )
            self.assertEqual(inventory.status_code, 200, inventory.text)
            self.assertEqual(
                inventory.json()["collections"]["hardware"][0]["payload"]["model"],
                "PowerEdge R650",
            )
            self.assertEqual(len(inventory.json()["networkInterfaces"]), 1)
            calls_before_link = len(device_discovery_calls)
            linked = self.client.post(
                "/api/integrations/ncentral/devices/link",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "externalId": "7002",
                    "assetId": "asset-1",
                },
            )
            self.assertEqual(linked.status_code, 200, linked.text)
            self.assertEqual(linked.json()["assetName"], "ACME-DC01")
            self.assertEqual(len(device_discovery_calls), calls_before_link)

    def test_ncentral_policy_installs_missing_root_connection(self):
        """Allow first-use policy storage before credentials have been saved."""

        headers = self._headers("admin@example.com")
        self.assertIsNone(backend_main.REPOSITORY.get_integration_connection("ncentral"))
        environment = {
            "NCENTRAL_BASE_URL": "",
            "NCENTRAL_USER_API_TOKEN": "",
            "NCENTRAL_USER_API_TOKEN_FILE": "",
            "NCENTRAL_API_TOKEN": "",
            "NCENTRAL_PAGE_SIZE": "",
        }

        with patch.dict(os.environ, environment):
            response = self.client.put(
                "/api/integrations/ncentral/policy",
                headers=headers,
                json={"excludedExternalIds": ["202", " 101 ", "202"]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["discoveryPolicy"]["excludedExternalIds"],
            ["101", "202"],
        )
        stored = backend_main.REPOSITORY.get_integration_connection("ncentral")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            stored["configuration"]["discoveryPolicy"]["excludedExternalIds"],
            ["101", "202"],
        )
        self.assertEqual(
            sum(item.get("type") == "ncentral" for item in core.DB["integrations"]),
            1,
        )

    def test_ncentral_active_but_disabled_connection_reports_the_recovery_action(self):
        """Do not mislabel a disabled connection as active in provider-call errors."""

        headers = self._headers("admin@example.com")
        backend_main.REPOSITORY.ensure_integration_connection("ncentral", "N-central", "admin")

        tested = self.client.post("/api/integrations/ncentral/test", headers=headers)

        self.assertEqual(tested.status_code, 409, tested.text)
        self.assertEqual(
            tested.json()["detail"],
            "N-central is disabled. Save and enable the connection before making "
            "provider requests.",
        )

    def test_ncentral_auth_rejection_returns_an_actionable_token_safe_error(self):
        """Map provider authorization failures without exposing their response body."""

        marker = "provider-secret-response-marker"
        error = backend_main.NcentralRequestError(marker, status_code=401)

        detail = backend_main._ncentral_public_failure(error, "connection test")

        self.assertEqual(
            detail,
            "N-central rejected the saved User-API token or its access scope. "
            "Generate or verify the token, save the connection, and retry.",
        )
        self.assertNotIn(marker, detail)

    def test_ncentral_graphql_environment_defaults_do_not_lock_the_wizard(self):
        """Treat Compose defaults as defaults, not as environment-owned credentials."""

        environment = {
            "NCENTRAL_GRAPHQL_ENABLED": "false",
            "NCENTRAL_GRAPHQL_ENDPOINT": "https://api.n-able.com/graphql",
            "NCENTRAL_GRAPHQL_API_TOKEN": "",
            "NCENTRAL_GRAPHQL_API_TOKEN_FILE": "",
            "NCENTRAL_GRAPHQL_PAGE_SIZE": "100",
            "NCENTRAL_GRAPHQL_SERVER_ID": "",
        }
        with patch.dict(os.environ, environment):
            self.assertIsNone(backend_main._ncentral_graphql_environment_configuration())
        with (
            patch.dict(os.environ, {**environment, "NCENTRAL_GRAPHQL_ENABLED": "true"}),
            self.assertRaises(backend_main.NableGraphqlConfigurationError),
        ):
            backend_main._ncentral_graphql_environment_configuration()
        with (
            patch.dict(
                os.environ,
                {
                    **environment,
                    "NCENTRAL_GRAPHQL_ENABLED": "true",
                    "NCENTRAL_GRAPHQL_API_TOKEN": "valid-token",
                    "NCENTRAL_GRAPHQL_SERVER_ID": "x" * 161,
                },
            ),
            self.assertRaises(backend_main.NableGraphqlConfigurationError),
        ):
            backend_main._ncentral_graphql_environment_configuration()

    def test_ncentral_graphql_capability_cache_is_flattened_and_token_free(self):
        """Expose persisted capability metadata in the UI contract without raw envelopes."""

        backend_main.REPOSITORY.ensure_integration_connection("ncentral", "N-central", "admin")
        backend_main._record_ncentral_graphql_capability(
            status="supported",
            summary={"readOnly": True, "reachable": True, "candidateCount": 2},
        )
        with patch.dict(
            os.environ,
            {
                "NCENTRAL_GRAPHQL_ENABLED": "false",
                "NCENTRAL_GRAPHQL_ENDPOINT": "https://api.n-able.com/graphql",
                "NCENTRAL_GRAPHQL_API_TOKEN": "",
                "NCENTRAL_GRAPHQL_API_TOKEN_FILE": "",
            },
        ):
            public = backend_main._ncentral_graphql_connection_public()
        self.assertTrue(public["capability"]["reachable"])
        self.assertTrue(public["capability"]["readOnly"])
        self.assertEqual(public["capability"]["candidateCount"], 2)
        self.assertNotIn("summary", public["capability"])
        self.assertNotIn("graphqlApiToken", json.dumps(public))

    def test_ncentral_rest_and_graphql_saves_preserve_each_other(self):
        """Keep shared encrypted credentials and configuration transport-specific."""

        headers = self._headers("admin@example.com")
        encryption_key = base64.urlsafe_b64encode(b"g" * 32).decode("ascii")
        environment = {
            "MFA_ENCRYPTION_KEY": encryption_key,
            "NCENTRAL_BASE_URL": "",
            "NCENTRAL_USER_API_TOKEN": "",
            "NCENTRAL_USER_API_TOKEN_FILE": "",
            "NCENTRAL_API_TOKEN": "",
            "NCENTRAL_GRAPHQL_ENABLED": "false",
            "NCENTRAL_GRAPHQL_ENDPOINT": "https://api.n-able.com/graphql",
            "NCENTRAL_GRAPHQL_API_TOKEN": "",
            "NCENTRAL_GRAPHQL_API_TOKEN_FILE": "",
        }
        with patch.dict(os.environ, environment):
            invalid_graphql_token = self.client.put(
                "/api/integrations/ncentral/graphql/config",
                headers=headers,
                json={
                    "graphqlEnabled": True,
                    "graphqlEndpoint": "https://api.n-able.com/graphql",
                    "graphqlApiToken": "invalid token",
                    "graphqlPageSize": 73,
                    "graphqlServerId": "server-a",
                    "expectedRevision": 1,
                },
            )
            self.assertEqual(invalid_graphql_token.status_code, 400)
            self.assertIsNone(backend_main.REPOSITORY.get_integration_connection("ncentral"))
            graphql_saved = self.client.put(
                "/api/integrations/ncentral/graphql/config",
                headers=headers,
                json={
                    "graphqlEnabled": False,
                    "graphqlEndpoint": "https://api.n-able.com/graphql",
                    "graphqlApiToken": "graphql-token",
                    "graphqlPageSize": 73,
                    "graphqlServerId": "server-a",
                    "expectedRevision": 1,
                },
            )
            self.assertEqual(graphql_saved.status_code, 200, graphql_saved.text)
            rest_public = self.client.get(
                "/api/integrations/ncentral/config", headers=headers
            ).json()
            self.assertFalse(rest_public["configured"])
            self.assertTrue(rest_public["graphql"]["configured"])
            self.assertTrue(rest_public["graphql"]["hasCredentials"])
            self.assertFalse(rest_public["graphql"]["graphqlEnabled"])
            self.assertEqual(rest_public["graphql"]["connectionStatus"], "configured")

            missing_rest_token = self.client.put(
                "/api/integrations/ncentral/config",
                headers=headers,
                json={
                    "enabled": True,
                    "baseUrl": "https://ncentral.example.com",
                    "userApiToken": "",
                    "pageSize": 250,
                    "expectedRevision": graphql_saved.json()["revision"],
                },
            )
            self.assertEqual(missing_rest_token.status_code, 400, missing_rest_token.text)

            rest_saved = self.client.put(
                "/api/integrations/ncentral/config",
                headers=headers,
                json={
                    "enabled": True,
                    "baseUrl": "https://ncentral.example.com",
                    "userApiToken": "rest-token",
                    "pageSize": 250,
                    "expectedRevision": graphql_saved.json()["revision"],
                },
            )
            self.assertEqual(rest_saved.status_code, 200, rest_saved.text)
            self.assertEqual(rest_saved.json()["graphql"]["graphqlServerId"], "server-a")
            self.assertEqual(rest_saved.json()["graphql"]["graphqlPageSize"], 73)

            with patch.object(
                backend_main.NableGraphqlClient,
                "test_connection",
                return_value={
                    "reachable": True,
                    "customerCandidates": [],
                    "candidateCount": 0,
                    "truncated": False,
                },
            ):
                disabled_graphql_test = self.client.post(
                    "/api/integrations/ncentral/graphql/test",
                    headers=headers,
                )
            self.assertEqual(
                disabled_graphql_test.status_code,
                200,
                disabled_graphql_test.text,
            )
            self.assertTrue(disabled_graphql_test.json()["reachable"])
            self.assertFalse(rest_saved.json()["graphql"]["graphqlEnabled"])

            graphql_rotated = self.client.put(
                "/api/integrations/ncentral/graphql/config",
                headers=headers,
                json={
                    "graphqlEnabled": True,
                    "graphqlEndpoint": "https://api.n-able.com/graphql",
                    "graphqlApiToken": "",
                    "graphqlPageSize": 50,
                    "graphqlServerId": "server-b",
                    "expectedRevision": rest_saved.json()["revision"],
                },
            )
            self.assertEqual(graphql_rotated.status_code, 200, graphql_rotated.text)
            rest_rotated = self.client.put(
                "/api/integrations/ncentral/config",
                headers=headers,
                json={
                    "enabled": True,
                    "baseUrl": "https://ncentral.example.com/v2",
                    "userApiToken": "",
                    "pageSize": 300,
                    "expectedRevision": graphql_rotated.json()["revision"],
                },
            )
            self.assertEqual(rest_rotated.status_code, 200, rest_rotated.text)
            self.assertTrue(rest_rotated.json()["graphql"]["graphqlEnabled"])
            self.assertEqual(rest_rotated.json()["graphql"]["graphqlServerId"], "server-b")

        backend_main.REPOSITORY.update_integration_connection(
            "connectwise",
            {
                "configuration": {"baseUrl": "https://cw.example.com"},
                "credentialsEncrypted": "opaque-connectwise-ciphertext",
                "credentialsNonce": "opaque-nonce",
            },
            "admin",
        )
        with patch.dict(
            os.environ,
            {
                "CW_BASE_URL": "",
                "CW_COMPANY_ID": "",
                "CW_PUBLIC_KEY": "",
                "CW_PRIVATE_KEY": "",
                "CW_CLIENT_ID": "",
            },
        ):
            self.assertTrue(backend_main._connectwise_connection_public()["configured"])

    def test_ncentral_policy_omission_preserves_graphql_scope_and_empty_clears(self):
        """Keep old clients from clearing scope while allowing an explicit reset."""

        headers = self._headers("admin@example.com")
        backend_main.REPOSITORY.ensure_integration_connection("ncentral", "N-central", "admin")
        core.DB.setdefault("providerCompanyObservations", []).append(
            {
                "id": "observation-101",
                "provider": "ncentral",
                "externalId": "101",
                "name": "Acme N-central",
                "active": True,
            }
        )
        backend_main.REPOSITORY.map_provider_company("ncentral", "101", "acme", "admin")
        initial = backend_main.REPOSITORY.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            {"graphqlOrganizationIds": ["graphql-acme"]},
            0,
            "admin",
        )
        omitted = self.client.put(
            "/api/integrations/ncentral/devices/policy",
            headers=headers,
            json={
                "companyId": "acme",
                "providerCompanyId": "101",
                "expectedRevision": initial["revision"],
            },
        )
        self.assertEqual(omitted.status_code, 200, omitted.text)
        self.assertEqual(omitted.json()["graphqlOrganizationIds"], ["graphql-acme"])
        cleared = self.client.put(
            "/api/integrations/ncentral/devices/policy",
            headers=headers,
            json={
                "companyId": "acme",
                "providerCompanyId": "101",
                "graphqlOrganizationIds": [],
                "expectedRevision": omitted.json()["revision"],
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(cleared.json()["graphqlOrganizationIds"], [])

    def test_graphql_preview_enforces_customer_scope_before_cache_read(self):
        """Reject a restricted operator before any cache or provider lookup."""

        headers = self._headers("operator@example.com")
        with patch.object(
            backend_main,
            "_ncentral_graphql_cached_read",
            side_effect=AssertionError("cache must not be read"),
        ):
            for path in ("preview", "refresh"):
                with self.subTest(path=path):
                    response = self.client.post(
                        f"/api/integrations/ncentral/graphql/{path}",
                        headers=headers,
                        json={
                            "companyId": "northwind",
                            "providerCompanyId": "202",
                            "queryKey": "asset_inventory",
                        },
                    )
                    self.assertEqual(response.status_code, 403, response.text)

    def test_graphql_customer_catalogue_is_platform_admin_only(self):
        """Prevent an MSP operator from enumerating token-wide Customer identities."""

        headers = self._headers("operator@example.com")
        with patch.object(
            backend_main,
            "_ncentral_graphql_effective_configuration",
            side_effect=AssertionError("provider configuration must not be read"),
        ):
            response = self.client.post(
                "/api/integrations/ncentral/graphql/test",
                headers=headers,
            )

        self.assertEqual(response.status_code, 403, response.text)

    def test_graphql_server_detection_is_platform_admin_only(self):
        """Reject source-server detection before any scoped provider read."""

        headers = self._headers("operator@example.com")
        with patch.object(
            backend_main,
            "_ncentral_graphql_server_candidate_read",
            side_effect=AssertionError("provider identity must not be read"),
        ):
            response = self.client.post(
                "/api/integrations/ncentral/graphql/server-candidates",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101", "limit": 25},
            )

        self.assertEqual(response.status_code, 403, response.text)

    def test_graphql_server_detection_correlates_rest_ids_without_persisting(self):
        """Recommend only one complete, REST-corroborated source-server observation."""

        headers = self._headers("admin@example.com")
        backend_main.REPOSITORY.ensure_integration_connection("ncentral", "N-central", "admin")
        core.DB.setdefault("providerCompanyObservations", []).append(
            {
                "id": "observation-101",
                "provider": "ncentral",
                "externalId": "101",
                "name": "Acme N-central",
                "active": True,
            }
        )
        backend_main.REPOSITORY.map_provider_company("ncentral", "101", "acme", "admin")
        backend_main.REPOSITORY.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            {"graphqlOrganizationIds": ["graphql-acme"]},
            0,
            "admin",
        )
        revision_before = backend_main.REPOSITORY.get_integration_connection("ncentral")["revision"]

        class FakeGraphqlClient:
            truncated = False
            candidates = (
                {
                    "serverId": "server-a",
                    "deviceIds": ["7001", "7002"],
                    "graphqlDeviceCount": 2,
                },
            )

            def __init__(self, configuration):
                self.configuration = configuration

            def source_server_candidates(self, *, organization_ids, limit):
                if organization_ids != ["graphql-acme"] or limit != 25:
                    raise AssertionError("unexpected GraphQL detection scope")
                return {
                    "organizationIds": organization_ids,
                    "candidates": list(self.__class__.candidates),
                    "truncated": self.__class__.truncated,
                }

        class FakeRestClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def discover_devices(self, org_unit_id, *, enrich_limit):
                if org_unit_id != "101" or enrich_limit != 0:
                    raise AssertionError("unexpected REST detection scope")
                return [
                    {"externalId": "7002"},
                    {"externalId": "9001"},
                ]

        with (
            patch.object(
                backend_main,
                "_ncentral_graphql_effective_configuration",
                return_value=({"graphqlApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(
                backend_main,
                "_ncentral_effective_configuration",
                return_value=({"userApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(backend_main, "NableGraphqlClient", FakeGraphqlClient),
            patch.object(backend_main, "NcentralClient", FakeRestClient),
        ):
            response = self.client.post(
                "/api/integrations/ncentral/graphql/server-candidates",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101", "limit": 25},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(),
                {
                    "companyId": "acme",
                    "providerCompanyId": "101",
                    "candidates": [
                        {
                            "serverId": "server-a",
                            "graphqlDeviceCount": 2,
                            "restDeviceMatchCount": 1,
                        }
                    ],
                    "recommendedServerId": "server-a",
                    "confidence": "exact",
                    "truncated": False,
                    "readOnly": True,
                    "writesAttempted": False,
                },
            )

            FakeGraphqlClient.truncated = True
            truncated = self.client.post(
                "/api/integrations/ncentral/graphql/server-candidates",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101", "limit": 25},
            )
        self.assertEqual(truncated.status_code, 200, truncated.text)
        self.assertIsNone(truncated.json()["recommendedServerId"])
        self.assertEqual(truncated.json()["confidence"], "ambiguous")
        self.assertTrue(truncated.json()["truncated"])

        FakeGraphqlClient.truncated = False
        FakeGraphqlClient.candidates = (
            {
                "serverId": "server-a",
                "deviceIds": ["7002"],
                "graphqlDeviceCount": 1,
            },
            {
                "serverId": "server-b",
                "deviceIds": ["8001"],
                "graphqlDeviceCount": 1,
            },
        )
        with (
            patch.object(
                backend_main,
                "_ncentral_graphql_effective_configuration",
                return_value=({"graphqlApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(
                backend_main,
                "_ncentral_effective_configuration",
                return_value=({"userApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(backend_main, "NableGraphqlClient", FakeGraphqlClient),
            patch.object(backend_main, "NcentralClient", FakeRestClient),
        ):
            ambiguous = self.client.post(
                "/api/integrations/ncentral/graphql/server-candidates",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101", "limit": 25},
            )
        self.assertEqual(ambiguous.status_code, 200, ambiguous.text)
        self.assertIsNone(ambiguous.json()["recommendedServerId"])
        self.assertEqual(ambiguous.json()["confidence"], "ambiguous")
        self.assertEqual(
            [item["serverId"] for item in ambiguous.json()["candidates"]],
            ["server-a", "server-b"],
        )

        FakeGraphqlClient.candidates = (
            {
                "serverId": "server-c",
                "deviceIds": ["8001"],
                "graphqlDeviceCount": 1,
            },
        )
        with (
            patch.object(
                backend_main,
                "_ncentral_graphql_effective_configuration",
                return_value=({"graphqlApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(
                backend_main,
                "_ncentral_effective_configuration",
                return_value=({"userApiToken": "secret"}, "encrypted_database"),
            ),
            patch.object(backend_main, "NableGraphqlClient", FakeGraphqlClient),
            patch.object(backend_main, "NcentralClient", FakeRestClient),
        ):
            no_overlap = self.client.post(
                "/api/integrations/ncentral/graphql/server-candidates",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "101", "limit": 25},
            )
        self.assertEqual(no_overlap.status_code, 200, no_overlap.text)
        self.assertIsNone(no_overlap.json()["recommendedServerId"])
        self.assertEqual(no_overlap.json()["confidence"], "none")
        self.assertEqual(
            no_overlap.json()["candidates"],
            [
                {
                    "serverId": "server-c",
                    "graphqlDeviceCount": 1,
                    "restDeviceMatchCount": 0,
                }
            ],
        )
        revision_after = backend_main.REPOSITORY.get_integration_connection("ncentral")["revision"]
        self.assertEqual(revision_after, revision_before)
        self.assertNotIn("secret", response.text)

    def test_graphql_refresh_populates_cache_and_rest_consumes_cache_only(self):
        """Make explicit refresh the only GraphQL asset-provider read path."""

        headers = self._headers("admin@example.com")
        encryption_key = base64.urlsafe_b64encode(b"q" * 32).decode("ascii")
        environment = {
            "MFA_ENCRYPTION_KEY": encryption_key,
            "NCENTRAL_BASE_URL": "",
            "NCENTRAL_USER_API_TOKEN": "",
            "NCENTRAL_USER_API_TOKEN_FILE": "",
            "NCENTRAL_API_TOKEN": "",
            "NCENTRAL_GRAPHQL_ENABLED": "false",
            "NCENTRAL_GRAPHQL_ENDPOINT": "https://api.n-able.com/graphql",
            "NCENTRAL_GRAPHQL_API_TOKEN": "",
            "NCENTRAL_GRAPHQL_API_TOKEN_FILE": "",
        }
        backend_main.REPOSITORY.ensure_integration_connection("ncentral", "N-central", "admin")
        core.DB.setdefault("providerCompanyObservations", []).append(
            {
                "id": "observation-101",
                "provider": "ncentral",
                "externalId": "101",
                "name": "Acme N-central",
                "active": True,
            }
        )
        backend_main.REPOSITORY.map_provider_company("ncentral", "101", "acme", "admin")
        policy = backend_main.REPOSITORY.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            {"graphqlOrganizationIds": ["graphql-acme"]},
            0,
            "admin",
        )

        class FakeGraphqlClient:
            calls = 0

            def __init__(self, configuration):
                self.configuration = configuration

            def full_asset_inventory(self, *, organization_ids, maximum):
                self.__class__.calls += 1
                if organization_ids != ["graphql-acme"] or maximum != 10_000:
                    raise AssertionError("unexpected GraphQL scope")
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": organization_ids,
                    "totalCount": 1,
                    "truncated": False,
                    "pagesRead": 1,
                    "items": [
                        {
                            "graphqlAssetId": "graph-7001",
                            "name": "ACME-NC01",
                            "customer": {"id": "graphql-acme", "name": "Acme"},
                            "site": None,
                            "serviceOrganization": None,
                            "sourceIdentity": {
                                "provider": "ncentral",
                                "namespace": "nable_graphql_asset",
                                "externalId": "graph-7001",
                            },
                            "restIdentity": {
                                "provider": "ncentral",
                                "namespace": "ncentral_rest_device",
                                "serverId": "server-a",
                                "deviceId": "7001",
                                "crosswalkKey": "server-a:7001",
                            },
                            "summary": {
                                "system": {"manufacturer": "Dell", "model": "R650"},
                                "networkInterfaces": [
                                    {
                                        "name": "Ethernet",
                                        "macAddress": "00:11:22:33:44:55",
                                        "addresses": [{"address": "10.0.0.71"}],
                                    }
                                ],
                            },
                        }
                    ],
                }

        class FakeRestClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def discover_devices(self, org_unit_id, **_options):
                if org_unit_id != "101":
                    raise AssertionError("unexpected REST customer")
                return [
                    {
                        "externalId": "7001",
                        "name": "ACME-NC01",
                        "fields": {},
                        "metadata": {},
                        "inventoryCollections": {"software": {"count": 2}},
                    }
                ]

            def list_device_filters(self):
                return []

        with patch.dict(os.environ, environment):
            encrypted, nonce = backend_main.encrypt_secret(
                json.dumps(
                    {
                        "userApiToken": "rest-token",
                        "graphqlApiToken": "graphql-token",
                    }
                ),
                "integration:ncentral",
            )
            backend_main.REPOSITORY.update_integration_connection(
                "ncentral",
                {
                    "enabled": True,
                    "configuration": {
                        "baseUrl": "https://ncentral.example.com",
                        "pageSize": 250,
                        "graphqlEnabled": True,
                        "graphqlEndpoint": "https://api.n-able.com/graphql",
                        "graphqlPageSize": 100,
                        "graphqlServerId": "server-a",
                    },
                    "credentialsEncrypted": encrypted,
                    "credentialsNonce": nonce,
                },
                "admin",
            )
            with patch.object(backend_main, "NableGraphqlClient", FakeGraphqlClient):
                refreshed = self.client.post(
                    "/api/integrations/ncentral/graphql/refresh",
                    headers=headers,
                    json={
                        "companyId": "acme",
                        "providerCompanyId": "101",
                        "queryKey": "asset_inventory",
                    },
                )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(refreshed.json()["cache"]["status"], "fresh")
            self.assertEqual(refreshed.json()["cache"]["deviceCount"], 1)
            self.assertEqual(FakeGraphqlClient.calls, 1)

            with patch.object(
                backend_main,
                "NableGraphqlClient",
                side_effect=AssertionError("preview must not call GraphQL"),
            ):
                preview = self.client.post(
                    "/api/integrations/ncentral/graphql/preview",
                    headers=headers,
                    json={
                        "companyId": "acme",
                        "providerCompanyId": "101",
                        "queryKey": "asset_inventory",
                    },
                )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["cache"]["status"], "fresh")
            self.assertEqual(preview.json()["items"][0]["graphqlAssetId"], "graph-7001")

            with (
                patch.object(backend_main, "NcentralClient", FakeRestClient),
                patch.object(
                    backend_main,
                    "NableGraphqlClient",
                    side_effect=AssertionError("REST discovery must consume cache only"),
                ),
            ):
                _mapped, records, _source, saved_policy, _filters = (
                    backend_main._ncentral_device_context("acme", "101")
                )
            self.assertEqual(saved_policy["id"], policy["id"])
            self.assertEqual(records[0]["fields"]["nableGraphqlAssetId"], "graph-7001")
            self.assertEqual(records[0]["inventoryCollections"]["software"]["count"], 2)
            self.assertEqual(
                records[0]["inventoryCollections"]["network_interfaces"][0]["ipAddresses"],
                ["10.0.0.71"],
            )

    def test_connectwise_company_discovery_is_root_scoped_review_gated_and_secret_safe(self):
        client_token = self._login("client@acme.example")
        forbidden = self.client.get(
            "/api/integrations",
            headers={"Authorization": f"Bearer {client_token}"},
        )
        self.assertEqual(forbidden.status_code, 403)

        admin_token = self._login("admin@example.com")
        headers = {"Authorization": f"Bearer {admin_token}"}
        encryption_key = base64.urlsafe_b64encode(b"c" * 32).decode("ascii")

        class FakeConnectWiseClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def test_connection(self):
                return {
                    "reachable": True,
                    "sampleCompany": {"externalId": "42", "name": "Acme Manufacturing"},
                }

            def discover_companies(self):
                return [
                    {
                        "externalId": "42",
                        "identifier": "ACME",
                        "name": "Acme Manufacturing",
                        "status": "Active",
                        "type": "Customer",
                        "site": "Head office",
                        "deleted": False,
                        "lastUpdated": "2026-07-21T09:00:00Z",
                    },
                    {
                        "externalId": "84",
                        "identifier": "NEW",
                        "name": "Unmapped Customer",
                        "status": "Active",
                        "type": "Customer",
                        "site": "",
                        "deleted": False,
                        "lastUpdated": "",
                    },
                ]

            def discover_configurations(self, company_external_id):
                self.requested_company_id = company_external_id
                return [
                    {
                        "externalId": "501",
                        "name": "ACME-DC01",
                        "type": "Server",
                        "status": "Active",
                        "providerTypeId": "11",
                        "providerTypeName": "Server",
                        "providerStatusId": "3",
                        "providerStatusName": "Managed",
                        "fields": {"ipAddress": "10.0.0.51"},
                        "metadata": {"lifecycle": "in_service"},
                        "identifiers": {},
                        "providerVersion": "2026-07-21T10:00:00Z",
                    },
                    {
                        "externalId": "502",
                        "name": "CW-SRV-502",
                        "type": "Server",
                        "status": "Active",
                        "providerTypeId": "11",
                        "providerTypeName": "Server",
                        "providerStatusId": "3",
                        "providerStatusName": "Managed",
                        "fields": {
                            "serialNumber": "CW-SN-502",
                            "ipAddress": "10.0.0.52",
                        },
                        "metadata": {
                            "lifecycle": "in_service",
                            "operationalStatus": "unknown",
                            "serialNumber": "CW-SN-502",
                        },
                        "identifiers": {"serial_number": "CW-SN-502"},
                        "providerVersion": "2026-07-21T10:00:00Z",
                    },
                    {
                        "externalId": "503",
                        "name": "CW-ROUTER-503",
                        "type": "Router",
                        "status": "Inactive",
                        "providerTypeId": "12",
                        "providerTypeName": "Router",
                        "providerStatusId": "4",
                        "providerStatusName": "Retired",
                        "fields": {},
                        "metadata": {"lifecycle": "retired"},
                        "identifiers": {},
                        "providerVersion": "2026-07-21T10:00:00Z",
                    },
                ]

        environment = {
            "MFA_ENCRYPTION_KEY": encryption_key,
            "CW_BASE_URL": "",
            "CW_COMPANY_ID": "",
            "CW_PUBLIC_KEY": "",
            "CW_PRIVATE_KEY": "",
            "CW_CLIENT_ID": "",
            "CW_PAGE_SIZE": "",
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(backend_main, "ConnectWiseClient", FakeConnectWiseClient),
        ):
            configured = self.client.put(
                "/api/integrations/connectwise/config",
                headers=headers,
                json={
                    "enabled": True,
                    "baseUrl": "https://api.example.com/v4_6_release/apis/3.0",
                    "companyId": "ipt",
                    "clientId": "client-id",
                    "publicKey": "public-key",
                    "privateKey": "private-key",
                    "pageSize": 100,
                    "expectedRevision": 1,
                },
            )
            self.assertEqual(configured.status_code, 200, configured.text)
            self.assertTrue(configured.json()["hasCredentials"])
            self.assertNotIn("publicKey", configured.json())
            self.assertNotIn("privateKey", configured.json())

            catalogue = self.client.get("/api/integration-providers", headers=headers)
            self.assertEqual(catalogue.status_code, 200, catalogue.text)
            manifest = next(item for item in catalogue.json() if item["key"] == "connectwise")
            self.assertEqual(manifest["name"], "ConnectWise PSA")
            self.assertTrue(
                any(
                    operation["key"] == "change_ticket.create"
                    and operation["status"] == "disabled"
                    and operation["writes_provider"]
                    for operation in manifest["operations"]
                )
            )

            tested = self.client.post("/api/integrations/connectwise/test", headers=headers)
            self.assertEqual(tested.status_code, 200, tested.text)
            self.assertEqual(tested.json()["credentialSource"], "encrypted_database")
            self.assertEqual(
                [stage["status"] for stage in tested.json()["stages"]],
                ["passed", "passed", "passed"],
            )
            self.assertFalse(tested.json()["writesAttempted"])

            options = self.client.get(
                "/api/integrations/connectwise/discovery-options", headers=headers
            )
            self.assertEqual(options.status_code, 200, options.text)
            self.assertEqual(options.json()["availableStatuses"], ["Active"])
            self.assertEqual(options.json()["availableTypes"], ["Customer"])
            self.assertEqual(options.json()["sampled"], 2)
            self.assertFalse(options.json()["writesAttempted"])

            policy = self.client.put(
                "/api/integrations/connectwise/policy",
                headers=headers,
                json={
                    "includedStatuses": ["Active"],
                    "includedTypes": ["Customer"],
                    "includedSites": [],
                    "includeDeleted": False,
                    "excludedExternalIds": ["84"],
                    "expectedRevision": configured.json()["revision"],
                },
            )
            self.assertEqual(policy.status_code, 200, policy.text)
            self.assertEqual(policy.json()["discoveryPolicy"]["excludedExternalIds"], ["84"])
            preview = self.client.post(
                "/api/integrations/connectwise/discovery-preview", headers=headers
            )
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["discovered"], 2)
            self.assertEqual(preview.json()["included"], 1)
            self.assertEqual(preview.json()["excluded"], 1)
            self.assertTrue(preview.json()["readOnly"])
            self.assertFalse(preview.json()["writesAttempted"])
            self.assertFalse(preview.json()["truncated"])
            result = self.client.post("/api/integrations/connectwise/sync", headers=headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "review_required")
            self.assertEqual(result.json()["discovered"], 1)

            mapped_for_import = self.client.put(
                "/api/integrations/connectwise/companies/42/mapping",
                headers=headers,
                json={"companyId": "acme"},
            )
            self.assertEqual(mapped_for_import.status_code, 200, mapped_for_import.text)
            ci_options = self.client.post(
                "/api/integrations/connectwise/configurations/options",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "42"},
            )
            self.assertEqual(ci_options.status_code, 200, ci_options.text)
            self.assertEqual(ci_options.json()["discovered"], 3)
            self.assertEqual(
                [(item["id"], item["count"]) for item in ci_options.json()["availableTypes"]],
                [("12", 1), ("11", 2)],
            )
            default_ci_policy = self.client.get(
                "/api/integrations/connectwise/configurations/policy?companyId=acme&providerCompanyId=42",
                headers=headers,
            )
            self.assertEqual(default_ci_policy.status_code, 200, default_ci_policy.text)
            saved_ci_policy = self.client.put(
                "/api/integrations/connectwise/configurations/policy",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "typeMode": "selected",
                    "includedTypeIds": ["11"],
                    "typeMappings": {"11": "Server"},
                    "blockUnmappedTypes": True,
                    "statusMode": "selected",
                    "includedStatusIds": ["3"],
                    "excludedExternalIds": [],
                    "syncMode": "continuous_preview",
                    "intervalMinutes": 120,
                    "enabled": True,
                    "expectedRevision": default_ci_policy.json()["revision"],
                },
            )
            self.assertEqual(saved_ci_policy.status_code, 200, saved_ci_policy.text)
            self.assertEqual(saved_ci_policy.json()["includedTypeIds"], ["11"])
            self.assertEqual(saved_ci_policy.json()["typeMappings"], {"11": "Server"})
            self.assertTrue(saved_ci_policy.json()["blockUnmappedTypes"])
            ci_preview = self.client.post(
                "/api/integrations/connectwise/configurations/preview",
                headers=headers,
                json={"companyId": "acme", "providerCompanyId": "42"},
            )
            self.assertEqual(ci_preview.status_code, 200, ci_preview.text)
            self.assertEqual(ci_preview.json()["counts"]["create"], 1)
            self.assertEqual(ci_preview.json()["counts"]["conflict"], 1)
            self.assertEqual(ci_preview.json()["discovered"], 3)
            self.assertEqual(ci_preview.json()["included"], 2)
            self.assertEqual(ci_preview.json()["excluded"], 1)
            self.assertTrue(ci_preview.json()["readOnly"])
            self.assertEqual(ci_preview.json()["queueSummary"]["pending"], 2)
            policy_id = saved_ci_policy.json()["id"]
            leased = backend_main.REPOSITORY.claim_ci_sync_policy_now(
                policy_id,
                "test-worker",
                lease_seconds=120,
            )
            self.assertIsNotNone(leased)
            already_running = self.client.post(
                (f"/api/integrations/connectwise/configurations/policies/{policy_id}/sync-now"),
                headers=headers,
            )
            self.assertEqual(already_running.status_code, 409, already_running.text)
            backend_main.REPOSITORY.complete_ci_sync_policy_run(
                policy_id,
                lease_owner="test-worker",
                success=True,
            )
            manual_sync = self.client.post(
                (f"/api/integrations/connectwise/configurations/policies/{policy_id}/sync-now"),
                headers=headers,
            )
            self.assertEqual(manual_sync.status_code, 200, manual_sync.text)
            self.assertEqual(manual_sync.json()["queueSummary"]["pending"], 2)
            review_queue = self.client.get(
                "/api/integrations/connectwise/configurations/review-queue?companyId=acme",
                headers=headers,
            )
            self.assertEqual(review_queue.status_code, 200, review_queue.text)
            self.assertEqual(review_queue.json()["total"], 2)
            self.assertEqual(
                {item["externalId"] for item in review_queue.json()["items"]},
                {"501", "502"},
            )
            worker_status = self.client.get(
                "/api/integrations/continuous-preview/status", headers=headers
            )
            self.assertEqual(worker_status.status_code, 200, worker_status.text)
            self.assertEqual(worker_status.json()["enabledPolicies"], 1)
            self.assertEqual(worker_status.json()["pendingReviews"], 2)
            linked = self.client.post(
                "/api/integrations/connectwise/configurations/link",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalId": "501",
                    "assetId": "asset-1",
                },
            )
            self.assertEqual(linked.status_code, 200, linked.text)
            self.assertEqual(linked.json()["assetName"], "ACME-DC01")
            ci_import = self.client.post(
                "/api/integrations/connectwise/configurations/import",
                headers=headers,
                json={
                    "companyId": "acme",
                    "providerCompanyId": "42",
                    "externalIds": ["502"],
                },
            )
            self.assertEqual(ci_import.status_code, 200, ci_import.text)
            self.assertEqual(ci_import.json()["created"], 1)
            imported_asset = next(
                item
                for item in backend_main.REPOSITORY.list_assets()
                if item["name"] == "CW-SRV-502"
            )
            self.assertEqual(imported_asset["source"], "connectwise")
            self.assertEqual(imported_asset["externalId"], "502")
            cleared_queue = self.client.get(
                "/api/integrations/connectwise/configurations/review-queue?companyId=acme",
                headers=headers,
            )
            self.assertEqual(cleared_queue.status_code, 200, cleared_queue.text)
            self.assertEqual(cleared_queue.json(), {"items": [], "total": 0})

        companies = self.client.get("/api/integrations/connectwise/companies", headers=headers)
        self.assertEqual(companies.status_code, 200, companies.text)
        exact_match = next(item for item in companies.json() if item["externalId"] == "42")
        self.assertEqual(exact_match["mappedCompanyId"], "acme")
        self.assertIsNone(exact_match["suggestedCompanyId"])

        operator_headers = self._headers("operator@example.com")
        forbidden_mapping = self.client.put(
            "/api/integrations/connectwise/companies/42/mapping",
            headers=operator_headers,
            json={"companyId": "acme"},
        )
        self.assertEqual(forbidden_mapping.status_code, 403)
        mapped = self.client.put(
            "/api/integrations/connectwise/companies/42/mapping",
            headers=headers,
            json={"companyId": "acme"},
        )
        self.assertEqual(mapped.status_code, 200, mapped.text)
        self.assertEqual(mapped.json()["mappedCompanyId"], "acme")
        unmapped = self.client.delete(
            "/api/integrations/connectwise/companies/42/mapping", headers=headers
        )
        self.assertEqual(unmapped.status_code, 200, unmapped.text)

        history = self.client.get("/api/sync-runs", headers=headers)
        self.assertEqual(history.json()[0]["type"], "connectwise")
        filtered_history = self.client.get(
            (
                "/api/sync-runs?provider=connectwise"
                "&operation=configuration_preview&companyId=acme&limit=5"
            ),
            headers=headers,
        )
        self.assertEqual(filtered_history.status_code, 200, filtered_history.text)
        self.assertTrue(filtered_history.json())
        self.assertTrue(
            all(
                item["attributes"]["operation"] == "configuration_preview"
                and item["attributes"]["companyId"] == "acme"
                for item in filtered_history.json()
            )
        )
        self.assertEqual(
            filtered_history.json()[0]["attributes"]["trigger"],
            "manual_sync",
        )
        serialized = json.dumps(core.DB)
        self.assertNotIn("public-key", serialized)
        self.assertNotIn("private-key", serialized)

    def test_connectwise_failures_return_and_store_only_stable_public_messages(self):
        headers = self._headers("admin@example.com")
        environment_marker = "environment-secret-stack-marker"
        with patch.object(
            backend_main,
            "_connectwise_environment_configuration",
            side_effect=backend_main.ConnectWiseConfigurationError(environment_marker),
        ):
            configuration = self.client.get("/api/integrations/connectwise/config", headers=headers)
        self.assertEqual(configuration.status_code, 200, configuration.text)
        self.assertEqual(
            configuration.json()["lastError"],
            "ConnectWise environment configuration is invalid. "
            "Review the container environment settings.",
        )
        self.assertNotIn(environment_marker, configuration.text)

        request_marker = "provider-secret-stack-marker"
        with patch.object(
            backend_main,
            "_connectwise_effective_configuration",
            side_effect=backend_main.ConnectWiseRequestError(request_marker),
        ):
            tested = self.client.post("/api/integrations/connectwise/test", headers=headers)
        self.assertEqual(tested.status_code, 502, tested.text)
        self.assertEqual(
            tested.json()["detail"],
            "ConnectWise could not complete the requested read operation. "
            "Verify connectivity, credentials and API permissions.",
        )
        self.assertNotIn(request_marker, tested.text)
        self.assertNotIn(request_marker, json.dumps(core.DB))

    def test_integration_lifecycle_blocks_provider_calls_and_removes_credentials(self):
        headers = self._headers("admin@example.com")
        operator_headers = self._headers("operator@example.com")
        configured = backend_main.REPOSITORY.update_integration_connection(
            "connectwise",
            {
                "configuration": {
                    "baseUrl": "https://api.example.com/v4_6_release/apis/3.0",
                    "companyId": "ipt",
                    "clientId": "client-id",
                },
                "credentialsEncrypted": "ciphertext",
                "credentialsNonce": "nonce",
                "enabled": True,
                "connectionStatus": "verified",
                "expectedRevision": 1,
            },
            "admin",
        )
        impact = self.client.get(
            "/api/integrations/connectwise/lifecycle-impact", headers=operator_headers
        )
        self.assertEqual(impact.status_code, 200, impact.text)
        self.assertEqual(impact.json()["lifecycleStatus"], "active")
        forbidden = self.client.post(
            "/api/integrations/connectwise/lifecycle",
            headers=operator_headers,
            json={
                "action": "pause",
                "reason": "Maintenance window",
                "expectedRevision": configured["revision"],
            },
        )
        self.assertEqual(forbidden.status_code, 403)

        paused = self.client.post(
            "/api/integrations/connectwise/lifecycle",
            headers=headers,
            json={
                "action": "pause",
                "reason": "Maintenance window",
                "expectedRevision": configured["revision"],
            },
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["lifecycleStatus"], "paused")
        blocked_sync = self.client.post("/api/integrations/connectwise/sync", headers=headers)
        self.assertEqual(blocked_sync.status_code, 409, blocked_sync.text)

        resumed = self.client.post(
            "/api/integrations/connectwise/lifecycle",
            headers=headers,
            json={
                "action": "resume",
                "reason": "Maintenance completed",
                "expectedRevision": paused.json()["revision"],
            },
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        rejected = self.client.post(
            "/api/integrations/connectwise/remove",
            headers=headers,
            json={
                "confirmation": "ConnectWise",
                "reason": "Provider retired",
                "expectedRevision": resumed.json()["revision"],
            },
        )
        self.assertEqual(rejected.status_code, 400)
        removed = self.client.post(
            "/api/integrations/connectwise/remove",
            headers=headers,
            json={
                "confirmation": "ConnectWise Manage",
                "reason": "Provider retired",
                "expectedRevision": resumed.json()["revision"],
            },
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["integration"]["lifecycleStatus"], "removed")
        stored = backend_main.REPOSITORY.get_integration_connection("connectwise")
        self.assertFalse(stored["credentialsEncrypted"])
        self.assertEqual(stored["configuration"]["mode"], "removed")
        listed = self.client.get("/api/integrations", headers=headers).json()[0]
        self.assertEqual(listed["lifecycleStatus"], "removed")
        self.assertFalse(listed["enabled"])

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
