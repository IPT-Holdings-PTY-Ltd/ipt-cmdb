# CMDB Hub — MSP-ready foundation

A Python browser-based CMDB designed for ConnectWise-centric MSPs. It has a working tenant-scoped inventory UI, login/session API, provider control plane, and safe sync proof-of-connection. It is intentionally an **inventory aggregator**, not a password vault: Passportal secrets are never copied into this CMDB.

## Run locally

```powershell
python app.py
```

Open `http://localhost:3000`. The seeded demo identities all use `ChangeMe!`:

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

### Authorization model

Every asset carries `companyId`; every read filters against the caller's permitted company IDs. `platform_admin` sees all companies, `msp_operator` sees an explicit MSP customer list, and `client_reader` sees only their company. Sync requires MSP operator or platform admin. Production authentication should replace the demo password login with Microsoft Entra ID (OIDC), mapping Entra group/app roles to these roles and customer claims.

### Provider approach

- **ConnectWise Manage:** configured with REST base URL, company ID, public/private API keys and client ID. The current adapter calls `/company/companies` to validate credentials, but does **not** create/update CMDB companies without a review policy.
- **N-central:** a connector boundary is ready for API token + base URL. Add device, customer, site, warranty and last-check-in normalizers next.
- **Passportal:** only metadata such as owner, folder/customer reference and asset association should be synchronized. Never ingest passwords, secure notes, or credential values.
- Future systems (Hudu, IT Glue, Intune, NinjaOne, Azure AD) implement the same adapter contract: collect → normalize → match → review/apply → audit.

## Azure hosting

The included `Dockerfile` and `azure.yaml` work with Azure Developer CLI (`azd up`) and Azure Container Apps. For production:

1. Use Azure Database for PostgreSQL (replace local JSON persistence), Blob Storage for exports, and Key Vault for integration credentials.
2. Enable a managed identity for the container and grant it Key Vault Secrets User; inject secret references as the environment variables shown in `.env.example`.
3. Put Entra ID authentication in front of the API and configure redirect URL `https://<your-cmdb-domain>/auth/callback`.
4. Run sync jobs using Container Apps Jobs or Azure Functions/Service Bus—not the web request process—and save an immutable run/audit record.
5. Configure a custom domain, HTTPS, Application Insights, backups, private endpoints, and least-privilege ConnectWise API member permissions.

## Next delivery increments

1. Replace the PostgreSQL `application_state` compatibility store with endpoint-specific repositories over the canonical CI, mapping, relationship and audit tables. The running API already uses PostgreSQL whenever `DATABASE_URL` is set; the schema, migration command, cross-source ID mapping and reconciliation rules are included in `db/schema.sql`, `scripts/migrate_postgres.py` and `ARCHITECTURE.md`.
2. Add Entra OIDC, invite flow, MFA/conditional access and company-to-group mapping UI.
3. Implement ConnectWise company/configuration/ticket matching with a review queue and idempotent upserts.
4. Implement N-central device/customer ingestion and Passportal metadata association.
5. Add relationship explorer, lifecycle/warranty tracking, CSV/API exports, webhooks and per-company audit logs.

## Security boundaries

- No credentials or secret content are stored in source, browser storage or CMDB records.
- Keep raw provider payloads encrypted with short retention; redact before user-visible logs.
- Make external writes disabled by default and require a selected-record review/apply step.
- Use provider-specific API members/tokens with only read scopes until an explicitly approved write workflow exists.
