# Local development

## Supported toolchain

- Python 3.12
- Node.js 22
- npm with the committed `package-lock.json`
- PostgreSQL 16
- Docker Compose v2 for the simplest setup

Windows PowerShell examples are used below; equivalent shell commands work on Linux and macOS.

## Docker workflow

Start the complete application:

```powershell
docker compose up --build -d
docker compose ps
Invoke-RestMethod http://localhost:3000/api/health
```

Follow logs:

```powershell
docker compose logs -f cmdb
```

Stop containers without removing the database volume:

```powershell
docker compose down
```

The Compose stack sets `DATABASE_SEED_MODE=demo`. PostgreSQL data remains in the named `cmdb-postgres` volume between restarts.

## Hot-reload workflow

Start only PostgreSQL:

```powershell
docker compose up -d postgres
```

Prepare the backend:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pre_commit install --hook-type pre-commit --hook-type pre-push
$env:DATABASE_URL='postgresql://cmdb:cmdb@localhost:5432/cmdb'
$env:DATABASE_SEED_MODE='demo'
$env:AUTH_MODE='local'
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 3000
```

In another terminal:

```powershell
npm ci
npm run dev
```

Open <http://localhost:5173>. The Vite server proxies `/api` to port 3000.

## Test suite

```powershell
python -m pre_commit run --all-files
python -m coverage run -m unittest discover -s test -v
python -m coverage report
npm test
npm run typecheck
npm run build
docker build --tag ipt-cmdb:local .
```

Database bootstrap and upgrade checks need a reachable PostgreSQL administrator connection:

```powershell
$env:TEST_POSTGRES_ADMIN_URL='postgresql://cmdb:cmdb@localhost:5432/postgres'
python -m unittest discover -s test -p "test_postgres_repository.py" -v
python scripts/verify_blank_postgres.py --admin-url postgresql://postgres:postgres@localhost:5432/postgres
python scripts/verify_postgres_upgrade.py --admin-url postgresql://postgres:postgres@localhost:5432/postgres
```

The repository contract test and both verifiers create isolated temporary databases and remove
them after each check. The ordinary local suite skips the PostgreSQL contract test when
`TEST_POSTGRES_ADMIN_URL` is not set; GitHub Actions always enables it.

The repository's `.python-version` keeps compatible version managers on Python 3.12,
matching the production image and GitHub Actions. Security tools are intentionally
isolated in `requirements-security.txt`; CI blocks medium/high confidence Bandit
findings while dependency advisories are initially reported without blocking merges.

## Database modes

| Configuration | Behaviour |
|---|---|
| `DATABASE_URL` set | Canonical PostgreSQL mode; migrations run at startup |
| No URL, default settings | Database setup-only mode; operational APIs fail closed |
| `ALLOW_LOCAL_DEVELOPMENT=true` | Explicit lightweight development repository; never use in production |

`DATABASE_SEED_MODE` accepts `empty`, `demo` or `current` for a previously uninitialised PostgreSQL database.

## API exploration

- OpenAPI UI: <http://localhost:3000/docs>
- OpenAPI document: <http://localhost:3000/openapi.json>
- Health: <http://localhost:3000/api/health>

Use the browser application for authentication before testing protected routes, or obtain a local bearer token through `/api/login` in development.

## Common problems

### The container starts before PostgreSQL is ready

Compose includes a PostgreSQL health check and makes the application depend on it. Confirm both services with `docker compose ps` and inspect `docker compose logs postgres` when readiness continues to fail.

### The API reports pending or incompatible migrations

Do not edit an applied migration. Add a new ordered file under `db/migrations`, update tests, and rerun both PostgreSQL verifiers.

### The frontend loads but API calls fail

For hot reload, confirm FastAPI is listening on port 3000 and Vite is running on port 5173. For Docker, use port 3000 only.

### Login redirects unexpectedly

Use `AUTH_MODE=local` for local development. `easy_auth` expects a trusted gateway to inject the configured identity headers.
