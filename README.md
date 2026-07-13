# CMDB Hub — MSP-ready foundation

A Python browser-based CMDB designed for ConnectWise-centric MSPs. It has a working tenant-scoped inventory UI, login/session API, provider control plane, and safe sync proof-of-connection. It is intentionally an **inventory aggregator**, not a password vault: Passportal secrets are never copied into this CMDB.

## Run locally

```powershell
docker compose up --build -d
```

Open `http://localhost:3000`. The modular React Admin workspace is served by FastAPI. The seeded demo identities all use `ChangeMe!`:

For frontend-only development with API proxying:

```powershell
npm install
npm run dev
```

| Login | Role | Scope |
|---|---|---|
| admin@example.com | Platform admin | every customer |
| msp@example.com | MSP operator | Acme + Northwind |
| client@acme.example | Client reader | Acme only |

## Core design

```
ConnectWise Manage ─┐
N-central ─────────┼─> Sync adapters ─> canonical CMDB / relationships ─> scoped API + portal
Passportal ────────┘                          │
                                             audit/sync-run history
Azure Entra ID ─────────────────────────────> identity + company/role claims
```

### CI metadata

Each CI has a structured metadata profile alongside its flexible provider fields: lifecycle and operational state, business criticality, service owner, technical owner, custodian, site, environment, vendor/model, purchase date, hardware warranty end, subscription/licence renewal date and scheduled review. This separates accountability and lifecycle governance from source-specific details such as serial, IP address and API IDs.

The overview also provides a 90-day attention queue for overdue and upcoming subscription renewals and vendor end-of-life dates, including the relevant technical or service owner.

Platform administrators and MSP operators also receive an MSP overview that aggregates those attention items across only the customers they are permitted to manage. Branding is hidden from customer-only users, while database configuration remains platform-admin only.

### Change control

From a customer relationship map, select a CI and choose **Create change package**. The four-stage workflow combines technician-entered scope, schedule, business impact, implementation, validation, rollback and communication details with a CMDB-derived downstream impact snapshot. The saved record freezes CI names, criticality, environment, site and owners so the historical form does not change when the live CMDB changes.

The backend generates a branded A4 PDF with the MSP logo, document footer, confidentiality label, impact table, transparent risk factors, execution plan, integration status and approval block. The same MSP brand profile drives the login page, workspace header and application colours. Change records already carry a provider-neutral external reference and a `connectwise.not_published` state; no ConnectWise ticket is created yet. A future publisher can populate that envelope idempotently without changing the form or frontend contract.

### Authorization model

Every asset carries `companyId`; every read filters against the caller's permitted company IDs. `platform_admin` sees all companies, `msp_operator` sees assigned customers and customer groups, and `client_reader` sees only their company. Sync requires MSP operator or platform admin. Production authentication uses Microsoft Entra ID through Azure Easy Auth and maps the Entra identity to these persisted roles and customer assignments. Local-development credentials are stored only as salted PBKDF2 hashes.

### Provider approach

- **ConnectWise Manage:** configured with REST base URL, company ID, public/private API keys and client ID. The current adapter calls `/company/companies` to validate credentials, but does **not** create/update CMDB companies without a review policy.
- **N-central:** a connector boundary is ready for API token + base URL. Add device, customer, site, warranty and last-check-in normalizers next.
- **Passportal:** only metadata such as owner, folder/customer reference and asset association should be synchronized. Never ingest passwords, secure notes, or credential values.
- Future systems (Hudu, IT Glue, Intune, NinjaOne, Azure AD) implement the same adapter contract: collect → normalize → match → review/apply → audit.

## Azure hosting

The included `Dockerfile` and `azure.yaml` work with Azure Developer CLI (`azd up`) and Azure Container Apps. For production:

1. Use Azure Database for PostgreSQL, Blob Storage for larger exports, and Key Vault for database and integration credentials. The app applies its versioned schema automatically after the server and database exist.
2. Enable a managed identity for the container and grant it Key Vault Secrets User; inject secret references as the environment variables shown in `.env.example`.
3. Put Entra ID authentication in front of the API and configure redirect URL `https://<your-cmdb-domain>/auth/callback`.
4. Run sync jobs using Container Apps Jobs or Azure Functions/Service Bus—not the web request process—and save an immutable run/audit record.
5. Configure a custom domain, HTTPS, Application Insights, backups, private endpoints, and least-privilege ConnectWise API member permissions.

The app can initialise an existing blank PostgreSQL database with either the current workspace, a platform-only seed, or demo data. Infrastructure provisioning remains outside the web process: Docker Compose creates the local server/database, while Azure deployments should use Bicep/Terraform or an equivalent controlled deployment. Environment-managed database connections are read-only in the UI.

The Database and recovery screen deliberately separates a checksum-protected **portable operational export/import** from a real database backup. Use Azure PostgreSQL point-in-time restore or scheduled `pg_dump`/`pg_restore` for disaster recovery; portable imports merge operational records and do not delete records absent from the file.

## Next delivery increments

1. Add separately permissioned customer theme overrides. MSP branding now uses its own canonical PostgreSQL profile with audited writes; customer-specific themes still use the synchronized application-state fallback.
2. Add Entra OIDC, invite flow, MFA/conditional access and company-to-group mapping UI.
3. Implement ConnectWise company/configuration/ticket matching and publish approved change packages as tickets using the existing external-reference envelope and an idempotency key.
4. Implement N-central device/customer ingestion and Passportal metadata association.
5. Add change approvals/revisions, CSV/API exports, webhooks and per-company audit logs.

## Security boundaries

- No credentials or secret content are stored in source, browser storage or CMDB records.
- Keep raw provider payloads encrypted with short retention; redact before user-visible logs.
- Make external writes disabled by default and require a selected-record review/apply step.
- Use provider-specific API members/tokens with only read scopes until an explicitly approved write workflow exists.
