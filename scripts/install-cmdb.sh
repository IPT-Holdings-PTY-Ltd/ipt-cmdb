#!/bin/sh
set -eu

admin_email="${1:-}"
instance_name="${2:-cmdb}"
public_base_url="${3:-}"
image="${4:-}"
port="${5:-3000}"
wait_seconds="${CMDB_INSTALL_WAIT_SECONDS:-180}"

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
compose_file="${repository_root}/compose.appliance.yml"
initializer="${repository_root}/scripts/initialize-appliance.sh"
instance_path="${repository_root}/.appliance/${instance_name}"
environment_path="${instance_path}/.env.appliance"
password_path="${instance_path}/secrets/bootstrap-admin-password.txt"

usage() {
  printf 'Usage: %s admin@example.com [instance-name] https://cmdb.example.com [immutable-image] [port]\n' "$0" >&2
}

manifest_image() {
  json_manifest="${repository_root}/release-manifest.json"
  if [ -f "$json_manifest" ]; then
    reference=''
    if command -v python3 >/dev/null 2>&1; then
      reference="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["image"]["reference"])' "$json_manifest" 2>/dev/null || true)"
    elif command -v python >/dev/null 2>&1; then
      reference="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["image"]["reference"])' "$json_manifest" 2>/dev/null || true)"
    else
      reference="$(sed -n 's/^[[:space:]]*"reference":[[:space:]]*"\([^"]*\)".*$/\1/p' "$json_manifest" | head -n 1)"
    fi
    if ! printf '%s' "$reference" | grep -Eq '@sha256:[0-9A-Fa-f]{64}$'; then
      printf 'The release manifest does not contain a valid immutable image reference: %s\n' "$json_manifest" >&2
      return 2
    fi
    printf '%s' "$reference"
    return 0
  fi

  # Transitional compatibility for release bundles created before manifest v1.
  manifest="${repository_root}/release-manifest.txt"
  [ -f "$manifest" ] || return 1
  container="$(sed -n 's/^Container:[[:space:]]*//p' "$manifest" | head -n 1)"
  digest="$(sed -n 's/^Digest:[[:space:]]*//p' "$manifest" | head -n 1)"
  if [ -z "$container" ] || ! printf '%s' "$digest" | grep -Eq '^sha256:[0-9A-Fa-f]{64}$'; then
    printf 'The legacy release manifest does not contain a valid Container and Digest: %s\n' "$manifest" >&2
    return 2
  fi
  printf '%s@%s' "$container" "$digest"
}

case "$admin_email" in
  *@*.*) ;;
  *)
    usage
    exit 2
    ;;
esac
case "$instance_name" in
  ''|*[!a-z0-9-]*)
    usage
    exit 2
    ;;
esac
case "$port" in
  ''|*[!0-9]*)
    usage
    exit 2
    ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
  usage
  exit 2
fi
case "$wait_seconds" in
  ''|*[!0-9]*)
    printf 'CMDB_INSTALL_WAIT_SECONDS must be a number from 30 to 900.\n' >&2
    exit 2
    ;;
esac
if [ "$wait_seconds" -lt 30 ] || [ "$wait_seconds" -gt 900 ]; then
  printf 'CMDB_INSTALL_WAIT_SECONDS must be a number from 30 to 900.\n' >&2
  exit 2
fi
if [ -z "$image" ]; then
  image="$(manifest_image)" || {
    printf 'An immutable image is required when release-manifest.json is not present beside the release bundle.\n' >&2
    exit 2
  }
  printf 'Using the immutable container digest from the release manifest: %s\n' "$image"
fi

if [ -e "$instance_path" ]; then
  printf 'The appliance instance path already exists and its secrets will not be regenerated: %s\n' "$instance_path" >&2
  exit 2
fi
command -v docker >/dev/null 2>&1 || {
  printf 'Docker with the Compose v2 plugin is required.\n' >&2
  exit 2
}
compose_version="$(docker compose version --short 2>/dev/null || true)"
compose_major="$(printf '%s' "$compose_version" | sed 's/^v//' | cut -d. -f1)"
case "$compose_major" in
  ''|*[!0-9]*)
    printf 'Docker with the Compose v2 plugin is required.\n' >&2
    exit 2
    ;;
esac
if [ "$compose_major" -lt 2 ]; then
  printf 'Docker Compose v2 or newer is required; detected %s.\n' "$compose_version" >&2
  exit 2
fi

sh "$initializer" "$admin_email" "$instance_name" "$public_base_url" "$image" "$port"

compose() {
  docker compose --env-file "$environment_path" -f "$compose_file" "$@"
}

diagnostics() {
  compose ps >&2 || true
  printf 'Inspect sanitized runtime diagnostics with:\n' >&2
  printf 'docker compose --env-file "%s" -f "%s" logs --tail 200 cmdb postgres\n' \
    "$environment_path" "$compose_file" >&2
  printf 'Do not rerun the installer. Correct the issue and resume with:\n' >&2
  printf 'docker compose --env-file "%s" -f "%s" up -d --wait\n' \
    "$environment_path" "$compose_file" >&2
}

if ! compose config --quiet; then
  diagnostics
  exit 1
fi
if ! compose pull cmdb postgres; then
  diagnostics
  exit 1
fi
if ! compose up -d --wait --wait-timeout "$wait_seconds"; then
  diagnostics
  exit 1
fi

probe() {
  path="$1"
  compose exec -T cmdb python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000${path}', timeout=3).read()" \
    >/dev/null 2>&1
}

wait_for() {
  path="$1"
  attempts=$((wait_seconds / 2))
  [ "$attempts" -gt 0 ] || attempts=1
  while [ "$attempts" -gt 0 ]; do
    if probe "$path"; then
      return 0
    fi
    attempts=$((attempts - 1))
    sleep 2
  done
  return 1
}

if ! wait_for /api/live || ! wait_for /api/ready; then
  diagnostics
  exit 1
fi

printf '\nIPT CMDB is live at http://127.0.0.1:%s\n' "$port"
printf 'Configured public origin: %s\n' "$public_base_url"
printf 'Bootstrap administrator: %s\n' "$(printf '%s' "$admin_email" | tr '[:upper:]' '[:lower:]')"
printf 'Bootstrap password file: %s\n' "$password_path"
printf 'The password value was not printed. Sign in, enroll MFA, and rotate the bootstrap password.\n'
printf 'Keep the instance secret directory protected and configure verified off-host backups.\n'
