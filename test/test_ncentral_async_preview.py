"""Focused tests for the durable asynchronous N-central preview queue."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as core
import backend.main as backend_main
from src.cmdb.repository import StateRepository


def _policy_input(
    *,
    enrichment_mode: str = "balanced",
    continuous: bool = False,
) -> dict:
    """Return one safe manual-preview policy used by repository tests."""

    return {
        "providerFilterId": "managed-servers",
        "typeMode": "all",
        "includedTypeIds": [],
        "typeMappings": {"server": "Server"},
        "blockUnmappedTypes": False,
        "statusMode": "all",
        "includedStatusIds": [],
        "excludedExternalIds": [],
        "enrichmentMode": enrichment_mode,
        "syncMode": "continuous_preview" if continuous else "manual",
        "intervalMinutes": 360,
        "enabled": continuous,
    }


def _review_item(external_id: str, action: str = "create") -> dict:
    """Return one normalized reviewable N-central device observation."""

    return {
        "externalId": external_id,
        "name": f"Device {external_id}",
        "action": action,
        "reason": "New immutable provider identity",
        "confidence": 0.95,
        "assetId": None,
        "assetName": "",
        "changedFields": [],
        "record": {
            "externalId": external_id,
            "name": f"Device {external_id}",
            "type": "Server",
            "status": "Active",
            "providerTypeId": "server",
            "providerTypeName": "Server",
            "providerStatusId": "normal",
            "providerStatusName": "Normal",
            "fields": {"serialNumber": f"SN-{external_id}"},
            "metadata": {},
        },
    }


class StateRepositoryNcentralPreviewQueueTests(unittest.TestCase):
    """Verify the local queue has the same durable lifecycle as PostgreSQL."""

    def setUp(self) -> None:
        self.state = {
            "companies": [
                {"id": "acme", "name": "Acme Manufacturing", "externalIds": {}},
                {"id": "northwind", "name": "Northwind Traders", "externalIds": {}},
            ],
            "users": [],
            "integrations": [
                {
                    "id": "ncentral",
                    "name": "N-central",
                    "type": "ncentral",
                    "enabled": True,
                    "lifecycleStatus": "active",
                }
            ],
            "syncRuns": [],
            "assets": [],
            "relationships": [],
        }
        self.saved: list[dict] = []
        self.repository = StateRepository(
            self.state,
            lambda value: self.saved.append(deepcopy(value)),
        )
        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(),
            actor_id="admin",
        )

    def _queue(
        self,
        *,
        company_id: str = "acme",
        provider_company_id: str = "101",
        policy: dict | None = None,
        trigger: str = "manual_preview",
    ) -> dict:
        selected = policy or self.policy
        return self.repository.create_ci_preview_run(
            "ncentral",
            company_id,
            provider_company_id,
            selected["id"],
            trigger,
            "operator",
            selected,
        )

    def test_active_scope_is_deduplicated_then_claimed_once(self) -> None:
        first = self._queue()
        duplicate = self._queue(trigger="manual_sync")

        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(len(self.state["syncRuns"]), 1)
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["phase"], "queued")
        self.assertTrue(first["canCancel"])
        self.assertNotIn("dedupeKey", first)
        self.assertNotIn("leaseOwner", first)

        claimed = self.repository.claim_ci_preview_run("worker-a", provider="ncentral")

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], first["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["phase"], "starting")
        self.assertEqual(claimed["attemptCount"], 1)
        self.assertEqual(claimed["policySnapshot"]["revision"], self.policy["revision"])
        self.assertIsNone(self.repository.claim_ci_preview_run("worker-b", provider="ncentral"))

    def test_progress_is_owner_guarded_bounded_and_payload_free(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("worker-a")

        denied = self.repository.renew_ci_preview_run(
            queued["id"],
            "worker-b",
            {"phase": "enriching", "current": 2, "total": 4},
        )
        renewed = self.repository.renew_ci_preview_run(
            queued["id"],
            "worker-a",
            {
                "phase": "enriching",
                "current": 2,
                "total": 4,
                "percent": 250,
                "devicesDiscovered": 4,
                "devicesEnriched": 2,
                "counts": {"create": 1},
                "providerRecords": [{"token": "must-not-leak"}],
                "token": "must-not-leak",
            },
            "Enriched 2 of 4 devices.",
        )

        self.assertIsNone(denied)
        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertEqual(renewed["progress"]["phase"], "enriching")
        self.assertEqual(renewed["progress"]["current"], 2)
        self.assertEqual(renewed["progress"]["total"], 4)
        self.assertEqual(renewed["progress"]["percent"], 100)
        self.assertEqual(renewed["progress"]["discovered"], 4)
        self.assertEqual(renewed["progress"]["enriched"], 2)
        self.assertNotIn("providerRecords", renewed["progress"])
        self.assertNotIn("token", renewed["progress"])
        self.assertNotIn("leaseOwner", renewed)
        self.assertNotIn("leaseUntil", renewed)

    def test_get_and_latest_respect_explicit_customer_scope(self) -> None:
        acme_run = self._queue()
        northwind_policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "northwind",
            "202",
            _policy_input(enrichment_mode="fast"),
            actor_id="admin",
        )
        northwind_run = self._queue(
            company_id="northwind",
            provider_company_id="202",
            policy=northwind_policy,
        )

        self.assertEqual(
            self.repository.get_sync_run(acme_run["id"], company_ids={"acme"})["id"],
            acme_run["id"],
        )
        self.assertIsNone(self.repository.get_sync_run(acme_run["id"], company_ids={"northwind"}))
        self.assertEqual(
            self.repository.get_latest_ci_preview_run("ncentral", "acme", "101")["id"],
            acme_run["id"],
        )
        self.assertEqual(
            self.repository.get_latest_ci_preview_run("ncentral", "northwind", "202")["id"],
            northwind_run["id"],
        )
        self.assertIsNone(self.repository.get_latest_ci_preview_run("ncentral", "acme", "202"))

    def test_queued_cancel_is_immediate_and_releases_policy(self) -> None:
        queued = self._queue()

        cancelled = self.repository.request_sync_run_cancel(queued["id"], "admin")

        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["phase"], "cancelled")
        self.assertTrue(cancelled["cancelRequested"])
        self.assertFalse(cancelled["canCancel"])
        self.assertTrue(cancelled["canRetry"])
        stored_policy = self.repository.get_ci_sync_policy("ncentral", "acme", "101")
        self.assertIsNone(stored_policy.get("leaseOwner"))
        self.assertIsNone(stored_policy.get("leaseUntil"))
        self.assertIsNone(self.repository.claim_ci_preview_run("worker-a"))

    def test_queued_cancel_does_not_clear_a_newer_policy_owner(self) -> None:
        queued = self._queue()
        raw_policy = next(
            item for item in self.state["integrationCiPolicies"] if item["id"] == self.policy["id"]
        )
        raw_policy["leaseOwner"] = "current-worker"
        raw_policy["leaseUntil"] = (
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        )

        cancelled = self.repository.request_sync_run_cancel(queued["id"], "admin")

        self.assertIsNotNone(cancelled)
        self.assertEqual(raw_policy["leaseOwner"], "current-worker")
        self.assertIsNotNone(raw_policy["leaseUntil"])

    def test_exhausted_run_cleanup_does_not_clear_a_newer_policy_owner(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("stale-worker")
        raw_run = next(item for item in self.state["syncRuns"] if item["id"] == queued["id"])
        raw_run["attemptCount"] = raw_run["maxAttempts"]
        raw_run["leaseUntil"] = (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
        raw_policy = next(
            item for item in self.state["integrationCiPolicies"] if item["id"] == self.policy["id"]
        )
        raw_policy["leaseOwner"] = "current-worker"
        raw_policy["leaseUntil"] = (
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        )

        claimed = self.repository.claim_ci_preview_run("cleanup-worker")

        self.assertIsNone(claimed)
        self.assertEqual(raw_run["status"], "failed")
        self.assertEqual(raw_policy["leaseOwner"], "current-worker")
        self.assertIsNotNone(raw_policy["leaseUntil"])

    def test_running_cancel_is_cooperative_and_wins_completion_race(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("worker-a")

        cancelling = self.repository.request_sync_run_cancel(queued["id"], "admin")

        self.assertIsNotNone(cancelling)
        assert cancelling is not None
        self.assertEqual(cancelling["status"], "running")
        self.assertEqual(cancelling["phase"], "cancelling")
        self.assertTrue(cancelling["cancelRequested"])
        self.assertFalse(cancelling["canCancel"])
        self.assertTrue(self.repository.is_sync_run_cancel_requested(queued["id"], "worker-a"))
        self.assertFalse(self.repository.is_sync_run_cancel_requested(queued["id"], "worker-b"))

        terminal = self.repository.complete_ci_preview_run(
            queued["id"],
            "worker-a",
            {"discovered": 4, "included": 4, "counts": {"create": 4}},
        )

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(terminal["phase"], "cancelled")
        self.assertIsNone(terminal["previewSummary"])

    def test_cancelled_atomic_publication_does_not_change_review_queue(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("worker-a")
        self.repository.request_sync_run_cancel(queued["id"], "admin")

        terminal = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-a",
            [_review_item("should-not-publish")],
            {"discovered": 1, "included": 1, "counts": {"create": 1}},
        )

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "cancelled")
        self.assertEqual(
            self.repository.list_ci_review_items_for_run(
                "ncentral",
                queued["id"],
                "acme",
            ),
            [],
        )
        self.assertNotIn(
            "review_queue_refreshed",
            [
                item["action"]
                for item in self.state["auditEvents"]
                if item["entityId"] == self.policy["id"]
                and item.get("metadata", {}).get("syncRunId") == queued["id"]
            ],
        )

    def test_atomic_publication_rejects_wrong_or_expired_worker_lease(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("worker-a")

        denied = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-b",
            [_review_item("wrong-owner")],
            {"discovered": 1, "included": 1, "counts": {"create": 1}},
        )
        raw_run = next(item for item in self.state["syncRuns"] if item["id"] == queued["id"])
        raw_run["leaseUntil"] = (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
        expired = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-a",
            [_review_item("expired-owner")],
            {"discovered": 1, "included": 1, "counts": {"create": 1}},
        )

        self.assertIsNone(denied)
        self.assertIsNone(expired)
        self.assertEqual(self.repository.list_ci_review_items("ncentral", "acme"), [])
        self.assertEqual(raw_run["status"], "running")

    def test_policy_edit_preserves_active_preview_lease_and_snapshot(self) -> None:
        queued = self._queue()
        self.repository.claim_ci_preview_run("worker-a")

        changed = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(enrichment_mode="full"),
            expected_revision=self.policy["revision"],
            actor_id="admin",
        )

        self.assertEqual(changed["leaseOwner"], "worker-a")
        self.assertIsNotNone(changed["leaseUntil"])
        self.assertIsNone(
            self.repository.claim_ci_sync_policy_now(
                self.policy["id"],
                "worker-b",
            )
        )
        claimed_run = self.repository.get_sync_run(queued["id"])
        self.assertEqual(claimed_run["status"], "running")

    def test_retry_uses_immutable_original_policy_snapshot(self) -> None:
        queued = self._queue()
        self.repository.request_sync_run_cancel(queued["id"], "admin")
        changed_policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(enrichment_mode="full"),
            expected_revision=self.policy["revision"],
            actor_id="admin",
        )

        retry = self.repository.retry_ci_preview_run(queued["id"], "operator")
        claimed = self.repository.claim_ci_preview_run("worker-retry")

        self.assertNotEqual(retry["id"], queued["id"])
        self.assertEqual(retry["retryOfId"], queued["id"])
        self.assertEqual(retry["trigger"], "retry")
        self.assertEqual(changed_policy["enrichmentMode"], "full")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], retry["id"])
        self.assertEqual(claimed["policySnapshot"]["enrichmentMode"], "balanced")
        self.assertEqual(
            claimed["policySnapshot"]["revision"],
            self.policy["revision"],
        )

    def test_retry_preserves_manual_sync_schedule_bookkeeping(self) -> None:
        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(continuous=True),
            expected_revision=self.policy["revision"],
            actor_id="admin",
        )
        queued = self._queue(trigger="manual_sync")
        self.repository.claim_ci_preview_run("worker-failure")
        self.repository.fail_ci_preview_run(
            queued["id"],
            "worker-failure",
            "Temporary provider failure",
        )
        failed_policy = self.repository.get_ci_sync_policy("ncentral", "acme", "101")
        self.assertEqual(failed_policy["consecutiveFailures"], 1)
        self.assertIsNotNone(failed_policy["nextRunAt"])

        retry = self.repository.retry_ci_preview_run(queued["id"], "operator")
        claimed = self.repository.claim_ci_preview_run("worker-retry")
        self.assertIsNotNone(claimed)
        terminal = self.repository.publish_and_complete_ci_preview_run(
            retry["id"],
            "worker-retry",
            [],
            {"discovered": 0, "included": 0, "counts": {}},
        )

        self.assertEqual(retry["trigger"], "retry")
        self.assertIsNotNone(terminal)
        recovered_policy = self.repository.get_ci_sync_policy("ncentral", "acme", "101")
        self.assertEqual(recovered_policy["consecutiveFailures"], 0)
        self.assertIsNotNone(recovered_policy["lastSuccessAt"])
        self.assertFalse(recovered_policy["lastError"])
        self.assertIsNotNone(recovered_policy["nextRunAt"])
        raw_retry = next(item for item in self.state["syncRuns"] if item["id"] == retry["id"])
        self.assertEqual(raw_retry["attributes"]["scheduleTrigger"], "manual_sync")

    def test_worker_executes_queued_snapshot_after_live_policy_changes(self) -> None:
        queued = self._queue()
        changed_policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(enrichment_mode="full"),
            expected_revision=self.policy["revision"],
            actor_id="admin",
        )
        claimed = self.repository.claim_ci_preview_run("worker-snapshot")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        observed_policy: dict = {}

        def preview(
            company_id: str,
            provider_company_id: str,
            *,
            progress_callback,
            cancel_requested,
            policy_override,
        ) -> dict:
            del progress_callback
            self.assertFalse(cancel_requested())
            self.assertEqual(company_id, "acme")
            self.assertEqual(provider_company_id, "101")
            observed_policy.update(deepcopy(policy_override))
            return {
                "companyId": company_id,
                "companyName": "Acme Manufacturing",
                "providerCompanyId": provider_company_id,
                "providerCompanyName": "Acme Manufacturing",
                "credentialSource": "configured",
                "readOnly": True,
                "writesAttempted": False,
                "discovered": 0,
                "included": 0,
                "excluded": 0,
                "exclusionReasons": {},
                "availableTypes": [],
                "availableStatuses": [],
                "typeMappingSummary": {
                    "mapped": 0,
                    "unmapped": 0,
                    "blocked": 0,
                    "unmappedTypes": [],
                },
                "appliedPolicy": deepcopy(policy_override),
                "counts": {
                    "create": 0,
                    "update": 0,
                    "link": 0,
                    "unchanged": 0,
                    "conflict": 0,
                },
                "items": [],
                "enrichment": {
                    "mode": policy_override["enrichmentMode"],
                    "assetDetailsRequested": 0,
                    "bounded": True,
                },
            }

        with (
            patch.object(backend_main, "REPOSITORY", self.repository),
            patch.object(backend_main, "_ncentral_device_preview", side_effect=preview),
        ):
            backend_main._execute_queued_ncentral_preview(
                claimed,
                "worker-snapshot",
            )

        terminal = self.repository.get_sync_run(queued["id"], company_ids={"acme"})
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(changed_policy["enrichmentMode"], "full")
        self.assertEqual(observed_policy["enrichmentMode"], "balanced")
        self.assertEqual(observed_policy["revision"], self.policy["revision"])
        self.assertEqual(terminal["status"], "success")

    def test_terminal_result_is_aggregate_only_and_review_items_are_run_scoped(self) -> None:
        first = self._queue()
        self.repository.claim_ci_preview_run("worker-1")
        first_terminal = self.repository.publish_and_complete_ci_preview_run(
            first["id"],
            "worker-1",
            [_review_item("7001")],
            {
                "discovered": 2,
                "included": 1,
                "excluded": 1,
                "counts": {
                    "create": 1,
                    "update": 0,
                    "link": 0,
                    "unchanged": 0,
                    "conflict": 0,
                },
                "items": [{"token": "secret-provider-payload"}],
                "accessToken": "secret-provider-token",
                "message": "Preview completed without writes.",
            },
        )

        self.assertIsNotNone(first_terminal)
        assert first_terminal is not None
        self.assertEqual(first_terminal["status"], "success")
        self.assertEqual(first_terminal["progress"]["percent"], 100)
        self.assertEqual(first_terminal["previewSummary"]["counts"]["create"], 1)
        self.assertNotIn("items", first_terminal["previewSummary"])
        self.assertNotIn("accessToken", first_terminal["previewSummary"])
        raw_first = next(item for item in self.state["syncRuns"] if item["id"] == first["id"])
        self.assertNotIn("items", raw_first["attributes"]["resultSummary"])
        self.assertNotIn("accessToken", raw_first["attributes"]["resultSummary"])

        second = self._queue()
        self.repository.claim_ci_preview_run("worker-2")
        self.repository.publish_and_complete_ci_preview_run(
            second["id"],
            "worker-2",
            [_review_item("7002", action="conflict")],
            {
                "discovered": 1,
                "included": 1,
                "counts": {"conflict": 1},
            },
        )

        first_items = self.repository.list_ci_review_items_for_run(
            "ncentral",
            first["id"],
            "acme",
        )
        second_items = self.repository.list_ci_review_items_for_run(
            "ncentral",
            second["id"],
            "acme",
        )
        self.assertEqual([item["externalId"] for item in first_items], ["7001"])
        self.assertEqual([item["externalId"] for item in second_items], ["7002"])
        self.assertEqual(first_items[0]["state"], "resolved")
        self.assertEqual(second_items[0]["state"], "pending")

    def test_active_run_is_retained_when_terminal_history_is_bounded(self) -> None:
        self.state["syncRuns"] = [
            {
                "id": f"terminal-{index:03d}",
                "type": "ncentral",
                "status": "success",
                "requestedAt": f"2026-07-28T00:{index % 60:02d}:00Z",
                "attributes": {
                    "operation": "device_preview",
                    "companyId": "acme",
                    "providerCompanyId": "101",
                },
            }
            for index in range(300)
        ]

        active = self._queue()

        retained_ids = {item["id"] for item in self.state["syncRuns"]}
        self.assertIn(active["id"], retained_ids)
        self.assertEqual(
            sum(item["status"] == "success" for item in self.state["syncRuns"]),
            250,
        )
        self.assertEqual(len(self.state["syncRuns"]), 251)


class NcentralPreviewQueueRouteTests(unittest.TestCase):
    """Verify the polling API contract and tenant denial without provider calls."""

    def setUp(self) -> None:
        self.original_db = core.DB
        self.original_database_mode = core.DATABASE_MODE
        self.original_repository = backend_main.REPOSITORY
        self.state = {
            "companies": [
                {"id": "acme", "name": "Acme Manufacturing", "externalIds": {}},
                {"id": "northwind", "name": "Northwind Traders", "externalIds": {}},
            ],
            "users": [
                {
                    "id": "acme-operator",
                    "email": "operator@acme.example",
                    "role": "msp_operator",
                    "companyIds": ["acme"],
                    "status": "active",
                },
                {
                    "id": "northwind-operator",
                    "email": "operator@northwind.example",
                    "role": "msp_operator",
                    "companyIds": ["northwind"],
                    "status": "active",
                },
            ],
            "integrations": [
                {
                    "id": "ncentral",
                    "name": "N-central",
                    "type": "ncentral",
                    "enabled": True,
                    "lifecycleStatus": "active",
                    "connectionStatus": "verified",
                }
            ],
            "providerCompanyObservations": [
                {
                    "id": "provider-company-101",
                    "provider": "ncentral",
                    "externalId": "101",
                    "name": "Acme Manufacturing",
                    "type": "CUSTOMER",
                    "active": True,
                }
            ],
            "providerCompanyMappings": [
                {
                    "id": "mapping-101",
                    "provider": "ncentral",
                    "externalId": "101",
                    "externalName": "Acme Manufacturing",
                    "companyId": "acme",
                    "active": True,
                }
            ],
            "syncRuns": [],
            "assets": [],
            "relationships": [],
        }
        core.DATABASE_MODE = "local development state"
        core.DB = self.state
        backend_main.REPOSITORY = StateRepository(self.state, lambda _value: None)
        self.policy = backend_main.REPOSITORY.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(),
            actor_id="acme-operator",
        )
        expires = datetime.now(UTC) + timedelta(hours=1)
        expires_at = expires.isoformat().replace("+00:00", "Z")
        backend_main.REPOSITORY.create_session(
            backend_main.opaque_token_hash("acme-token"),
            "acme-operator",
            expires_at,
        )
        backend_main.REPOSITORY.create_session(
            backend_main.opaque_token_hash("northwind-token"),
            "northwind-operator",
            expires_at,
        )
        self.client = TestClient(backend_main.api)

    def tearDown(self) -> None:
        backend_main.REPOSITORY = self.original_repository
        core.DB = self.original_db
        core.DATABASE_MODE = self.original_database_mode

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_queue_and_poll_return_stable_progress_contract(self) -> None:
        queued = self.client.post(
            "/api/integrations/ncentral/devices/preview-runs",
            headers=self._headers("acme-token"),
            json={"companyId": "acme", "providerCompanyId": "101"},
        )

        self.assertEqual(queued.status_code, 202, queued.text)
        body = queued.json()
        self.assertEqual(body["companyId"], "acme")
        self.assertEqual(body["providerCompanyId"], "101")
        self.assertEqual(body["policyId"], self.policy["id"])
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["phase"], "queued")
        self.assertEqual(
            body["progress"],
            {
                "current": 0,
                "total": 0,
                "percent": 0,
                "discovered": 0,
                "enriched": 0,
                "reviewed": 0,
            },
        )
        self.assertTrue(body["canCancel"])
        self.assertFalse(body["canRetry"])
        self.assertNotIn("leaseOwner", body)
        self.assertNotIn("policySnapshot", body)

        polled = self.client.get(
            f"/api/integrations/ncentral/devices/preview-runs/{body['id']}",
            headers=self._headers("acme-token"),
        )
        latest = self.client.get(
            "/api/integrations/ncentral/devices/preview-runs/latest"
            "?companyId=acme&providerCompanyId=101",
            headers=self._headers("acme-token"),
        )

        self.assertEqual(polled.status_code, 200, polled.text)
        self.assertEqual(polled.json(), body)
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["id"], body["id"])

    def test_poll_and_latest_deny_cross_customer_operator(self) -> None:
        queued = self.client.post(
            "/api/integrations/ncentral/devices/preview-runs",
            headers=self._headers("acme-token"),
            json={"companyId": "acme", "providerCompanyId": "101"},
        )
        self.assertEqual(queued.status_code, 202, queued.text)
        run_id = queued.json()["id"]

        cross_tenant_poll = self.client.get(
            f"/api/integrations/ncentral/devices/preview-runs/{run_id}",
            headers=self._headers("northwind-token"),
        )
        cross_tenant_latest = self.client.get(
            "/api/integrations/ncentral/devices/preview-runs/latest"
            "?companyId=acme&providerCompanyId=101",
            headers=self._headers("northwind-token"),
        )

        self.assertEqual(cross_tenant_poll.status_code, 404, cross_tenant_poll.text)
        self.assertEqual(cross_tenant_latest.status_code, 403, cross_tenant_latest.text)
        self.assertNotIn("Acme", cross_tenant_poll.text)


if __name__ == "__main__":
    unittest.main()
