import unittest
from pathlib import Path

from src.cmdb.migrations import apply_migrations, latest_schema_version, migration_plan

ROOT = Path(__file__).parents[1]


class FakeCursor:
    def __init__(self, history: dict[str, str | None]):
        self.history = history
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith("select version, checksum from schema_migrations"):
            self.rows = sorted(self.history.items())
        elif normalized.startswith("update schema_migrations set checksum"):
            checksum, version = params
            self.history[version] = checksum
        elif normalized.startswith("insert into schema_migrations (version, description, checksum"):
            version, _description, checksum, _execution_ms = params
            self.history[version] = checksum

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, history):
        self.history = history

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return FakeCursor(self.history)


class MigrationTests(unittest.TestCase):
    def test_plan_is_ordered_and_latest_version_is_incremental_migration(self):
        plan = migration_plan(ROOT)
        self.assertEqual(
            [item.version for item in plan],
            [
                "2026.07.13.1",
                "2026.07.15.1",
                "2026.07.15.2",
                "2026.07.15.3",
                "2026.07.16.1",
                "2026.07.16.2",
                "2026.07.17.1",
                "2026.07.17.2",
            ],
        )
        self.assertEqual(latest_schema_version(ROOT), "2026.07.17.2")
        self.assertTrue(all(len(item.checksum) == 64 for item in plan))

    def test_migrations_apply_once_and_reject_checksum_drift(self):
        history = {}

        def factory():
            return FakeConnection(history)

        self.assertEqual(
            apply_migrations(factory, ROOT),
            [
                "2026.07.13.1",
                "2026.07.15.1",
                "2026.07.15.2",
                "2026.07.15.3",
                "2026.07.16.1",
                "2026.07.16.2",
                "2026.07.17.1",
                "2026.07.17.2",
            ],
        )
        self.assertEqual(apply_migrations(factory, ROOT), [])
        history["2026.07.15.1"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "changed on disk"):
            apply_migrations(factory, ROOT)

    def test_non_prefix_history_is_rejected(self):
        history = {"2026.07.15.1": migration_plan(ROOT)[1].checksum}
        with self.assertRaisesRegex(RuntimeError, "forward-only prefix"):
            apply_migrations(lambda: FakeConnection(history), ROOT)


if __name__ == "__main__":
    unittest.main()
