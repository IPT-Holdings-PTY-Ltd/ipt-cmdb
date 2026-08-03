"""Create an isolated local database and prove first-start bootstrap works.

The database name is constrained to the ``cmdb_bootstrap_verify`` prefix and is
always removed after the check. Use only against a disposable development
PostgreSQL server.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def database_url(base_url: str, name: str) -> str:
    """Return the admin URL with its database path replaced by ``name``."""

    parsed = urlparse(base_url)
    return urlunparse(parsed._replace(path=f"/{name}"))


def main() -> None:
    """Create a temporary blank database and verify first-start bootstrap."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--admin-url",
        default="postgresql://cmdb:cmdb@127.0.0.1:5432/postgres",
    )
    parser.add_argument("--database", default="cmdb_bootstrap_verify")
    args = parser.parse_args()
    if not args.database.startswith("cmdb_bootstrap_verify"):
        raise SystemExit("Verification database must start with cmdb_bootstrap_verify")

    with (
        psycopg.connect(
            args.admin_url,
            autocommit=True,
            connect_timeout=10,
        ) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (args.database,),
        )
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(args.database)))
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))

    target_url = database_url(args.admin_url, args.database)
    os.environ["DATABASE_URL"] = target_url
    os.environ["DATABASE_SEED_MODE"] = "empty"
    try:
        import app as core
        import backend.main as backend

        diagnostics = core.database_diagnostics()
        companies = backend.REPOSITORY.list_companies()
        users = backend.REPOSITORY.list_users()
        with (
            psycopg.connect(target_url, connect_timeout=10) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT to_regclass('integration_capability_snapshots')::text,
                       to_regclass('integration_enrichment_previews')::text
                """
            )
            cache_tables = cursor.fetchone()
        assert core.DATABASE_MODE == "PostgreSQL"
        assert backend.REPOSITORY.mode == "canonical_postgresql"
        assert diagnostics["schemaVersion"] == core.SCHEMA_VERSION
        assert diagnostics["initialized"] is True
        assert companies == []
        assert any(user["role"] == "platform_admin" for user in users)
        assert cache_tables == (
            "integration_capability_snapshots",
            "integration_enrichment_previews",
        )
        print(
            f"Blank bootstrap verified: schema={diagnostics['schemaVersion']} "
            f"repository={backend.REPOSITORY.mode} companies={len(companies)} "
            f"users={len(users)} provider_cache=ready"
        )
    finally:
        with (
            psycopg.connect(
                args.admin_url,
                autocommit=True,
                connect_timeout=10,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (args.database,),
            )
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(args.database))
            )


if __name__ == "__main__":
    main()
