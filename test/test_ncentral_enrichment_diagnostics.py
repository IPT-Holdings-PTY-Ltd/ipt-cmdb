"""Focused contracts for persisted N-central enrichment diagnostics."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import backend.main as backend_main


def _provider_company(*, company_id: str = "acme", external_id: str = "101") -> dict:
    """Return one active explicit N-central customer mapping."""

    return {
        "externalId": external_id,
        "name": f"N-central {external_id}",
        "active": True,
        "mappedCompanyId": company_id,
        "mappedCompanyName": "Acme Manufacturing",
    }


def _mapping(external_id: str) -> dict:
    """Return a safe provider-CI identity mapping fixture."""

    return {
        "id": f"mapping-{external_id}",
        "provider": "ncentral",
        "companyId": "acme",
        "externalId": external_id,
        "externalName": f"Device {external_id}",
        "assetId": f"asset-{external_id}",
        "active": True,
        "lastSeenAt": "2026-08-03T07:30:00Z",
    }


def _cache_rows(*, stale: bool = False, unmatched: int = 2) -> list[dict]:
    """Return one valid published GraphQL cache generation."""

    scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(["graphql-acme"])
    common = {
        "cacheGenerationId": "generation-1",
        "cacheProviderAssetCount": 1 + unmatched,
        "cacheEligibleDeviceCount": 1,
        "cacheUnmatchedDeviceCount": unmatched,
        "cachePagesRead": 1,
        "cacheScopeFingerprint": scope_fingerprint,
    }
    return [
        {
            "sourceServerId": "server-a",
            "sourceDeviceId": "dev-1",
            "observedAt": "2026-08-03T08:00:00Z",
            "expiresAt": "2026-08-04T08:00:00Z",
            "stale": stale,
            "summary": {
                "queryKey": "asset_inventory",
                "graphqlAssetId": "graph-dev-1",
                "assetName": "Device dev-1",
                "graphqlCustomerId": "graphql-acme",
                "graphqlCustomerName": "Acme",
                "sourceIdentity": {
                    "provider": "ncentral",
                    "namespace": "nable_graphql_asset",
                    "externalId": "graph-dev-1",
                },
                "inventory": {"privateValue": "never-return-cache-secret"},
                "cacheComplete": False,
                "cachePublished": False,
                **common,
            },
        },
        {
            "sourceServerId": "server-a",
            "sourceDeviceId": "__cache_generation__",
            "observedAt": "2026-08-03T08:00:01Z",
            "expiresAt": "2026-08-04T08:00:00Z",
            "stale": stale,
            "summary": {
                "queryKey": "asset_inventory_cache_generation",
                "cacheComplete": True,
                "cachePublished": True,
                **common,
            },
        },
    ]


class NcentralEnrichmentDiagnosticTests(unittest.TestCase):
    """Verify tenant safety, classification, paging and response minimization."""

    def _repository(self) -> MagicMock:
        repository = MagicMock()
        repository.list_companies.return_value = [
            {"id": "acme", "name": "Acme Manufacturing"},
            {"id": "northwind", "name": "Northwind Traders"},
        ]
        repository.list_provider_companies.return_value = [_provider_company()]
        repository.get_ci_sync_policy.return_value = {
            "id": "policy-1",
            "companyId": "acme",
            "providerParentId": "101",
            "graphqlOrganizationIds": ["graphql-acme"],
            "excludedExternalIds": ["dev-3"],
        }
        repository.list_provider_ci_mappings.return_value = [
            _mapping("dev-1"),
            _mapping("dev-2"),
        ]
        repository.list_ci_review_items.return_value = [
            {
                "externalId": "dev-2",
                "externalName": "Device dev-2",
                "providerParentId": "101",
                "providerTypeName": "Server",
                "providerStatusName": "Normal",
                "state": "pending",
                "action": "update",
                "assetId": "asset-dev-2",
                "assetName": "Device dev-2",
                "lastSeenAt": "2026-08-03T08:10:00Z",
                "providerRecord": {"credential": "never-return-review-secret"},
            },
            {
                "externalId": "dev-3",
                "externalName": "Excluded device",
                "providerParentId": "101",
                "state": "resolved",
                "providerRecord": {"credential": "never-return-review-secret"},
            },
        ]
        repository.query_integration_object_suppressions.return_value = {
            "items": [
                {
                    "externalId": "dev-4",
                    "externalName": "Ignored device",
                    "providerParentId": "101",
                    "providerRecord": {"credential": "never-return-suppression-secret"},
                }
            ],
            "total": 1,
        }
        repository.list_assets.return_value = [
            {
                "id": "asset-dev-1",
                "companyId": "acme",
                "name": "Canonical dev-1",
            },
            {
                "id": "asset-dev-2",
                "companyId": "acme",
                "name": "Canonical dev-2",
            },
            {
                "id": "other-tenant-secret",
                "companyId": "northwind",
                "name": "never-return-other-tenant",
            },
        ]
        repository.list_integration_enrichment_previews.return_value = _cache_rows()
        repository.list_sync_runs.return_value = [
            {
                "id": "run-1",
                "status": "success",
                "finishedAt": "2026-08-03T08:15:00Z",
                "providerCompanyId": "101",
                "previewSummary": {
                    "discovered": 12,
                    "included": 9,
                    "excluded": 3,
                    "exclusionReasons": {"status": 2, "provider_filter": 1},
                    "counts": {"create": 1, "update": 2, "unchanged": 6},
                },
            }
        ]
        return repository

    def test_classifies_safe_persisted_evidence_and_reports_aggregate_limits(self) -> None:
        """Current cache rows, gaps, policy exclusions and suppressions remain distinct."""

        repository = self._repository()
        cache_rows = _cache_rows()
        graphql_only_row = deepcopy(cache_rows[0])
        graphql_only_row["sourceDeviceId"] = "dev-5"
        graphql_only_row["summary"]["graphqlAssetId"] = "graph-dev-5"
        graphql_only_row["summary"]["assetName"] = "Device dev-5"
        graphql_only_row["summary"]["sourceIdentity"]["externalId"] = "graph-dev-5"
        for cache_row in [cache_rows[0], graphql_only_row, cache_rows[1]]:
            cache_row["summary"]["cacheEligibleDeviceCount"] = 2
            cache_row["summary"]["cacheProviderAssetCount"] = 4
        repository.list_integration_enrichment_previews.return_value = [
            cache_rows[0],
            graphql_only_row,
            cache_rows[1],
        ]
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": True,
                    "configured": True,
                    "graphqlServerId": "server-a",
                },
            ),
        ):
            result = backend_main._ncentral_enrichment_diagnostics("acme", "101")

        reasons = {item["externalId"]: item["reason"] for item in result["items"]}
        self.assertEqual(reasons["dev-1"], "enriched")
        self.assertEqual(reasons["dev-5"], "graphql_only")
        self.assertEqual(reasons["dev-2"], "graphql_missing")
        self.assertEqual(reasons["dev-3"], "filtered_excluded")
        self.assertEqual(reasons["dev-4"], "ignored")
        self.assertEqual(result["freshness"]["status"], "partial")
        self.assertTrue(result["freshness"]["complete"])
        self.assertEqual(result["freshness"]["unmatchedDeviceCount"], 2)
        self.assertEqual(result["summary"]["reasonCounts"]["enriched"], 1)
        self.assertEqual(result["summary"]["reasonCounts"]["graphql_only"], 1)
        self.assertEqual(result["summary"]["reasonCounts"]["ignored"], 1)
        self.assertEqual(result["latestPreview"]["excluded"], 3)
        self.assertEqual(result["latestPreview"]["exclusionReasons"]["provider_filter"], 1)
        self.assertEqual(result["items"][0]["nextAction"].keys(), {"key", "label"})
        encoded = json.dumps(result)
        for sensitive in (
            "never-return-cache-secret",
            "never-return-review-secret",
            "never-return-suppression-secret",
            "never-return-other-tenant",
            "providerRecord",
            "inventory",
        ):
            self.assertNotIn(sensitive, encoded)

    def test_stale_generation_distinguishes_matching_evidence_from_unknown_coverage(self) -> None:
        """Expired evidence is never presented as current enrichment or a proven miss."""

        repository = self._repository()
        repository.query_integration_object_suppressions.return_value = {"items": [], "total": 0}
        repository.get_ci_sync_policy.return_value["excludedExternalIds"] = []
        repository.list_ci_review_items.return_value = []
        repository.list_integration_enrichment_previews.return_value = _cache_rows(
            stale=True,
            unmatched=0,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": True,
                    "configured": True,
                    "graphqlServerId": "server-a",
                },
            ),
        ):
            result = backend_main._ncentral_enrichment_diagnostics("acme", "101")

        reasons = {item["externalId"]: item["reason"] for item in result["items"]}
        self.assertEqual(result["freshness"]["status"], "stale")
        self.assertEqual(reasons["dev-1"], "stale_cache_evidence")
        self.assertEqual(reasons["dev-2"], "cache_unavailable")

    def test_server_side_status_search_and_page_bounds_are_deterministic(self) -> None:
        """Large persisted scopes remain filtered and paged without returning extra rows."""

        repository = self._repository()
        repository.list_provider_ci_mappings.return_value = [
            _mapping(f"dev-{index:03d}") for index in range(300)
        ]
        repository.list_ci_review_items.return_value = []
        repository.query_integration_object_suppressions.return_value = {"items": [], "total": 0}
        repository.get_ci_sync_policy.return_value["excludedExternalIds"] = []
        repository.get_ci_sync_policy.return_value["graphqlOrganizationIds"] = []
        repository.list_integration_enrichment_previews.return_value = []
        repository.list_sync_runs.return_value = []
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": False,
                    "configured": False,
                    "graphqlServerId": "",
                },
            ),
        ):
            result = backend_main._ncentral_enrichment_diagnostics(
                "acme",
                "101",
                reason="rest_only",
                search="device",
                limit=999,
                offset=10,
            )

        self.assertEqual(result["total"], 300)
        self.assertEqual(len(result["items"]), 250)
        self.assertEqual(result["filters"]["limit"], 250)
        self.assertEqual(result["filters"]["offset"], 10)
        self.assertTrue(all(item["reason"] == "rest_only" for item in result["items"]))

    def test_usable_cache_with_unmatched_assets_reports_fresh_with_gaps(self) -> None:
        """Usable enrichment must still expose aggregate crosswalk gaps to operators."""

        repository = self._repository()
        repository.list_integration_enrichment_previews.return_value = _cache_rows(unmatched=2)
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": True,
                    "configured": True,
                    "graphqlServerId": "server-a",
                },
            ),
        ):
            assets, diagnostics = backend_main._ncentral_graphql_enrichment_context(
                "acme",
                "101",
                repository.get_ci_sync_policy.return_value,
                rest_device_count=2,
                refresh_when_due=False,
            )

        self.assertEqual(len(assets), 1)
        self.assertEqual(diagnostics["status"], "fresh_with_gaps")
        self.assertEqual(diagnostics["unmatchedDeviceCount"], 2)
        self.assertIn("2 provider asset(s)", diagnostics["reason"])
        self.assertTrue(diagnostics["complete"])

    def test_route_enforces_mapping_and_operator_customer_scope(self) -> None:
        """MSP reads are allowed only inside an explicit customer boundary."""

        repository = self._repository()
        repository.list_provider_companies.return_value = [
            _provider_company(company_id="northwind", external_id="101")
        ]
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "current_user",
                return_value={"id": "admin", "role": "platform_admin", "companyIds": ["*"]},
            ),
            self.assertRaises(HTTPException) as tenant_error,
        ):
            backend_main.get_ncentral_enrichment_diagnostics(
                MagicMock(), companyId="acme", providerCompanyId="101"
            )
        self.assertEqual(tenant_error.exception.status_code, 409)

        repository.list_provider_companies.return_value = [_provider_company()]
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "current_user",
                return_value={
                    "id": "operator",
                    "role": "msp_operator",
                    "companyIds": ["acme"],
                },
            ),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": True,
                    "configured": True,
                    "graphqlServerId": "server-a",
                },
            ),
        ):
            allowed = backend_main.get_ncentral_enrichment_diagnostics(
                MagicMock(), companyId="acme", providerCompanyId="101"
            )
        self.assertEqual(allowed["company"]["id"], "acme")

        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "current_user",
                return_value={
                    "id": "operator",
                    "role": "msp_operator",
                    "companyIds": ["northwind"],
                },
            ),
            self.assertRaises(HTTPException) as scope_error,
        ):
            backend_main.get_ncentral_enrichment_diagnostics(
                MagicMock(), companyId="acme", providerCompanyId="101"
            )
        self.assertEqual(scope_error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
