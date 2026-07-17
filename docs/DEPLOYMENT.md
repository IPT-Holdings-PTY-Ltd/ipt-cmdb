# Container and Azure deployment

## Deployment status

The repository contains a production-style multi-stage `Dockerfile`, a Compose development stack and an `azure.yaml` service descriptor. It does not yet include opinionated Bicep or Terraform for a complete Azure environment. Provision the supporting Azure resources through your organisation's infrastructure-as-code process.

## Container contract

- listens on port `3000`;
- serves the compiled SPA and `/api` from one origin;
- exposes `/api/health` and `/api/v2/health`;
- requires writable temporary space only for explicitly configured local data;
- uses PostgreSQL for all production operational state;
- applies compatible database migrations during startup.

## Required production services

- Azure Container Apps or an equivalent container runtime;
- Azure Database for PostgreSQL Flexible Server;
- Microsoft Entra ID authentication through Container Apps/App Service authentication or a trusted identity-aware proxy;
- Azure Key Vault for database and provider credentials;
- Log Analytics/Application Insights;
- a custom domain and managed certificate;
- PostgreSQL backups and point-in-time restore;
- Container Apps Jobs, Functions or another worker runtime for scheduled integrations.

## Core environment variables

| Variable | Production guidance |
|---|---|
| `PORT` | Keep `3000` unless the platform injects another value |
| `DATABASE_URL` | Key Vault-backed PostgreSQL connection string; SSL required |
| `DATABASE_SEED_MODE` | `empty` for a new platform; ignored after initialisation |
| `AUTH_MODE` | `easy_auth` behind the Azure authentication boundary |
| `ALLOW_LOCAL_BREAK_GLASS` | `false` unless an audited emergency design requires it |
| `MFA_ENCRYPTION_KEY` | Stable URL-safe base64 encoding of 32 random bytes from Key Vault; required for local TOTP |
| `LOCAL_MFA_POLICY` | `optional`, `admins` or `all`; applies only to local accounts |
| `ALLOW_UI_DATABASE_CONFIG` | `false`; production database settings are infrastructure-owned |
| `ALLOW_LOCAL_DEVELOPMENT` | Always `false` |
| `ENTRA_LOGIN_URL` | Runtime login endpoint, normally `/.auth/login/aad?...` |
| `ENTRA_LOGOUT_URL` | Runtime logout endpoint |
| `ENTRA_PRINCIPAL_HEADER` | Header containing the encoded principal |
| `ENTRA_PRINCIPAL_NAME_HEADER` | Optional direct user/email header |
| `ENTRA_EMAIL_CLAIMS` | Ordered accepted email claim names |

See `.env.example` for provider variables. Never commit populated values.

## Entra ID boundary

1. Configure Azure authentication to require an authenticated Microsoft identity.
2. Configure the application's login/logout URLs to match the hosting surface.
3. Ensure the proxy strips untrusted inbound identity headers and injects its own verified headers.
4. Create the corresponding CMDB user and customer/group assignments.
5. Keep API authorization enabled; external authentication does not replace tenant checks.

If an authenticated Entra user is not mapped to a CMDB account, access should be denied rather than auto-provisioned with broad scope.

Entra identities should receive MFA through Conditional Access. Application TOTP is reserved for local and deliberately enabled break-glass identities. Every container replica must receive the same `MFA_ENCRYPTION_KEY`; changing it without re-encrypting stored seeds prevents enrolled users from completing TOTP.

## PostgreSQL

Use a private endpoint or equivalent network restriction. The runtime database role needs access to the CMDB schema and permission to apply packaged migrations. If schema changes are managed by a separate deployment identity, run the migration command in a controlled pre-deployment job and give the web role only runtime privileges.

The application creates schema objects inside an existing database. It does not provision the PostgreSQL server or database.

## Suggested release flow

1. Pull or build the immutable image produced from a `v*` Git tag.
2. Review release notes and migration files.
3. Verify a recent PostgreSQL recovery point or backup.
4. Deploy to a staging revision and check `/api/health`.
5. Exercise login, tenant switching, asset reads and a PDF generation smoke test.
6. Shift traffic to the new revision.
7. Monitor error rate, database connections and migration status.

Rollback the application revision only when the older image supports the already-applied database schema. Database migrations are forward-only; restore PostgreSQL only through an approved recovery procedure.

## Integration workers

Provider collection should not run as a long web request in production. Use a scheduled Container Apps Job or queue-driven worker with:

- the same canonical repository package;
- least-privilege, read-only provider credentials;
- per-connection locking;
- retry with backoff and provider rate-limit handling;
- durable sync-run and reconciliation records;
- no browser-facing secret output.

## References

- [Azure Container Apps authentication and authorization](https://learn.microsoft.com/azure/container-apps/authentication)
- [Azure Developer CLI `azure.yaml` schema](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Azure Database for PostgreSQL backup and restore](https://learn.microsoft.com/azure/postgresql/backup-restore/concepts-backup-restore)
