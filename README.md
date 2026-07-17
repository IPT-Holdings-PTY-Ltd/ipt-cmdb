# IPT CMDB

[![CI](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/ci.yml/badge.svg)](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/ci.yml)
[![Container](https://img.shields.io/badge/container-ghcr.io-2496ed?logo=docker&logoColor=white)](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/pkgs/container/ipt-cmdb)
[![License](https://img.shields.io/badge/license-MIT-50d5b9.svg)](LICENSE)

An MSP-oriented configuration management platform for turning technical inventory into customer, service and change-impact intelligence.

IPT CMDB combines a tenant-aware asset inventory, business-system modelling, interactive dependency maps, ITIL-aligned ownership and lifecycle data, and impact-aware change packages. It is designed to consolidate metadata from ConnectWise Manage, N-central and Passportal without becoming a password vault.

> [!IMPORTANT]
> This repository is a pre-release platform under active development. The local demo is suitable for development and controlled evaluation. Review the [security guidance](SECURITY.md) before any internet-facing deployment.

## What works today

| Area | Current capability |
|---|---|
| Multi-tenancy | MSP/root workspace, isolated customer workspaces and server-enforced company scope |
| Access | Governed user lifecycle, platform/MSP/customer roles, customer groups, effective-access views and restricted personal API tokens |
| Authentication | Local development sessions or Microsoft Entra ID through Azure Easy Auth-compatible headers; session revocation on sensitive identity changes |
| Assets | ITIL-aligned lifecycle, owners, criticality, environment, site, renewal, EOL and integration identity metadata |
| Business systems | Business-facing services with owners, RTO/RPO, sign-off context and supporting CI stacks |
| Relationships | Drag-and-drop React Flow maps with full-stack, network, storage, virtualization and business-impact perspectives |
| Impact analysis | Upstream/downstream traversal, shared dependencies, HA evidence and protected/degraded/outage decisions |
| Change control | Guided change form, frozen impact snapshot, suggested risk and branded PDF generation |
| Dashboards | MSP customer-risk queue and customer business-system/action dashboards |
| PostgreSQL | Canonical repository, forward-only checksum migrations, blank-database bootstrap and upgrade verification |
| Recovery | Portable checksum-protected export/import plus operational guidance for PostgreSQL PITR or `pg_dump` |
| Branding | MSP identity, logo, colours and report footer; tenant-scoped customer branding storage |
| Governance | Append-only attributable audit ledger, request correlation, tenant-aware Audit Center and CI activity timelines |
| Data quality | MSP/customer quality scores, prioritized findings, audited exceptions, reconciliation review and per-customer field authority |
| Reports | Controlled MSP/customer report catalogue with branded PDF, filterable XLSX and UTF-8 CSV exports |
| Integrations | Root control plane and connectivity boundary; production ingestion/mapping is the next delivery increment |

Passportal passwords, secure notes and credential values are explicitly out of scope. Only approved metadata associations should enter the CMDB.

## Quick start with Docker

Prerequisites: Docker Desktop or Docker Engine with Compose v2.

```powershell
git clone https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb.git
cd ipt-cmdb
docker compose up --build -d
```

Open <http://localhost:3000>. The Compose stack starts the application and PostgreSQL, applies database migrations and seeds the demo workspace.

Check readiness:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:3000/api/health
```

The development identities all use the password `ChangeMe!`:

| Login | Role | Scope |
|---|---|---|
| `admin@example.com` | Platform administrator | All customers and root settings |
| `msp@example.com` | MSP operator | Assigned managed customers |
| `client@acme.example` | Customer reader | Acme Manufacturing only |

These identities are demo data. Never expose them on a public deployment.

## Developer workflow

The production container serves a compiled React application from FastAPI. For hot-reload development, run PostgreSQL and FastAPI on port 3000, then Vite on port 5173:

```powershell
docker compose up -d postgres
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pre_commit install --hook-type pre-commit --hook-type pre-push
npm ci
$env:DATABASE_URL='postgresql://cmdb:cmdb@localhost:5432/cmdb'
$env:DATABASE_SEED_MODE='demo'
python -m uvicorn backend.main:app --reload --port 3000
```

In a second terminal:

```powershell
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` to FastAPI.

Before opening a pull request:

```powershell
python -m pre_commit run --all-files
python -m coverage run -m unittest discover -s test -v
python -m coverage report
npm test
npm run typecheck
npm run build
docker build --tag ipt-cmdb:local .
```

The pre-commit hook runs Ruff and mypy on each commit. The pre-push hook also runs
the Python suite with branch coverage and enforces the checked-in coverage floor.

## Architecture at a glance

```mermaid
flowchart LR
    SPA[React Admin SPA] -->|Authenticated HTTPS| API[FastAPI API]
    API --> PG[(PostgreSQL)]
    API --> PDF[Change PDF generator]
    ENTRA[Microsoft Entra ID / Easy Auth] --> API
    CW[ConnectWise Manage] --> WORKERS[Read-only sync workers]
    NC[N-central] --> WORKERS
    PP[Passportal metadata] --> WORKERS
    WORKERS --> MAP[Mapping and reconciliation]
    MAP --> PG
```

Canonical CI UUIDs remain stable when provider names change. Provider IDs are stored as mappings and observations; ambiguous matches must enter a review workflow rather than silently creating or overwriting records.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the runtime, tenancy, identity and reconciliation design.

## Documentation

- [Local development](docs/LOCAL_DEVELOPMENT.md)
- [Architecture](ARCHITECTURE.md)
- [Data model and relationship semantics](docs/DATA_MODEL.md)
- [Governance, audit and reporting](docs/GOVERNANCE.md)
- [Data quality and reconciliation](docs/DATA_QUALITY.md)
- [Integration design and provider boundaries](docs/INTEGRATIONS.md)
- [Azure/container deployment](docs/DEPLOYMENT.md)
- [Operations, upgrades and recovery](docs/OPERATIONS.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Changelog](CHANGELOG.md)

Interactive API documentation is available at `/docs` while the API is running.

## Near-term roadmap

1. Read-only ConnectWise company and configuration ingestion using the reconciliation and authority workbench.
2. Bulk ownership, relationship-layer and lifecycle correction actions from data-quality findings.
3. Scheduled worker execution, retry, locking and richer integration diagnostics.
4. N-central device/customer ingestion and Passportal metadata association.
5. Explicitly approved ConnectWise change-ticket publishing with PDF attachment and idempotency.

External provider writes remain disabled until a reviewable, auditable workflow is implemented.

## License

Licensed under the [MIT License](LICENSE).
