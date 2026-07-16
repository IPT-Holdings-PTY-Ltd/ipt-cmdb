# Contributing

Thank you for helping improve IPT CMDB. Contributions should preserve tenant isolation, stable canonical identity and review-first external integration behaviour.

## Before starting

- Search existing issues and pull requests.
- Use a focused branch from `main`.
- Discuss broad schema, authorization or provider-write changes before implementation.
- Never use real customer exports, credentials or screenshots as test fixtures.

## Development setup

Follow [docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md). The normal validation suite is:

```powershell
python -m unittest discover -s test -v
npm test
npm run typecheck
npm run build
docker build --tag ipt-cmdb:local .
```

Schema changes must also pass:

```powershell
python scripts/verify_blank_postgres.py --admin-url postgresql://postgres:postgres@localhost:5432/postgres
python scripts/verify_postgres_upgrade.py --admin-url postgresql://postgres:postgres@localhost:5432/postgres
```

## Design requirements

- Enforce company scope in the API, not only in the UI.
- Keep canonical UUIDs stable; provider IDs are mappings.
- Treat names as mutable evidence, not identity.
- Make synchronization idempotent and auditable.
- Put ambiguous identity matches into review.
- Keep provider writes disabled unless the workflow has explicit review and confirmation.
- Never expose database strings or integration credentials to the browser.
- Never ingest Passportal passwords, secure notes or credential values.
- Keep applied migrations immutable; add a new forward migration.

## Code areas

| Path | Responsibility |
|---|---|
| `frontend/src` | React Admin application, workspace UI and topology |
| `backend/main.py` | FastAPI routes, authentication and API authorization |
| `app.py` | Domain helpers and first-start/local support |
| `src/cmdb` | Repository, migrations, reconciliation and change reports |
| `db` | Baseline schema and ordered incremental migrations |
| `scripts` | Database verification and state import utilities |
| `test` | Python and Node test suites |

## Pull requests

Describe:

- the user problem and visible behaviour;
- tenant/security implications;
- database migration impact;
- integration read/write behaviour;
- tests and manual verification performed;
- screenshots for material UI changes using demo data only.

Keep commits reviewable and avoid unrelated formatting or generated artifacts. Do not commit `frontend/dist`, `node_modules`, local data, exports or `.env` files.

## Reporting security issues

Do not open a public issue for suspected vulnerabilities or exposed data. Follow [SECURITY.md](SECURITY.md).
