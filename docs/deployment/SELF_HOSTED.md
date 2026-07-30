# Self-hosted container deployment

This profile runs the application on a controlled Docker/Compose host and uses an
external PostgreSQL service. It is suitable for a VM, a small private cloud, or a
single container host. The included `docker-compose.yml` remains development-only.

For a one-command application plus PostgreSQL installation, use the
[compact Docker appliance](APPLIANCE.md) instead.

## Prerequisites

- Docker Engine/Desktop with Compose v2
- PowerShell 7, or a POSIX shell with `curl`, `sha256sum`/`shasum`, and Python 3 or
  `jq`, for the host-side updater
- PostgreSQL 16 reachable from the container host
- A trusted OIDC-aware HTTPS reverse proxy (for example oauth2-proxy plus Caddy,
  Traefik, or Nginx)
- An immutable IPT CMDB image tag or digest
- A protected host directory for secret files

## 1. Prepare PostgreSQL

Create an empty `cmdb` database and a dedicated login with ownership/DDL rights. The
connection must require TLS across an untrusted network. Save only the URL in a
protected file, for example:

```text
postgresql://cmdb_app:REDACTED@postgres.example.internal:5432/cmdb?sslmode=require
```

Do not add a trailing comment or additional values to the file.

## 2. Generate the stable MFA encryption key

Run once and store the output in a second protected secret file:

