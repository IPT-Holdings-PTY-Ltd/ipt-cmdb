# Deployment guide

IPT CMDB supports three explicit deployment profiles. Choose one profile and do
not promote the development Compose file into production.

| Profile | Intended use | Database | Authentication | Entry point |
|---|---|---|---|---|
| Local Compose | Development and controlled evaluation | Included PostgreSQL | Demo local accounts | `docker-compose.yml` |
| Compact appliance | Small production instance on one Docker host | Included PostgreSQL container | Local TOTP or trusted identity proxy | `compose.appliance.yml` |
| Self-hosted containers | VM or container host production | External PostgreSQL | Trusted OIDC/identity proxy | `compose.production.yml` |
| Azure Container Apps | Azure production baseline | Private PostgreSQL Flexible Server | Container Apps Entra authentication | `azure.yaml` + `infra/` |

Detailed runbooks:

- [Self-hosted container deployment](deployment/SELF_HOSTED.md)
- [Compact Docker appliance](deployment/APPLIANCE.md)
- [Azure Container Apps deployment](deployment/AZURE_CONTAINER_APPS.md)
- [Local development](LOCAL_DEVELOPMENT.md)

## Runtime contract

Every deployment uses the same application image:

- port `3000` serves the SPA and `/api` from one origin;
- `/api/live` proves that the process is accepting HTTP requests;
- `/api/ready` returns `200` only when the canonical PostgreSQL repository is ready;
- `/api/health` is an operator-safe informational status and is not a readiness probe;
- production operational state lives in PostgreSQL;
- compatible, checksum-protected forward-only migrations run during startup;
- migrations hold a PostgreSQL transaction-scoped advisory lock, preventing two
  replicas from modifying the schema concurrently;
- no production secret is sent to the browser or returned by health responses.

The container image runs as the unprivileged `cmdb` user. The production Compose
profile additionally makes the root filesystem read-only, drops Linux capabilities,
and mounts only a small runtime volume and temporary filesystem.

## Required environment settings

| Variable | Production value |
|---|---|
| `DATABASE_URL` or `DATABASE_URL_FILE` | PostgreSQL URL with TLS, normally `sslmode=require` |
| `DATABASE_SEED_MODE` | `empty` |
| `AUTH_MODE` | `easy_auth` behind a trusted identity boundary |
| `MFA_ENCRYPTION_KEY` or `MFA_ENCRYPTION_KEY_FILE` | Stable URL-safe base64 encoding of 32 random bytes |
| `ALLOW_UI_DATABASE_CONFIG` | `false` |
| `ALLOW_LOCAL_DEVELOPMENT` | `false` |
| `ALLOW_LOCAL_BREAK_GLASS` | `false` unless a separately approved emergency design exists |
| `BOOTSTRAP_ADMIN_EMAIL` | First platform administrator for a blank database |
| `BOOTSTRAP_ADMIN_PASSWORD` or `_FILE` | Random initial password supplied through the secret store |

Provider credentials belong in a secret manager. See `.env.example` for the full
runtime configuration surface; never commit populated values.

## Identity boundary

`AUTH_MODE=easy_auth` trusts identity headers. It is safe only when the application
is unreachable except through a gateway that authenticates the user, removes any
client-supplied identity headers, and injects verified values. Azure Container Apps
authentication provides that boundary in the Azure profile. A self-hosted deployment
must provide an equivalent OIDC-aware reverse proxy.

External authentication does not replace application authorization. The Entra email
must map to an enabled CMDB user, and customer/group/RBAC scope is still enforced by
the API. Unmapped identities are denied.

## Database lifecycle

The application can initialize an empty PostgreSQL database and migrate an older
supported CMDB schema. It does not create the PostgreSQL server itself outside the
Azure IaC profile. The runtime role currently needs DDL rights because migrations run
during startup.

For a production release:

1. Confirm a recent recovery point or tested backup.
2. Review new files under `db/migrations`.
3. Deploy the immutable image to staging.
4. Require `/api/live` and `/api/ready` to pass.
5. Test identity mapping, tenant switching, one asset read, and one PDF report.
6. Promote the same image digest.

Application rollback is safe only when the older image understands the already-applied
schema. Migrations are forward-only; do not casually reverse them.

## Current scaling boundary

The Azure baseline intentionally uses one replica. Schema migrations are serialized,
but first-ever repository seeding has not yet been certified under simultaneous cold
starts. Complete that concurrency test and move initialization to a deployment job
before raising `maxReplicas`.

## Production checklist

- Immutable image tag or digest, vulnerability scanned before release
- PostgreSQL private networking, backups, PITR retention, and restore rehearsal
- Entra Conditional Access/MFA or equivalent OIDC policy
- Key Vault or protected container secret files
- HTTPS-only public endpoint and restrictive ingress/firewall rules
- Central logs, alerts for readiness failure and HTTP 5xx, and request correlation
- Root/admin user review and local break-glass policy
- Restore and application rollback runbooks tested in a non-production environment

## References

- [Azure Container Apps authentication](https://learn.microsoft.com/azure/container-apps/authentication)
- [Azure Container Apps health probes](https://learn.microsoft.com/azure/container-apps/health-probes)
- [Azure Container Apps Key Vault references](https://learn.microsoft.com/azure/container-apps/manage-secrets)
- [Azure Developer CLI schema](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-schema)
- [Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/)
