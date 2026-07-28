"""Prove an existing baseline database upgrades without replaying JSON state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cmdb.migrations import (  # noqa: E402 - repository root is added above
    apply_migrations,
    latest_schema_version,
    migration_plan,
)
from src.cmdb.repository import (  # noqa: E402 - repository root is added above
    PostgresCmdbRepository,
)


def database_url(base_url: str, name: str) -> str:
    """Return the admin URL with its database path replaced by ``name``."""

    return urlunparse(urlparse(base_url)._replace(path=f"/{name}"))


def main() -> None:
    """Create a baseline database and verify its forward-only upgrade path."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", default="postgresql://cmdb:cmdb@localhost:5432/postgres")
    parser.add_argument("--database", default="cmdb_upgrade_verify")
    args = parser.parse_args()
    if not args.database.startswith("cmdb_upgrade_verify"):
        raise SystemExit("Verification database must start with cmdb_upgrade_verify")

    with (
        psycopg.connect(args.admin_url, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (args.database,),
        )
        cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(args.database)))
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(args.database)))

    target_url = database_url(args.admin_url, args.database)

    def factory():
        """Open a connection to the temporary upgrade-verification database."""
        return psycopg.connect(target_url, connect_timeout=8)

    legacy_state = {
        "companies": [{"id": "acme", "name": "Acme Manufacturing", "externalIds": {}}],
        "users": [],
        "assets": [],
        "relationships": [],
        "changes": [],
        "integrations": [],
        "syncRuns": [],
        "accessGroups": [],
        "branding": {
            "acme": {
                "name": "Acme Service Portal",
                "logoText": "AC",
                "accent": "#123456",
            }
        },
    }
    try:
        with factory() as connection, connection.cursor() as cursor:
            cursor.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
            cursor.execute(
                "INSERT INTO companies (slug, name) VALUES ('acme', 'Acme Manufacturing')"
            )
            cursor.execute(
                "INSERT INTO users (email, display_name) VALUES ('admin@example.com', 'Admin') RETURNING id"
            )
            user_id = str(cursor.fetchone()[0])
            cursor.execute(
                "INSERT INTO user_platform_roles (user_id, role) VALUES (%s::uuid, 'platform_admin')",
                (user_id,),
            )
            cursor.execute(
                "INSERT INTO application_state (state_key, state) VALUES ('cmdb_api', %s::jsonb)",
                (json.dumps(legacy_state),),
            )

        applied = apply_migrations(factory, ROOT)
        expected_incremental_versions = [item.version for item in migration_plan(ROOT)[1:]]
        assert applied == expected_incremental_versions, applied
        repository = PostgresCmdbRepository(legacy_state, lambda _state: None, factory)
        assert repository.migrate_legacy_company_branding() == 1
        assert repository.get_company_branding("acme")["name"] == "Acme Service Portal"
        assert repository.get_company_branding("acme")["accent"] == "#123456"
        email_connection = repository.update_email_connection(
            {
                "enabled": False,
                "authMode": "managed_identity",
                "senderAddress": "cmdb@example.com",
                "status": "configured",
            },
            user_id,
        )
        assert email_connection["senderAddress"] == "cmdb@example.com"
        queued_email = repository.create_email_outbox(
            {
                "idempotencyKey": "upgrade-verification-email",
                "to": ["tech@example.com"],
                "subject": "Upgrade verification",
                "bodyText": "PostgreSQL email outbox is operational.",
            },
            user_id,
        )
        duplicate_email = repository.create_email_outbox(
            {
                "idempotencyKey": "upgrade-verification-email",
                "to": ["tech@example.com"],
                "subject": "Upgrade verification",
            },
            user_id,
        )
        assert duplicate_email["id"] == queued_email["id"]
        accepted_email = repository.update_email_outbox(
            queued_email["id"],
            {"status": "accepted", "attempts": 1, "acceptedAt": "2026-07-20T12:00:00Z"},
            user_id,
        )
        assert accepted_email and accepted_email["status"] == "accepted"
        assert "bodyText" not in repository.list_email_outbox()[0]
        repository.record_worker_runtime(
            "integrations",
            "upgrade-worker",
            "dedicated",
            60,
            "starting",
        )
        worker_runtime = repository.record_worker_runtime(
            "integrations",
            "upgrade-worker",
            "dedicated",
            60,
            "cycle_succeeded",
            processed=2,
            metadata={"result": {"processed": 2}},
        )
        assert worker_runtime["status"] == "running"
        assert worker_runtime["itemsProcessed"] == 2
        rate_limit = repository.record_provider_rate_limit(
            "connectwise",
            {
                "httpStatus": 200,
                "limit": 1200,
                "remaining": 1199,
                "requestPath": "/company/companies",
            },
        )
        assert rate_limit["remaining"] == 1199
        with factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM companies WHERE slug = 'acme'")
            company_id = str(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO configuration_items (
                    id, company_id, ci_type, display_name, normalized_name
                ) VALUES
                    ('00000000-0000-4000-8000-000000000101', %s::uuid, 'server', 'Reconnect source', 'reconnect source'),
                    ('00000000-0000-4000-8000-000000000102', %s::uuid, 'service', 'Reconnect target', 'reconnect target')
                """,
                (company_id, company_id),
            )
        first_relationship = repository.create_relationship(
            {
                "id": "00000000-0000-4000-8000-000000000201",
                "fromId": "00000000-0000-4000-8000-000000000101",
                "toId": "00000000-0000-4000-8000-000000000102",
                "type": "used_by",
                "impactPolicy": "degraded",
            },
            "acme",
            "upgrade-verifier",
        )
        assert repository.delete_relationship(first_relationship["id"], "acme", "upgrade-verifier")
        reactivated_relationship = repository.create_relationship(
            {
                "id": "00000000-0000-4000-8000-000000000202",
                "fromId": "00000000-0000-4000-8000-000000000101",
                "toId": "00000000-0000-4000-8000-000000000102",
                "type": "used_by",
            },
            "acme",
            "upgrade-verifier",
        )
        assert reactivated_relationship["id"] == first_relationship["id"]
        assert reactivated_relationship["impactPolicy"] == "required"
        with factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM legacy_application_state WHERE state_key = 'cmdb_api'"
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*) FROM ci_relationships
                WHERE from_ci_id = '00000000-0000-4000-8000-000000000101'::uuid
                  AND to_ci_id = '00000000-0000-4000-8000-000000000102'::uuid
                  AND relationship_type = 'used_by' AND retired_at IS NULL
                """
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                "SELECT impact_policy FROM ci_relationships WHERE id = %s::uuid",
                (reactivated_relationship["id"],),
            )
            assert cursor.fetchone()[0] == "required"
            cursor.execute(
                """
                SELECT action FROM audit_events
                WHERE entity_type = 'relationship'
                  AND entity_id = %s::uuid
                ORDER BY created_at DESC LIMIT 1
                """,
                (reactivated_relationship["id"],),
            )
            assert cursor.fetchone()[0] == "reactivated"
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            versions = [row[0] for row in cursor.fetchall()]
        assert versions[-1] == latest_schema_version(ROOT)
        assert apply_migrations(factory, ROOT) == []
        print(
            f"Upgrade verified: versions={','.join(versions)} "
            "customer_branding=preserved legacy_state=retired "
            "relationship_reconnect=verified email_outbox=verified "
            "worker_telemetry=verified"
        )
    finally:
        with (
            psycopg.connect(args.admin_url, autocommit=True) as connection,
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
