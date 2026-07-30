import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as core


class FakeCursor:
    def __init__(self, history, migration_table="schema_migrations"):
        self.history = history
        self.migration_table = migration_table
        self.query = ""
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, parameters=None):
        self.query = " ".join(query.split()).lower()
        self.executions.append((self.query, parameters))

    def fetchone(self):
        if "to_regclass" in self.query:
            return (self.migration_table,)
        return (1,)

    def fetchall(self):
        return list(self.history)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class DatabaseReadinessTests(unittest.TestCase):
    def probe(self, history, *, migration_table="schema_migrations"):
        cursor = FakeCursor(history, migration_table=migration_table)
        connection = FakeConnection(cursor)
        captured = {}

        def connect(database_url=None, **kwargs):
            captured["database_url"] = database_url
            captured.update(kwargs)
            return connection

        plan = [
            SimpleNamespace(version="2026.01.01.1", checksum="checksum-1"),
            SimpleNamespace(version="2026.01.02.1", checksum="checksum-2"),
        ]
        with (
            patch.object(core, "postgres_connection", side_effect=connect),
            patch.object(core, "migration_plan", return_value=plan),
            patch.object(core, "SCHEMA_VERSION", "2026.01.02.1"),
            patch.dict(
                os.environ,
                {
                    "READINESS_CONNECT_TIMEOUT_SECONDS": "3",
                    "READINESS_STATEMENT_TIMEOUT_MS": "2000",
                },
            ),
        ):
            result = core.database_readiness("postgresql://redacted")
        return result, cursor, captured

    def test_exact_migration_history_is_ready(self):
        result, cursor, captured = self.probe(
            [
                ("2026.01.01.1", "checksum-1"),
                ("2026.01.02.1", "checksum-2"),
            ]
        )

        self.assertTrue(result["databaseAvailable"])
        self.assertTrue(result["schemaCurrent"])
        self.assertEqual(result["schemaVersion"], "2026.01.02.1")
        self.assertEqual(result["schemaHistorySha256"], core.SCHEMA_HISTORY_SHA256)
        self.assertEqual(captured["connect_timeout"], 3)
        self.assertIn(
            ("select set_config('statement_timeout', %s, true)", ("2000ms",)),
            cursor.executions,
        )
        self.assertTrue(any(query == "select 1" for query, _params in cursor.executions))

    def test_checksum_or_version_drift_is_not_ready(self):
        result, _cursor, _captured = self.probe(
            [
                ("2026.01.01.1", "checksum-1"),
                ("2026.01.02.1", "changed-checksum"),
            ]
        )

        self.assertTrue(result["databaseAvailable"])
        self.assertFalse(result["schemaCurrent"])

    def test_missing_migration_table_is_not_ready(self):
        result, _cursor, _captured = self.probe([], migration_table=None)

        self.assertTrue(result["databaseAvailable"])
        self.assertFalse(result["schemaCurrent"])
        self.assertIsNone(result["schemaVersion"])


if __name__ == "__main__":
    unittest.main()
