"""Focused contracts for the N-central setup-readiness checklist."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import backend.main as backend_main


def _public_connection(
    *,
    revision: int = 3,
    graphql_enabled: bool = True,
) -> dict:
    """Return token-safe connection state for readiness tests."""

    return {
        "id": "ncentral",
        "enabled": True,
        "configured": True,
        "credentialSource": "encrypted_database",
        "connectionStatus": "verified",
        "revision": revision,
        "lifecycleStatus": "active",
        "graphql": {
            "graphqlEnabled": graphql_enabled,
            "configured": graphql_enabled,
            "credentialSource": ("encrypted_database" if graphql_enabled else "not_configured"),
            "graphqlServerId": "server-a" if graphql_enabled else "",
            "capability": (
                {
                    "status": "supported",
                    "connectionRevision": revision,
                    "stale": False,
                    "checkedAt": "2026-08-03T08:00:00Z",
                    "expiresAt": "2026-08-03T08:15:00Z",
                }
                if graphql_enabled
                else {}
            ),
        },
    }


def _mapped_customer() -> dict:
    """Return one active explicit provider-to-CMDB customer mapping."""

    return {
        "externalId": "101",
        "name": "Acme N-central",
        "active": True,
        "mappedCompanyId": "acme",
        "mappedCompanyName": "Acme Manufacturing",
    }


def _saved_policy(*, continuous: bool = False) -> dict:
    """Return one saved customer policy with an explicit GraphQL scope."""

    return {
        "id": "policy-1",
        "provider": "ncentral",
        "companyId": "acme",
        "providerParentId": "101",
        "graphqlOrganizationIds": ["graphql-acme"],
        "syncMode": "continuous_preview" if continuous else "manual",
        "enabled": continuous,
        "consecutiveFailures": 0,
    }


class NcentralSetupStatusTests(unittest.TestCase):
    """Verify deterministic readiness gates without making provider requests."""

    def _repository(
        self,
        *,
        revision: int = 3,
        rest_test_revision: int | None = 3,
        companies: list[dict] | None = None,
        policies: list[dict] | None = None,
    ) -> MagicMock:
        repository = MagicMock()
        repository.get_integration_connection.return_value = {
            "id": "ncentral",
            "revision": revision,
            "credentialsEncrypted": "must-never-be-returned",
        }
        if rest_test_revision is None:
            rest_snapshot = None
        else:
            rest_snapshot = {
                "status": "supported",
                "summary": {"connectionRevision": rest_test_revision, "readOnly": True},
                "checkedAt": "2026-08-03T08:00:00Z",
                "expiresAt": "2026-08-04T08:00:00Z",
                "stale": False,
            }
        repository.get_integration_capability_snapshot.return_value = rest_snapshot
        repository.list_provider_companies.return_value = deepcopy(
            [_mapped_customer()] if companies is None else companies
        )
        repository.list_ci_sync_policies.return_value = deepcopy(
            [_saved_policy()] if policies is None else policies
        )
        return repository

    def test_ready_status_has_ordered_safe_gates_and_cached_customer_evidence(self) -> None:
        """A fully prepared manual setup should be ready and leak no stored secrets."""

        repository = self._repository()
        cached = {
            "cache": {
                "status": "fresh",
                "complete": True,
                "deviceCount": 14,
                "providerAssetCount": 14,
                "eligibleDeviceCount": 14,
                "lastRefreshedAt": "2026-08-03T08:05:00Z",
                "expiresAt": "2026-08-03T09:05:00Z",
            }
        }
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_connection_public",
                return_value=_public_connection(),
            ),
            patch.object(
                backend_main,
                "_ncentral_graphql_cached_read",
                return_value=cached,
            ) as cached_read,
        ):
            result = backend_main._ncentral_setup_status()

        self.assertEqual(result["overallStatus"], "ready")
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            [gate["key"] for gate in result["gates"]],
            [
                "connection_installed",
                "connection_active",
                "connection_enabled",
                "rest_credentials",
                "rest_connection_test",
                "customer_discovery",
                "customer_mapping",
                "device_policies",
                "graphql_configuration",
                "graphql_capability",
                "graphql_server_binding",
                "graphql_customer_scope",
                "graphql_cache",
                "continuous_worker",
            ],
        )
        cached_read.assert_called_once()
        self.assertEqual(result["customers"][0]["graphqlScopeCount"], 1)
        self.assertNotIn("must-never-be-returned", json.dumps(result))

    def test_changed_connection_revision_is_blocked_before_customer_mapping(self) -> None:
        """A successful test for an older revision must never authorize the new settings."""

        repository = self._repository(
            revision=4,
            rest_test_revision=3,
            companies=[
                {
                    "externalId": "101",
                    "name": "Unmapped customer",
                    "active": True,
                    "mappedCompanyId": None,
                }
            ],
            policies=[],
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_connection_public",
                return_value=_public_connection(revision=4, graphql_enabled=False),
            ),
        ):
            result = backend_main._ncentral_setup_status()

        self.assertEqual(result["overallStatus"], "blocked")
        self.assertIn(
            "rest_connection_test",
            {item["gate"] for item in result["blockers"]},
        )
        self.assertIn("customer_mapping", {item["gate"] for item in result["blockers"]})
        self.assertEqual(result["recommendedNextAction"]["key"], "test_connection")
        rest_gate = next(gate for gate in result["gates"] if gate["key"] == "rest_connection_test")
        self.assertEqual(rest_gate["evidence"]["state"], "stale_revision")

    def test_continuous_policy_requires_an_enabled_healthy_worker(self) -> None:
        """Continuous preview is blocked when deployment scheduling is disabled."""

        repository = self._repository(policies=[_saved_policy(continuous=True)])
        public = _public_connection(graphql_enabled=False)
        public["graphql"]["configured"] = False
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(backend_main, "_ncentral_connection_public", return_value=public),
            patch.object(backend_main, "_worker_flag", return_value=False),
            patch.object(
                backend_main,
                "_worker_runtime_summary",
                return_value={
                    "workerHealthy": False,
                    "executionMode": "dedicated",
                    "heartbeatAgeSeconds": None,
                },
            ),
        ):
            result = backend_main._ncentral_setup_status()

        worker_gate = next(gate for gate in result["gates"] if gate["key"] == "continuous_worker")
        self.assertEqual(worker_gate["status"], "blocked")
        self.assertIn("continuous_worker", {item["gate"] for item in result["blockers"]})

    def test_capability_records_are_bound_to_the_saved_revision(self) -> None:
        """Both provider transports must retain only safe revision-bound test metadata."""

        repository = MagicMock()
        repository.get_integration_connection.return_value = {"revision": 7}
        with patch.object(backend_main, "REPOSITORY", repository):
            backend_main._record_ncentral_rest_capability(
                status="supported",
                summary={"reachable": True},
            )
            backend_main._record_ncentral_graphql_capability(
                status="supported",
                summary={"queryCount": 2},
            )

        calls = repository.upsert_integration_capability_snapshot.call_args_list
        self.assertEqual(
            [call.args[1] for call in calls], ["rest.connection", "graphql.connection"]
        )
        self.assertEqual(calls[0].args[2]["summary"]["connectionRevision"], 7)
        self.assertEqual(calls[1].args[2]["summary"]["connectionRevision"], 7)
        self.assertNotIn("secret", json.dumps([call.args for call in calls]))

    def test_setup_status_route_is_platform_admin_only(self) -> None:
        """MSP operators may run previews but may not inspect root setup readiness."""

        with (
            patch.object(
                backend_main,
                "current_user",
                return_value={"id": "operator", "role": "msp_operator"},
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            backend_main.get_ncentral_setup_status(MagicMock())
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
