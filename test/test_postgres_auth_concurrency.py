"""Real PostgreSQL concurrency tests for local-password abuse protection."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

from src.cmdb.migrations import apply_migrations
from src.cmdb.repository import PostgresCmdbRepository

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv("TEST_POSTGRES_ADMIN_URL", "")
DATABASE_NAME = "cmdb_auth_concurrency_test"


def database_url(base_url: str, name: str) -> str:
    """Replace the database component of one PostgreSQL URL."""

    return urlunparse(urlparse(base_url)._replace(path=f"/{name}"))


@unittest.skipUnless(ADMIN_URL, "TEST_POSTGRES_ADMIN_URL is not configured")
class PostgresAuthenticationConcurrencyTests(unittest.TestCase):
    """Prove login limits cannot be overshot by separate API replicas."""

    @classmethod
    def setUpClass(cls):
        if not DATABASE_NAME.startswith("cmdb_auth_concurrency_test"):
            raise RuntimeError("Unsafe PostgreSQL authentication-test database name")
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
        cls.target_url = database_url(ADMIN_URL, DATABASE_NAME)

        def connection_factory():
            return psycopg.connect(cls.target_url)

        cls.connection_factory = staticmethod(connection_factory)
        apply_migrations(connection_factory, ROOT)

    @classmethod
    def tearDownClass(cls):
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

    def setUp(self):
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("TRUNCATE auth_login_challenges, auth_login_attempts")
            cursor.execute(
                """
                INSERT INTO users (id, email, display_name, status)
                VALUES (
                    'a63861ef-f3c0-4f18-bb6e-83e4267232cf',
                    'concurrency@example.com',
                    'Concurrency Test',
                    'active'
                )
                ON CONFLICT (id) DO NOTHING
                """
            )

    def repository(self) -> PostgresCmdbRepository:
        """Return one repository instance representing an independent replica."""

        return PostgresCmdbRepository({}, lambda _state: None, self.connection_factory)

    def test_identifier_reservation_does_not_overshoot_across_replicas(self):
        workers = 5
        barrier = threading.Barrier(workers)

        def reserve(index: int) -> dict:
            barrier.wait()
            return self.repository().reserve_local_login_attempt(
                "shared-identifier",
                f"source-{index}",
                identifier_limit=1,
                source_limit=100,
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(reserve, range(workers)))

        self.assertEqual(sum(bool(item["allowed"]) for item in results), 1)
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM auth_login_attempts WHERE outcome = 'pending'")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_source_reservation_does_not_overshoot_across_identifiers(self):
        workers = 5
        barrier = threading.Barrier(workers)

        def reserve(index: int) -> dict:
            barrier.wait()
            return self.repository().reserve_local_login_attempt(
                f"identifier-{index}",
                "shared-source",
                identifier_limit=100,
                source_limit=1,
            )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(reserve, range(workers)))

        self.assertEqual(sum(bool(item["allowed"]) for item in results), 1)

    def test_identifier_clear_preserves_source_failure_history(self):
        repository = self.repository()
        failed = repository.reserve_local_login_attempt(
            "identifier-a",
            "source-a",
            identifier_limit=1,
            source_limit=1,
        )
        self.assertTrue(
            repository.finish_local_login_attempt(
                failed["attemptId"],
                "password_failed",
            )
        )
        self.assertEqual(repository.clear_local_login_failures("identifier-a"), 1)

        identifier_released = repository.reserve_local_login_attempt(
            "identifier-a",
            "source-b",
            identifier_limit=1,
            source_limit=1,
        )
        source_preserved = repository.reserve_local_login_attempt(
            "identifier-b",
            "source-a",
            identifier_limit=100,
            source_limit=1,
        )
        self.assertTrue(identifier_released["allowed"])
        self.assertFalse(source_preserved["allowed"])

    def test_mfa_attempt_count_does_not_fall_back_to_zero_at_the_limit(self):
        workers = 5
        barrier = threading.Barrier(workers)
        token_hash = "a" * 64
        self.repository().create_login_challenge(
            {
                "tokenHash": token_hash,
                "userId": "a63861ef-f3c0-4f18-bb6e-83e4267232cf",
                "purpose": "verify",
                "maxAttempts": 2,
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        )

        def fail_challenge(_index: int) -> int:
            barrier.wait()
            return self.repository().record_login_challenge_attempt(token_hash)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(fail_challenge, range(workers)))

        self.assertEqual(sorted(results), [1, 2, 2, 2, 2])
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempts FROM auth_login_challenges WHERE token_hash = %s",
                (token_hash,),
            )
            self.assertEqual(cursor.fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
