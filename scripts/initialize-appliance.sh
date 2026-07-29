#!/bin/sh
set -eu

admin_email="${1:-}"
instance_name="${2:-cmdb}"
image="${3:-ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:latest}"
port="${4:-3000}"

case "$admin_email" in
  *@*.*) ;;
  *)
    printf 'Usage: %s admin@example.com [instance-name] [image] [port]\n' "$0" >&2
    exit 2
    ;;
esac
case "$instance_name" in
  ''|*[!a-z0-9-]*)
    printf 'Instance name may contain lowercase letters, numbers, and hyphens only.\n' >&2
    exit 2
    ;;
esac
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

if [ -e "$environment_path" ]; then
  printf 'Configuration already exists: %s\n' "$environment_path" >&2
  exit 2
fi
mkdir -p "$secret_path" "$backup_path"
chmod 700 "$instance_path" "$secret_path" "$backup_path"

postgres_password="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')"
bootstrap_password="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')"
mfa_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"

printf '%s' "$postgres_password" > "${secret_path}/postgres-password.txt"
printf 'postgresql://cmdb:%s@postgres:5432/cmdb?sslmode=disable' "$postgres_password" > "${secret_path}/database-url.txt"
printf '%s' "$bootstrap_password" > "${secret_path}/bootstrap-admin-password.txt"
printf '%s' "$mfa_key" > "${secret_path}/mfa-encryption-key.txt"
chmod 600 "$secret_path"/*

cat > "$environment_path" <<EOF
COMPOSE_PROJECT_NAME=cmdb-${instance_name}
CMDB_IMAGE=${image}
CMDB_BIND_ADDRESS=127.0.0.1
CMDB_PORT=${port}
CMDB_SECRET_DIRECTORY=${secret_path}
CMDB_BACKUP_DIRECTORY=${backup_path}
BOOTSTRAP_ADMIN_EMAIL=${admin_email}
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

printf 'Appliance configuration created: %s\n' "$environment_path"
printf 'Bootstrap administrator: %s\n' "$admin_email"
printf 'Bootstrap password file: %s\n' "${secret_path}/bootstrap-admin-password.txt"
printf 'The first login requires authenticator enrollment. Do not publish port %s directly.\n' "$port"
printf 'Start with: docker compose --env-file %s -f compose.appliance.yml up -d\n' "$environment_path"
