"""Explicitly merge an original JSON development store into canonical PostgreSQL."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.cmdb.migrations import apply_migrations
from src.cmdb.repository import PostgresCmdbRepository


def main() -> None:
    """Merge the configured JSON development store into canonical PostgreSQL."""

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required.")
    source = Path(os.environ.get("DATA_DIR", Path(__file__).parents[1] / "data")) / "cmdb.json"
    if not source.exists():
        raise SystemExit(f"JSON state not found: {source}")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit(
            "Install dependencies first: python -m pip install -r requirements.txt"
        ) from error
    state = json.loads(source.read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]

    def connection_factory():
        """Open a short-lived connection to the configured PostgreSQL database."""
        return psycopg.connect(database_url, connect_timeout=8)

    apply_migrations(connection_factory, root)
    repository = PostgresCmdbRepository(state, lambda _value: None, connection_factory)
    result = repository.import_state(state)
    print(
        f"Merged {result['companies']} customers, {result['assets']} assets and "
        f"{result['relationships']} relationships into canonical PostgreSQL."
    )


if __name__ == "__main__":
    main()
