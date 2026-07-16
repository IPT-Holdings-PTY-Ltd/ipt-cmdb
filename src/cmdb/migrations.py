"""Ordered, forward-only PostgreSQL migrations for CMDB Hub."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


BASELINE_VERSION = "2026.07.13.1"
MIGRATION_NAME = re.compile(r"^(?P<version>\d{4}\.\d{2}\.\d{2}\.\d+)__(?P<description>[a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    path: Path
    checksum: str


def _migration(version: str, description: str, path: Path) -> Migration:
    content = path.read_bytes()
    return Migration(version, description, path, hashlib.sha256(content).hexdigest())


def migration_plan(root: Path) -> list[Migration]:
    """Return the immutable baseline followed by ordered incremental migrations."""
    plan = [_migration(BASELINE_VERSION, "Canonical CMDB baseline", root / "db" / "schema.sql")]
    migrations_dir = root / "db" / "migrations"
    if migrations_dir.exists():
        for path in sorted(migrations_dir.glob("*.sql")):
            match = MIGRATION_NAME.match(path.name)
            if not match:
                raise RuntimeError(f"Invalid migration filename: {path.name}")
            plan.append(
                _migration(
                    match.group("version"),
                    match.group("description").replace("_", " ").capitalize(),
                    path,
                )
            )
    versions = [item.version for item in plan]
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise RuntimeError("Migration versions must be unique and ordered")
    return plan


def apply_migrations(connection_factory: Callable[[], Any], root: Path) -> list[str]:
    """Apply missing migrations once and reject changed or newer histories."""
    plan = migration_plan(root)
    planned = {item.version: item for item in plan}
    applied_now: list[str] = []
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('cmdb_hub_schema'))")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                description text NOT NULL,
                checksum varchar(64),
                execution_ms integer,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cursor.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum varchar(64)")
        cursor.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS execution_ms integer")
        cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        applied = {version: checksum for version, checksum in cursor.fetchall()}
        unknown = sorted(set(applied) - set(planned))
        if unknown:
            raise RuntimeError(
                "Database schema is newer than this application: " + ", ".join(unknown)
            )
        applied_versions = [item.version for item in plan if item.version in applied]
        if applied_versions != [item.version for item in plan[: len(applied_versions)]]:
            raise RuntimeError("Database migration history is not a valid forward-only prefix")

        for item in plan:
            existing_checksum = applied.get(item.version)
            if item.version in applied:
                if existing_checksum and existing_checksum != item.checksum:
                    raise RuntimeError(f"Applied migration {item.version} has changed on disk")
                if not existing_checksum:
                    cursor.execute(
                        "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                        (item.checksum, item.version),
                    )
                continue
            started = time.monotonic()
            cursor.execute(item.path.read_text(encoding="utf-8"))
            execution_ms = max(0, round((time.monotonic() - started) * 1000))
            cursor.execute(
                """
                INSERT INTO schema_migrations (version, description, checksum, execution_ms)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (version) DO UPDATE SET
                    description = EXCLUDED.description,
                    checksum = EXCLUDED.checksum,
                    execution_ms = EXCLUDED.execution_ms
                """,
                (item.version, item.description, item.checksum, execution_ms),
            )
            applied_now.append(item.version)
    return applied_now


def latest_schema_version(root: Path) -> str:
    return migration_plan(root)[-1].version
