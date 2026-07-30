# Operations, upgrades and recovery

## Health and readiness

`GET /api/health` returns the API state, repository mode, expected schema version and authentication mode. A healthy production response should identify PostgreSQL and `canonical_postgresql` without a repository error.

Use liveness probes against `/api/live` and traffic readiness probes against `/api/ready`.
Treat database/schema failures as unavailable; do not route production traffic to a
local fallback. `/api/health` and `/api/v2/health` remain informational compatibility
surfaces rather than traffic gates. Readiness opens a bounded live PostgreSQL
connection, runs a bounded statement, and compares the complete version/checksum
migration history with the image. A reachable database with a missing, changed, or
unexpected migration therefore still returns `503`.

## Logs and alerts

Container profiles emit one-line JSON application logs by default. Request records use
stable `event`, `request_id`, `correlation_id`, `method`, route-template, status and
duration fields; they do not include query strings, request bodies, authorization
headers, client addresses or arbitrary application dictionaries. Common credential
forms are redacted as a second line of defence. Set `LOG_FORMAT=text` only for an
interactive local troubleshooting session.

The Azure baseline provisions an independent Azure Monitor action group and alerts for:

- readiness `503` responses;
- non-readiness application `5xx` responses;
- a missing web/worker runtime heartbeat;
- PostgreSQL unavailability; and
- PostgreSQL storage utilization at or above 80 percent by default.

Azure Monitor sends these notifications directly to the configured operations email;
the application's Microsoft Graph email integration is not a dependency. Repeated
readiness alerts require a database-connectivity and migration-history check. A worker
alert requires checking the Container App replica and the durable worker status before
restarting it. Raise the storage limit only after capacity planning, not to silence an
active capacity incident.

## Routine checks

- Container restart and failure count
- API 5xx rate and latency
- PostgreSQL storage, connections, locks and replication/backup state
- Schema version against the packaged expected version
- Integration last-run status and age
- Background worker heartbeat, deployment mode and last successful cycle
- Provider quota remaining or retry-after state when the provider supplies it
- Reconciliation backlog
- CIs with stale observations, missing owners or no relationships
- Upcoming renewals and end-of-life dates

The MSP dashboard exposes the last five categories as operational work queues.

## Upgrade procedure

Every release has an authoritative `release-manifest.json` and `SHA256SUMS`. The
manifest pins the exact container digest and records the application version, expected
schema/history, supported PostgreSQL major, database change classification, backup
requirement, and rollback boundary. Never infer those values from `latest`.

For either Compose appliance or external-PostgreSQL deployments, use the host-side
`scripts/Update-Cmdb.ps1` or `scripts/update-cmdb.sh` supplied in the verified release
bundle:

1. run its check action, which is the default and makes no deployment change;
2. review the verified target digest, manifest SHA-256, changelog, database
   classification, rollback boundary, maintenance window, and smoke-test plan;
3. confirm off-host appliance-backup retention or a tested external PostgreSQL
   PITR/backup point;
4. apply only with both the exact target version and exact manifest SHA-256 printed by
   that check;
5. require the updater's version, digest, schema, schema-history, and readiness
   validation to pass;
6. verify authentication, tenant isolation, one asset, one relationship, one report,
   integration/notification queues, and the worker heartbeat; and
7. retain the recovery point and monitor application/database state through the
   acceptance period.

External PostgreSQL apply also requires a structured, non-secret recovery reference,
for example `change:CHG-12345`. Update history stores only its SHA-256 and limited
metadata, never the raw reference. The POSIX form uses
`--recovery-evidence-ref`; PowerShell uses `-RecoveryPointEvidence`.

Both updaters inspect the existing Compose project and require `-WorkerSplit` or
`--worker-split` to match the presence of a worker container exactly. A topology
mismatch fails before any mutation. They preserve the worker's initial state: a running
worker is stopped before web and restarted after readiness, while an intentionally
stopped worker stays stopped.

If the target digest is already configured, check/apply succeeds only when the
container is healthy and readiness matches the target version, digest, schema, and
schema-history checksum. A mismatch is a repair incident: inspect `/api/ready` and
recreate or repair the exact approved image rather than claiming the update succeeded.
For a different digest, apply refuses unavailable or invalid current schema metadata
and rejects schema downgrade before pulling or stopping services.

