# IPT CMDB

[![CI](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/ci.yml/badge.svg)](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/ci.yml)
[![Release](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/release.yml/badge.svg)](https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb/actions/workflows/release.yml)
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
| Authentication | Microsoft Entra ID or local accounts with encrypted TOTP MFA, one-use recovery codes, email password recovery, PostgreSQL sessions and revocation on sensitive identity changes |
| Assets | ITIL-aligned lifecycle, owners, criticality, environment, site, renewal, EOL and integration identity metadata |
| Business systems | Business-facing services with owners, RTO/RPO, sign-off context and supporting CI stacks |
| Relationships | Drag-and-drop React Flow maps with full-stack, network, storage, virtualization and business-impact perspectives |
| Impact analysis | Upstream/downstream traversal, shared dependencies, HA evidence and protected/degraded/outage decisions |
| Change control | Governed global/customer procedure templates, guided change form, scoped technician assignment and reassignment history, frozen named-asset impact snapshot, per-asset change history, suggested risk, branded PDF and email-based owner sign-off with expiring single-use links |
| Dashboards | MSP customer-risk queue and customer business-system/action dashboards |
| PostgreSQL | Canonical repository, forward-only checksum migrations, blank-database bootstrap and upgrade verification |
| Recovery | Portable checksum-protected export/import plus operational guidance for PostgreSQL PITR or `pg_dump` |
| Branding | MSP identity, logo, colours and report footer; tenant-scoped customer branding storage |
| Governance | Append-only attributable audit ledger, request correlation, tenant-aware Audit Center and CI activity timelines |
| Data quality | MSP/customer quality scores, prioritized findings, audited exceptions, provider-neutral reconciliation workbench and enforced per-customer field authority |
| Reports | Controlled MSP/customer report catalogue with branded PDF, filterable XLSX and UTF-8 CSV exports |
| Integrations | Capability-driven provider registry; guided ConnectWise and N-central setup, encrypted/environment credentials, immutable-ID filters and customer mapping, lease-safe continuous previews, provider-neutral review queues and authority-governed imports |
| Email | Root-managed Microsoft Graph sender, Azure managed identity or app credentials, Exchange mailbox scoping, audited outbox and test delivery |

Passportal passwords, secure notes and credential values are explicitly out of scope. Only approved metadata associations should enter the CMDB.

## Quick start with Docker

Prerequisites: Docker Desktop or Docker Engine with Compose v2.

```powershell
git clone https://github.com/IPT-Holdings-PTY-Ltd/ipt-cmdb.git
cd ipt-cmdb
docker compose up --build -d
```

Open <http://localhost:3000>. The Compose stack starts the application and PostgreSQL, applies database migrations and seeds the demo workspace.

Check process health and database readiness:

```powershell
docker compose ps
Invoke-RestMethod http://localhost:3000/api/live
Invoke-RestMethod http://localhost:3000/api/ready
```

The development identities all use the password `ChangeMe!`:

| Login | Role | Scope |
|---|---|---|
| `admin@example.com` | Platform administrator | All customers and root settings |
| `msp@example.com` | MSP operator | Assigned managed customers |
| `client@acme.example` | Customer reader | Acme Manufacturing only |

These identities are demo data. Never expose them on a public deployment.

## Compact customer appliance

For a cost-effective dedicated instance on one Docker host, use the production appliance
profile. It starts one CMDB container and one PostgreSQL container with generated secrets,
mandatory first-login MFA, persistent volumes, and verified backup/restore tooling:

```powershell
.\scripts\Initialize-Appliance.ps1 -AdminEmail owner@example.com -InstanceName customer-acme
$environment = '.appliance\customer-acme\.env.appliance'
docker compose --env-file $environment -f compose.appliance.yml up -d
```

See the [compact Docker appliance runbook](docs/deployment/APPLIANCE.md) before exposing
the instance or scheduling backups.

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
npm run lint
npm run typecheck
npm run build
docker build --tag ipt-cmdb:local .
```

The pre-commit hook runs Ruff and mypy on each commit. The pre-push hook also runs
the Python suite with branch coverage and enforces the checked-in coverage floor.

## Architecture at a glance

```mermaid
flowchart LR
    SPA[React SPA with MUI and React Router 8] -->|Authenticated HTTPS| API[FastAPI API]
    API --> PG[(PostgreSQL)]
    API --> PDF[Change PDF generator]
    ENTRA[Microsoft Entra ID / Easy Auth] --> API
    CW[ConnectWise Manage] --> WORKERS[Read-only sync workers]
    NC[N-central] --> WORKERS
    PP[Passportal metadata] --> WORKERS
    API -->|Application Mail.Send| GRAPH[Microsoft Graph / Exchange Online]
    NOTIFY[Notification evaluator and outbox worker] --> API
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
- [ConnectWise company discovery setup](docs/CONNECTWISE.md)
- [N-central customer and device inventory](docs/NCENTRAL.md)
- [Microsoft 365 email delivery](docs/MICROSOFT_365_EMAIL.md)
- [Local account password recovery](docs/LOCAL_ACCOUNT_RECOVERY.md)
- [Notification rules, ownership routing and delivery](docs/NOTIFICATIONS.md)
- [Change approval and business-owner sign-off](docs/CHANGE_APPROVALS.md)
- [Change-template governance and authoring](docs/CHANGE_TEMPLATES.md)
- [Deployment selector and production contract](docs/DEPLOYMENT.md)
- [Self-hosted container deployment](docs/deployment/SELF_HOSTED.md)
- [Compact Docker appliance](docs/deployment/APPLIANCE.md)
- [Azure Container Apps deployment](docs/deployment/AZURE_CONTAINER_APPS.md)
- [Background worker deployment and monitoring](docs/deployment/WORKERS.md)
- [Operations, upgrades and recovery](docs/OPERATIONS.md)
- [Release process and container publishing](docs/RELEASING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Changelog](CHANGELOG.md)

Interactive API documentation is available at `/docs` while the API is running.

## Near-term roadmap

1. Add the Passportal metadata adapter through the provider registry and canonical identity layer.
2. Expand N-central enrichment with selective deep inventory and operational-state evidence.
3. Bulk ownership, relationship-layer and lifecycle correction actions from data-quality findings.
4. Expand change approval with reusable policies, escalation reminders and ConnectWise ticket publishing.
5. Add governed workflow triggers and outputs through the provider-neutral worker boundary.

External provider writes remain disabled until a reviewable, auditable workflow is implemented.

## License

Licensed under the [MIT License](LICENSE).
