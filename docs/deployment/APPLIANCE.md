# Compact Docker appliance

The compact appliance is the lowest-cost supported production shape. One Compose
project starts two isolated containers on one host:

```text
HTTPS / identity proxy -> IPT CMDB -> PostgreSQL
                                      |
                                      +-> persistent Docker volume
```

It is intentionally not a single container. Keeping PostgreSQL in its own container
preserves standard backup, upgrade, recovery, and later external-database migration
without introducing SQLite or another repository implementation.

## Suitable uses

- A smaller MSP running its own instance
- A customer that requires a dedicated CMDB installation
- A branch or private-cloud Docker host
- A low-cost starting point that may later move PostgreSQL to a managed service

Recommended minimum host: 2 vCPU, 4 GB RAM, reliable SSD storage, and enough separate
backup capacity for the database retention period. Increase resources based on asset,
relationship, audit, and report volume.

## Prerequisites

- Docker Engine/Desktop with Compose v2
- PowerShell 7 on Windows, or POSIX shell plus OpenSSL on Linux
- A protected HTTPS reverse proxy before remote access is enabled
- A pinned image version or digest for production

## 1. Create an instance

From the repository root on Windows:

```powershell
.\scripts\Initialize-Appliance.ps1 `
  -AdminEmail 'owner@example.com' `
  -InstanceName 'customer-acme' `
  -Port 3001 `
  -Image 'ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:v0.3.0'
```

On Linux:

```bash
./scripts/initialize-appliance.sh \
  owner@example.com \
  customer-acme \
  ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:v0.3.0 \
  3001
```

The initializer creates `.appliance/customer-acme/` containing:

- a Compose environment file;
- separate PostgreSQL, bootstrap administrator, and MFA encryption secrets;
- the internal PostgreSQL connection URL;
- an initially empty backup directory.

The directory is excluded from Git. Back it up securely; losing the MFA key prevents
existing local TOTP seeds from being decrypted. Do not copy one instance's secret
directory into another customer instance.

## 2. Validate and start

```powershell
$environment = '.appliance\customer-acme\.env.appliance'
docker compose --env-file $environment -f compose.appliance.yml config --quiet
docker compose --env-file $environment -f compose.appliance.yml pull
docker compose --env-file $environment -f compose.appliance.yml up -d
docker compose --env-file $environment -f compose.appliance.yml ps
```

Once Microsoft 365 email is verified, the same environment file can enable the
read-only ConnectWise scheduler and alert delivery:

```text
INTEGRATION_WORKER_ENABLED=true
NOTIFICATION_WORKER_ENABLED=true
INTEGRATION_ALERT_RECIPIENTS=integration-ops@example.com
```

Leave these values false during initial setup. When the recipient list is blank, active
platform-administrator email addresses are used.

For a busier dedicated instance, initialize the appliance first, require `/api/ready`,
and then add `-f compose.worker.yml` to the Compose commands. This moves the same
enabled jobs into an isolated service without moving PostgreSQL or changing review
controls. The complete cutover and rollback sequence is in the
[worker runbook](WORKERS.md).

The application binds to `127.0.0.1:3000` by default. Read the generated bootstrap
password from `secrets/bootstrap-admin-password.txt`, sign in with the selected email,
and enroll an authenticator during the first login. The known demonstration password is
not used.

Verify both runtime gates:

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/live
Invoke-RestMethod http://127.0.0.1:3000/api/ready
```

Do not change the bind address or expose port 3000 until HTTPS and the chosen identity
boundary have been verified.

## Authentication options

The generated appliance starts with local authentication, a random administrator
password, and `LOCAL_MFA_POLICY=all`. This is intended for isolated dedicated instances
that enforce HTTPS, mandatory TOTP, rate-limited ingress, and tight administrator access.

For Entra ID, place the appliance behind a trusted OIDC-aware reverse proxy, set
`AUTH_MODE=easy_auth`, and configure `ENTRA_PRINCIPAL_NAME_HEADER` to the verified email
header produced by that proxy. The proxy must remove client-supplied identity headers.
See [Self-hosted container deployment](SELF_HOSTED.md#4-configure-the-identity-proxy).

## Backups

Create and immediately verify a custom-format PostgreSQL backup:

```powershell
$environment = '.appliance\customer-acme\.env.appliance'
docker compose --env-file $environment -f compose.appliance.yml `
  --profile tools run --rm backup
```

Each run writes a timestamped `.dump` and matching `.sha256` file under the instance
backup directory. Schedule this exact command with Windows Task Scheduler, cron, or the
host's orchestrator. Copy completed files to encrypted off-host storage and alert when
the newest successful backup exceeds the required RPO.

The backup job verifies that `pg_restore` can read the archive. That is useful but does
not replace a periodic full recovery rehearsal.

## Restore rehearsal or recovery

Restoring replaces the appliance database. Keep the application stopped while the
restore tool validates the checksum, recreates the database, and loads the archive:

```powershell
$environment = '.appliance\customer-acme\.env.appliance'
$backup = 'cmdb-20260720T120000Z.dump'
docker compose --env-file $environment -f compose.appliance.yml stop cmdb
docker compose --env-file $environment -f compose.appliance.yml `
  --profile tools run --rm -e BACKUP_FILE=$backup restore
docker compose --env-file $environment -f compose.appliance.yml start cmdb
Invoke-RestMethod http://127.0.0.1:3000/api/ready
```

Always rehearse on a separate Compose project and host path first. Confirm customer,
asset, relationship, audit, change, user, and branding counts after recovery.

## Move PostgreSQL to an external service later

No application data conversion is required:

1. Stop CMDB writes and create a final verified appliance backup.
2. Create an empty PostgreSQL 16 database and least-privilege application role on the
   target server.
3. Restore the custom archive with standard `pg_restore --no-owner --no-privileges`.
4. Put the TLS-enabled target URL in a protected `database-url.txt` file.
5. Switch from `compose.appliance.yml` to `compose.production.yml`, retaining the same
   Compose project name and MFA/bootstrap secret files.
6. Start the application and require `/api/ready`, login, tenant isolation, asset reads,
   relationship traversal, and report generation to pass.
7. Retain the stopped local PostgreSQL volume until the cutover acceptance period ends.

Use `sslmode=verify-full` when the external service and CA configuration support it;
otherwise require the strongest TLS mode approved for the target provider.

## Multiple dedicated customers

Run the initializer once per customer with a unique instance name. Compose project names,
database volumes, runtime volumes, backup directories, and secrets are then isolated.
Assign each instance a distinct hostname and host port, and never reuse one customer's
database role or secret set for another.

Central operations should track, per instance: customer, hostname, image digest, schema
version, last successful backup, last restore rehearsal, database location, authentication
mode, and upgrade window.

## Upgrade

1. Create and copy off-host a verified backup.
2. Update only `CMDB_IMAGE` in the instance environment file.
3. Run `docker compose ... pull` and review `docker compose ... config`.
4. Recreate the application with `docker compose ... up -d`.
5. Verify readiness, identity, tenant scope, one asset, one relationship, and one report.

PostgreSQL major-version upgrades are a separate operation. Never change the database
image from `postgres:16` to another major version against the existing volume.
