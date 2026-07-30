#!/bin/sh
set -eu

admin_email="${1:-}"
instance_name="${2:-cmdb}"
public_base_url="${3:-}"
image="${4:-}"
port="${5:-3000}"

case "$admin_email" in
  *@*.*) ;;
  *)
    printf 'Usage: %s admin@example.com [instance-name] https://cmdb.example.com immutable-image [port]\n' "$0" >&2
    exit 2
    ;;
esac
case "$instance_name" in
  ''|*[!a-z0-9-]*)
    printf 'Instance name may contain lowercase letters, numbers, and hyphens only.\n' >&2
    exit 2
    ;;
esac
if ! printf '%s' "$public_base_url" | grep -Eq '^https://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:[0-9]{1,5})?/?$' &&
  ! printf '%s' "$public_base_url" | grep -Eq '^http://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?/?$'; then
  printf 'Public base URL must be an HTTPS origin. HTTP is allowed only for an explicit loopback origin.\n' >&2
  exit 2
fi
public_base_url="${public_base_url%/}"
if ! printf '%s' "$image" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]+)?(/[A-Za-z0-9][A-Za-z0-9._-]*)*((:v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?)?@sha256:[0-9A-Fa-f]{64}|:v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?)$'; then
  printf 'Image must use an immutable release tag such as :v1.2.3 or a sha256 digest. Mutable tags such as :latest are not accepted.\n' >&2
  exit 2
fi
case "$public_base_url" in
  http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]:*|\
  https://localhost|https://localhost:*|https://127.0.0.1|https://127.0.0.1:*|https://\[::1\]|https://\[::1\]:*)
    ;;
  *)
    if ! printf '%s' "$image" | grep -Eq '@sha256:[0-9A-Fa-f]{64}$'; then
      printf 'A non-loopback deployment must pin the image by sha256 digest. Version tags are allowed only for explicit loopback evaluation.\n' >&2
      exit 2
    fi
    ;;
esac
image_digest="$(printf '%s' "$image" | sed -n 's/^.*@\(sha256:[0-9A-Fa-f]\{64\}\)$/\1/p')"
image_digest="${image_digest:-unknown}"
command -v openssl >/dev/null 2>&1 || {
  printf 'openssl is required to generate deployment secrets.\n' >&2
  exit 2
}
case "$port" in
  ''|*[!0-9]*)
    printf 'Port must be a number from 1 to 65535.\n' >&2
    exit 2
    ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
  printf 'Port must be a number from 1 to 65535.\n' >&2
  exit 2
fi

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
instance_path="${repository_root}/.appliance/${instance_name}"
secret_path="${instance_path}/secrets"
backup_path="${instance_path}/backups"
environment_path="${instance_path}/.env.appliance"

if [ -e "$instance_path" ]; then
  printf 'The appliance instance path already exists; existing directories and secrets are never replaced: %s\n' "$instance_path" >&2
  exit 2
fi

instance_parent="$(dirname -- "$instance_path")"
mkdir -p "$instance_parent"
staging_path="$(mktemp -d "${instance_parent}/${instance_name}-initializing.XXXXXX")"
cleanup() {
  if [ -n "${staging_path:-}" ] && [ -d "$staging_path" ]; then
    rm -rf -- "$staging_path"
  fi
}
trap cleanup EXIT HUP INT TERM
secret_path="${staging_path}/secrets"
backup_path="${staging_path}/backups"
environment_path="${staging_path}/.env.appliance"
mkdir -p "$secret_path" "$backup_path"
chmod 700 "$staging_path" "$secret_path" "$backup_path"

postgres_password="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')"
bootstrap_password="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')"
mfa_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"

printf '%s' "$postgres_password" > "${secret_path}/postgres-password.txt"
printf 'postgresql://cmdb:%s@postgres:5432/cmdb?sslmode=disable' "$postgres_password" > "${secret_path}/database-url.txt"
printf '%s' "$bootstrap_password" > "${secret_path}/bootstrap-admin-password.txt"
printf '%s' "$mfa_key" > "${secret_path}/mfa-encryption-key.txt"
chmod 444 "$secret_path"/*

cat > "$environment_path" <<EOF
COMPOSE_PROJECT_NAME=cmdb-${instance_name}
CMDB_IMAGE=${image}
CMDB_IMAGE_DIGEST=${image_digest}
CMDB_BIND_ADDRESS=127.0.0.1
CMDB_PORT=${port}
CMDB_MIGRATION_LOCK_TIMEOUT_MS=60000
CMDB_MIGRATION_STATEMENT_TIMEOUT_MS=900000
PUBLIC_BASE_URL=${public_base_url}
CMDB_SECRET_DIRECTORY=${instance_path}/secrets
CMDB_BACKUP_DIRECTORY=${instance_path}/backups
BOOTSTRAP_ADMIN_EMAIL=$(printf '%s' "$admin_email" | tr '[:upper:]' '[:lower:]')
AUTH_MODE=local
FORWARDED_ALLOW_IPS=
LOCAL_MFA_POLICY=all
LOCAL_LOGIN_IDENTIFIER_LIMIT=5
LOCAL_LOGIN_IDENTIFIER_WINDOW_SECONDS=900
LOCAL_LOGIN_SOURCE_LIMIT=20
LOCAL_LOGIN_SOURCE_WINDOW_SECONDS=900
LOCAL_LOGIN_PENDING_TTL_SECONDS=120
LOCAL_LOGIN_THROTTLE_AUDIT_SECONDS=300
CMDB_APP_CPU_LIMIT=1.0
CMDB_APP_MEMORY_LIMIT=1g
CMDB_POSTGRES_CPU_LIMIT=1.0
CMDB_POSTGRES_MEMORY_LIMIT=1g
EOF
chmod 600 "$environment_path"

if [ -e "$instance_path" ]; then
  printf 'The appliance instance path appeared while initialization was in progress: %s\n' "$instance_path" >&2
  exit 2
fi
mv "$staging_path" "$instance_path"
staging_path=''
trap - EXIT HUP INT TERM

secret_path="${instance_path}/secrets"
environment_path="${instance_path}/.env.appliance"
printf 'Appliance configuration created: %s\n' "$environment_path"
printf 'Bootstrap administrator: %s\n' "$(printf '%s' "$admin_email" | tr '[:upper:]' '[:lower:]')"
printf 'Bootstrap password file: %s\n' "${secret_path}/bootstrap-admin-password.txt"
printf 'The first login requires authenticator enrollment. Do not publish port %s directly.\n' "$port"
printf 'Start with: docker compose --env-file %s -f compose.appliance.yml up -d --wait\n' "$environment_path"