```powershell
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Keep this value stable across upgrades and replicas. Losing it prevents enrolled local
break-glass users from decrypting their TOTP seeds.

Generate a separate random bootstrap administrator password and save it in the file
referenced by `BOOTSTRAP_ADMIN_PASSWORD_SECRET_FILE`. Set `BOOTSTRAP_ADMIN_EMAIL` to the
email that should receive the first platform-administrator mapping on a blank database.

## 3. Configure the deployment

Copy `.env.production.example` to `.env.production`, select an immutable image, and
set absolute paths for all secret files. On Linux, keep them in a deployment-owned
directory that is `0700`, then make only the individual files read-only after writing
them. Other host users cannot traverse the parent directory, while native Docker
Engine can bind-mount the files for the non-root CMDB process:

```sh
secret_directory=/srv/ipt-cmdb/secrets
chmod 0700 "$secret_directory"
chmod 0444 "$secret_directory"/*.txt
```

Rotate a value by writing a protected temporary file and atomically replacing the old
file, then restore mode `0444`. Docker Desktop users should retain protected Windows
parent ACLs instead of applying POSIX modes.

The default bind address is `127.0.0.1`, intentionally preventing direct remote
access. If the reverse proxy runs on another host or container network, bind only to
the required private interface and restrict the port with a firewall.

Set `FORWARDED_ALLOW_IPS` to the exact address or smallest CIDR used by the immediate
reverse proxy as seen from the application container. Leave it blank when clients
connect directly. Never use `*` or an all-address CIDR: untrusted forwarding headers
would otherwise let callers evade requester throttling and falsify audit evidence.
The local-login defaults are five failures per identifier and twenty per resolved
source over fifteen minutes; adjust the `LOCAL_LOGIN_*` values only after reviewing
NAT/shared-office traffic and alert volume.

Validate the rendered model before starting it:

```powershell
docker compose --env-file .env.production -f compose.production.yml config --quiet
docker compose --env-file .env.production -f compose.production.yml pull
docker compose --env-file .env.production -f compose.production.yml up -d
```

After Microsoft 365 email is configured and verified, unattended ConnectWise and
N-central previews can be enabled in `.env.production`:

```text
INTEGRATION_WORKER_ENABLED=true
INTEGRATION_WORKER_INTERVAL_SECONDS=2
NOTIFICATION_WORKER_ENABLED=true
NOTIFICATION_WORKER_INTERVAL_SECONDS=60
INTEGRATION_ALERT_RECIPIENTS=integration-ops@example.com
```

The alert recipient list is optional; active platform administrators are the fallback.
The integration worker remains read-only and only refreshes the governed review queue.

Small installations can keep these jobs in the application container. To isolate them,
first start the web service and verify `/api/ready`, then apply `compose.worker.yml`:

```powershell
docker compose --env-file .env.production `
  -f compose.production.yml -f compose.worker.yml up -d
```

The overlay preserves the same immutable image and PostgreSQL data while changing the
web process to API-only mode. See the [worker runbook](WORKERS.md) for one-shot
execution, monitoring and rollback.

## 4. Configure the identity proxy

The proxy must:

1. authenticate against your Entra/OIDC tenant;
2. enforce MFA/Conditional Access at the identity provider;
3. remove inbound identity headers before forwarding;
4. set a verified email header;
5. proxy only authenticated traffic to port 3000;
6. terminate HTTPS and set forwarded host/protocol headers.
7. append (rather than copy blindly) the client address in `X-Forwarded-For`.

If the proxy emits `X-Forwarded-Email`, set this in `.env.production`:

```dotenv
ENTRA_PRINCIPAL_NAME_HEADER=x-forwarded-email
```

Do not expose `AUTH_MODE=easy_auth` directly to users. A forged header would otherwise
be treated as an authenticated identity.

After proxy configuration, submit two safe failed logins through the public route and
confirm their audit events show the same canonical proxy-resolved client address. A
request sent directly to the bound application port with a forged forwarding header
must not change that address.

## 5. Verify

From the container host:

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/live
Invoke-RestMethod http://127.0.0.1:3000/api/ready
docker compose --env-file .env.production -f compose.production.yml ps
docker compose --env-file .env.production -f compose.production.yml exec cmdb id
```

The first two calls must return `alive` and `ready`; `id` must show the unprivileged
`cmdb` account. Then verify the public HTTPS URL redirects to the identity provider and
an unmapped Entra user is denied by the CMDB.

## Upgrade and rollback

Use the host-side updater included in each release bundle. With
`.env.production` in the current directory, its default check-only operation is:

```powershell
.\scripts\Update-Cmdb.ps1 -Action Check -InstanceRoot .
```

On Linux, the POSIX updater detects the same `.env.production` external-database
profile:

```bash
./scripts/update-cmdb.sh --check --instance-root .
```

Check downloads the latest stable release manifest and `SHA256SUMS`, verifies their
checksum and immutable digest, and reports the version, manifest SHA-256, schema,
schema-history checksum, database classification, and rollback policy without changing
the deployment. An offline check can use `-ManifestPath` with an extracted release
bundle that still contains both files; on Linux use
`--manifest ./release-manifest.json`.

Before applying, create or verify a PostgreSQL point-in-time recovery point and record a
structured, non-secret evidence reference. Apply requires that evidence plus both the
exact target version and manifest SHA-256 printed by the same `Check`:

```powershell
.\scripts\Update-Cmdb.ps1 `
  -Action Apply `
  -InstanceRoot . `
  -ApproveVersion '0.5.0' `
  -ApproveManifestSha256 '<64-lowercase-hex-characters>' `
  -RecoveryPointEvidence 'change:CHG-12345'
```

On Linux:

```bash
./scripts/update-cmdb.sh \
  --apply \
  --instance-root . \
  --approved-version 0.5.0 \
  --approved-manifest-sha256 '<64-lowercase-hex-characters>' \
  --recovery-evidence-ref 'change:CHG-12345'
```

The POSIX evidence type must be one of `ticket`, `change`, `backup`, `snapshot`, `pitr`,
`restore`, or `runbook`, followed by a colon and a non-secret reference. Update history
records only the evidence type and SHA-256. PowerShell records only presence, length,
and SHA-256. Neither updater stores or prints the raw external recovery reference.

The updater detects the existing Compose worker container and requires the operator's
topology assertion to match it. Add `-WorkerSplit` on PowerShell or `--worker-split` on
Linux exactly when that container exists. A mismatch fails closed. A worker that was
running is stopped before web and restarted only after readiness; one that was already
stopped remains stopped.

The updater pre-pulls the exact target, stops the selected worker and web writers,
accepts the external recovery-point evidence, atomically updates the image digest, runs
one target-image migration container, and requires web `/api/ready` to report the
expected version, digest, schema, and schema-history checksum. It never pulls or
recreates the external PostgreSQL service.

If the target digest is already configured, check/apply returns success only after the
container is healthy and exact readiness metadata matches. If it does not, keep the
instance under review and repair or recreate the exact approved image before checking
again. For a different digest, invalid or unavailable current schema metadata and any
schema downgrade are rejected before pull or service stop.

Migration lock acquisition defaults to 60 seconds and each migration transaction to 15
minutes through `CMDB_MIGRATION_LOCK_TIMEOUT_MS` and
`CMDB_MIGRATION_STATEMENT_TIMEOUT_MS`. A final readiness wait defaults to 180 seconds
and can be changed with `-WaitSeconds`.

Before migration starts, the updater can restore the previous environment and restart
the old image. After migration starts it deliberately does not perform an automatic
image rollback. Any schema-version change currently requires restoring the reviewed
database recovery point before starting an older image. A reviewed image-only rollback
is allowed only when the manifest declares no database change and the migration history
is confirmed unchanged.

Run update checks on a schedule if useful, but route the output to monitoring and leave
`Apply` as a human-approved maintenance action. Do not give the application container a
Docker socket, use Watchtower, schedule an apply command, or deploy `latest`.
PostgreSQL major upgrades remain a separate provider-specific operation. See
[Operations, upgrades and recovery](../OPERATIONS.md) for the shared validation and
recovery procedure.

## Secret-store note

Compose mounts the secret files at `/run/secrets`. In non-Swarm Compose, protection
ultimately depends on host filesystem ACLs; it is not a substitute for an external
secret manager. Never commit `.env.production` or either secret file.
