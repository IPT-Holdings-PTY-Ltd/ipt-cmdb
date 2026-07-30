"""Apply the canonical checksum-protected CMDB PostgreSQL migration plan."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cmdb.migrations import (  # noqa: E402 - repository root is added above
    apply_migrations,
    latest_schema_version,
)


def configured_database_url() -> str:
    """Load the database URL from the same environment or secret-file contract as runtime."""

    direct_value = os.getenv("DATABASE_URL", "").strip()
    if direct_value:
        return direct_value
    secret_file = os.getenv("DATABASE_URL_FILE", "").strip()
    if not secret_file:
        raise RuntimeError("DATABASE_URL or DATABASE_URL_FILE is required")
    try:
        file_value = Path(secret_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("The configured database URL secret file cannot be read") from error
    if not file_value:
        raise RuntimeError("The configured database URL secret file is empty")
    return file_value


def migrate(database_url: str) -> list[str]:
    """Apply every missing immutable migration in one advisory-locked transaction."""

    return apply_migrations(
        lambda: psycopg.connect(database_url, connect_timeout=8),
        ROOT,
    )


def main() -> None:
    """Apply all pending migrations without printing connection credentials."""

    try:
        applied = migrate(configured_database_url())
    except RuntimeError as error:
        raise SystemExit(str(error)) from error
    schema_version = latest_schema_version(ROOT)
    if applied:
        print(f"Applied {len(applied)} CMDB migration(s); schema is now {schema_version}.")
    else:
        print(f"No pending CMDB migrations; schema is {schema_version}.")


if __name__ == "__main__":
    main()
