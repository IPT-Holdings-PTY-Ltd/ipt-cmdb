# Operations, upgrades and recovery

## Health and readiness

`GET /api/health` returns the API state, repository mode, expected schema version and authentication mode. A healthy production response should identify PostgreSQL and `canonical_postgresql` without a repository error.

Use platform health probes against `/api/v2/health`. Treat database/schema failures as unavailable; do not route production traffic to a local fallback.

## Routine checks

- Container restart and failure count
- API 5xx rate and latency
- PostgreSQL storage, connections, locks and replication/backup state
- Schema version against the packaged expected version
- Integration last-run status and age
- Reconciliation backlog
- CIs with stale observations, missing owners or no relationships
- Upcoming renewals and end-of-life dates

The MSP dashboard exposes the last five categories as operational work queues.

## Upgrade procedure

1. Review `CHANGELOG.md` and new files under `db/migrations`.
2. Run the full automated test suite.
3. Run blank-bootstrap and forward-upgrade verification against PostgreSQL.
4. Take or verify a recoverable production backup/PITR point.
5. Deploy the image to a staging revision.
6. Confirm health, authentication and tenant isolation.
7. Promote the revision and monitor logs and database state.

Never change the contents of an applied migration. Add a new migration with a later ordered version.

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

## Local Docker operations

```powershell
docker compose ps
docker compose logs --tail 200 cmdb
docker compose logs --tail 200 postgres
docker compose restart cmdb
```

`docker compose down` preserves the named database volume. Removing the volume destroys the local PostgreSQL data and should only be done for an intentional demo reset.

## Incident notes

Capture the image tag/digest, schema version, hosting revision, affected tenant, correlation time and relevant sanitized logs. Do not paste database URLs, provider tokens, Easy Auth principal payloads, customer exports or Passportal content into public issues.
