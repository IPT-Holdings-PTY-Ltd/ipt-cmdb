"""Apply the CMDB PostgreSQL schema using DATABASE_URL (never stores credentials)."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    """Apply all pending migrations to the configured PostgreSQL database."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "DATABASE_URL is required, e.g. postgresql://cmdb:cmdb@localhost:5432/cmdb"
        )
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit(
            "Install dependencies first: python -m pip install -r requirements.txt"
        ) from error
    schema = (Path(__file__).parents[1] / "db" / "schema.sql").read_text(encoding="utf-8")
    with (
        psycopg.connect(database_url, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(schema)
    print("CMDB PostgreSQL schema applied.")


if __name__ == "__main__":
    main()
