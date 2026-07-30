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
- PowerShell 7 on Windows; or a POSIX shell, OpenSSL, `curl`, `sha256sum`/`shasum`,
  and Python 3 or `jq` on Linux
- A protected HTTPS reverse proxy before remote access is enabled
- The self-hosted deployment bundle from a GitHub release, or an exact image digest

On Windows, verify the downloaded bundle against the release `SHA256SUMS` before
removing its Mark-of-the-Web. For example:

```powershell
$archive = '.\ipt-cmdb-0.4.0-self-hosted.zip'
$archiveNamePattern = [regex]::Escape((Split-Path $archive -Leaf))
$expected = (
  Get-Content .\SHA256SUMS |
    Where-Object { $_ -match "${archiveNamePattern}$" } |
    ForEach-Object { ($_ -split '\s+')[0].ToLowerInvariant() }
)
$actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if (-not $expected -or $actual -ne $expected) { throw 'Release checksum mismatch.' }
Unblock-File $archive
Expand-Archive $archive -DestinationPath .\ipt-cmdb-release
```

If a blocked archive was already extracted, run `Unblock-File` only on the verified
PowerShell scripts. Never solve a downloaded-script warning with
`Set-ExecutionPolicy Unrestricted`.

## 1. Create an instance

The guided installer generates protected secrets, validates the rendered Compose model,
pulls both first-install images, starts the project with `--wait`, and verifies liveness
plus database/schema readiness. From an extracted release bundle on Windows:

```powershell
.\scripts\Install-Cmdb.ps1 `
  -AdminEmail 'owner@example.com' `
  -PublicBaseUrl 'https://cmdb.customer.example' `
  -InstanceName 'customer-acme' `
  -Port 3001
```

`Install-Cmdb.ps1` reads the exact `image.reference` from the bundle's
`release-manifest.json`. When installing from a source checkout instead, also pass
`-Image 'ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb@sha256:<release-digest>'`.

On Linux, the release manifest supplies the same digest:

```bash
./scripts/install-cmdb.sh \
  owner@example.com \
  customer-acme \
  https://cmdb.customer.example \
  '' \
  3001
```

The initializer creates `.appliance/customer-acme/` containing:

- a Compose environment file;
- separate PostgreSQL, bootstrap administrator, and MFA encryption secrets;
- the internal PostgreSQL connection URL;
- an initially empty backup directory.

On Linux, the initializer keeps the instance and secret directories `0700`, keeps the
environment file `0600`, and makes the individual container-mounted secret files
read-only (`0444`). Other host users cannot traverse the protected parent directory,
while native Docker Engine can bind-mount those files for the non-root CMDB process.
Docker Desktop retains the protected Windows ACL model instead.

It also records `CMDB_IMAGE_DIGEST`, allowing health and incident evidence to identify
the exact release. A non-loopback installation must use a digest. A SemVer image tag is
accepted only with an explicit loopback public origin for local evaluation.

The directory is excluded from Git. Back it up securely; losing the MFA key prevents
existing local TOTP seeds from being decrypted. Do not copy one instance's secret
directory into another customer instance.

Initializers never overwrite an existing instance directory or secret. If startup fails
after configuration was created, do not rerun the installer. Correct the reported
problem and resume without generating secrets:

```powershell
$environment = '.appliance\customer-acme\.env.appliance'
docker compose --env-file $environment -f compose.appliance.yml config --quiet
docker compose --env-file $environment -f compose.appliance.yml pull
docker compose --env-file $environment -f compose.appliance.yml up -d --wait
docker compose --env-file $environment -f compose.appliance.yml ps
```

Once Microsoft 365 email is verified, the same environment file can enable the
read-only ConnectWise and N-central scheduler and alert delivery:

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

