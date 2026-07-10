# IPT CMDB

IPT CMDB is a Python-based, multi-tenant CMDB foundation for managed service providers. It combines asset inventory, lifecycle and ownership tracking, relationship mapping, renewal/end-of-life attention queues, scoped access, and a root-level MSP workspace.

It is designed to integrate safely with ConnectWise Manage, N-central, and Passportal. The current release provides the CMDB foundation and provider control plane; external syncs remain deliberately review-gated.

> [!WARNING]
> This is an early public foundation, not a production-ready password vault or an automatic integration writer. Demo credentials and local JSON persistence are for development only. Use Entra ID, PostgreSQL, Key Vault, and a formal backup strategy before production use.

## Highlights

- Multi-tenant MSP/customer workspace with server-enforced company scope.
- Platform admin, MSP operator, and customer reader role model.
- ITIL-aligned asset metadata: lifecycle, operational condition, criticality, owners, site, environment, vendor, warranty, renewal and end-of-life dates.
- Customer and MSP-level renewal/end-of-life attention queues.
- Interactive CI topology for relationships, dependencies, network devices, software, and licences.
- Root-level customer groups, RBAC preview, branding, database configuration, and validated backup/restore.
- PostgreSQL-compatible persistence with a canonical relational schema and cross-provider mapping design.

## Quick start

### Prerequisites

- Python 3.12+
- Optional: Docker Desktop for PostgreSQL

```powershell
git clone https://github.com/<your-org>/ipt-cmdb.git
cd ipt-cmdb
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open [http://localhost:3000](http://localhost:3000).

The development seed accounts use `ChangeMe!`:

| Account | Role | Scope |
| --- | --- | --- |
| `admin@example.com` | Platform admin | All customers |
| `msp@example.com` | MSP operator | Assigned customers |
| `client@acme.example` | Customer reader | Acme Manufacturing |

Change or remove these accounts before exposing the application to a network.

### Run with PostgreSQL

```powershell
docker compose up -d
$env:DATABASE_URL = 'postgresql://cmdb:cmdb@localhost:5432/cmdb'
python app.py
```

The application automatically applies its compatibility state store to PostgreSQL. The canonical production schema is in [`db/schema.sql`](db/schema.sql); see [`ARCHITECTURE.md`](ARCHITECTURE.md) for the planned endpoint-specific repository migration.

## Containers

Build and run the application image:

```powershell
docker build -t ipt-cmdb:local .
docker run --rm -p 3000:3000 --env DATA_DIR=/app/data ipt-cmdb:local
```

The GitHub workflow builds every pull request and publishes a multi-platform image to GitHub Container Registry when a version tag is pushed. See [Publishing](#publishing).

## Security model

- Every asset and relationship is constrained to a customer/company scope.
- Only Platform Admins can manage the database, perform backup/restore, manage RBAC, or create customers.
- Backup exports contain operational and user data, so treat them as sensitive and store them encrypted.
- Integration credentials are API-host-only configuration. Do not store credentials in source control or browser storage.
- Passportal integrations must synchronize metadata only—never passwords, secure notes, or credential values.

See [`SECURITY.md`](SECURITY.md) for reporting guidance and deployment expectations.

## Integration roadmap

The recommended first production connector is ConnectWise Manage:

1. Configure server-side credentials and test read-only access.
2. Map ConnectWise companies to CMDB customers.
3. Preview proposed inserts, updates, duplicates, and mappings.
4. Approve selected changes before writing canonical records.

N-central and Passportal should implement the same `collect → normalize → match → review/apply → audit` flow. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for identifiers, mapping tables, authority, and reconciliation rules.

## Backup and restore

Platform Admins can use the root **Database** page to download a versioned CMDB Hub JSON backup or restore a previously downloaded backup. Restore validates the backup format and requires that it retains at least one Platform Admin, but it replaces the current CMDB state. Test restores in a non-production environment first.

For production, use encrypted database backups and point-in-time recovery provided by Azure Database for PostgreSQL in addition to application-level exports.

## Development

```powershell
python -m unittest discover -s test -v
```

The CI workflow runs the Python test suite, checks front-end JavaScript syntax, and validates the container build.

## Publishing

1. Review [`CHANGELOG.md`](CHANGELOG.md) and update the version/date.
2. Tag a release, for example: `v0.1.0`.
3. Push the tag.
4. GitHub Actions publishes `ghcr.io/<owner>/ipt-cmdb:<tag>` and `:latest`.
5. Create the GitHub Release from the generated tag and use the matching changelog section as release notes.

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md), keep customer data and secrets out of issues/commits, and make external integration writes opt-in and reviewable.

## Licence

Distributed under the [Apache License 2.0](LICENSE).
