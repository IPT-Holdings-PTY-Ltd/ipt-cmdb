# Self-hosted container deployment

This profile runs the application on a controlled Docker/Compose host and uses an
external PostgreSQL service. It is suitable for a VM, a small private cloud, or a
single container host. The included `docker-compose.yml` remains development-only.

For a one-command application plus PostgreSQL installation, use the
[compact Docker appliance](APPLIANCE.md) instead.

## Prerequisites

- Docker Engine/Desktop with Compose v2
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
set absolute paths for both secret files. Apply host ACLs so only the deployment
account can read them.

The default bind address is `127.0.0.1`, intentionally preventing direct remote
access. If the reverse proxy runs on another host or container network, bind only to
the required private interface and restrict the port with a firewall.

Validate the rendered model before starting it:

```powershell
docker compose --env-file .env.production -f compose.production.yml config --quiet
docker compose --env-file .env.production -f compose.production.yml pull
docker compose --env-file .env.production -f compose.production.yml up -d
```

## 4. Configure the identity proxy

The proxy must:

1. authenticate against your Entra/OIDC tenant;
2. enforce MFA/Conditional Access at the identity provider;
3. remove inbound identity headers before forwarding;
4. set a verified email header;
5. proxy only authenticated traffic to port 3000;
6. terminate HTTPS and set forwarded host/protocol headers.

If the proxy emits `X-Forwarded-Email`, set this in `.env.production`:

```dotenv
ENTRA_PRINCIPAL_NAME_HEADER=x-forwarded-email
```

Do not expose `AUTH_MODE=easy_auth` directly to users. A forged header would otherwise
be treated as an authenticated identity.

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

1. Take or verify a PostgreSQL recovery point.
2. Change `CMDB_IMAGE` to the new immutable tag/digest.
3. Pull, inspect the Compose config, and recreate the service.
4. Watch `/api/ready` and container logs.
5. Run login, tenant, asset-read, and report smoke tests.

Reverting the image does not revert forward-only database migrations. Consult
`docs/OPERATIONS.md` before an application or database rollback.

## Secret-store note

Compose mounts the secret files at `/run/secrets`. In non-Swarm Compose, protection
ultimately depends on host filesystem ACLs; it is not a substitute for an external
secret manager. Never commit `.env.production` or either secret file.