The generated environment keeps `FORWARDED_ALLOW_IPS` blank, so a direct container
connection cannot spoof its address with `X-Forwarded-For`. When the HTTPS proxy is
enabled, set this value to its exact address or smallest container-network CIDR as
observed by CMDB. Never use `*` or an all-address CIDR. The appliance also writes the
reviewed local-login defaults (five identifier failures and twenty source failures
over fifteen minutes); PostgreSQL enforces them even if the application is later
scaled to multiple replicas.

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
restore tool validates the checksum, recreates the database, and loads the archive.
When the worker overlay is active, stop both runtime roles:

```powershell
$environment = '.appliance\customer-acme\.env.appliance'
$backup = 'cmdb-20260720T120000Z.dump'
# Combined profile:
docker compose --env-file $environment -f compose.appliance.yml stop cmdb
# Worker-split profile (use instead of the preceding stop command):
docker compose --env-file $environment -f compose.appliance.yml `
  -f compose.worker.yml stop cmdb worker

docker compose --env-file $environment -f compose.appliance.yml `
  --profile tools run --rm -e BACKUP_FILE=$backup restore

# Combined profile:
docker compose --env-file $environment -f compose.appliance.yml start cmdb
# Worker-split profile (use instead of the preceding start command):
docker compose --env-file $environment -f compose.appliance.yml `
  -f compose.worker.yml up -d --wait cmdb worker
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

## Safe updates

Run the bundled updater on the Docker host. It is deliberately outside the application
container: CMDB has no Docker socket, registry credential, or self-update permission.
`Check` is the default action and makes no deployment or database change:

```powershell
.\scripts\Update-Cmdb.ps1 -Action Check -InstanceName 'customer-acme'
```

On Linux:

```bash
./scripts/update-cmdb.sh --check --instance-name customer-acme
```

The check reads the latest stable GitHub release, verifies
`release-manifest.json` against `SHA256SUMS`, validates the current Compose
configuration, and reports the current and target image digests, versions, schemas,
schema-history checksum, database classification, rollback policy, and verified
manifest SHA-256. If the target digest is already configured, `Check` succeeds only
when the web container is healthy and `/api/ready` reports that exact version, digest,
schema, and schema-history checksum. Otherwise it fails and instructs the operator to
repair or recreate the exact approved image; an installed digest alone is not success.

To check an already downloaded release without querying GitHub, point `-ManifestPath`
at the extracted bundle directory; keep its `release-manifest.json` and `SHA256SUMS`
together. On Linux use `--manifest ./release-manifest.json`; its checksum defaults to
the adjacent `SHA256SUMS`, or can be selected explicitly with `--checksums`.

Review the result, release notes, maintenance window, and off-host backup retention.
Then approve both the exact target version and manifest SHA-256 printed by that same
`Check`:

```powershell
.\scripts\Update-Cmdb.ps1 `
  -Action Apply `
  -InstanceName 'customer-acme' `
  -ApproveVersion '0.5.0' `
  -ApproveManifestSha256 '<64-lowercase-hex-characters>'
```

The equivalent Linux command can include a structured, non-secret evidence reference:

```bash
./scripts/update-cmdb.sh \
  --apply \
  --instance-name customer-acme \
  --approved-version 0.5.0 \
  --approved-manifest-sha256 '<64-lowercase-hex-characters>' \
  --recovery-evidence-ref 'change:CHG-12345'
```

The appliance does not require an operator evidence reference because it always creates
a final verified backup. The POSIX updater accepts an optional structured reference and
stores only its type and SHA-256; the Windows record retains the verified backup details.
Neither stores a raw operator reference for appliance recovery.

The updater inspects the existing Compose project before making changes. Add
`-WorkerSplit` to both commands only when the instance has a worker container; on Linux
use `--worker-split`. A detected worker without the flag, or the flag without a detected
worker, fails closed. A running worker is stopped before web and restarted after
readiness; an intentionally stopped worker remains stopped.

`Apply` performs a bounded, fail-closed sequence:

1. validates the release manifest, checksum, immutable `image@sha256` reference,
   PostgreSQL major, current environment, and rendered Compose model;
