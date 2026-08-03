"""Real PostgreSQL race tests for durable integration preview publication."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from src.cmdb.migrations import apply_migrations
from src.cmdb.repository import PostgresCmdbRepository

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv("TEST_POSTGRES_ADMIN_URL", "")
DATABASE_NAME = "cmdb_preview_concurrency_test"


def _database_url(base_url: str, name: str) -> str:
    """Replace the database component of one PostgreSQL URL."""

    return urlunparse(urlparse(base_url)._replace(path=f"/{name}"))


def _policy_input(
    *,
    enrichment_mode: str = "balanced",
    continuous: bool = False,
) -> dict:
    """Return a deterministic N-central policy for concurrency tests."""

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
        "intervalMinutes": 60,
        "enabled": continuous,
    }


def _review_item(external_id: str, action: str = "create") -> dict:
    """Return one reviewable immutable provider observation."""

    return {
        "externalId": external_id,
        "name": f"Device {external_id}",
        "action": action,
        "reason": "Immutable provider identity requires review",
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


def _summary(count: int = 1) -> dict:
    """Return bounded aggregate evidence for a successful preview."""

    return {
        "discovered": count,
        "included": count,
        "excluded": 0,
        "counts": {
            "create": count,
            "update": 0,
            "link": 0,
            "unchanged": 0,
            "conflict": 0,
        },
        "message": "Preview completed without provider or CMDB writes.",
    }


def _direct_run(policy_id: str, run_id: str, status: str = "success") -> dict:
    """Return one terminal direct-preview record under a policy lease."""

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "id": run_id,
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


def _presence_snapshot(
    completed_at: str,
    *,
    policy_revision: int,
    connection_revision: int,
    observed: bool = False,
    complete: bool = True,
) -> dict:
    """Return one bounded immutable-scope presence envelope."""

    return {
        "observedRecords": (
            [
                {
                    "externalId": "presence-device-1",
                    "externalName": "PRESENCE-01",
                    "providerParentId": "101",
                }
            ]
            if observed
            else []
        ),
        "providerReadComplete": complete,
        "providerFilterId": "",
        "scopeMode": "unfiltered",
        "discoveryScopeFingerprint": "a" * 64,
        "policyDecisionFingerprint": "b" * 64,
        "connectionRevision": connection_revision,
        "policyRevision": policy_revision,
        "snapshotStartedAt": completed_at,
        "providerReadCompletedAt": completed_at,
        "requiredAbsences": 3,
        "minimumMissingHours": 24,
    }


@unittest.skipUnless(ADMIN_URL, "TEST_POSTGRES_ADMIN_URL is not configured")
class PostgresPreviewConcurrencyTests(unittest.TestCase):
    """Prove cross-connection preview and policy lease invariants."""

    target_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """Create one disposable database used only by this test class."""

        if not DATABASE_NAME.startswith("cmdb_preview_concurrency_test"):
            raise RuntimeError("Unsafe PostgreSQL concurrency-test database name")
        with (
            psycopg.connect(ADMIN_URL, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (DATABASE_NAME,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE_NAME))
            )
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE_NAME)))
        cls.target_url = _database_url(ADMIN_URL, DATABASE_NAME)

    @classmethod
    def tearDownClass(cls) -> None:
        """Drop the disposable database after terminating test connections."""

        with (
            psycopg.connect(ADMIN_URL, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (DATABASE_NAME,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE_NAME))
            )

    def setUp(self) -> None:
        """Reset the schema and seed one customer, provider and saved policy."""

        with self.connection() as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA public CASCADE")
                cursor.execute("CREATE SCHEMA public")
        apply_migrations(self.connection, ROOT)
        state = {
            "companies": [
                {
                    "id": "acme",
                    "name": "Acme Manufacturing",
                    "externalIds": {},
                }
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
        self.repository = PostgresCmdbRepository(state, lambda _state: None, self.connection)
        self.repository.bootstrap()
        discovery_time = "2026-07-01T00:00:00Z"
        self.repository.record_company_discovery(
            "ncentral",
            {
                "id": "00000000-0000-0000-0000-000000000101",
                "status": "success",
                "startedAt": discovery_time,
                "finishedAt": discovery_time,
                "message": "Test organization discovery",
                "attributes": {"operation": "company_discovery"},
            },
            [
                {
                    "externalId": "101",
                    "identifier": "acme",
                    "name": "Acme Manufacturing",
                    "deleted": False,
                }
            ],
        )
        self.repository.map_provider_company("ncentral", "101", "acme")
        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(),
            expected_revision=0,
            actor_id=None,
        )

    def connection(self) -> psycopg.Connection:
        """Open an independent bounded-wait connection to the test database."""

        return psycopg.connect(
            self.target_url,
            options="-c statement_timeout=5000 -c lock_timeout=2000",
        )

    def _queue_and_claim(self, worker_id: str) -> tuple[dict, dict]:
        """Queue and claim one manual preview using the current policy snapshot."""

        queued = self.repository.create_ci_preview_run(
            "ncentral",
            "acme",
            "101",
            self.policy["id"],
            "manual_preview",
            None,
            self.policy,
        )
        claimed = self.repository.claim_ci_preview_run(
            worker_id,
            provider="ncentral",
            lease_seconds=120,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return queued, claimed

    def _review_rows(self) -> list[tuple]:
        """Return canonical review identity, state and run linkage."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT external_id, state, last_sync_run_id::text, content_hash
                FROM integration_ci_review_items
                WHERE policy_id = %s::uuid
                ORDER BY external_id
                """,
                (self.policy["id"],),
            )
            return cursor.fetchall()

    def _run_row(self, run_id: str) -> tuple:
        """Return raw terminal and lease evidence for one run."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, attempt_count, lease_owner, lease_until,
                       finished_at, review_count, attributes
                FROM sync_runs
                WHERE id = %s::uuid
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        assert row is not None
        return row

    def _policy_lease(self) -> tuple:
        """Return the current policy lease, revision and filter evidence."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT lease_owner, lease_until, revision, filter_policy
                FROM integration_ci_policies
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
            row = cursor.fetchone()
        assert row is not None
        return row

    def _policy_schedule(self) -> tuple:
        """Return persisted schedule and failure bookkeeping for the test policy."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT last_run_at, last_success_at, last_error,
                       consecutive_failures, next_run_at
                FROM integration_ci_policies
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
            row = cursor.fetchone()
        assert row is not None
        return row

    def _audit_count(self, entity_id: str, action: str) -> int:
        """Count one audited action for a canonical entity."""

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM audit_events
                WHERE entity_id = %s
                  AND action = %s
                """,
                (entity_id, action),
            )
            row = cursor.fetchone()
        assert row is not None
        return int(row[0])

    def test_publish_commits_review_snapshot_and_terminal_run_together(self) -> None:
        """A successful publish should expose one coherent terminal snapshot."""

        queued, _claimed = self._queue_and_claim("worker-a")

        terminal = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-a",
            [_review_item("7001"), _review_item("7002")],
            _summary(2),
        )

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "success")
        self.assertEqual(terminal["previewSummary"]["queueSummary"]["pending"], 2)
        run = self._run_row(queued["id"])
        self.assertEqual(run[0], "succeeded")
        self.assertEqual(run[1], 1)
        self.assertIsNone(run[2])
        self.assertIsNone(run[3])
        self.assertIsNotNone(run[4])
        self.assertEqual(run[5], 2)
        self.assertEqual(run[6]["resultSummary"]["queueSummary"]["pending"], 2)
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in self._review_rows()],
            [
                ("7001", "pending", queued["id"]),
                ("7002", "pending", queued["id"]),
            ],
        )
        self.assertEqual(self._policy_lease()[:2], (None, None))
        self.assertEqual(self._audit_count(self.policy["id"], "review_queue_refreshed"), 1)
        self.assertEqual(self._audit_count(queued["id"], "completed"), 1)

    def test_terminal_and_generic_runs_never_reenable_integration(self) -> None:
        """Only explicit administrator configuration may change the kill switch."""

        owner = "disabled-terminal-owner"
        self.assertIsNotNone(
            self.repository.claim_ci_sync_policy_now(self.policy["id"], owner, lease_seconds=120)
        )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections SET enabled = false
                WHERE provider = 'ncentral' AND company_id IS NULL
                """
            )
        run = _direct_run(
            self.policy["id"],
            "00000000-0000-0000-0000-000000000812",
        )
        run["attributes"]["policyRevision"] = self.policy["revision"]
        self.assertIsNotNone(
            self.repository.publish_and_complete_ci_policy_preview(
                "ncentral", self.policy["id"], owner, run, []
            )
        )
        self.repository.record_sync_run(
            "ncentral",
            {
                "id": "disabled-generic-run",
                "status": "success",
                "finishedAt": "2026-07-10T01:00:00Z",
                "message": "Completed while disabled",
            },
            configured=True,
        )
        connection = self.repository.get_integration_connection("ncentral")
        assert connection is not None
        self.assertFalse(connection["enabled"])
        self.repository.record_company_discovery(
            "ncentral",
            {
                "id": "00000000-0000-0000-0000-000000000813",
                "status": "success",
                "startedAt": "2026-07-10T02:00:00Z",
                "finishedAt": "2026-07-10T02:00:00Z",
                "message": "Discovery completed while disabled",
                "attributes": {"operation": "company_discovery"},
            },
            [
                {
                    "externalId": "101",
                    "identifier": "acme",
                    "name": "Acme Manufacturing",
                    "deleted": False,
                }
            ],
        )
        connection = self.repository.get_integration_connection("ncentral")
        assert connection is not None
        self.assertFalse(connection["enabled"])

    def test_publish_rolls_back_when_terminal_update_fails(self) -> None:
        """A terminal write failure must roll back review rows and audits."""

        baseline, _claimed = self._queue_and_claim("worker-baseline")
        self.repository.publish_and_complete_ci_preview_run(
            baseline["id"],
            "worker-baseline",
            [_review_item("existing-1")],
            _summary(),
        )
        original = self._review_rows()
        queued, _claimed = self._queue_and_claim("worker-a")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE FUNCTION reject_preview_success() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    IF OLD.status = 'running' AND NEW.status = 'succeeded' THEN
                        RAISE EXCEPTION 'test blocks preview success';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            cursor.execute(
                """
                CREATE TRIGGER reject_preview_success
                BEFORE UPDATE ON sync_runs
                FOR EACH ROW EXECUTE FUNCTION reject_preview_success()
                """
            )
        try:
            with self.assertRaises(psycopg.errors.RaiseException):
                self.repository.publish_and_complete_ci_preview_run(
                    queued["id"],
                    "worker-a",
                    [_review_item("replacement-1")],
                    _summary(),
                )
        finally:
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER IF EXISTS reject_preview_success ON sync_runs")
                cursor.execute("DROP FUNCTION IF EXISTS reject_preview_success()")

        self.assertEqual(self._review_rows(), original)
        run = self._run_row(queued["id"])
        self.assertEqual(run[0], "running")
        self.assertEqual(run[2], "worker-a")
        self.assertNotIn("resultSummary", run[6])
        self.assertEqual(self._audit_count(queued["id"], "completed"), 0)

    def test_cancel_requested_before_publish_does_not_mutate_review_queue(self) -> None:
        """Cancellation should win before any new review snapshot is visible."""

        baseline, _claimed = self._queue_and_claim("worker-baseline")
        self.repository.publish_and_complete_ci_preview_run(
            baseline["id"],
            "worker-baseline",
            [_review_item("existing-1")],
            _summary(),
        )
        original = self._review_rows()
        queued, _claimed = self._queue_and_claim("worker-a")
        self.repository.request_sync_run_cancel(queued["id"], None)

        terminal = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-a",
            [_review_item("replacement-1")],
            _summary(),
        )

        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal["status"], "cancelled")
        self.assertIsNone(terminal["previewSummary"])
        self.assertEqual(self._review_rows(), original)
        self.assertEqual(self._policy_lease()[:2], (None, None))
        self.assertEqual(self._audit_count(queued["id"], "completed"), 0)

    def test_queued_cancel_does_not_clear_a_newer_policy_owner(self) -> None:
        """Cancelling an old queued run must not unlock a reclaimed policy."""

        queued = self.repository.create_ci_preview_run(
            "ncentral",
            "acme",
            "101",
            self.policy["id"],
            "manual_preview",
            None,
            self.policy,
        )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = 'current-worker',
                    lease_until = now() + interval '5 minutes'
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )

        cancelled = self.repository.request_sync_run_cancel(queued["id"], None)

        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self._policy_lease()[0], "current-worker")

    def test_exhausted_cleanup_does_not_clear_a_newer_policy_owner(self) -> None:
        """Max-attempt cleanup must not unlock a policy reclaimed by another worker."""

        queued, _claimed = self._queue_and_claim("stale-worker")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sync_runs
                SET attempt_count = max_attempts,
                    lease_until = now() - interval '1 second'
                WHERE id = %s::uuid
                """,
                (queued["id"],),
            )
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = 'current-worker',
                    lease_until = now() + interval '5 minutes'
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )

        claimed = self.repository.claim_ci_preview_run(
            "cleanup-worker",
            provider="ncentral",
        )

        self.assertIsNone(claimed)
        self.assertEqual(self._run_row(queued["id"])[0], "failed")
        self.assertEqual(self._policy_lease()[0], "current-worker")

    def test_expired_lease_is_taken_over_and_stale_worker_cannot_publish(self) -> None:
        """Only the worker that reclaimed an expired run may publish its result."""

        queued, _claimed = self._queue_and_claim("worker-a")
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sync_runs
                SET lease_until = clock_timestamp() - interval '1 second'
                WHERE id = %s::uuid
                """,
                (queued["id"],),
            )
        reclaimed = self.repository.claim_ci_preview_run(
            "worker-b",
            provider="ncentral",
            lease_seconds=120,
        )
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertEqual(reclaimed["id"], queued["id"])
        self.assertEqual(reclaimed["attemptCount"], 2)

        stale = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-a",
            [_review_item("stale-item")],
            _summary(),
        )
        current = self.repository.publish_and_complete_ci_preview_run(
            queued["id"],
            "worker-b",
            [_review_item("current-item")],
            _summary(),
        )

        self.assertIsNone(stale)
        self.assertIsNotNone(current)
        self.assertEqual([row[0] for row in self._review_rows()], ["current-item"])
        self.assertEqual(self._run_row(queued["id"])[:2], ("succeeded", 2))
        self.assertEqual(self._audit_count(queued["id"], "claimed"), 2)
        self.assertEqual(self._audit_count(queued["id"], "completed"), 1)

    def test_concurrent_claim_has_exactly_one_winner(self) -> None:
        """Two independent replicas must not claim the same fresh attempt."""

        queued = self.repository.create_ci_preview_run(
            "ncentral",
            "acme",
            "101",
            self.policy["id"],
            "manual_preview",
            None,
            self.policy,
        )
        barrier = threading.Barrier(2)

        def claim(worker_id: str) -> tuple[str, dict | None]:
            barrier.wait(timeout=5)
            return (
                worker_id,
                self.repository.claim_ci_preview_run(
                    worker_id,
                    provider="ncentral",
                    lease_seconds=120,
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, ("worker-a", "worker-b")))

        winners = [(worker, run) for worker, run in outcomes if run is not None]
        self.assertEqual(len(winners), 1)
        winner, run = winners[0]
        assert run is not None
        self.assertEqual(run["id"], queued["id"])
        raw = self._run_row(queued["id"])
        self.assertEqual(raw[:3], ("running", 1, winner))
        self.assertEqual(self._policy_lease()[0], winner)
        self.assertEqual(self._audit_count(queued["id"], "claimed"), 1)

    def test_policy_edit_preserves_owner_and_stale_owner_cannot_finish(self) -> None:
        """Editing a leased policy must neither unlock it nor let a stale owner clear it."""

        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(continuous=True),
            expected_revision=self.policy["revision"],
            actor_id=None,
        )
        claimed = self.repository.claim_due_ci_sync_policy(
            "ncentral",
            "worker-a",
            lease_seconds=120,
        )
        self.assertIsNotNone(claimed)
        before = self._policy_lease()

        changed = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(enrichment_mode="full", continuous=True),
            expected_revision=self.policy["revision"],
            actor_id=None,
        )
        after = self._policy_lease()

        self.assertEqual(after[0], "worker-a")
        self.assertEqual(after[1], before[1])
        self.assertEqual(after[2], before[2] + 1)
        self.assertEqual(changed["enrichmentMode"], "full")
        self.assertIsNone(self.repository.claim_due_ci_sync_policy("ncentral", "worker-b"))
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = 'worker-b',
                    lease_until = now() + interval '2 minutes'
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
        self.assertIsNone(
            self.repository.complete_ci_sync_policy_run(
                self.policy["id"],
                lease_owner="worker-a",
                success=True,
            )
        )
        self.assertEqual(self._policy_lease()[0], "worker-b")
        with patch.object(
            self.repository,
            "list_ci_sync_policies",
            side_effect=AssertionError("single-policy operations must use a targeted lookup"),
        ):
            renewed = self.repository.renew_ci_sync_policy_run(
                self.policy["id"],
                "worker-b",
                lease_seconds=120,
            )
            completed = self.repository.complete_ci_sync_policy_run(
                self.policy["id"],
                lease_owner="worker-b",
                success=True,
            )
        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertEqual(renewed["id"], self.policy["id"])
        self.assertIsNotNone(completed)
        self.assertEqual(self._policy_lease()[:2], (None, None))

    def test_retry_preserves_manual_sync_schedule_bookkeeping(self) -> None:
        """A retry keeps its UI label while recovering the original policy schedule."""

        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            _policy_input(continuous=True),
            expected_revision=self.policy["revision"],
            actor_id=None,
        )
        queued = self.repository.create_ci_preview_run(
            "ncentral",
            "acme",
            "101",
            self.policy["id"],
            "manual_sync",
            None,
            self.policy,
        )
        self.repository.claim_ci_preview_run("worker-failure", provider="ncentral")
        self.repository.fail_ci_preview_run(
            queued["id"],
            "worker-failure",
            "Temporary provider failure",
        )
        failed_schedule = self._policy_schedule()
        self.assertEqual(failed_schedule[3], 1)
        self.assertIsNotNone(failed_schedule[4])

        retry = self.repository.retry_ci_preview_run(queued["id"], None)
        claimed = self.repository.claim_ci_preview_run("worker-retry", provider="ncentral")
        self.assertIsNotNone(claimed)
        terminal = self.repository.publish_and_complete_ci_preview_run(
            retry["id"],
            "worker-retry",
            [],
            _summary(0),
        )

        self.assertEqual(retry["trigger"], "retry")
        self.assertIsNotNone(terminal)
        recovered_schedule = self._policy_schedule()
        self.assertIsNotNone(recovered_schedule[0])
        self.assertIsNotNone(recovered_schedule[1])
        self.assertIsNone(recovered_schedule[2])
        self.assertEqual(recovered_schedule[3], 0)
        self.assertIsNotNone(recovered_schedule[4])
        raw_retry = self._run_row(retry["id"])
        self.assertEqual(raw_retry[6]["scheduleTrigger"], "manual_sync")

    def test_direct_preview_publication_is_atomic_and_owner_guarded(self) -> None:
        """A stale direct worker can publish neither success nor failure evidence."""

        claimed = self.repository.claim_ci_sync_policy_now(
            self.policy["id"],
            "worker-a",
            lease_seconds=120,
        )
        self.assertIsNotNone(claimed)
        successful_run_id = "00000000-0000-0000-0000-000000000951"
        with patch.object(
            self.repository,
            "list_ci_sync_policies",
            side_effect=AssertionError("single-policy operations must use a targeted lookup"),
        ):
            published = self.repository.publish_and_complete_ci_policy_preview(
                "ncentral",
                self.policy["id"],
                "worker-a",
                _direct_run(self.policy["id"], successful_run_id),
                [_review_item("direct-current")],
                None,
            )

        self.assertIsNotNone(published)
        assert published is not None
        self.assertEqual(published["run"]["id"], successful_run_id)
        self.assertEqual(published["queueSummary"]["pending"], 1)
        self.assertEqual([row[0] for row in self._review_rows()], ["direct-current"])
        self.assertEqual(self._run_row(successful_run_id)[0], "succeeded")
        self.assertEqual(self._policy_lease()[:2], (None, None))

        self.repository.claim_ci_sync_policy_now(
            self.policy["id"],
            "worker-a",
            lease_seconds=120,
        )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET lease_owner = 'worker-b',
                    lease_until = now() + interval '2 minutes'
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
            cursor.execute("SELECT count(*) FROM sync_runs")
            runs_before = int(cursor.fetchone()[0])
        stale_success = self.repository.publish_and_complete_ci_policy_preview(
            "ncentral",
            self.policy["id"],
            "worker-a",
            _direct_run(
                self.policy["id"],
                "00000000-0000-0000-0000-000000000952",
            ),
            [_review_item("direct-stale")],
            None,
        )
        stale_failure = self.repository.fail_and_complete_ci_policy_preview(
            "ncentral",
            self.policy["id"],
            "worker-a",
            _direct_run(
                self.policy["id"],
                "00000000-0000-0000-0000-000000000953",
                "failed",
            ),
            "Provider unavailable",
            None,
        )

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM sync_runs")
            runs_after = int(cursor.fetchone()[0])
        self.assertIsNone(stale_success)
        self.assertIsNone(stale_failure)
        self.assertEqual(runs_after, runs_before)
        self.assertEqual([row[0] for row in self._review_rows()], ["direct-current"])
        self.assertEqual(self._policy_lease()[0], "worker-b")

        current_failure_id = "00000000-0000-0000-0000-000000000955"
        with patch.object(
            self.repository,
            "list_ci_sync_policies",
            side_effect=AssertionError("single-policy operations must use a targeted lookup"),
        ):
            current_failure = self.repository.fail_and_complete_ci_policy_preview(
                "ncentral",
                self.policy["id"],
                "worker-b",
                _direct_run(self.policy["id"], current_failure_id, "failed"),
                "Provider unavailable",
                None,
            )
        self.assertIsNotNone(current_failure)
        self.assertEqual(self._run_row(current_failure_id)[0], "failed")
        self.assertEqual(self._policy_lease()[:2], (None, None))
        self.assertEqual(self._policy_schedule()[3], 1)

    def test_postgres_presence_lifecycle_is_review_gated_and_non_destructive(self) -> None:
        """Complete absence can retire only a mapping and fresh evidence can restore it."""

        lifecycle_policy = _policy_input()
        lifecycle_policy["providerFilterId"] = ""
        self.policy = self.repository.update_ci_sync_policy(
            "ncentral",
            "acme",
            "101",
            lifecycle_policy,
            expected_revision=self.policy["revision"],
            actor_id=None,
        )

        asset = self.repository.create_asset(
            {
                "id": "presence-asset-1",
                "companyId": "acme",
                "name": "PRESENCE-01",
                "type": "Server",
                "status": "Active",
                "source": "manual",
                "fields": {},
                "metadata": {"lifecycle": "in_service", "operationalStatus": "healthy"},
            }
        )
        mapping = self.repository.record_provider_ci_mapping(
            "ncentral",
            "acme",
            {
                "externalId": "presence-device-1",
                "name": "PRESENCE-01",
                "providerParentId": "101",
            },
            asset["id"],
        )
        dependency = self.repository.create_asset(
            {
                "id": "presence-asset-2",
                "companyId": "acme",
                "name": "PRESENCE-SQL-01",
                "type": "Database",
                "status": "Active",
                "source": "manual",
                "fields": {},
                "metadata": {"lifecycle": "in_service", "operationalStatus": "healthy"},
            }
        )
        provider_relationship = self.repository.create_relationship(
            {
                "id": "presence-provider-edge",
                "fromId": asset["id"],
                "toId": dependency["id"],
                "type": "depends_on",
                "sourceMappingId": mapping["id"],
                "provenance": "provider",
            },
            "acme",
        )
        manual_relationship = self.repository.create_relationship(
            {
                "id": "presence-manual-edge",
                "fromId": dependency["id"],
                "toId": asset["id"],
                "type": "managed_by",
            },
            "acme",
        )
        connection = self.repository.get_integration_connection("ncentral")
        assert connection is not None
        relationship_context = {
            "policy_id": self.policy["id"],
            "expected_policy_revision": self.policy["revision"],
            "expected_connection_revision": connection["revision"],
            "provider_parent_id": "101",
        }
        for unsafe_parent in ("202", None):
            with self.connection() as database, database.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE external_object_mappings
                    SET external_parent_id = %s
                    WHERE id = %s::uuid
                    """,
                    (unsafe_parent, mapping["id"]),
                )
            with self.assertRaisesRegex(ValueError, "Provider mapping is unavailable"):
                self.repository.upsert_relationship_candidates(
                    "ncentral", "acme", mapping["id"], [], **relationship_context
                )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE external_object_mappings
                SET external_parent_id = '101'
                WHERE id = %s::uuid
                """,
                (mapping["id"],),
            )
        candidate = self.repository.upsert_relationship_candidates(
            "ncentral",
            "acme",
            mapping["id"],
            [
                {
                    "fromCiId": asset["id"],
                    "toCiId": dependency["id"],
                    "relationshipType": "connected_to",
                    "confidence": 0.9,
                    "evidence": {"rule": "presence_lifecycle_contract"},
                }
            ],
            observed_at="2029-12-31T00:00:00Z",
            **relationship_context,
        )[0]
        with self.assertRaisesRegex(ValueError, "CI policy changed"):
            self.repository.upsert_relationship_candidates(
                "ncentral",
                "acme",
                mapping["id"],
                [],
                expected_policy_revision=self.policy["revision"] - 1,
                expected_connection_revision=connection["revision"],
                policy_id=self.policy["id"],
                provider_parent_id="101",
            )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections
                SET revision = revision + 1
                WHERE provider = 'ncentral' AND company_id IS NULL
                """
            )
        with self.assertRaisesRegex(ValueError, "integration settings changed"):
            self.repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                expected_revision=candidate["revision"],
            )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_connections
                SET revision = %s
                WHERE provider = 'ncentral' AND company_id IS NULL
                """,
                (connection["revision"],),
            )
        self.assertTrue(self.repository.unmap_provider_company("ncentral", "101"))
        with self.assertRaisesRegex(ValueError, "provider customer mapping"):
            self.repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                expected_revision=candidate["revision"],
            )
        self.repository.map_provider_company("ncentral", "101", "acme")
        scope_conflict = self.repository.classify_provider_ci_mapping_import(
            "ncentral", "acme", "presence-device-1", "different-parent"
        )
        self.assertEqual(scope_conflict["decision"], "scope_conflict")
        self.assertFalse(scope_conflict["providerParentMatches"])
        with self.assertRaisesRegex(ValueError, "different customer or provider parent"):
            self.repository.record_provider_ci_mapping(
                "ncentral",
                "acme",
                {
                    "externalId": "presence-device-1",
                    "name": "PRESENCE-01",
                    "providerParentId": "different-parent",
                },
                asset["id"],
            )

        def publish(run_id: str, completed_at: str, *, observed: bool = False) -> None:
            claimed = self.repository.claim_ci_sync_policy_now(
                self.policy["id"], run_id, lease_seconds=120
            )
            self.assertIsNotNone(claimed)
            run = _direct_run(self.policy["id"], run_id)
            run["attributes"]["policyRevision"] = self.policy["revision"]
            published = self.repository.publish_and_complete_ci_policy_preview(
                "ncentral",
                self.policy["id"],
                run_id,
                run,
                [],
                presence_snapshot=_presence_snapshot(
                    completed_at,
                    policy_revision=self.policy["revision"],
                    connection_revision=connection["revision"],
                    observed=observed,
                ),
            )
            self.assertIsNotNone(published)

        publish("00000000-0000-0000-0000-000000000961", "2030-01-01T00:00:00Z")
        publish("00000000-0000-0000-0000-000000000962", "2030-01-02T01:00:00Z")
        publish("00000000-0000-0000-0000-000000000963", "2030-01-03T02:00:00Z")
        lifecycle = self.repository.list_ci_presence_lifecycle(
            provider="ncentral", company_ids=["acme"]
        )
        self.assertEqual(lifecycle["summary"]["eligible"], 1)
        eligible = lifecycle["items"][0]
        completed_at = datetime(2030, 1, 3, 2, tzinfo=UTC)
        with patch(
            "src.cmdb.repository._ci_presence_reference_time",
            return_value=completed_at + timedelta(hours=24),
        ):
            self.assertTrue(
                self.repository.list_ci_presence_lifecycle(
                    provider="ncentral", company_ids=["acme"]
                )["items"][0]["actionAllowed"]
            )
        with patch(
            "src.cmdb.repository._ci_presence_reference_time",
            return_value=completed_at + timedelta(hours=24, seconds=1),
        ):
            expired = self.repository.list_ci_presence_lifecycle(
                provider="ncentral", company_ids=["acme"]
            )["items"][0]
            self.assertFalse(expired["actionAllowed"])
            with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
                self.repository.retire_ci_presence_mapping(
                    eligible["id"], eligible["revision"], "Expired provider evidence", None
                )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET filter_policy = filter_policy || '{"providerFilterId":"managed"}'::jsonb
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
        filtered = self.repository.list_ci_presence_lifecycle(
            provider="ncentral", company_ids=["acme"]
        )["items"][0]
        self.assertFalse(filtered["actionAllowed"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.retire_ci_presence_mapping(
                eligible["id"], eligible["revision"], "Stale filtered evidence", None
            )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE integration_ci_policies
                SET filter_policy = filter_policy || '{"providerFilterId":""}'::jsonb
                WHERE id = %s::uuid
                """,
                (self.policy["id"],),
            )
            cursor.execute(
                """
                UPDATE provider_company_observations observation
                SET active = false
                FROM integration_connections integration
                WHERE observation.integration_connection_id = integration.id
                  AND integration.provider = 'ncentral'
                  AND observation.external_id = '101'
                """
            )
        inactive_scope = self.repository.list_ci_presence_lifecycle(
            provider="ncentral", company_ids=["acme"]
        )["items"][0]
        self.assertFalse(inactive_scope["actionAllowed"])
        with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
            self.repository.retire_ci_presence_mapping(
                eligible["id"], eligible["revision"], "Inactive customer evidence", None
            )
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE provider_company_observations observation
                SET active = true
                FROM integration_connections integration
                WHERE observation.integration_connection_id = integration.id
                  AND integration.provider = 'ncentral'
                  AND observation.external_id = '101'
                """
            )
        retired = self.repository.retire_ci_presence_mapping(
            eligible["id"], eligible["revision"], "Verified provider retirement", None
        )
        assert retired is not None
        self.assertEqual(retired["state"], "retired")
        self.assertEqual(
            self.repository.classify_provider_ci_mapping_import(
                "ncentral", "acme", "presence-device-1", "101"
            )["decision"],
            "restore_required",
        )
        self.assertIsNotNone(self.repository.get_asset(asset["id"]))
        self.assertTrue(
            {provider_relationship["id"], manual_relationship["id"]}.issubset(
                {item["id"] for item in self.repository.list_relationships()}
            )
        )
        self.assertEqual(
            self.repository.list_relationship_candidates("acme", include_retired=True), []
        )
        with self.assertRaisesRegex(ValueError, "Restore the provider mapping"):
            self.repository.approve_relationship_candidate(
                "acme",
                candidate["id"],
                expected_revision=candidate["revision"],
            )
        with self.assertRaisesRegex(ValueError, "restore it from Missing devices"):
            self.repository.record_provider_ci_mapping(
                "ncentral",
                "acme",
                {
                    "externalId": "presence-device-1",
                    "name": "PRESENCE-01",
                    "providerParentId": "101",
                },
                asset["id"],
            )

        publish(
            "00000000-0000-0000-0000-000000000964",
            "2030-01-04T03:00:00Z",
            observed=True,
        )
        restore_ready = self.repository.list_ci_presence_lifecycle(
            provider="ncentral", company_ids=["acme"]
        )["items"][0]
        self.assertEqual(restore_ready["state"], "restore_ready")
        reappeared_at = datetime(2030, 1, 4, 3, tzinfo=UTC)
        with patch(
            "src.cmdb.repository._ci_presence_reference_time",
            return_value=reappeared_at + timedelta(hours=24, seconds=1),
        ):
            self.assertFalse(
                self.repository.list_ci_presence_lifecycle(
                    provider="ncentral", company_ids=["acme"]
                )["items"][0]["actionAllowed"]
            )
            with self.assertRaisesRegex(ValueError, "Presence evidence is stale"):
                self.repository.restore_ci_presence_mapping(
                    restore_ready["id"],
                    restore_ready["revision"],
                    "Expired positive evidence",
                    None,
                )
        restored = self.repository.restore_ci_presence_mapping(
            restore_ready["id"],
            restore_ready["revision"],
            "Fresh provider evidence verified",
            None,
        )
        assert restored is not None
        self.assertEqual(restored["state"], "observed")
        active = self.repository.list_provider_ci_mappings("ncentral", "acme")
        self.assertEqual(active[0]["id"], mapping["id"])
        self.assertEqual(
            self.repository.classify_provider_ci_mapping_import(
                "ncentral", "acme", "presence-device-1", "101"
            )["decision"],
            "allow",
        )
        self.assertEqual(self.repository.list_relationship_candidates("acme"), [])
        self.assertTrue(
            {provider_relationship["id"], manual_relationship["id"]}.issubset(
                {item["id"] for item in self.repository.list_relationships()}
            )
        )

    def test_atomic_provider_create_rolls_back_every_write_at_failpoint(self) -> None:
        """A mapping failure cannot commit an orphan canonical CI or audit trail."""

        external_id = "atomic-fail-device"
        with (
            patch.object(
                self.repository,
                "_provider_import_failpoint",
                side_effect=RuntimeError("synthetic atomic import failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "synthetic atomic import failure"),
        ):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {
                    "externalId": external_id,
                    "name": "ATOMIC-FAIL-01",
                    "providerParentId": "101",
                },
                "create",
                asset={
                    "id": "atomic-fail-asset",
                    "companyId": "acme",
                    "name": "ATOMIC-FAIL-01",
                    "type": "Server",
                    "status": "Active",
                    "source": "ncentral",
                    "externalId": external_id,
                    "fields": {},
                    "metadata": {},
                },
                provider_parent_id="101",
            )
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM configuration_items WHERE attributes->>'externalId' = %s",
                (external_id,),
            )
            self.assertEqual(int(cursor.fetchone()[0]), 0)
            cursor.execute(
                "SELECT count(*) FROM external_object_mappings WHERE external_id = %s",
                (external_id,),
            )
            self.assertEqual(int(cursor.fetchone()[0]), 0)

    def test_atomic_provider_import_locks_review_generation_and_customer_scope(self) -> None:
        """Reviewed writes reject queue, generation, remap and stale-update races."""

        external_id = "atomic-reviewed-device"
        review_id = "00000000-0000-0000-0000-000000000811"
        content_hash = "a" * 64
        connection = self.repository.get_integration_connection("ncentral")
        assert connection is not None
        with self.connection() as database, database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO integration_ci_review_items (
                    id, policy_id, company_id, external_id, external_name,
                    decision, provider_record, evidence, content_hash, state
                )
                SELECT %s::uuid, %s::uuid, company.id, %s, %s,
                       'create', '{}'::jsonb, '{}'::jsonb, %s, 'pending'
                FROM companies company WHERE company.slug = 'acme'
                """,
                (review_id, self.policy["id"], external_id, "ATOMIC-REVIEWED-01", content_hash),
            )

        payload = {
            "id": "atomic-reviewed-asset",
            "companyId": "acme",
            "name": "ATOMIC-REVIEWED-01",
            "type": "Server",
            "status": "Active",
            "source": "ncentral",
            "externalId": external_id,
            "fields": {},
            "metadata": {},
        }
        record = {
            "externalId": external_id,
            "name": "ATOMIC-REVIEWED-01",
            "providerParentId": "101",
        }
        with self.assertRaisesRegex(ValueError, "generation is stale"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                record,
                "create",
                asset=payload,
                provider_parent_id="101",
                policy_id=self.policy["id"],
                review_item_id=review_id,
                review_content_hash=content_hash,
                expected_policy_revision=self.policy["revision"] - 1,
                expected_connection_revision=connection["revision"],
            )
        with self.assertRaisesRegex(ValueError, "queue item changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                record,
                "create",
                asset=payload,
                provider_parent_id="101",
                policy_id=self.policy["id"],
                review_item_id=review_id,
                review_content_hash="b" * 64,
                expected_policy_revision=self.policy["revision"],
                expected_connection_revision=connection["revision"],
            )
        applied = self.repository.apply_reviewed_provider_ci_import(
            "ncentral",
            "acme",
            record,
            "create",
            asset=payload,
            provider_parent_id="101",
            policy_id=self.policy["id"],
            review_item_id=review_id,
            review_content_hash=content_hash,
            expected_policy_revision=self.policy["revision"],
            expected_connection_revision=connection["revision"],
        )
        self.assertEqual(applied["reviewItemsResolved"], 1)
        target = self.repository.create_asset(
            {
                "id": "atomic-other-asset",
                "companyId": "acme",
                "name": "ATOMIC-OTHER-01",
                "type": "Server",
                "status": "Active",
                "fields": {},
                "metadata": {},
            }
        )
        with self.assertRaisesRegex(ValueError, "update target no longer matches"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                record,
                "update",
                asset_id=target["id"],
                changes={"name": "SHOULD-NOT-CHANGE"},
                provider_parent_id="101",
            )
        self.assertEqual(self.repository.get_asset(target["id"])["name"], "ATOMIC-OTHER-01")

        self.assertTrue(self.repository.unmap_provider_company("ncentral", "101"))
        with self.assertRaisesRegex(ValueError, "active provider customer mapping changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                {"externalId": "scope-race-create", "providerParentId": "101"},
                "create",
                asset={**payload, "id": "scope-race-asset", "externalId": "scope-race-create"},
                provider_parent_id="101",
            )
        with self.assertRaisesRegex(ValueError, "active provider customer mapping changed"):
            self.repository.apply_reviewed_provider_ci_import(
                "ncentral",
                "acme",
                record,
                "link",
                asset_id=target["id"],
                provider_parent_id="101",
            )
        self.assertEqual(applied["mapping"]["assetId"], applied["asset"]["id"])

    def test_concurrent_atomic_provider_create_has_one_ci_and_one_mapping(self) -> None:
        """Identity advisory locking prevents a duplicate-CI orphan race."""

        external_id = "atomic-race-device"
        second_repository = PostgresCmdbRepository(
            deepcopy(self.repository.state),
            lambda _state: None,
            self.connection,
        )
        barrier = threading.Barrier(2)

        def create(repository: PostgresCmdbRepository, suffix: str) -> dict:
            barrier.wait(timeout=10)
            try:
                return repository.apply_reviewed_provider_ci_import(
                    "ncentral",
                    "acme",
                    {
                        "externalId": external_id,
                        "name": "ATOMIC-RACE-01",
                        "providerParentId": "101",
                    },
                    "create",
                    asset={
                        "id": f"atomic-race-asset-{suffix}",
                        "companyId": "acme",
                        "name": "ATOMIC-RACE-01",
                        "type": "Server",
                        "status": "Active",
                        "source": "ncentral",
                        "externalId": external_id,
                        "fields": {},
                        "metadata": {},
                    },
                    provider_parent_id="101",
                )
            except ValueError as error:
                return {"error": str(error)}

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda pair: create(*pair),
                    [(self.repository, "a"), (second_repository, "b")],
                )
            )
        self.assertEqual(sum("mapping" in item for item in results), 1)
        self.assertEqual(sum("already mapped" in item.get("error", "") for item in results), 1)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM configuration_items WHERE attributes->>'externalId' = %s",
                (external_id,),
            )
            self.assertEqual(int(cursor.fetchone()[0]), 1)
            cursor.execute(
                """
                SELECT count(*), count(DISTINCT canonical_entity_id)
                FROM external_object_mappings
                WHERE external_object_type = 'configuration' AND external_id = %s
                """,
                (external_id,),
            )
            mapping_count, canonical_count = cursor.fetchone()
            self.assertEqual((int(mapping_count), int(canonical_count)), (1, 1))

    def test_direct_preview_queue_failure_rolls_back_every_side_effect(self) -> None:
        """A queue write error cannot leave terminal history or release the lease."""

        self.repository.claim_ci_sync_policy_now(
            self.policy["id"],
            "worker-a",
            lease_seconds=120,
        )
        run_id = "00000000-0000-0000-0000-000000000954"
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE FUNCTION reject_direct_review() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION 'forced direct queue failure';
                END;
                $$
                """
            )
            cursor.execute(
                """
                CREATE TRIGGER reject_direct_review
                BEFORE INSERT ON integration_ci_review_items
                FOR EACH ROW EXECUTE FUNCTION reject_direct_review()
                """
            )
            cursor.execute(
                """
                SELECT count(*)
                FROM audit_events
                WHERE action = 'review_queue_refreshed'
                """
            )
            queue_audits_before = int(cursor.fetchone()[0])

        with self.assertRaisesRegex(Exception, "forced direct queue failure"):
            self.repository.publish_and_complete_ci_policy_preview(
                "ncentral",
                self.policy["id"],
                "worker-a",
                _direct_run(self.policy["id"], run_id),
                [_review_item("must-roll-back")],
                None,
            )

        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM sync_runs WHERE id = %s::uuid", (run_id,))
            self.assertEqual(int(cursor.fetchone()[0]), 0)
            cursor.execute("SELECT count(*) FROM integration_ci_review_items")
            self.assertEqual(int(cursor.fetchone()[0]), 0)
            cursor.execute(
                """
                SELECT count(*)
                FROM audit_events
                WHERE action = 'review_queue_refreshed'
                """
            )
            self.assertEqual(int(cursor.fetchone()[0]), queue_audits_before)
        self.assertEqual(self._policy_lease()[0], "worker-a")

    def test_policy_revision_compare_and_swap_allows_one_concurrent_editor(self) -> None:
        """Database-side revision CAS rejects one of two simultaneous stale edits."""

        stale_policy = deepcopy(self.policy)
        barrier = threading.Barrier(2)

        def update(mode: str) -> tuple[str, str]:
            barrier.wait(timeout=5)
            try:
                self.repository.update_ci_sync_policy(
                    "ncentral",
                    "acme",
                    "101",
                    _policy_input(enrichment_mode=mode),
                    expected_revision=stale_policy["revision"],
                    actor_id=None,
                )
            except ValueError as error:
                return mode, str(error)
            return mode, "saved"

        with (
            patch.object(
                self.repository,
                "get_ci_sync_policy",
                return_value=stale_policy,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            outcomes = list(executor.map(update, ("fast", "full")))

        self.assertEqual(sum(result == "saved" for _mode, result in outcomes), 1)
        self.assertEqual(
            sum("reload before saving" in result for _mode, result in outcomes),
            1,
        )
        self.assertEqual(self._policy_lease()[2], stale_policy["revision"] + 1)


if __name__ == "__main__":
    unittest.main()
