"""Review-gated provider-presence lifecycle contract tests."""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.cmdb.repository import StateRepository


class StateCiPresenceLifecycleTests(unittest.TestCase):
    """Keep missing evidence scoped, consecutive and non-destructive."""

    def setUp(self) -> None:
        clock_patcher = patch(
            "src.cmdb.repository._ci_presence_reference_time",
            return_value=datetime(2026, 7, 14, tzinfo=UTC),
        )
        self.clock = clock_patcher.start()
        self.addCleanup(clock_patcher.stop)
        self.state = {
            "companies": [{"id": "acme", "name": "Acme"}],
            "users": [{"id": "admin", "email": "admin@example.test"}],
            "integrations": [
                {
                    "id": "ncentral",
                    "type": "ncentral",
                    "name": "N-central",
                    "revision": 5,
                    "enabled": True,
                    "lifecycleStatus": "active",
                }
            ],
            "assets": [
                {
                    "id": "asset-1",
                    "companyId": "acme",
                    "name": "APP-01",
                    "status": "Active",
                }
            ],
            "providerCiMappings": [
                {
                    "id": "mapping-1",
                    "provider": "ncentral",
                    "companyId": "acme",
                    "assetId": "asset-1",
                    "externalId": "device-1",
                    "externalName": "APP-01",
                    "providerParentId": "101",
                    "active": True,
                    "firstSeenAt": "2026-07-01T00:00:00Z",
                }
            ],
            "providerCompanyMappings": [
                {
                    "id": "company-mapping-1",
                    "provider": "ncentral",
                    "externalId": "101",
                    "externalName": "Acme",
                    "companyId": "acme",
                    "active": True,
                    "lastSyncedAt": "2026-07-01T00:00:00Z",
                }
            ],
            "providerCompanyObservations": [
                {
                    "id": "company-observation-1",
                    "provider": "ncentral",
                    "externalId": "101",
                    "name": "Acme",
                    "active": True,
                    "deleted": False,
                }
            ],
            "integrationCiPolicies": [],
            "integrationCiReviewItems": [],
            "syncRuns": [],
            "relationships": [],
        }
        self.repository = StateRepository(self.state, lambda _value: None)
        self.policy = {
            "id": "policy-1",
            "provider": "ncentral",
            "companyId": "acme",
            "providerParentId": "101",
            "revision": 7,
            "providerFilterId": "",
            "syncMode": "continuous_preview",
            "enabled": True,
        }
        self.state["integrationCiPolicies"].append(self.policy)

    @staticmethod
    def snapshot(
        completed_at: str,
        *,
        observed: bool = False,
        complete: bool = True,
        scope_mode: str = "unfiltered",
        scope_fingerprint: str = "a" * 64,
        policy_revision: int = 7,
        connection_revision: int = 5,
    ) -> dict:
        """Build a bounded snapshot for one immutable provider scope."""

        return {
            "observedRecords": (
                [
                    {
                        "externalId": "device-1",
                        "externalName": "APP-01",
                        "providerParentId": "101",
                    }
                ]
                if observed
                else []
            ),
            "providerReadComplete": complete,
            "providerFilterId": "filter-1" if scope_mode == "provider_filtered" else "",
            "scopeMode": scope_mode,
            "discoveryScopeFingerprint": scope_fingerprint,
            "policyDecisionFingerprint": "b" * 64,
            "connectionRevision": connection_revision,
            "policyRevision": policy_revision,
            "snapshotStartedAt": completed_at,
            "providerReadCompletedAt": completed_at,
            "requiredAbsences": 3,
            "minimumMissingHours": 24,
        }

    def apply(self, run_id: str, snapshot: dict) -> dict[str, int]:
        """Apply a snapshot with the immutable run policy revision."""

        return self.repository._apply_state_ci_presence_snapshot(
            "ncentral",
            self.policy,
            run_id,
            snapshot,
            7,
        )

    def lifecycle_item(self) -> dict:
        result = self.repository.list_ci_presence_lifecycle(
            provider="ncentral",
            company_ids=["acme"],
        )
        self.assertEqual(result["total"], 1)
        return result["items"][0]

    def make_eligible(self) -> dict:
        """Publish the minimum complete evidence needed for one retirement action."""

        self.apply("run-1", self.snapshot("2026-07-10T00:00:00Z"))
        self.apply("run-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.apply("run-3", self.snapshot("2026-07-13T00:00:00Z"))
        return self.lifecycle_item()

    def test_complete_consecutive_absences_become_review_eligible(self) -> None:
        self.apply("run-observed", self.snapshot("2026-07-10T00:00:00Z", observed=True))
        self.apply("run-missing-1", self.snapshot("2026-07-11T00:00:00Z"))
        self.apply("run-missing-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.apply("run-missing-3", self.snapshot("2026-07-13T00:00:00Z"))

        item = self.lifecycle_item()
        self.assertEqual(item["state"], "eligible")
        self.assertEqual(item["consecutiveCompleteAbsences"], 3)
        self.assertTrue(item["actionAllowed"])
        self.assertEqual(item["availableAction"], "retire")
        self.assertEqual(self.state["assets"][0]["status"], "Active")

    def test_retire_and_restore_authority_expires_after_three_policy_cadences(self) -> None:
        """A stale preview cannot authorize a later irreversible mapping decision."""

        eligible = self.make_eligible()
        completed = datetime(2026, 7, 13, tzinfo=UTC)
        self.clock.return_value = completed + timedelta(hours=24)
        self.assertTrue(self.lifecycle_item()["actionAllowed"])
        self.clock.return_value = completed + timedelta(hours=24, seconds=1)
        expired = self.lifecycle_item()
        self.assertFalse(expired["actionAllowed"])
        self.assertIn("older than the allowed 24-hour window", expired["staleEvidenceReason"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.retire_ci_presence_mapping(
                eligible["id"], eligible["revision"], "Expired absence evidence", "admin"
            )

        self.clock.return_value = completed + timedelta(hours=24)
        retired = self.repository.retire_ci_presence_mapping(
            eligible["id"], eligible["revision"], "Verified absence evidence", "admin"
        )
        assert retired is not None
        reappeared_at = datetime(2026, 7, 14, tzinfo=UTC)
        self.apply(
            "run-reappeared-expiry",
            self.snapshot("2026-07-14T00:00:00Z", observed=True),
        )
        restore_ready = self.lifecycle_item()
        self.clock.return_value = reappeared_at + timedelta(hours=24)
        self.assertTrue(self.lifecycle_item()["actionAllowed"])
        self.clock.return_value = reappeared_at + timedelta(hours=24, seconds=1)
        self.assertFalse(self.lifecycle_item()["actionAllowed"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.restore_ci_presence_mapping(
                restore_ready["id"],
                restore_ready["revision"],
                "Expired reappearance evidence",
                "admin",
            )

    def test_manual_policy_can_publish_and_authorize_review(self) -> None:
        """Scheduler disablement must not invalidate an explicitly run manual preview."""

        self.policy.update(enabled=False, syncMode="manual")
        eligible = self.make_eligible()
        self.assertEqual(eligible["state"], "eligible")
        self.assertTrue(eligible["actionAllowed"])

    def test_terminal_preview_never_reenables_the_admin_kill_switch(self) -> None:
        """Run bookkeeping cannot override an administrator-disabled integration."""

        integration = self.state["integrations"][0]
        integration["enabled"] = False
        self.repository._record_state_ci_policy_preview(
            "ncentral",
            self.policy,
            {
                "id": "run-disabled-terminal",
                "type": "ncentral",
                "status": "success",
                "finishedAt": "2026-07-10T00:00:00Z",
                "attributes": {
                    "policyId": "policy-1",
                    "companyId": "acme",
                    "providerCompanyId": "101",
                },
            },
            "admin",
        )
        self.assertFalse(integration["enabled"])
        self.repository.record_company_discovery(
            "ncentral",
            {
                "id": "run-disabled-company-discovery",
                "type": "ncentral",
                "status": "success",
                "finishedAt": "2026-07-10T02:00:00Z",
            },
            [
                {
                    "externalId": "101",
                    "name": "Acme",
                    "active": True,
                    "deleted": False,
                }
            ],
            actor_id="admin",
        )
        self.assertFalse(integration["enabled"])
        self.repository.record_sync_run(
            "ncentral",
            {
                "id": "run-disabled-generic",
                "type": "ncentral",
                "status": "success",
                "finishedAt": "2026-07-10T01:00:00Z",
            },
            configured=True,
            actor_id="admin",
        )
        self.assertFalse(integration["enabled"])

    def test_incomplete_and_stale_snapshots_make_no_presence_mutation(self) -> None:
        before = deepcopy(self.state["integrationCiPresence"])
        self.apply(
            "run-incomplete",
            self.snapshot("2026-07-11T00:00:00Z", observed=True, complete=False),
        )
        self.apply(
            "run-stale-policy",
            self.snapshot("2026-07-12T00:00:00Z", policy_revision=6),
        )
        self.apply(
            "run-stale-connection",
            self.snapshot("2026-07-13T00:00:00Z", connection_revision=4),
        )
        self.assertEqual(self.state["integrationCiPresence"], before)

    def test_policy_and_connection_drift_revoke_stored_actions(self) -> None:
        """An operator cannot act on evidence captured under obsolete settings."""

        eligible = self.make_eligible()
        self.policy["providerFilterId"] = "managed-servers"
        filtered = self.lifecycle_item()
        self.assertFalse(filtered["actionAllowed"])
        self.assertIn("current CI policy uses provider filtering", filtered["staleEvidenceReason"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.retire_ci_presence_mapping(
                eligible["id"], eligible["revision"], "Obsolete evidence", "admin"
            )

        self.policy["providerFilterId"] = ""
        self.policy["revision"] = 8
        revised = self.lifecycle_item()
        self.assertFalse(revised["actionAllowed"])
        self.assertIn("policy changed", revised["staleEvidenceReason"])

        self.policy["revision"] = 7
        self.state["integrations"][0]["revision"] = 6
        changed_connection = self.lifecycle_item()
        self.assertFalse(changed_connection["actionAllowed"])
        self.assertIn("integration settings changed", changed_connection["staleEvidenceReason"])

        self.state["integrations"][0]["revision"] = 5
        self.state["integrations"][0]["enabled"] = False
        disabled_connection = self.lifecycle_item()
        self.assertFalse(disabled_connection["actionAllowed"])
        self.assertIn("integration is not active", disabled_connection["staleEvidenceReason"])

    def test_company_remap_blocks_publication_and_existing_actions(self) -> None:
        """Provider evidence cannot cross a customer remap race."""

        company_mapping = self.state["providerCompanyMappings"][0]
        company_mapping.update(
            companyId="other-customer",
            lastSyncedAt="2026-07-11T00:00:00Z",
        )
        before = deepcopy(self.state["integrationCiPresence"])
        result = self.apply("run-remapped", self.snapshot("2026-07-10T00:00:00Z"))
        self.assertEqual(sum(result.values()), 0)
        self.assertEqual(self.state["integrationCiPresence"], before)

        company_mapping.update(
            companyId="acme",
            active=True,
            lastSyncedAt="2026-07-01T00:00:00Z",
        )
        eligible = self.make_eligible()
        company_mapping.update(active=False, lastSyncedAt="2026-07-14T00:00:00Z")
        stale = self.lifecycle_item()
        self.assertFalse(stale["actionAllowed"])
        self.assertIn("provider customer mapping changed", stale["staleEvidenceReason"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.retire_ci_presence_mapping(
                eligible["id"], eligible["revision"], "Mapping changed", "admin"
            )

    def test_provider_filter_never_counts_absence(self) -> None:
        self.apply("run-observed", self.snapshot("2026-07-10T00:00:00Z", observed=True))
        self.apply(
            "run-filtered",
            self.snapshot("2026-07-12T00:00:00Z", scope_mode="provider_filtered"),
        )
        item = self.lifecycle_item()
        self.assertEqual(item["state"], "not_evaluated")
        self.assertEqual(item["consecutiveCompleteAbsences"], 0)
        self.assertFalse(item["actionAllowed"])
        denied = self.repository.list_ci_presence_lifecycle(provider="ncentral", company_ids=[])
        self.assertEqual(denied["total"], 0)

    def test_scope_change_restarts_missing_generation(self) -> None:
        self.apply("run-1", self.snapshot("2026-07-10T00:00:00Z"))
        self.apply("run-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.apply(
            "run-3",
            self.snapshot("2026-07-13T00:00:00Z", scope_fingerprint="c" * 64),
        )
        item = self.lifecycle_item()
        self.assertEqual(item["state"], "monitoring")
        self.assertEqual(item["consecutiveCompleteAbsences"], 1)

    def test_new_and_legacy_unscoped_mappings_cannot_advance_absence(self) -> None:
        mapping = self.state["providerCiMappings"][0]
        mapping["firstSeenAt"] = "2026-07-20T00:00:00Z"
        mapping["providerParentId"] = None
        self.apply("run-skip", self.snapshot("2026-07-10T00:00:00Z"))
        self.assertEqual(self.state["integrationCiPresence"], [])

        self.apply("run-seen", self.snapshot("2026-07-21T00:00:00Z", observed=True))
        self.assertEqual(mapping["providerParentId"], "101")
        self.assertEqual(self.lifecycle_item()["state"], "observed")

    def test_positive_observation_cannot_move_an_immutable_parent_scope(self) -> None:
        """The same external ID under another provider parent remains a review conflict."""

        mapping = self.state["providerCiMappings"][0]
        mapping["providerParentId"] = "202"
        result = self.apply(
            "run-wrong-parent",
            self.snapshot("2026-07-10T00:00:00Z", observed=True),
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(mapping["providerParentId"], "202")
        self.assertEqual(self.state["integrationCiPresence"], [])

    def test_retire_reappear_restore_is_explicit_and_never_retires_asset(self) -> None:
        self.apply("run-1", self.snapshot("2026-07-10T00:00:00Z"))
        self.apply("run-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.apply("run-3", self.snapshot("2026-07-13T00:00:00Z"))
        eligible = self.lifecycle_item()
        retired = self.repository.retire_ci_presence_mapping(
            eligible["id"], eligible["revision"], "Decommissioned in N-central", "admin"
        )
        assert retired is not None
        self.assertEqual(retired["state"], "retired")
        self.assertFalse(self.state["providerCiMappings"][0]["active"])
        self.assertEqual(self.state["assets"][0]["status"], "Active")
        with self.assertRaisesRegex(ValueError, "restore it from Missing devices"):
            self.repository.record_provider_ci_mapping(
                "ncentral",
                "acme",
                {"externalId": "device-1", "name": "APP-01", "providerParentId": "101"},
                "asset-1",
            )

        self.policy["providerFilterId"] = "managed-servers"
        self.apply(
            "run-reappeared",
            self.snapshot(
                "2026-07-14T00:00:00Z",
                observed=True,
                scope_mode="provider_filtered",
            ),
        )
        restore_ready = self.lifecycle_item()
        self.assertEqual(restore_ready["state"], "restore_ready")
        self.assertIsNotNone(restore_ready["reappearedAt"])
        self.assertTrue(restore_ready["actionAllowed"])
        self.state["integrationCiReviewItems"].append(
            {
                "policyId": "policy-1",
                "externalId": "device-1",
                "state": "pending",
            }
        )
        restored = self.repository.restore_ci_presence_mapping(
            restore_ready["id"],
            restore_ready["revision"],
            "Provider evidence verified",
            "admin",
        )
        assert restored is not None
        self.assertEqual(restored["state"], "observed")
        self.assertTrue(self.state["providerCiMappings"][0]["active"])
        self.assertEqual(self.state["integrationCiReviewItems"][0]["state"], "resolved")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repository.restore_ci_presence_mapping(
                restore_ready["id"],
                restore_ready["revision"],
                "Stale request",
                "admin",
            )

    def test_retirement_hides_pending_suggestions_but_preserves_topology(self) -> None:
        """Retiring source identity must not erase already reviewed CMDB knowledge."""

        self.state["assets"].append(
            {
                "id": "asset-2",
                "companyId": "acme",
                "name": "SQL-01",
                "status": "Active",
            }
        )
        self.state["relationships"].extend(
            [
                {
                    "id": "provider-edge",
                    "companyId": "acme",
                    "fromId": "asset-1",
                    "toId": "asset-2",
                    "type": "depends_on",
                    "impactPolicy": "required",
                    "sourceMappingId": "mapping-1",
                    "confidence": 0.9,
                    "evidence": {"provider": "ncentral"},
                    "provenance": "provider",
                },
                {
                    "id": "manual-edge",
                    "companyId": "acme",
                    "fromId": "asset-2",
                    "toId": "asset-1",
                    "type": "hosted_on",
                    "impactPolicy": "required",
                    "sourceMappingId": None,
                    "confidence": 1.0,
                    "evidence": {},
                    "provenance": "manual",
                },
            ]
        )
        self.state["ciRelationshipCandidates"] = [
            {
                "id": "candidate-1",
                "companyId": "acme",
                "sourceMappingId": "mapping-1",
                "provider": "ncentral",
                "candidateKey": "c" * 64,
                "fromCiId": "asset-1",
                "toCiId": "asset-2",
                "relationshipType": "depends_on",
                "confidence": 0.9,
                "evidence": {
                    "providerContext": {
                        "policyId": "policy-1",
                        "policyRevision": 7,
                        "connectionRevision": 5,
                        "providerParentId": "101",
                    }
                },
                "state": "pending",
                "firstObservedAt": "2026-07-01T00:00:00Z",
                "lastSeenAt": "2026-07-01T00:00:00Z",
                "retiredAt": None,
                "revision": 1,
                "observationCount": 1,
            }
        ]
        self.apply("run-1", self.snapshot("2026-07-10T00:00:00Z"))
        self.apply("run-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.apply("run-3", self.snapshot("2026-07-13T00:00:00Z"))
        eligible = self.lifecycle_item()

        retired = self.repository.retire_ci_presence_mapping(
            eligible["id"], eligible["revision"], "Source device removed", "admin"
        )
        assert retired is not None
        self.assertEqual(
            {item["id"] for item in self.repository.list_relationships()},
            {"provider-edge", "manual-edge"},
        )
        self.assertIsNotNone(self.state["ciRelationshipCandidates"][0]["retiredAt"])
        self.assertEqual(
            self.repository.list_relationship_candidates("acme", include_retired=True), []
        )
        with self.assertRaisesRegex(ValueError, "restore or refresh the source provider mapping"):
            self.repository.approve_relationship_candidate(
                "acme",
                "candidate-1",
                "admin",
                expected_revision=2,
            )

        self.apply("run-reappeared", self.snapshot("2026-07-14T00:00:00Z", observed=True))
        restore_ready = self.lifecycle_item()
        self.repository.restore_ci_presence_mapping(
            restore_ready["id"],
            restore_ready["revision"],
            "Fresh provider evidence verified",
            "admin",
        )
        self.assertEqual(
            {item["id"] for item in self.repository.list_relationships()},
            {"provider-edge", "manual-edge"},
        )
        self.assertEqual(self.repository.list_relationship_candidates("acme"), [])

    def test_reviewed_remap_within_immutable_scope_resets_missing_evidence(self) -> None:
        self.apply("run-1", self.snapshot("2026-07-10T00:00:00Z"))
        self.apply("run-2", self.snapshot("2026-07-12T00:00:00Z"))
        self.state["assets"].append(
            {
                "id": "asset-2",
                "companyId": "acme",
                "name": "APP-02",
                "status": "Active",
            }
        )
        self.repository.record_provider_ci_mapping(
            "ncentral",
            "acme",
            {"externalId": "device-1", "name": "APP-02", "providerParentId": "101"},
            "asset-2",
            "admin",
        )

        stored = self.state["integrationCiPresence"][0]
        self.assertEqual(stored["policyId"], "policy-1")
        self.assertEqual(stored["providerParentId"], "101")
        self.assertEqual(stored["assetId"], "asset-2")
        self.assertEqual(stored["state"], "observed")
        self.assertEqual(stored["absenceCount"], 0)

    def test_relationship_mutations_reject_stale_provider_context(self) -> None:
        """Topology proposals and approval share the preview's locked tenant generation."""

        self.state["assets"].append(
            {
                "id": "asset-2",
                "companyId": "acme",
                "name": "SQL-01",
                "status": "Active",
            }
        )
        context = {
            "policy_id": "policy-1",
            "expected_policy_revision": 7,
            "expected_connection_revision": 5,
            "provider_parent_id": "101",
        }
        mapping = self.state["providerCiMappings"][0]
        for unsafe_parent in ("202", None):
            mapping["providerParentId"] = unsafe_parent
            with self.assertRaisesRegex(ValueError, "source CI mapping belongs"):
                self.repository.upsert_relationship_candidates(
                    "ncentral", "acme", "mapping-1", [], **context
                )
        mapping["providerParentId"] = "101"
        candidates = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            "mapping-1",
            [
                {
                    "fromCiId": "asset-1",
                    "toCiId": "asset-2",
                    "relationshipType": "depends_on",
                    "confidence": 0.95,
                    "evidence": {"rule": "hypervisor_guest"},
                }
            ],
            **context,
        )
        candidate = candidates[0]
        self.assertEqual(candidate["evidence"]["providerContext"]["policyRevision"], 7)

        before_candidates = deepcopy(self.state["ciRelationshipCandidates"])
        self.policy["revision"] = 8
        with self.assertRaisesRegex(ValueError, "CI policy changed"):
            self.repository.upsert_relationship_candidates(
                "ncentral", "acme", "mapping-1", [], **context
            )
        self.assertEqual(self.state["ciRelationshipCandidates"], before_candidates)

        self.policy["revision"] = 7
        self.state["integrations"][0]["revision"] = 6
        with self.assertRaisesRegex(ValueError, "integration settings changed"):
            self.repository.approve_relationship_candidate(
                "acme", candidate["id"], "admin", expected_revision=1
            )
        self.assertEqual(self.state["relationships"], [])

        self.state["integrations"][0]["revision"] = 5
        self.state["providerCompanyMappings"][0].update(companyId="other-customer", active=True)
        with self.assertRaisesRegex(ValueError, "provider customer mapping changed"):
            self.repository.approve_relationship_candidate(
                "acme", candidate["id"], "admin", expected_revision=1
            )
        self.assertEqual(self.state["relationships"], [])

    def test_mapping_import_preflight_fails_closed_for_scope_and_inactive_rows(self) -> None:
        """An import cannot move or silently reactivate an immutable provider identity."""

        company_conflict = self.repository.classify_provider_ci_mapping_import(
            "ncentral", "other-customer", "device-1", "101"
        )
        self.assertEqual(company_conflict["decision"], "scope_conflict")
        self.assertFalse(company_conflict["companyMatches"])
        parent_conflict = self.repository.classify_provider_ci_mapping_import(
            "ncentral", "acme", "device-1", "202"
        )
        self.assertEqual(parent_conflict["decision"], "scope_conflict")
        self.assertFalse(parent_conflict["providerParentMatches"])
        with self.assertRaisesRegex(ValueError, "different customer or provider parent"):
            self.repository.record_provider_ci_mapping(
                "ncentral",
                "acme",
                {"externalId": "device-1", "name": "APP-01", "providerParentId": "202"},
                "asset-1",
            )
        self.state["providerCiMappings"][0]["active"] = False
        inactive = self.repository.classify_provider_ci_mapping_import(
            "ncentral", "acme", "device-1", "101"
        )
        self.assertEqual(inactive["decision"], "inactive_mapping")
        with self.assertRaisesRegex(ValueError, "inactive without a governed restore action"):
            self.repository.record_provider_ci_mapping(
                "ncentral",
                "acme",
                {"externalId": "device-1", "name": "APP-01", "providerParentId": "101"},
                "asset-1",
            )

    def test_atomic_reviewed_import_rolls_back_local_create_on_failure(self) -> None:
        """The local repository publishes neither half of a failed reviewed import."""

        before = deepcopy(self.state)
        with (
            patch.object(
                self.repository,
                "_provider_import_failpoint",
                side_effect=RuntimeError("synthetic mapping failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic mapping failure"),
        ):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {
                    "externalId": "device-new",
                    "name": "NEW-01",
                    "providerParentId": "101",
                },
                "create",
                asset={
                    "id": "asset-new",
                    "companyId": "acme",
                    "name": "NEW-01",
                    "type": "Server",
                    "status": "Active",
                    "fields": {},
                    "metadata": {},
                },
                actor_id="admin",
                provider_parent_id="101",
                policy_id="policy-1",
            )
        self.assertEqual(self.state, before)

    def test_atomic_update_guard_runs_before_canonical_mutation(self) -> None:
        """An inactive mapping cannot leave a partial canonical update behind."""

        self.state["providerCiMappings"][0]["active"] = False
        with self.assertRaisesRegex(ValueError, "inactive without a governed restore action"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {
                    "externalId": "device-1",
                    "name": "RENAMED",
                    "providerParentId": "101",
                },
                "update",
                asset_id="asset-1",
                changes={"name": "RENAMED"},
                actor_id="admin",
                provider_parent_id="101",
            )
        self.assertEqual(self.state["assets"][0]["name"], "APP-01")

    def test_atomic_import_revalidates_scope_review_and_update_target(self) -> None:
        """Every reviewed action fails before canonical writes when its context races."""

        self.state["assets"].append(
            {
                "id": "asset-2",
                "companyId": "acme",
                "name": "APP-02",
                "type": "Server",
                "status": "Active",
                "fields": {},
                "metadata": {},
            }
        )
        company_mapping = self.state["providerCompanyMappings"][0]
        company_mapping["companyId"] = "other-customer"
        before = deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "active provider customer mapping changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "device-new", "providerParentId": "101"},
                "create",
                asset={
                    "id": "asset-new",
                    "companyId": "acme",
                    "name": "NEW-01",
                    "type": "Server",
                    "status": "Active",
                    "fields": {},
                    "metadata": {},
                },
                provider_parent_id="101",
            )
        self.assertEqual(self.state, before)

        company_mapping["companyId"] = "acme"
        self.state["providerCompanyObservations"][0]["active"] = False
        with self.assertRaisesRegex(ValueError, "active provider customer mapping changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "device-1", "providerParentId": "101"},
                "link",
                asset_id="asset-2",
                provider_parent_id="101",
            )
        self.assertEqual(self.state["providerCiMappings"][0]["assetId"], "asset-1")

        self.state["providerCompanyObservations"][0]["active"] = True
        with self.assertRaisesRegex(ValueError, "update target no longer matches"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "device-1", "providerParentId": "101"},
                "update",
                asset_id="asset-2",
                changes={"name": "SHOULD-NOT-CHANGE"},
                provider_parent_id="101",
            )
        self.assertEqual(self.state["assets"][1]["name"], "APP-02")

        self.state["integrationCiReviewItems"].append(
            {
                "id": "review-new",
                "policyId": "policy-1",
                "companyId": "acme",
                "externalId": "device-new",
                "contentHash": "a" * 64,
                "state": "pending",
            }
        )
        with self.assertRaisesRegex(ValueError, "generation is stale"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "device-new", "providerParentId": "101"},
                "create",
                asset={
                    "id": "asset-new",
                    "companyId": "acme",
                    "name": "NEW-01",
                    "type": "Server",
                    "status": "Active",
                    "fields": {},
                    "metadata": {},
                },
                provider_parent_id="101",
                policy_id="policy-1",
                review_item_id="review-new",
                review_content_hash="a" * 64,
                expected_policy_revision=6,
                expected_connection_revision=5,
            )
        with self.assertRaisesRegex(ValueError, "queue item changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "device-new", "providerParentId": "101"},
                "create",
                asset={
                    "id": "asset-new",
                    "companyId": "acme",
                    "name": "NEW-01",
                    "type": "Server",
                    "status": "Active",
                    "fields": {},
                    "metadata": {},
                },
                provider_parent_id="101",
                policy_id="policy-1",
                review_item_id="review-new",
                review_content_hash="b" * 64,
                expected_policy_revision=7,
                expected_connection_revision=5,
            )
        self.assertFalse(any(item.get("id") == "asset-new" for item in self.state["assets"]))


if __name__ == "__main__":
    unittest.main()