The application container does not receive the Docker socket and cannot update itself.
Scheduled jobs may run check-only mode and send the result to operations monitoring;
they must never contain an apply approval. Do not use Watchtower or another unattended
container updater, and do not deploy mutable `latest`.

The updater stops every selected application writer before the final appliance backup,
then executes `python scripts/migrate_postgres.py` in one ephemeral target-image
container. Migrations serialize on a PostgreSQL advisory lock and are bounded by these
defaults:

```dotenv
CMDB_MIGRATION_LOCK_TIMEOUT_MS=60000
CMDB_MIGRATION_STATEMENT_TIMEOUT_MS=900000
```

The lock timeout may be configured from 1 second to 15 minutes and the statement timeout
from 1 second to 60 minutes. A timeout stops the update for investigation; it is not a
reason to change or skip an applied migration. Never change the contents of an applied
migration. Add a new migration with a later ordered version.

The web container's `/api/ready` gate compares the live database's complete
version/checksum history with the target image and exposes its deterministic
`schemaHistorySha256`. A dedicated worker exposes no HTTP port, so its image-level HTTP
health check is disabled. Treat a running worker process plus a fresh durable heartbeat
and recent success as its health signal. The updater restarts a previously running
worker only after exact web readiness succeeds.

### Failure and rollback

Before migration begins, the Compose updater can restore the previous environment and
restart the previous application image. Once the migration command starts, automatic
image rollback is disabled because a failed command may already have committed schema
work.

The current repository enforces exact migration history. Therefore any schema-version
change requires restoring the pre-update database backup/PITR point before starting an
older image, even when the SQL change was operationally classified as expand-only. An
image-only rollback may be reviewed only when the manifest says `database.changeClassification`
is `none` and the schema version/history are confirmed unchanged. Keep workers stopped
during database recovery.

PostgreSQL major-version upgrades are separate rehearsed database operations; the
application updater never pulls, recreates, or upgrades PostgreSQL. Azure deployments
use a staged Container Apps revision rather than the Compose scripts, but must enforce
the same manifest, recovery-point, migration, readiness, and rollback boundaries.

## Portable export/import

The root Database and recovery screen can download a checksum-protected operational export and preview it before import.

Portable exports:

- support controlled migration or merge between IPT CMDB installations;
- contain operational and user-related data and must be encrypted at rest;
- do not delete target records that are absent from the file;
- exclude database roles, grants, immutable audit history and full transaction history;
- are not a replacement for PostgreSQL backup and recovery.

## PostgreSQL backup and recovery

For Azure Database for PostgreSQL, configure and test point-in-time restore according to the organisation's RPO/RTO. For self-managed PostgreSQL, use independently scheduled `pg_dump`/`pg_restore` or physical backup tooling.

A recovery exercise should verify:

1. a new database/server can be restored;
2. the application can connect with least-privilege credentials;
3. migration history is accepted;
4. customer, CI, relationship, change and branding counts are plausible;
5. login and tenant authorization still work;
6. generated change documents remain available.

Azure PostgreSQL restores create a new server rather than overwriting the source. Plan DNS/connection-string cutover and reapply required network and high-availability settings during the runbook. See [Microsoft's backup and restore guidance](https://learn.microsoft.com/azure/postgresql/backup-restore/concepts-backup-restore).

The compact appliance provides checksum-producing `pg_dump` and destructive, explicitly
invoked `pg_restore` tool containers. See the [appliance runbook](deployment/APPLIANCE.md)
for scheduling, restore rehearsal, and external-database cutover.

## Local Docker operations

```powershell
docker compose ps
docker compose logs --tail 200 cmdb
docker compose logs --tail 200 postgres
docker compose restart cmdb
```

`restart` restarts the currently configured container; it does not adopt a newly pulled
image. Use it only for same-image incident recovery, not as an upgrade mechanism.

`docker compose down` preserves the named database volume. Removing the volume destroys the local PostgreSQL data and should only be done for an intentional demo reset.

## Incident notes

Capture the image tag/digest, schema version, hosting revision, affected tenant, correlation time and relevant sanitized logs. Do not paste database URLs, provider tokens, Easy Auth principal payloads, customer exports or Passportal content into public issues.
