"""Regression contracts for complete and observable N-central enrichment."""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

import backend.main as backend_main


def _rest_record(external_id: str) -> dict:
    """Return one minimal normalized REST device used by preview fixtures."""

    return {
        "externalId": external_id,
        "name": f"Device {external_id}",
        "type": "Server",
        "status": "Active",
        "providerTypeId": "server",
        "providerTypeName": "Server",
        "providerStatusId": "active",
        "providerStatusName": "Active",
        "fields": {},
        "metadata": {},
        "identifiers": {},
        "inventoryCollections": {},
        "providerFingerprint": f"sha256:{external_id}",
        "providerVersion": f"sha256:{external_id}",
    }


def _graphql_asset(device_id: str, *, customer_id: str = "graphql-acme") -> dict:
    """Return one merge-safe GraphQL inventory observation."""

    graphql_id = f"graph-{device_id}"
    return {
        "graphqlAssetId": graphql_id,
        "name": f"Device {device_id}",
        "customer": {"id": customer_id, "name": "Acme"},
        "site": None,
        "serviceOrganization": None,
        "sourceIdentity": {
            "provider": "ncentral",
            "namespace": "nable_graphql_asset",
            "externalId": graphql_id,
        },
        "restIdentity": {
            "provider": "ncentral",
            "namespace": "ncentral_rest_device",
            "serverId": "server-a",
            "deviceId": device_id,
            "crosswalkKey": f"server-a:{device_id}",
        },
        "summary": {"system": {"serialNumber": f"SERIAL-{device_id}"}},
    }


