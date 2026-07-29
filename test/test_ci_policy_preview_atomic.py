"""Contract tests for synchronous preview publication under policy leases."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.cmdb.repository import StateRepository


def _policy() -> dict:
    """Return one enabled continuous-preview policy."""

    return {
        "providerFilterId": "",
        "typeMode": "all",
        "includedTypeIds": [],
        "typeMappings": {},
        "blockUnmappedTypes": False,
        "statusMode": "all",
        "includedStatusIds": [],
        "excludedExternalIds": [],
        "enrichmentMode": "balanced",
        "syncMode": "continuous_preview",
        "intervalMinutes": 60,
        "enabled": True,
    }


def _run(policy_id: str, status: str = "success") -> dict:
    """Return terminal direct-preview evidence for the leased policy."""

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "id": "00000000-0000-0000-0000-000000000901",
        "type": "ncentral",
        "status": status,
        "startedAt": timestamp,
        "finishedAt": timestamp,
        "discovered": 1 if status == "success" else 0,
        "imported": 0,
        "updated": 0,
        "review": 1 if status == "success" else 0,
        "message": "Preview completed." if status == "success" else "Preview failed.",
        "attributes": {
            "operation": "device_preview",
            "trigger": "manual_sync",
            "companyId": "acme",
            "providerCompanyId": "101",
            "policyId": policy_id,
            "readOnly": True,
        },
    }


def _review_item() -> dict:
    """Return one reviewable immutable provider observation."""

    return {
        "externalId": "device-101",
        "name": "Device 101",
        "action": "create",
        "reason": "New immutable provider identity",
        "record": {
            "externalId": "device-101",
            "name": "Device 101",
            "type": "Server",
            "status": "Active",
            "fields": {},
            "metadata": {},
        },
    }


class StateDirectPreviewAtomicTests(unittest.TestCase):
    """Keep direct preview history, queue and policy outcomes linearizable."""

    def setUp(self) -> None:
        self.state = {
            "companies": [{"id": "acme", "name": "Acme Manufacturing"}],
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
            _policy(),
            actor_id="admin",
        )

    def _claim(self, owner: str) -> dict:
        claimed = self.repository.claim_ci_sync_policy_now(
            self.policy["id"],
            owner,
            lease_seconds=120,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return claimed

    def test_success_publishes_run_queue_and_policy_once(self) -> None:
        self._claim("worker-a")
        saves_before = len(self.saved)

        published = self.repository.publish_and_complete_ci_policy_preview(
            "ncentral",
            self.policy["id"],
            "worker-a",
            _run(self.policy["id"]),
            [_review_item()],
            "operator",
        )

        self.assertIsNotNone(published)
        assert published is not None
        self.assertEqual(published["run"]["status"], "success")
        self.assertEqual(published["queueSummary"]["pending"], 1)
        self.assertEqual(len(self.state["syncRuns"]), 1)
        self.assertEqual(len(self.repository.list_ci_review_items("ncentral", "acme")), 1)
        self.assertIsNone(published["policy"].get("leaseOwner"))
        self.assertEqual(len(self.saved), saves_before + 1)

    def test_stale_success_and_failure_publish_nothing_after_takeover(self) -> None:
        self._claim("worker-a")
        raw_policy = next(
            item for item in self.state["integrationCiPolicies"] if item["id"] == self.policy["id"]
        )
        raw_policy["leaseUntil"] = (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
        self._claim("worker-b")
        before = deepcopy(self.state)

        success = self.repository.publish_and_complete_ci_policy_preview(
            "ncentral",
            self.policy["id"],
            "worker-a",
            _run(self.policy["id"]),
            [_review_item()],
            "operator",
        )
        failed = self.repository.fail_and_complete_ci_policy_preview(
            "ncentral",
            self.policy["id"],
            "worker-a",
            _run(self.policy["id"], "failed"),
            "Provider unavailable",
            "operator",
        )

        self.assertIsNone(success)
        self.assertIsNone(failed)
        self.assertEqual(self.state, before)

    def test_queue_failure_rolls_back_run_queue_policy_and_audit(self) -> None:
        self._claim("worker-a")
        before = deepcopy(self.state)

        with (
            patch.object(
                self.repository,
                "_replace_ci_review_items_in_state",
                side_effect=RuntimeError("forced queue failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "forced queue failure"),
        ):
            self.repository.publish_and_complete_ci_policy_preview(
                "ncentral",
                self.policy["id"],
                "worker-a",
                _run(self.policy["id"]),
                [_review_item()],
                "operator",
            )

        self.assertEqual(self.state, before)

    def test_renewal_never_resurrects_an_expired_or_reclaimed_lease(self) -> None:
        self._claim("worker-a")
        renewed = self.repository.renew_ci_sync_policy_run(
            self.policy["id"],
            "worker-a",
            lease_seconds=600,
        )
        self.assertIsNotNone(renewed)
        raw_policy = next(
            item for item in self.state["integrationCiPolicies"] if item["id"] == self.policy["id"]
        )
        raw_policy["leaseUntil"] = (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )

        expired = self.repository.renew_ci_sync_policy_run(
            self.policy["id"],
            "worker-a",
        )
        raw_policy["leaseOwner"] = "worker-b"
        raw_policy["leaseUntil"] = (
            (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        )
        reclaimed = self.repository.renew_ci_sync_policy_run(
            self.policy["id"],
            "worker-a",
        )

        self.assertIsNone(expired)
        self.assertIsNone(reclaimed)
        self.assertEqual(raw_policy["leaseOwner"], "worker-b")


if __name__ == "__main__":
    unittest.main()
