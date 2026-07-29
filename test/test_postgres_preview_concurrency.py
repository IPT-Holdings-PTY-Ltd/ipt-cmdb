"""Real PostgreSQL race tests for durable integration preview publication."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
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
        completed = self.repository.complete_ci_sync_policy_run(
            self.policy["id"],
            lease_owner="worker-b",
            success=True,
        )
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