2. pulls only the exact target application image while the old application is running;
3. stops the dedicated worker, when selected, and then stops the web container;
4. creates a final custom-format PostgreSQL backup after all application writers have
   stopped, then verifies its filename, companion checksum, archive readability, and
   host-side SHA-256;
5. atomically pins `CMDB_IMAGE` and `CMDB_IMAGE_DIGEST`;
6. runs `python scripts/migrate_postgres.py` once in an ephemeral target-image
   container;
7. starts the web container and requires `/api/ready` to report the exact target
   application version, image digest, schema version, and schema-history checksum; and
8. restarts a previously running dedicated worker only after web readiness succeeds.

Before pulling or stopping anything, a different-digest update also requires valid
current runtime schema metadata and rejects a target schema older than the current
schema. Repair an unhealthy current runtime before retrying; do not use the updater to
bypass an unknown database state.

`-WaitSeconds` controls the final readiness wait and defaults to 180 seconds; Linux uses
`--timeout` for the same 30-to-900-second range. Database migrations separately use a
60-second PostgreSQL lock timeout and a 15-minute statement timeout by default:

```dotenv
CMDB_MIGRATION_LOCK_TIMEOUT_MS=60000
CMDB_MIGRATION_STATEMENT_TIMEOUT_MS=900000
```

Increase those values only for a reviewed migration window. A timeout is a failed
update, not permission to bypass the migration or readiness checks.

The worker intentionally has no HTTP health check because it does not listen on port
3000. After an update, require its container process to remain running and verify its
durable heartbeat and last-success state in the administration screens or worker
runbook. Web `/api/ready` success alone does not prove worker health.

### Failure and rollback boundary

Before the one-shot migration begins, the updater can restore the previous environment
file and restart the previous image. Once migration begins, it never automatically
restores an older image: even a failed command may have changed PostgreSQL.

Any release that changes the schema currently requires the final backup to be restored
before an older application image is used. An image-only rollback is permitted only
when the release manifest says there was no database change and the exact migration
history is confirmed unchanged. Keep the worker stopped during a post-migration
incident and follow the reviewed restore procedure above.

PostgreSQL major-version upgrades are always separate, rehearsed operations. The updater
supports PostgreSQL 16 and never pulls, recreates, or upgrades the PostgreSQL service.
Never point Watchtower or another unattended image updater at this stack, never deploy
the mutable `latest` tag, and never schedule `Apply`.

### Schedule update checks, not updates

Use host monitoring to notify operators when a scheduled `Check` exits unsuccessfully
or reports a different verified digest. For Windows Task Scheduler, this example creates
a daily check-only task:

```powershell
$action = New-ScheduledTaskAction `
  -Execute 'C:\Program Files\PowerShell\7\pwsh.exe' `
  -Argument '-NoProfile -NonInteractive -File "C:\ipt-cmdb\scripts\Update-Cmdb.ps1" -Action Check -InstanceName "customer-acme" -InstanceRoot "C:\ipt-cmdb\.appliance\customer-acme"'
$trigger = New-ScheduledTaskTrigger -Daily -At '08:00'
Register-ScheduledTask `
  -TaskName 'IPT CMDB update check - customer-acme' `
  -Action $action `
  -Trigger $trigger `
  -Description 'Check only; an operator must review and approve Apply.'
```

The scheduled account needs read access to the instance environment and permission to
run Docker Compose, but it should not contain an approval version or manifest SHA-256.
Route its result to the MSP's normal task-failure or log alerting.

On a Linux host, add only the check action to the deployment account's crontab. For
example, at 08:00 each day:

```cron
0 8 * * * cd /opt/ipt-cmdb && ./scripts/update-cmdb.sh --check --instance-name customer-acme --instance-root /opt/ipt-cmdb/.appliance/customer-acme
```

Configure the host's cron output or job-exit monitoring to notify operations. Do not add
`--apply`, `--approved-version`, or `--approved-manifest-sha256` to cron. Add
`--worker-split` to the scheduled check only when that worker topology is deployed.
