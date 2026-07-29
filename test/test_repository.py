import unittest
import uuid
from unittest.mock import MagicMock, patch

from src.cmdb.repository import (
    PostgresCmdbRepository,
    StateRepository,
    canonical_uuid,
    hash_password,
    verify_password,
)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "companies": [{"id": "acme", "name": "Acme", "externalIds": {}}],
            "users": [
                {
                    "id": "admin",
                    "email": "admin@example.com",
                    "password": "ChangeMe!",
                    "role": "platform_admin",
                    "companyIds": ["*"],
                }
            ],
            "accessGroups": [],
            "integrations": [
                {
                    "id": "connectwise",
                    "name": "ConnectWise Manage",
                    "type": "connectwise",
                    "enabled": False,
                    "status": "Not configured",
                }
            ],
            "syncRuns": [],
            "assets": [],
            "relationships": [],
        }
        self.saved = []
        self.repository = StateRepository(self.state, lambda value: self.saved.append(value))

    def test_string_ids_migrate_to_stable_canonical_uuids(self):
        first = canonical_uuid("configuration_item", "asset-1")
        second = canonical_uuid("configuration_item", "asset-1")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36)

    def test_existing_uuid_is_preserved(self):
        current = "175925a0-73a4-5a44-aee0-af21152589d8"
        self.assertEqual(canonical_uuid("configuration_item", current), current)

    def test_postgres_change_asset_filter_preserves_existing_uuid(self):
        current = str(uuid.uuid4())
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection_factory = MagicMock()
        connection_factory.return_value.__enter__.return_value = connection
        repository = PostgresCmdbRepository({}, lambda _state: None, connection_factory)

        self.assertEqual(repository.list_changes(asset_id=current), [])

        parameters = cursor.execute.call_args.args[1]
        self.assertEqual(parameters[-2:], (current, current))

    def test_postgres_change_methods_do_not_fall_through_to_state(self):
        self.assertIs(PostgresCmdbRepository.list_changes, StateRepository._postgres_list_changes)
        self.assertIs(
            PostgresCmdbRepository.create_change,
            StateRepository._postgres_create_change,
        )
        self.assertIs(
            PostgresCmdbRepository.update_change,
            StateRepository._postgres_update_change,
        )

    def test_password_hash_verifies_without_storing_plaintext(self):
        encoded = hash_password("VerySecret!42")
        self.assertNotIn("VerySecret!42", encoded)
        self.assertTrue(verify_password("VerySecret!42", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_plaintext_password_is_migrated_after_successful_login(self):
        user = self.repository.authenticate("admin@example.com", "ChangeMe!")
        self.assertIsNotNone(user)
        self.assertNotIn("password", self.state["users"][0])
        self.assertTrue(verify_password("ChangeMe!", self.state["users"][0]["passwordHash"]))

    def test_unknown_and_disabled_users_receive_dummy_password_verification(self):
        with patch(
            "src.cmdb.repository.verify_password",
            wraps=verify_password,
        ) as verifier:
            self.assertIsNone(self.repository.authenticate("missing@example.com", "wrong"))
            self.assertEqual(verifier.call_count, 1)

            self.state["users"][0]["status"] = "disabled"
            self.assertIsNone(self.repository.authenticate("admin@example.com", "wrong"))
            self.assertEqual(verifier.call_count, 2)

    def test_local_login_reservations_enforce_both_dimensions_and_dedupe_audit(self):
        first = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-a",
            identifier_limit=2,
            source_limit=10,
        )
        second = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-b",
            identifier_limit=2,
            source_limit=10,
        )
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.repository.finish_local_login_attempt(first["attemptId"], "password_failed")
        self.repository.finish_local_login_attempt(second["attemptId"], "password_failed")

        limited = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-c",
            identifier_limit=2,
            source_limit=10,
        )
        repeated = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-d",
            identifier_limit=2,
            source_limit=10,
        )
        self.assertFalse(limited["allowed"])
        self.assertGreaterEqual(limited["retryAfterSeconds"], 1)
        self.assertTrue(limited["auditRequired"])
        self.assertFalse(repeated["auditRequired"])

        source_one = self.repository.reserve_local_login_attempt(
            "identifier-b",
            "shared-source",
            identifier_limit=10,
            source_limit=1,
        )
        self.repository.finish_local_login_attempt(source_one["attemptId"], "password_failed")
        source_limited = self.repository.reserve_local_login_attempt(
            "identifier-c",
            "shared-source",
            identifier_limit=10,
            source_limit=1,
        )
        self.assertFalse(source_limited["allowed"])

    def test_completed_login_clears_identifier_but_preserves_source_failures(self):
        failed = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-a",
            identifier_limit=1,
            source_limit=1,
        )
        self.repository.finish_local_login_attempt(failed["attemptId"], "password_failed")
        self.assertEqual(self.repository.clear_local_login_failures("identifier-a"), 1)

        identifier_released = self.repository.reserve_local_login_attempt(
            "identifier-a",
            "source-b",
            identifier_limit=1,
            source_limit=1,
        )
        source_preserved = self.repository.reserve_local_login_attempt(
            "identifier-b",
            "source-a",
            identifier_limit=10,
            source_limit=1,
        )
        self.assertTrue(identifier_released["allowed"])
        self.assertFalse(source_preserved["allowed"])

    def test_login_challenge_can_only_be_consumed_once(self):
        self.repository.create_login_challenge(
            {
                "tokenHash": "challenge-token",
                "userId": "admin",
                "purpose": "verify",
                "attempts": 0,
                "maxAttempts": 5,
                "expiresAt": "2999-01-01T00:00:00Z",
            }
        )

        self.assertTrue(self.repository.consume_login_challenge("challenge-token"))
        self.assertFalse(self.repository.consume_login_challenge("challenge-token"))

    def test_created_user_only_stores_a_password_hash(self):
        created = self.repository.create_user(
            {
                "id": "reader",
                "email": "reader@example.com",
                "role": "client_reader",
                "companyIds": ["acme"],
            },
            "VerySecret!42",
            "admin",
        )
        self.assertNotIn("password", created)
        stored = next(item for item in self.state["users"] if item["id"] == "reader")
        self.assertNotIn("password", stored)
        self.assertTrue(verify_password("VerySecret!42", stored["passwordHash"]))

    def test_access_group_changes_are_audited(self):
        group = {
            "id": "managed",
            "name": "Managed",
            "description": "Managed customers",
            "companyIds": ["acme"],
            "ownerUserId": "admin",
            "membershipMode": "manual",
            "membershipRules": {},
            "system": False,
        }
        created = self.repository.create_access_group(group, "admin")
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["ownerLabel"], "admin@example.com")
        updated = self.repository.update_access_group(
            "managed",
            {"name": "Managed customers", "expectedRevision": 1},
            "admin",
        )
        self.assertEqual(updated["revision"], 2)
        self.assertIsNone(
            self.repository.update_access_group(
                "managed", {"name": "Stale update", "expectedRevision": 1}, "admin"
            )
        )
        self.assertTrue(self.repository.delete_access_group("managed", "admin"))
        self.assertEqual(
            [item["action"] for item in self.state["auditEvents"][:3]],
            ["deleted", "updated", "created"],
        )

    def test_change_package_is_numbered_persisted_and_audited(self):
        change = {
            "id": "change-1",
            "number": self.repository.next_change_number(2026),
            "companyId": "acme",
            "title": "Patch database",
            "scopeAssetIds": ["database-1"],
            "impactSnapshot": [
                {"assetId": "database-1", "name": "SQL01", "role": "Scope"},
                {"assetId": "application-1", "name": "Sage 200", "role": "Direct impact"},
            ],
        }
        stored = self.repository.create_change(change, "admin")
        self.assertEqual(stored["number"], "CHG-2026-0001")
        self.assertEqual(self.repository.get_change("change-1")["title"], "Patch database")
        self.assertEqual(self.repository.list_changes(), [change])
        self.assertEqual(self.repository.list_changes(company_id="acme"), [change])
        self.assertEqual(self.repository.list_changes(asset_id="database-1"), [change])
        self.assertEqual(self.repository.list_changes(asset_id="application-1"), [change])
        self.assertEqual(self.repository.list_changes(asset_id="unrelated"), [])
        self.assertEqual(self.repository.list_changes(company_id="northwind"), [])
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "change_request")

    def test_change_revision_update_is_persisted_and_audited(self):
        change = {
            "id": "change-1",
            "number": "CHG-2026-0001",
            "companyId": "acme",
            "title": "Patch database",
            "revision": 1,
            "impactSnapshot": [],
        }
        self.repository.create_change(change, "admin")
        updated = {**change, "title": "Patch database safely", "revision": 2}
        stored = self.repository.update_change(
            "change-1", updated, "admin", action="updated", reason="Plan revised"
        )
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(self.repository.get_change("change-1")["title"], "Patch database safely")
        self.assertEqual(self.state["auditEvents"][0]["reason"], "Plan revised")

    def test_sync_run_updates_connection_history_and_audit(self):
        run = {
            "id": "run-1",
            "type": "connectwise",
            "status": "blocked",
            "startedAt": "2026-07-13T10:00:00Z",
            "finishedAt": "2026-07-13T10:00:01Z",
            "discovered": 0,
            "imported": 0,
            "message": "Credentials are not configured",
            "attributes": {
                "operation": "configuration_preview",
                "companyId": "acme",
            },
        }
        stored = self.repository.record_sync_run("connectwise", run, False, "admin")
        self.assertEqual(stored["status"], "blocked")
        self.assertEqual(self.repository.list_sync_runs()[0]["id"], "run-1")
        self.assertEqual(
            self.repository.list_sync_runs(
                "connectwise",
                "blocked",
                "configuration_preview",
                "acme",
                10,
            )[0]["id"],
            "run-1",
        )
        self.assertEqual(
            self.repository.list_sync_runs(
                "connectwise",
                "blocked",
                "configuration_preview",
                company_ids={"acme"},
            )[0]["id"],
            "run-1",
        )
        self.assertEqual(
            self.repository.list_sync_runs(
                "connectwise",
                "blocked",
                "configuration_preview",
                company_ids={"northwind"},
            ),
            [],
        )
        self.assertEqual(self.repository.list_sync_runs(operation="company_discovery"), [])
        self.assertEqual(self.repository.list_integrations()[0]["lastSync"], run["finishedAt"])
        self.assertEqual(self.state["auditEvents"][0]["action"], "sync_completed")

    def test_connectwise_configuration_discovery_and_explicit_mapping(self):
        configured = self.repository.update_integration_connection(
            "connectwise",
            {
                "configuration": {
                    "baseUrl": "https://api.example.com/v4_6_release/apis/3.0",
                    "companyId": "ipt",
                    "clientId": "client-id",
                    "pageSize": 100,
                },
                "credentialsEncrypted": "ciphertext",
                "credentialsNonce": "nonce",
                "enabled": True,
                "connectionStatus": "configured",
                "expectedRevision": 1,
            },
            "admin",
        )
        self.assertEqual(configured["revision"], 2)
        self.assertNotIn("credentialsEncrypted", self.repository.list_integrations()[0])
        failed_test = self.repository.mark_integration_test(
            "connectwise", "error", "Provider rejected the credentials", "admin"
        )
        self.assertEqual(failed_test["connectionStatus"], "error")
        self.assertEqual(self.state["auditEvents"][0]["outcome"], "failed")
        with self.assertRaisesRegex(ValueError, "reload"):
            self.repository.update_integration_connection(
                "connectwise", {"expectedRevision": 1}, "admin"
            )

        run = {
            "id": "discovery-1",
            "type": "connectwise",
            "status": "review_required",
            "startedAt": "2026-07-21T10:00:00Z",
            "finishedAt": "2026-07-21T10:00:01Z",
            "discovered": 1,
            "imported": 0,
            "review": 1,
            "message": "One company requires review",
        }
        self.repository.record_company_discovery(
            "connectwise",
            run,
            [
                {
                    "externalId": "42",
                    "identifier": "ACME",
                    "name": "Acme",
                    "status": "Active",
                    "type": "Customer",
                    "site": "Head office",
                    "deleted": False,
                    "lastUpdated": "2026-07-21T09:00:00Z",
                }
            ],
            "admin",
        )
        observed = self.repository.list_provider_companies("connectwise")[0]
        self.assertIsNone(observed["mappedCompanyId"])
        mapped = self.repository.map_provider_company("connectwise", "42", "acme", "admin")
        self.assertEqual(mapped["mappedCompanyId"], "acme")
        default_policy = self.repository.get_ci_sync_policy("connectwise", "acme", "42")
        self.assertEqual(default_policy["typeMode"], "all")
        saved_policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {
                "typeMode": "selected",
                "includedTypeIds": ["11"],
                "typeMappings": {"11": "Server"},
                "blockUnmappedTypes": True,
                "statusMode": "selected",
                "includedStatusIds": ["3"],
                "syncMode": "continuous_preview",
                "intervalMinutes": 120,
                "enabled": True,
            },
            expected_revision=0,
            actor_id="admin",
        )
        self.assertEqual(saved_policy["revision"], 1)
        self.assertEqual(saved_policy["includedTypeIds"], ["11"])
        self.assertEqual(saved_policy["typeMappings"], {"11": "Server"})
        self.assertTrue(saved_policy["blockUnmappedTypes"])
        with self.assertRaisesRegex(ValueError, "reload"):
            self.repository.update_ci_sync_policy(
                "connectwise", "acme", "42", {}, expected_revision=0
            )
        self.assertTrue(self.repository.unmap_provider_company("connectwise", "42", "admin"))
        self.assertIsNone(
            self.repository.list_provider_companies("connectwise")[0]["mappedCompanyId"]
        )
        audit_actions = [item["action"] for item in self.state["auditEvents"]]
        self.assertIn("configuration_updated", audit_actions)
        self.assertIn("mapped", audit_actions)
        self.assertIn("created", audit_actions)
        self.assertIn("unmapped", audit_actions)

    def test_continuous_ci_policy_lease_and_review_queue_are_durable(self):
        self.repository.update_integration_connection(
            "connectwise",
            {
                "enabled": True,
                "credentialsEncrypted": "ciphertext",
                "credentialsNonce": "nonce",
                "expectedRevision": 1,
            },
            "admin",
        )
        policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {
                "syncMode": "continuous_preview",
                "intervalMinutes": 60,
                "enabled": True,
            },
            expected_revision=0,
            actor_id="admin",
        )
        claimed = self.repository.claim_due_ci_sync_policy(
            "connectwise", "worker-a", lease_seconds=120
        )
        self.assertEqual(claimed["id"], policy["id"])
        self.assertEqual(claimed["leaseOwner"], "worker-a")
        self.assertIsNone(self.repository.claim_due_ci_sync_policy("connectwise", "worker-b"))

        observation = {
            "externalId": "501",
            "name": "ACME-NEW-01",
            "action": "create",
            "assetId": None,
            "assetName": "",
            "reason": "No canonical identity was found",
            "changes": {},
            "changedFields": [],
            "record": {
                "externalId": "501",
                "name": "ACME-NEW-01",
                "type": "Laptop",
                "status": "Active",
                "providerTypeName": "Managed Laptop",
                "providerStatusName": "Active",
            },
        }
        summary = self.repository.replace_ci_review_items(
            policy["id"], "acme", "run-1", [observation], None
        )
        self.assertEqual(summary["created"], 1)
        pending = self.repository.list_ci_review_items("connectwise", "acme")
        self.assertEqual([item["externalId"] for item in pending], ["501"])
        self.assertEqual(self.repository.count_ci_review_items("connectwise", "acme"), 1)

        dismissed = self.repository.dismiss_ci_review_item(
            pending[0]["id"], "Approved duplicate noise", "admin"
        )
        self.assertEqual(dismissed["state"], "dismissed")
        self.repository.replace_ci_review_items(policy["id"], "acme", "run-2", [observation], None)
        self.assertEqual(
            self.repository.list_ci_review_items("connectwise", "acme", state="dismissed")[0][
                "externalId"
            ],
            "501",
        )

        changed = {**observation, "reason": "Provider evidence changed"}
        self.repository.replace_ci_review_items(policy["id"], "acme", "run-3", [changed], None)
        self.assertEqual(
            self.repository.list_ci_review_items("connectwise", "acme")[0]["state"],
            "pending",
        )
        completed = self.repository.complete_ci_sync_policy_run(
            policy["id"],
            lease_owner="worker-a",
            success=True,
        )
        self.assertEqual(completed["consecutiveFailures"], 0)
        self.assertIsNotNone(completed["nextRunAt"])
        self.assertIsNone(completed.get("leaseOwner"))

        manually_claimed = self.repository.claim_ci_sync_policy_now(
            policy["id"], "operator-a", lease_seconds=120
        )
        self.assertEqual(manually_claimed["leaseOwner"], "operator-a")
        self.assertIsNone(
            self.repository.claim_ci_sync_policy_now(policy["id"], "operator-b", lease_seconds=120)
        )
        first_failure = self.repository.complete_ci_sync_policy_run(
            policy["id"],
            lease_owner="operator-a",
            success=False,
            error="Provider unavailable",
        )
        self.assertTrue(first_failure["backoffActive"])
        self.assertEqual(first_failure["consecutiveFailures"], 1)
        self.assertEqual(first_failure["retryDelayMinutes"], 15)
        self.assertIsNotNone(first_failure["nextRunAt"])

        self.assertIsNotNone(
            self.repository.claim_ci_sync_policy_now(policy["id"], "operator-b", lease_seconds=120)
        )
        second_failure = self.repository.complete_ci_sync_policy_run(
            policy["id"],
            lease_owner="operator-b",
            success=False,
            error="Provider still unavailable",
        )
        self.assertEqual(second_failure["consecutiveFailures"], 2)
        self.assertEqual(second_failure["retryDelayMinutes"], 30)

        manual_policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "manual-parent",
            {"syncMode": "manual", "enabled": False},
            expected_revision=0,
            actor_id="admin",
        )
        self.assertIsNotNone(
            self.repository.claim_ci_sync_policy_now(
                manual_policy["id"], "operator-c", lease_seconds=120
            )
        )
        manual_completed = self.repository.complete_ci_sync_policy_run(
            manual_policy["id"],
            lease_owner="operator-c",
            success=False,
            error="Manual preview failed",
        )
        self.assertIsNone(manual_completed["nextRunAt"])

    def test_ci_review_ignore_is_durable_audited_and_reversible(self):
        """Immutable provider IDs should stay hidden until an administrator restores them."""

        policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {"syncMode": "continuous_preview", "enabled": True},
            expected_revision=0,
            actor_id="admin",
        )
        observation = {
            "externalId": "501",
            "name": "ACME-UNMANAGED-01",
            "action": "create",
            "assetId": None,
            "assetName": "",
            "reason": "No canonical identity was found",
            "record": {
                "externalId": "501",
                "name": "ACME-UNMANAGED-01",
                "type": "Laptop",
                "status": "Active",
            },
        }
        self.repository.replace_ci_review_items(policy["id"], "acme", "run-ignore-1", [observation])
        queued = self.repository.list_ci_review_items("connectwise", "acme")[0]

        suppressions = self.repository.ignore_ci_review_items(
            [queued["id"]], "Not managed under the MSP agreement", "admin"
        )

        self.assertEqual(len(suppressions), 1)
        self.assertEqual(
            self.repository.get_ci_sync_policy("connectwise", "acme", "42")["excludedExternalIds"],
            ["501"],
        )
        self.assertEqual(
            self.repository.query_ci_review_items(company_id="acme")["total"],
            0,
        )
        ignored = self.repository.query_integration_object_suppressions(company_id="acme")
        self.assertEqual(ignored["total"], 1)
        self.assertEqual(ignored["items"][0]["reason"], "Not managed under the MSP agreement")

        restored = self.repository.restore_integration_object_suppression(
            suppressions[0]["id"], "Agreement scope was expanded", "admin"
        )

        self.assertFalse(restored["active"])
        self.assertEqual(
            self.repository.query_ci_review_items(company_id="acme")["total"],
            1,
        )
        self.assertEqual(
            self.repository.get_ci_sync_policy("connectwise", "acme", "42")["excludedExternalIds"],
            [],
        )
        self.assertEqual(
            self.repository.query_integration_object_suppressions(company_id="acme", active=False)[
                "total"
            ],
            1,
        )
        actions = [item["action"] for item in self.state["auditEvents"]]
        self.assertIn("ignored", actions)
        self.assertIn("restored", actions)

    def test_integration_lifecycle_is_audited_and_blocks_worker_leases(self):
        configured = self.repository.update_integration_connection(
            "connectwise",
            {
                "enabled": True,
                "credentialsEncrypted": "ciphertext",
                "credentialsNonce": "nonce",
                "connectionStatus": "verified",
                "expectedRevision": 1,
            },
            "admin",
        )
        policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {"syncMode": "continuous_preview", "enabled": True},
            expected_revision=0,
            actor_id="admin",
        )
        impact = self.repository.integration_lifecycle_impact("connectwise")
        self.assertEqual(impact["ciPolicies"], 1)
        self.assertEqual(impact["enabledPolicies"], 1)

        paused = self.repository.change_integration_lifecycle(
            "connectwise",
            "paused",
            "Maintenance window",
            "admin",
            configured["revision"],
        )
        self.assertFalse(paused["enabled"])
        self.assertEqual(paused["lifecycleStatus"], "paused")
        self.assertIsNone(self.repository.claim_due_ci_sync_policy("connectwise", "worker-a"))

        resumed = self.repository.change_integration_lifecycle(
            "connectwise",
            "active",
            "Maintenance completed",
            "admin",
            paused["revision"],
        )
        self.assertEqual(
            self.repository.claim_due_ci_sync_policy("connectwise", "worker-b")["id"],
            policy["id"],
        )
        removed = self.repository.change_integration_lifecycle(
            "connectwise",
            "removed",
            "Provider retired",
            "admin",
            resumed["revision"],
            remove_configuration=True,
        )
        self.assertEqual(removed["lifecycleStatus"], "removed")
        self.assertFalse(removed["enabled"])
        self.assertFalse(removed["credentialsEncrypted"])
        self.assertEqual(removed["configuration"]["mode"], "removed")
        self.assertIn(
            "integration_removed",
            [item["action"] for item in self.state["auditEvents"]],
        )

    def test_ci_review_workbench_filters_pages_and_returns_exact_items(self):
        """The reconciliation workbench should scale without losing decision evidence."""

        policy = self.repository.update_ci_sync_policy(
            "connectwise",
            "acme",
            "42",
            {"syncMode": "continuous_preview", "enabled": True},
            expected_revision=0,
            actor_id="admin",
        )
        observations = [
            {
                "externalId": str(index),
                "name": name,
                "action": action,
                "assetId": None,
                "assetName": "",
                "reason": reason,
                "changes": {"status": "Active"} if action == "update" else {},
                "changedFields": ["status"] if action == "update" else [],
                "blockedFields": ["name"] if action == "conflict" else [],
                "fieldDecisions": [{"field": "name", "allowed": False, "reason": "protected"}]
                if action == "conflict"
                else [],
                "record": {
                    "externalId": str(index),
                    "name": name,
                    "type": "Server",
                    "status": "Active",
                },
            }
            for index, name, action, reason in (
                (501, "ACME-WEB-01", "create", "New provider identity"),
                (502, "ACME-DB-01", "update", "Mapped CI changed"),
                (503, "ACME-EDGE-01", "conflict", "Possible duplicate name"),
            )
        ]
        self.repository.replace_ci_review_items(policy["id"], "acme", "run-1", observations)

        first_page = self.repository.query_ci_review_items(
            kind="connectwise", company_ids={"acme"}, limit=2
        )
        self.assertEqual(first_page["total"], 3)
        self.assertEqual(len(first_page["items"]), 2)
        self.assertEqual(
            first_page["summary"], {"create": 1, "update": 1, "link": 0, "conflict": 1}
        )
        self.assertEqual(
            self.repository.query_ci_review_items(company_ids=set())["total"],
            0,
        )
        updates = self.repository.query_ci_review_items(
            kind="connectwise", company_id="acme", action="update", search="DB-01"
        )
        self.assertEqual(updates["total"], 1)
        self.assertEqual(updates["items"][0]["changedFields"], ["status"])
        conflict = self.repository.query_ci_review_items(action="conflict")["items"][0]
        exact = self.repository.get_ci_review_item(conflict["id"])
        self.assertEqual(exact["blockedFields"], ["name"])
        self.assertFalse(exact["fieldDecisions"][0]["allowed"])

    def test_msp_branding_is_normalized_persisted_and_audited(self):
        self.assertEqual(self.repository.get_msp_branding()["secondaryAccent"], "#7997ff")
        stored = self.repository.update_msp_branding(
            {
                "name": "IPT CMDB",
                "logoText": "IPT",
                "accent": "#4ed477",
                "secondaryAccent": "#5b7cfa",
                "logoDataUrl": "data:image/png;base64,abc",
                "logoFileName": "ipt.png",
                "reportFooter": "IPT Holdings | Controlled document",
            },
            "admin",
        )
        self.assertEqual(stored["name"], "IPT CMDB")
        self.assertEqual(self.state["mspBranding"]["logoFileName"], "ipt.png")
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "msp_branding")
        self.assertNotIn("data:image", self.state["auditEvents"][0]["after"]["logoDataUrl"])

    def test_email_configuration_and_outbox_are_audited_but_not_exported(self):
        configured = self.repository.update_email_connection(
            {
                "enabled": True,
                "authMode": "client_secret",
                "senderAddress": "cmdb@example.com",
                "clientSecretEncrypted": "ciphertext",
                "clientSecretNonce": "nonce",
                "status": "configured",
            },
            "admin",
        )
        message = self.repository.create_email_outbox(
            {
                "idempotencyKey": "test:1",
                "to": ["tech@example.com"],
                "subject": "Test",
                "bodyHtml": "<p>Secret operational content</p>",
            },
            "admin",
        )
        accepted = self.repository.update_email_outbox(
            message["id"],
            {"status": "accepted", "attempts": 1, "acceptedAt": "2026-07-20T12:00:00Z"},
            "admin",
        )
        self.assertEqual(configured["status"], "configured")
        self.assertEqual(accepted["status"], "accepted")
        self.assertNotIn("bodyHtml", self.repository.list_email_outbox()[0])
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "email_message")
        exported = self.repository.export_state()
        self.assertNotIn("emailConnection", exported)
        self.assertNotIn("emailOutbox", exported)

        failed_message = self.repository.create_email_outbox(
            {
                "idempotencyKey": "test:failed",
                "to": ["tech@example.com"],
                "subject": "Failed test",
            },
            "admin",
        )
        failed = self.repository.update_email_outbox(
            failed_message["id"],
            {"status": "failed", "attempts": 1, "lastError": "Provider unavailable"},
            "admin",
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.state["auditEvents"][0]["outcome"], "failed")

    def test_customer_branding_is_scoped_persisted_and_audited(self):
        self.assertEqual(self.repository.get_company_branding("acme")["name"], "Acme")
        stored = self.repository.update_company_branding(
            "acme",
            {
                "name": "Acme Portal",
                "logoText": "AC",
                "accent": "#123456",
                "secondaryAccent": "#654321",
                "logoDataUrl": "",
                "logoFileName": "",
            },
            "admin",
        )
        self.assertEqual(stored["name"], "Acme Portal")
        self.assertEqual(self.repository.list_company_branding()["acme"]["accent"], "#123456")
        self.assertEqual(self.state["auditEvents"][0]["entityType"], "company_branding")
        self.assertEqual(self.state["auditEvents"][0]["companyId"], "acme")

    def test_asset_write_creates_an_audit_event(self):
        asset = {
            "id": "asset-1",
            "companyId": "acme",
            "name": "APP01",
            "type": "Server",
        }
        self.repository.create_asset(asset, "admin")
        self.assertEqual(self.state["auditEvents"][0]["action"], "created")
        self.assertEqual(self.state["auditEvents"][0]["entityId"], "asset-1")
        self.assertEqual(len(self.saved), 1)

    def test_contacts_and_responsibility_history_are_tenant_scoped_and_audited(self):
        self.repository.create_asset(
            {
                "id": "asset-1",
                "companyId": "acme",
                "name": "Sage 200",
                "type": "Business system",
                "metadata": {},
            },
            "admin",
        )
        contact = self.repository.create_contact(
            {
                "id": "contact-1",
                "companyId": "acme",
                "displayName": "Jane Owner",
                "email": "jane@acme.example",
                "status": "active",
                "source": "manual",
                "syncStatus": "not_synced",
                "attributes": {},
            },
            "admin",
        )
        self.assertEqual(contact["responsibilityCount"], 0)
        assigned = self.repository.replace_asset_responsibilities(
            "asset-1",
            [
                {
                    "contactId": "contact-1",
                    "role": "business_owner",
                    "isPrimary": True,
                    "escalationOrder": 1,
                }
            ],
            "acme",
            "admin",
            reason="Owner confirmed",
        )
        self.assertEqual(assigned[0]["contactName"], "Jane Owner")
        self.assertEqual(
            self.repository.get_asset("asset-1")["responsibilities"][0]["role"],
            "business_owner",
        )
        self.assertEqual(self.repository.get_contact("contact-1")["responsibilityCount"], 1)
        self.repository.replace_asset_responsibilities(
            "asset-1", [], "acme", "admin", reason="Owner departed"
        )
        history = self.repository.list_contact_responsibilities(
            "acme", contact_id="contact-1", include_inactive=True
        )
        self.assertIsNotNone(history[0]["effectiveUntil"])
        self.assertEqual(self.state["auditEvents"][0]["reason"], "Owner departed")

    def test_contact_profile_changes_capture_field_level_history(self):
        self.repository.create_contact(
            {
                "id": "contact-1",
                "companyId": "acme",
                "displayName": "Jane Owner",
                "email": "jane@acme.example",
                "status": "active",
                "source": "manual",
                "syncStatus": "not_synced",
                "attributes": {},
            },
            "admin",
        )
        updated = self.repository.update_contact(
            "contact-1",
            {"jobTitle": "Finance Director", "status": "on_leave"},
            "admin",
            reason="Extended leave",
        )
        self.assertEqual(updated["jobTitle"], "Finance Director")
        event = self.state["auditEvents"][0]
        self.assertEqual(event["entityType"], "contact")
        self.assertEqual(event["reason"], "Extended leave")
        self.assertIn("jobTitle", {item["field"] for item in event["changes"]})

    def test_relationship_retirement_is_audited(self):
        relationship = {"id": "rel-1", "fromId": "a", "toId": "b", "type": "depends_on"}
        self.repository.create_relationship(relationship, "acme", "admin")
        self.assertTrue(self.repository.delete_relationship("rel-1", "acme", "admin"))
        self.assertEqual(self.state["relationships"], [])
        self.assertEqual(self.state["auditEvents"][0]["action"], "retired")

    def test_user_company_and_asset_lifecycle_handles_missing_and_disabled_records(self):
        self.assertIsNone(self.repository.authenticate("missing@example.com", "secret"))
        self.assertIsNone(self.repository.authenticate("admin@example.com", "wrong"))
        self.assertFalse(self.repository.set_user_status("missing", "disabled", "admin"))

        self.repository.create_user(
            {
                "id": "reader",
                "email": "reader@example.com",
                "role": "client_reader",
                "companyIds": ["acme"],
            },
            "VerySecret!42",
            "admin",
        )
        self.assertTrue(
            self.repository.set_user_status("reader", "disabled", "admin", reason="Access review")
        )
        self.assertNotIn("reader", {item["id"] for item in self.repository.list_users()})
        self.assertIsNone(self.repository.authenticate("reader@example.com", "VerySecret!42"))

        company = self.repository.create_company(
            {"id": "northwind", "name": "Northwind Traders", "externalIds": {}}, "admin"
        )
        self.assertEqual(company["id"], "northwind")
        asset = self.repository.create_asset(
            {"id": "asset-1", "companyId": "acme", "name": "APP01", "type": "Server"},
            "admin",
        )
        updated = self.repository.update_asset(asset["id"], {"status": "Retired"}, "admin")
        self.assertEqual(updated["status"], "Retired")
        self.assertIsNone(self.repository.update_asset("missing", {"status": "Retired"}, "admin"))
        self.assertFalse(self.repository.delete_relationship("missing", "acme", "admin"))

    def test_data_quality_exceptions_are_upserted_scoped_and_resolved(self):
        exception = {
            "id": "owner-gap:asset-1",
            "companyId": "acme",
            "ruleKey": "owner-gap",
            "entityId": "asset-1",
            "reason": "Temporary project ownership",
        }
        created = self.repository.create_data_quality_exception(exception, "admin")
        exception["reason"] = "Ownership review scheduled"
        updated = self.repository.create_data_quality_exception(exception, "admin")
        self.assertEqual(created["id"], updated["id"])
        self.assertEqual(len(self.repository.list_data_quality_exceptions("acme")), 1)
        self.assertEqual(updated["reason"], "Ownership review scheduled")

        resolved = self.repository.resolve_data_quality_exception(updated["id"], "admin")
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["resolvedBy"], "admin")
        self.assertIsNone(self.repository.resolve_data_quality_exception("missing", "admin"))
        self.assertEqual(self.repository.list_data_quality_exceptions("northwind"), [])

    def test_reconciliation_and_field_authority_decisions_are_governed(self):
        self.state["reconciliationCandidates"] = [
            {"id": "candidate-1", "companyId": "acme", "state": "pending"},
            {"id": "candidate-2", "companyId": "northwind", "state": "pending"},
        ]
        self.assertEqual(
            [item["id"] for item in self.repository.list_reconciliation_candidates("acme")],
            ["candidate-1"],
        )
        decision = self.repository.resolve_reconciliation_candidate(
            "candidate-1", "use_existing", "Serial number confirmed", "asset-1", "admin"
        )
        self.assertEqual(decision["state"], "approved")
        self.assertEqual(decision["targetAssetId"], "asset-1")
        self.assertIsNone(
            self.repository.resolve_reconciliation_candidate(
                "missing", "ignore", "Not relevant", None, "admin"
            )
        )

        rule = {
            "companyId": "acme",
            "ciType": "Server",
            "fieldName": "name",
            "provider": "ncentral",
            "priority": 100,
        }
        self.repository.upsert_field_authority(rule, "admin")
        rule["priority"] = 10
        updated = self.repository.upsert_field_authority(rule, "admin")
        self.assertEqual(updated["priority"], 10)
        self.assertEqual(len(self.repository.list_field_authority("acme")), 1)
        self.assertTrue(
            self.repository.delete_field_authority("acme", "Server", "name", "ncentral", "admin")
        )
        self.assertFalse(
            self.repository.delete_field_authority("acme", "Server", "name", "ncentral", "admin")
        )

    def test_audit_search_filters_and_redaction_preserve_tenant_boundaries(self):
        self.repository.record_audit_event(
            "acme",
            "admin",
            "configuration_item",
            "asset-1",
            "updated",
            before={"name": "APP01", "apiToken": "old-secret"},
            after={"name": "APP02", "apiToken": "new-secret"},
            reason="Rename approved",
            metadata={"password": "hidden", "ticket": "CHG-1"},
        )
        self.repository.record_audit_event(
            "northwind",
            None,
            "database",
            "database-1",
            "restored",
            severity="warning",
            actor_type="service",
            source_system="recovery",
        )
        acme = self.repository.list_audit_events(
            "acme",
            actor_id="admin",
            category="data",
            action="updated",
            entity_type="configuration_item",
            entity_id="asset-1",
            outcome="success",
            search="APP02",
            limit=1,
        )
        self.assertEqual(len(acme), 1)
        self.assertEqual(acme[0]["before"]["apiToken"], "[redacted]")
        self.assertEqual(acme[0]["metadata"]["password"], "[redacted]")
        self.assertEqual(self.repository.list_audit_events("acme", search="not-found"), [])
        self.assertEqual(len(self.repository.list_audit_events("northwind")), 1)

    def test_state_export_import_is_deep_copied_and_reinitializes_optional_collections(self):
        exported = self.repository.export_state()
        exported["companies"][0]["name"] = "Changed outside repository"
        self.assertEqual(self.state["companies"][0]["name"], "Acme")
        with self.assertRaisesRegex(ValueError, "Customer not found"):
            self.repository.get_company_branding("missing")

        result = self.repository.import_state(
            {
                "companies": [{"id": "new", "name": "New Customer"}],
                "users": [],
                "accessGroups": [],
                "assets": [{"id": "new-asset", "companyId": "new", "name": "CI"}],
                "relationships": [],
                "integrations": [],
                "syncRuns": [],
            },
            "admin",
        )
        self.assertEqual(result, {"companies": 1, "assets": 1, "relationships": 0})
        self.assertEqual(self.repository.list_contacts(), [])
        self.assertIn("auditEvents", self.state)


if __name__ == "__main__":
    unittest.main()
