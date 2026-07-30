import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import migrate_postgres


class CanonicalMigrationCommandTests(unittest.TestCase):
    def test_database_url_uses_direct_environment_value_first(self):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://direct.example/cmdb",
                "DATABASE_URL_FILE": "unused-secret-file",
            },
            clear=True,
        ):
            self.assertEqual(
                migrate_postgres.configured_database_url(),
                "postgresql://direct.example/cmdb",
            )

    def test_database_url_can_be_loaded_from_a_container_secret_file(self):
        secret_file = Path("container-database-url")
        with (
            patch.dict(
                os.environ,
                {"DATABASE_URL_FILE": str(secret_file)},
                clear=True,
            ),
            patch.object(
                Path,
                "read_text",
                return_value="postgresql://secret-file.example/cmdb\n",
            ) as read_text,
        ):
            self.assertEqual(
                migrate_postgres.configured_database_url(),
                "postgresql://secret-file.example/cmdb",
            )

        read_text.assert_called_once_with(encoding="utf-8")

    def test_migrate_delegates_to_checksum_protected_plan(self):
        connection = object()

        def canonical_apply(connection_factory, root):
            self.assertEqual(root, migrate_postgres.ROOT)
            self.assertIs(connection_factory(), connection)
            return ["2026.07.29.2"]

        with (
            patch.object(migrate_postgres.psycopg, "connect", return_value=connection) as connect,
            patch.object(
                migrate_postgres,
                "apply_migrations",
                side_effect=canonical_apply,
            ),
        ):
            applied = migrate_postgres.migrate("postgresql://db.example/cmdb")

        self.assertEqual(applied, ["2026.07.29.2"])
        connect.assert_called_once_with("postgresql://db.example/cmdb", connect_timeout=8)

    def test_main_reports_schema_without_echoing_database_url(self):
        database_url = "postgresql://user:do-not-print@db.example/cmdb"
        output = StringIO()
        with (
            patch.dict(os.environ, {"DATABASE_URL": database_url}, clear=True),
            patch.object(migrate_postgres, "migrate", return_value=[]),
            redirect_stdout(output),
        ):
            migrate_postgres.main()

        self.assertIn("No pending CMDB migrations", output.getvalue())
        self.assertNotIn(database_url, output.getvalue())
        self.assertNotIn("do-not-print", output.getvalue())


if __name__ == "__main__":
    unittest.main()