class _RestClient:
    """Capture bounded REST enrichment options without provider I/O."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.discovery_options: list[dict] = []

    def discover_devices(self, provider_company_id: str, **options) -> list[dict]:
        if provider_company_id != "101":
            raise AssertionError("unexpected provider company")
        self.discovery_options.append(deepcopy(options))
        return deepcopy(self.records)

    def list_device_filters(self) -> list[dict]:
        return []


class NcentralEnrichmentCoverageTests(unittest.TestCase):
    """Define coverage and diagnostics expected from normal N-central previews."""

    def _repository(self, *, policy: dict | None = None) -> MagicMock:
        repository = MagicMock()
        repository.get_ci_sync_policy.return_value = {
            "id": "policy-1",
            "enrichmentMode": "balanced",
            "graphqlOrganizationIds": ["graphql-acme"],
            **(policy or {}),
        }
        repository.list_assets.return_value = []
        repository.list_provider_ci_mappings.return_value = []
        repository.list_field_authority.return_value = []
        repository.list_integration_enrichment_previews.return_value = []
        return repository

    def test_balanced_context_forwards_a_persisted_rotation_offset(self) -> None:
        """A later run must not enrich the same first 25 devices indefinitely."""

        rest_client = _RestClient([_rest_record(str(index)) for index in range(1, 61)])
        repository = self._repository()
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_mapped_company",
                return_value={"mappedCompanyName": "Acme", "name": "Acme N-central"},
            ),
            patch.object(
                backend_main,
                "_ncentral_effective_configuration",
                return_value=({"enabled": True}, "encrypted_database"),
            ),
            patch.object(backend_main, "_ncentral_client", return_value=rest_client),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlEnabled": False, "configured": True},
            ),
        ):
            backend_main._ncentral_device_context(
                "acme",
                "101",
                enrich_limit=None,
                enrichment_offset=25,
            )

        self.assertEqual(rest_client.discovery_options[0]["enrich_limit"], 25)
        self.assertEqual(rest_client.discovery_options[0]["enrichment_offset"], 25)

    def test_presence_snapshot_uses_raw_identity_scope_before_local_policy(self) -> None:
        """Local type, status and ignore rules must not turn present devices into absences."""

        active = _rest_record("device-1")
        excluded = _rest_record("device-2")
        excluded.update(providerStatusId="inactive", providerStatusName="Inactive")
        snapshot, summary = backend_main._ncentral_presence_snapshot(
            [active, excluded, deepcopy(active)],
            {
                "revision": 7,
                "providerFilterId": "managed-servers",
                "statusMode": "selected",
                "includedStatusIds": ["active"],
                "excludedExternalIds": ["device-2"],
                "missingDeviceRequiredSnapshots": 4,
                "missingDeviceMinimumHours": 48,
            },
            company_id="acme",
            provider_company_id="101",
            connection_revision=9,
            snapshot_started_at="2026-08-03T08:00:00Z",
            provider_read_completed_at="2026-08-03T08:00:05Z",
        )

        self.assertEqual(
            snapshot["observedRecords"],
            [
                {
                    "externalId": "device-1",
                    "externalName": "Device device-1",
                    "providerParentId": "101",
                },
                {
                    "externalId": "device-2",
                    "externalName": "Device device-2",
                    "providerParentId": "101",
                },
            ],
        )
        self.assertEqual(snapshot["scopeMode"], "provider_filtered")
        self.assertEqual(snapshot["policyRevision"], 7)
        self.assertEqual(snapshot["connectionRevision"], 9)
        self.assertEqual(snapshot["requiredAbsences"], 4)
        self.assertEqual(snapshot["minimumMissingHours"], 48)
        self.assertEqual(summary["observedCount"], 2)
        self.assertNotIn("observedRecords", summary)
        self.assertNotIn("device-1", json.dumps(summary))

    def test_missing_device_policy_thresholds_are_safe_and_bounded(self) -> None:
        """The wizard defaults conservatively and rejects unsafe lifecycle thresholds."""

        defaults = backend_main.NcentralDevicePolicyRequest(
            companyId="acme",
            providerCompanyId="101",
        )
        self.assertEqual(defaults.missingDeviceRequiredSnapshots, 3)
        self.assertEqual(defaults.missingDeviceMinimumHours, 24)
        for values in (
            {"missingDeviceRequiredSnapshots": 1},
            {"missingDeviceRequiredSnapshots": 11},
            {"missingDeviceMinimumHours": 0},
            {"missingDeviceMinimumHours": 721},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                backend_main.NcentralDevicePolicyRequest(
                    companyId="acme",
                    providerCompanyId="101",
                    **values,
                )

    def test_direct_preview_publishes_private_snapshot_but_returns_only_summary(self) -> None:
        """Atomic publication receives raw evidence while callers receive aggregates only."""

        snapshot = {
            "observedRecords": [
                {
                    "externalId": "device-1",
                    "externalName": "Device device-1",
                    "providerParentId": "101",
                }
            ],
            "providerReadComplete": True,
        }
        preview = {
            "companyId": "acme",
            "companyName": "Acme Manufacturing",
            "providerCompanyId": "101",
            "providerCompanyName": "Acme N-central",
            "credentialSource": "encrypted_database",
            "readOnly": True,
            "writesAttempted": False,
            "discovered": 1,
            "included": 1,
            "excluded": 0,
            "exclusionReasons": {},
            "availableTypes": [],
            "availableStatuses": [],
            "typeMappingSummary": {},
            "appliedPolicy": {"id": "policy-1", "revision": 7},
            "counts": {
                "create": 1,
                "update": 0,
                "link": 0,
                "unchanged": 0,
                "conflict": 0,
            },
            "items": [{"externalId": "device-1", "record": _rest_record("device-1")}],
            "enrichment": {},
            "presenceSummary": {"observedCount": 1, "providerReadComplete": True},
            "_presenceSnapshot": snapshot,
        }
        repository = MagicMock()
        repository.renew_ci_sync_policy_run.return_value = {"id": "policy-1"}
        repository.publish_and_complete_ci_policy_preview.return_value = {
            "run": {"id": "run-1", "finishedAt": "2026-08-03T08:00:05Z"},
            "queueSummary": {"pending": 1, "created": 1, "updated": 0, "resolved": 0},
            "policy": {"id": "policy-1", "consecutiveFailures": 0},
        }
        relationship_summary = {
            "observed": 0,
            "autoApproved": 0,
            "reviewRequired": 0,
            "errors": 0,
        }
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(backend_main, "_ncentral_device_preview", return_value=deepcopy(preview)),
            patch.object(
                backend_main,
                "_observe_ncentral_relationships",
                return_value=relationship_summary,
            ),
        ):
            result = backend_main._execute_ncentral_device_preview(
                "acme",
                "101",
                actor_id="admin",
                trigger="manual_preview",
                policy_id="policy-1",
                lease_owner="worker-1",
            )

        publication = repository.publish_and_complete_ci_policy_preview.call_args
        self.assertEqual(publication.kwargs["presence_snapshot"], snapshot)
        self.assertEqual(
            publication.args[3]["attributes"]["presenceSummary"]["observedCount"],
            1,
        )
        self.assertNotIn("_presenceSnapshot", result)
        self.assertNotIn("observedRecords", json.dumps(result))

    def test_graphql_refresh_reads_complete_scope_not_the_ui_sample(self) -> None:
        """A 25-row preview sample must still refresh all 105 scoped assets."""

        calls: list[dict] = []

        class GraphqlClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def full_asset_inventory(self, *, organization_ids, maximum):
                calls.append(
                    {
                        "queryKey": "asset_inventory",
                        "organizationIds": organization_ids,
                        "maximum": maximum,
                    }
                )
                items = [_graphql_asset(str(index)) for index in range(1, 106)]
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": organization_ids,
                    "totalCount": 105,
                    "items": items,
                    "truncated": False,
                    "pagesRead": 2,
                }

        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(
                backend_main,
                "_ncentral_graphql_saved_scope",
                return_value=["graphql-acme"],
            ),
            patch.object(
                backend_main,
                "_ncentral_graphql_effective_configuration",
                return_value=({"graphqlApiToken": "write-only-secret"}, "encrypted_database"),
            ),
            patch.object(backend_main, "NableGraphqlClient", GraphqlClient),
        ):
            result = backend_main._ncentral_graphql_asset_read(payload)

        self.assertEqual(calls[0]["maximum"], 10_000)
        self.assertEqual(result["totalCount"], 105)
        self.assertEqual(len(result["items"]), 105)
        self.assertFalse(result["truncated"])

    def test_preview_reports_disabled_empty_and_expired_graphql_states(self) -> None:
        """Operators must see why otherwise valid REST devices lack GraphQL data."""

        states = (
            (
                "disabled",
                {"graphqlEnabled": False, "configured": True, "graphqlServerId": "server-a"},
                [],
            ),
            (
                "empty",
                {"graphqlEnabled": True, "configured": True, "graphqlServerId": "server-a"},
                [],
            ),
            (
                "expired",
                {"graphqlEnabled": True, "configured": True, "graphqlServerId": "server-a"},
                [
                    {
                        "sourceServerId": "server-a",
                        "sourceDeviceId": "1",
                        "summary": {},
                        "stale": True,
                    }
                ],
            ),
        )
        for expected_status, public_config, expired_rows in states:
            with self.subTest(expected_status=expected_status):
                rest_client = _RestClient([_rest_record("1")])
                repository = self._repository()

                def cached_rows(
                    *_args,
                    include_expired=False,
                    _expired_rows=expired_rows,
                    **_kwargs,
                ):
                    return deepcopy(_expired_rows) if include_expired else []

                repository.list_integration_enrichment_previews.side_effect = cached_rows
                with (
                    patch.object(backend_main, "REPOSITORY", repository),
                    patch.object(
                        backend_main,
                        "_ncentral_mapped_company",
                        return_value={
                            "mappedCompanyName": "Acme",
                            "name": "Acme N-central",
                        },
                    ),
                    patch.object(
                        backend_main,
                        "_ncentral_effective_configuration",
                        return_value=({"enabled": True}, "encrypted_database"),
                    ),
                    patch.object(backend_main, "_ncentral_client", return_value=rest_client),
                    patch.object(
                        backend_main,
                        "_ncentral_graphql_connection_public",
                        return_value=public_config,
                    ),
                ):
                    diagnostics: dict = {}
                    backend_main._ncentral_device_context(
                        "acme",
                        "101",
                        enrichment_diagnostics=diagnostics,
                    )

                graphql = diagnostics["graphql"]
                self.assertEqual(graphql["status"], expected_status)
                self.assertTrue(graphql["reason"])

    def test_normal_preview_refreshes_empty_graphql_cache_before_merge(self) -> None:
        """A normal preview should refresh due evidence and enrich its matching REST record."""

        rest_client = _RestClient([_rest_record("1")])
        repository = self._repository()
        cache_rows: list[dict] = []

        def list_cache(*_args, include_expired=False, **_kwargs):
            del include_expired
            return deepcopy(cache_rows)

        def upsert_cache(_kind, _company_id, preview, **_kwargs):
            stored = {
                "sourceServerId": preview["sourceServerId"],
                "sourceDeviceId": preview["sourceDeviceId"],
                "summary": deepcopy(preview["summary"]),
                "observedAt": "2026-08-03T09:00:00Z",
                "expiresAt": "2026-08-03T10:00:00Z",
                "stale": False,
            }
            cache_rows.append(stored)
            return deepcopy(stored)

        repository.list_integration_enrichment_previews.side_effect = list_cache
        repository.upsert_integration_enrichment_preview.side_effect = upsert_cache
        graphql_calls: list[int] = []

        class GraphqlClient:
            def __init__(self, configuration):
                self.configuration = configuration

            def full_asset_inventory(self, *, organization_ids, maximum):
                graphql_calls.append(maximum)
                return {
                    "queryKey": "asset_inventory",
                    "organizationIds": organization_ids,
                    "totalCount": 1,
                    "items": [_graphql_asset("1")],
                    "truncated": False,
                    "pagesRead": 1,
                }

        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_mapped_company",
                return_value={"mappedCompanyName": "Acme", "name": "Acme N-central"},
            ),
            patch.object(
                backend_main,
                "_ncentral_effective_configuration",
                return_value=({"enabled": True}, "encrypted_database"),
            ),
            patch.object(backend_main, "_ncentral_client", return_value=rest_client),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={
                    "graphqlEnabled": True,
                    "configured": True,
                    "graphqlServerId": "server-a",
                },
            ),
            patch.object(
                backend_main,
                "_ncentral_graphql_effective_configuration",
                return_value=({"graphqlApiToken": "write-only-secret"}, "encrypted_database"),
            ),
            patch.object(backend_main, "NableGraphqlClient", GraphqlClient),
        ):
            _mapped, records, _source, _policy, _filters = backend_main._ncentral_device_context(
                "acme", "101", refresh_graphql=True
            )

        self.assertEqual(len(graphql_calls), 1)
        self.assertEqual(records[0]["providerParentId"], "101")
        self.assertEqual(records[0]["fields"]["nableGraphqlAssetId"], "graph-1")
        self.assertEqual(
            records[0]["metadata"]["nableGraphql"]["system"]["serialNumber"],
            "SERIAL-1",
        )

    def test_complete_empty_refresh_records_and_replaces_the_cache_generation(self) -> None:
        """A valid empty provider result must supersede older cached asset rows."""

        repository = self._repository()
        repository.upsert_integration_enrichment_preview.return_value = {
            "sourceServerId": "server-a",
            "sourceDeviceId": "__cache_generation__",
            "summary": {},
            "observedAt": "2026-08-03T09:00:00Z",
            "expiresAt": "2026-08-04T09:00:00Z",
            "stale": False,
        }
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlServerId": "server-a"},
            ),
        ):
            cache = backend_main._cache_ncentral_graphql_assets(
                payload,
                {
                    "queryKey": "asset_inventory",
                    "organizationIds": ["graphql-acme"],
                    "totalCount": 0,
                    "items": [],
                    "truncated": False,
                    "pagesRead": 1,
                },
            )

        marker = repository.upsert_integration_enrichment_preview.call_args.args[2]
        self.assertTrue(marker["sourceNamespace"].startswith("nable_graphql_cache_"))
        self.assertEqual(
            marker["summary"]["queryKey"],
            "asset_inventory_cache_generation",
        )
        self.assertEqual(marker["summary"]["cacheProviderAssetCount"], 0)
        self.assertTrue(cache["complete"])
        self.assertEqual(cache["deviceCount"], 0)
        generation_id = cache["generationId"]
        repository.replace_integration_enrichment_generation.assert_called_once_with(
            "ncentral",
            "acme",
            "101",
            "server-a",
            generation_id,
        )

    def test_unpublished_newer_generation_does_not_hide_current_complete_cache(self) -> None:
        """Interrupted or overlapping refresh rows must remain invisible."""

        rows = [
            {
                "id": "partial-new",
                "summary": {
                    "cacheGenerationId": "generation-2",
                    "cacheComplete": False,
                    "cachePublished": False,
                },
            },
            {
                "id": "marker-current",
                "summary": {
                    "queryKey": "asset_inventory_cache_generation",
                    "cacheGenerationId": "generation-1",
                    "cacheComplete": True,
                    "cachePublished": True,
                },
            },
            {
                "id": "asset-current",
                "summary": {
                    "queryKey": "asset_inventory",
                    "cacheGenerationId": "generation-1",
                    "cacheComplete": False,
                    "cachePublished": False,
                },
            },
        ]

        selected = backend_main._ncentral_graphql_generation_rows(rows)

        self.assertEqual(
            [row["id"] for row in selected],
            ["marker-current", "asset-current"],
        )

    def test_unpublished_first_generation_retains_legacy_cache_rows(self) -> None:
        """Interrupted upgrade staging must not hide validated legacy evidence."""

        rows = [
            {
                "id": "partial-new",
                "summary": {
                    "cacheGenerationId": "generation-1",
                    "cacheComplete": False,
                    "cachePublished": False,
                },
            },
            {
                "id": "legacy-current",
                "summary": {
                    "queryKey": "asset_inventory",
                    "cacheComplete": True,
                },
            },
        ]

        selected = backend_main._ncentral_graphql_generation_rows(rows)

        self.assertEqual([row["id"] for row in selected], ["legacy-current"])

    def test_complete_cache_requires_exact_unique_valid_asset_row_count(self) -> None:
        """A partial or duplicate generation cannot suppress its next refresh."""

        scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(["graphql-acme"])

        def cached_row(device_id: str) -> dict:
            asset = _graphql_asset(device_id)
            return {
                "sourceServerId": "server-a",
                "sourceDeviceId": device_id,
                "summary": {
                    "queryKey": "asset_inventory",
                    "graphqlAssetId": asset["graphqlAssetId"],
                    "graphqlCustomerId": asset["customer"]["id"],
                    "sourceIdentity": deepcopy(asset["sourceIdentity"]),
                    "inventory": deepcopy(asset["summary"]),
                    "cacheComplete": True,
                    "cacheEligibleDeviceCount": 2,
                    "cacheScopeFingerprint": scope_fingerprint,
                },
            }

        first = cached_row("1")
        second = cached_row("2")
        complete = backend_main._ncentral_graphql_cache_metadata(
            [first, second],
            organization_ids={"graphql-acme"},
            server_id="server-a",
        )
        partial = backend_main._ncentral_graphql_cache_metadata(
            [first],
            organization_ids={"graphql-acme"},
            server_id="server-a",
        )
        duplicate = backend_main._ncentral_graphql_cache_metadata(
            [first, deepcopy(first)],
            organization_ids={"graphql-acme"},
            server_id="server-a",
        )

        self.assertTrue(complete["complete"])
        self.assertFalse(partial["complete"])
        self.assertFalse(duplicate["complete"])

    def test_cache_complete_requires_exact_valid_asset_row_count(self) -> None:
        """A published marker cannot make a partial generation appear complete."""

        scope = {"graphql-acme"}
        scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(scope)

        def cached_row(device_id: str) -> dict:
            asset = _graphql_asset(device_id)
            return {
                "sourceServerId": "server-a",
                "sourceDeviceId": device_id,
                "summary": {
                    "queryKey": "asset_inventory",
                    "graphqlAssetId": asset["graphqlAssetId"],
                    "graphqlCustomerId": "graphql-acme",
                    "graphqlCustomerName": "Acme",
                    "sourceIdentity": asset["sourceIdentity"],
                    "inventory": asset["summary"],
                    "cacheGenerationId": "generation-1",
                    "cacheScopeFingerprint": scope_fingerprint,
                },
            }

        marker = {
            "sourceServerId": "server-a",
            "sourceDeviceId": "__cache_generation__",
            "summary": {
                "queryKey": "asset_inventory_cache_generation",
                "cacheGenerationId": "generation-1",
                "cacheComplete": True,
                "cachePublished": True,
                "cacheEligibleDeviceCount": 2,
                "cacheScopeFingerprint": scope_fingerprint,
            },
        }

        partial = backend_main._ncentral_graphql_cache_metadata(
            [marker, cached_row("1")],
            organization_ids=scope,
            server_id="server-a",
        )
        complete = backend_main._ncentral_graphql_cache_metadata(
            [marker, cached_row("1"), cached_row("2")],
            organization_ids=scope,
            server_id="server-a",
        )

        self.assertFalse(partial["complete"])
        self.assertTrue(complete["complete"])

    def test_duplicate_rest_crosswalk_claims_are_quarantined(self) -> None:
        """Two GraphQL assets may not silently compete for one REST device ID."""

        repository = self._repository()

        def upsert_cache(_kind, _company_id, preview, **_kwargs):
            return {
                "sourceServerId": preview["sourceServerId"],
                "sourceDeviceId": preview["sourceDeviceId"],
                "summary": deepcopy(preview["summary"]),
                "observedAt": "2026-08-03T09:00:00Z",
                "expiresAt": "2026-08-04T09:00:00Z",
                "stale": False,
            }

        repository.upsert_integration_enrichment_preview.side_effect = upsert_cache
        first = _graphql_asset("1")
        second = _graphql_asset("1")
        second["graphqlAssetId"] = "graph-conflict"
        second["sourceIdentity"]["externalId"] = "graph-conflict"
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlServerId": "server-a"},
            ),
        ):
            cache = backend_main._cache_ncentral_graphql_assets(
                payload,
                {
                    "queryKey": "asset_inventory",
                    "organizationIds": ["graphql-acme"],
                    "totalCount": 2,
                    "items": [first, second],
                    "truncated": False,
                    "pagesRead": 1,
                },
            )

        self.assertEqual(cache["eligibleDeviceCount"], 0)
        self.assertEqual(cache["unmatchedDeviceCount"], 2)
        self.assertEqual(repository.upsert_integration_enrichment_preview.call_count, 1)
        marker = repository.upsert_integration_enrichment_preview.call_args.args[2]
        self.assertEqual(marker["summary"]["queryKey"], "asset_inventory_cache_generation")

    def test_saved_scope_change_invalidates_a_fresh_complete_generation(self) -> None:
        """A policy scope expansion or replacement must refresh before merging."""

        old_scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(["old-org"])
        marker = {
            "sourceServerId": "server-a",
            "sourceDeviceId": "__cache_generation__",
            "summary": {
                "queryKey": "asset_inventory_cache_generation",
                "cacheGenerationId": "generation-1",
                "cachePublished": True,
                "cacheComplete": True,
                "cacheScopeFingerprint": old_scope_fingerprint,
            },
            "observedAt": "2026-08-03T09:00:00Z",
            "expiresAt": "2026-08-04T09:00:00Z",
            "stale": False,
        }
        repository = self._repository()
        repository.list_integration_enrichment_previews.return_value = [marker]
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
                rest_device_count=1,
                refresh_when_due=False,
            )

        self.assertEqual(assets, [])
        self.assertEqual(diagnostics["status"], "scope_changed")
        self.assertFalse(diagnostics["scopeMatches"])

    def test_one_expired_row_expires_the_whole_published_generation(self) -> None:
        """A still-fresh marker may not mask older device rows expiring first."""

        scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(["graphql-acme"])
        repository = self._repository()
        repository.list_integration_enrichment_previews.return_value = [
            {
                "sourceServerId": "server-a",
                "sourceDeviceId": "__cache_generation__",
                "summary": {
                    "queryKey": "asset_inventory_cache_generation",
                    "cacheGenerationId": "generation-1",
                    "cachePublished": True,
                    "cacheComplete": True,
                    "cacheScopeFingerprint": scope_fingerprint,
                },
                "stale": False,
            },
            {
                "sourceServerId": "server-a",
                "sourceDeviceId": "1",
                "summary": {
                    "queryKey": "asset_inventory",
                    "cacheGenerationId": "generation-1",
                    "cachePublished": False,
                    "cacheComplete": False,
                    "cacheScopeFingerprint": scope_fingerprint,
                },
                "stale": True,
            },
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
            assets, diagnostics = backend_main._ncentral_graphql_enrichment_context(
                "acme",
                "101",
                repository.get_ci_sync_policy.return_value,
                rest_device_count=1,
                refresh_when_due=False,
            )

        self.assertEqual(assets, [])
        self.assertEqual(diagnostics["status"], "expired")

    def test_scope_change_during_provider_read_prevents_cache_publication(self) -> None:
        """Provider rows read under an older policy scope must never publish as current."""

        repository = self._repository(policy={"graphqlOrganizationIds": ["new-org"]})
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlServerId": "server-a"},
            ),
        ):
            cache = backend_main._cache_ncentral_graphql_assets(
                payload,
                {
                    "queryKey": "asset_inventory",
                    "organizationIds": ["old-org"],
                    "totalCount": 1,
                    "items": [_graphql_asset("1", customer_id="old-org")],
                    "truncated": False,
                    "pagesRead": 1,
                },
            )

        self.assertEqual(cache["status"], "scope_changed")
        self.assertFalse(cache["complete"])
        repository.upsert_integration_enrichment_preview.assert_not_called()

    def test_cached_sample_hides_rows_after_saved_scope_changes(self) -> None:
        """The cache preview must not label an old-scope generation fresh or complete."""

        old_scope_fingerprint = backend_main._ncentral_graphql_scope_fingerprint(["old-org"])
        repository = self._repository()
        repository.list_integration_enrichment_previews.return_value = [
            {
                "sourceServerId": "server-a",
                "sourceDeviceId": "__cache_generation__",
                "summary": {
                    "queryKey": "asset_inventory_cache_generation",
                    "cacheGenerationId": "generation-1",
                    "cachePublished": True,
                    "cacheComplete": True,
                    "cacheScopeFingerprint": old_scope_fingerprint,
                },
                "observedAt": "2026-08-03T09:00:00Z",
                "expiresAt": "2026-08-04T09:00:00Z",
                "stale": False,
            }
        ]
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(backend_main, "_require_integration_active"),
            patch.object(
                backend_main,
                "_ncentral_graphql_saved_scope",
                return_value=["new-org"],
            ),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlServerId": "server-a"},
            ),
        ):
            cached = backend_main._ncentral_graphql_cached_read(payload, include_expired=True)

        self.assertEqual(cached["items"], [])
        self.assertEqual(cached["cache"]["status"], "scope_changed")
        self.assertFalse(cached["cache"]["complete"])

    def test_partial_generation_is_removed_and_never_reported_as_usable(self) -> None:
        """A failed staging write must retain the prior generation and show an error."""

        repository = self._repository()
        first_write = {
            "sourceServerId": "server-a",
            "sourceDeviceId": "1",
            "summary": {},
            "observedAt": "2026-08-03T09:00:00Z",
            "expiresAt": "2026-08-04T09:00:00Z",
            "stale": False,
        }
        repository.upsert_integration_enrichment_preview.side_effect = [
            first_write,
            ValueError("simulated bounded cache failure"),
        ]
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                return_value={"graphqlServerId": "server-a"},
            ),
        ):
            cache = backend_main._cache_ncentral_graphql_assets(
                payload,
                {
                    "queryKey": "asset_inventory",
                    "organizationIds": ["graphql-acme"],
                    "totalCount": 2,
                    "items": [_graphql_asset("1"), _graphql_asset("2")],
                    "truncated": False,
                    "pagesRead": 1,
                },
            )

        self.assertEqual(cache["status"], "error")
        self.assertEqual(cache["deviceCount"], 0)
        self.assertEqual(cache["stagedDeviceCount"], 1)
        repository.replace_integration_enrichment_generation.assert_not_called()
        repository.delete_integration_enrichment_generation.assert_called_once()

    def test_server_identity_change_during_lock_acquisition_prevents_publication(self) -> None:
        """A refresh may not publish under a different server than its lock key."""

        repository = self._repository()
        payload = backend_main.NcentralGraphqlScopeRequest(
            companyId="acme",
            providerCompanyId="101",
            queryKey="asset_inventory",
            limit=25,
        )
        with (
            patch.object(backend_main, "REPOSITORY", repository),
            patch.object(
                backend_main,
                "_ncentral_graphql_connection_public",
                side_effect=[
                    {"graphqlServerId": "server-a"},
                    {"graphqlServerId": "server-b"},
                ],
            ),
        ):
            cache = backend_main._cache_ncentral_graphql_assets(
                payload,
                {
                    "queryKey": "asset_inventory",
                    "organizationIds": ["graphql-acme"],
                    "totalCount": 1,
                    "items": [_graphql_asset("1")],
                    "truncated": False,
                    "pagesRead": 1,
                },
            )

        self.assertEqual(cache["status"], "configuration_changed")
        self.assertFalse(cache["complete"])
        repository.upsert_integration_enrichment_preview.assert_not_called()

    def test_reviewed_graphql_evidence_requires_the_current_exact_cache_crosswalk(self) -> None:
        """A stale review may not carry old-scope GraphQL data into a direct link."""

        current = _graphql_asset("1")
        reviewed = _rest_record("1")
        reviewed["fields"].update(
            nableGraphqlAssetId="graph-1",
            nableGraphqlServerId="server-a",
        )
        reviewed["metadata"]["nableGraphql"] = {"system": {"serialNumber": "SERIAL-1"}}
        reviewed["metadata"]["nableGraphqlSourceIdentity"] = deepcopy(current["sourceIdentity"])

        self.assertEqual(
            backend_main._ncentral_reviewed_graphql_evidence_status(
                reviewed,
                [current],
                server_id="server-a",
                organization_ids={"graphql-acme"},
            ),
            "current",
        )
        self.assertEqual(
            backend_main._ncentral_reviewed_graphql_evidence_status(
                reviewed,
                [],
                server_id="server-a",
                organization_ids={"graphql-acme"},
            ),
            "unverified",
        )
        self.assertEqual(
            backend_main._ncentral_reviewed_graphql_evidence_status(
                reviewed,
                [{**current, "customer": {"id": "old-org", "name": "Old"}}],
                server_id="server-a",
                organization_ids={"graphql-acme"},
            ),
            "unverified",
        )

    def test_rest_only_review_record_does_not_require_graphql_cache_evidence(self) -> None:
        """REST-only reviews remain linkable when optional GraphQL has no evidence."""

        self.assertEqual(
            backend_main._ncentral_reviewed_graphql_evidence_status(
                _rest_record("1"),
                [],
                server_id="server-a",
                organization_ids={"graphql-acme"},
            ),
            "absent",
        )


if __name__ == "__main__":
    unittest.main()
