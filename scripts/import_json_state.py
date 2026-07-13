"""One-time local migration from the original JSON demo store into PostgreSQL."""
from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required.")
    source = Path(os.environ.get("DATA_DIR", Path(__file__).parents[1] / "data")) / "cmdb.json"
    if not source.exists():
        raise SystemExit(f"JSON state not found: {source}")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements.txt") from error
    state = json.loads(source.read_text(encoding="utf-8"))
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO application_state (state_key, state, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (state_key) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            ("cmdb_api", json.dumps(state)),
        )
    print(f"Imported {len(state.get('assets', []))} assets and {len(state.get('relationships', []))} relationships into PostgreSQL.")


if __name__ == "__main__":
    main()
