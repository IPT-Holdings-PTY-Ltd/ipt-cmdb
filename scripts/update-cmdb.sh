#!/bin/sh
# Safely check or apply an IPT CMDB appliance image update.
set -eu

umask 077

action=check
manifest_source=latest
manifest_path=''
checksums_path=''
instance_name=cmdb
instance_root=''
worker_split=false
wait_seconds=180
approved_version=''
approved_manifest_sha256=''
recovery_evidence=''
github_repository="${CMDB_GITHUB_REPOSITORY:-IPT-Holdings-PTY-Ltd/ipt-cmdb}"

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
compose_file=''
deployment_mode=''
environment_filename=''

temporary_directory=''
lock_directory=''
environment_backup=''
evidence_file=''
apply_started=false
environment_changed=false
environment_restore_verified=false
migration_started=false
cmdb_stopped=false
worker_stopped=false
initial_worker_running=false
update_succeeded=false

usage() {
  cat >&2 <<'EOF'
Usage: update-cmdb.sh [options]

Default action:
  --check                         Validate and report only; make no instance changes.

Update source:
  --latest                        Use the latest stable GitHub release (default).
  --manifest PATH                 Use a local release-manifest.json.
  --checksums PATH                SHA256SUMS for --manifest (defaults beside it).

Instance:
  --instance-name NAME            Instance name (default: cmdb).
  --instance-root PATH            Existing appliance or external-DB instance root.
  --worker-split                  Manage the compose.worker.yml worker service.
  --timeout SECONDS               Readiness timeout from 30 to 900 (default: 180).

Apply gate:
  --apply                         Apply the validated update.
  --approved-version VERSION      Exact manifest version approved for --apply.
  --approved-manifest-sha256 HASH Exact reviewed release-manifest SHA-256.
  --recovery-evidence-ref REF     Structured TYPE:REFERENCE recovery evidence.
  --recovery-evidence REF         Compatibility alias for the structured reference.
                                  Optional for an appliance; required externally.

Other:
  -h, --help                      Show this help.

Exit status: 0 success/check complete, 1 validation or operational failure, 2 usage error.
EOF
}

usage_error() {
  printf 'Usage error: %s\n\n' "$*" >&2
  usage
  exit 2
}

fail() {
  printf 'Update error: %s\n' "$*" >&2
  exit 1
}

require_argument() {
  option="$1"
  value="${2:-}"
  [ -n "$value" ] || usage_error "$option requires a value."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      action=check
      ;;
    --apply)
      action=apply
      ;;
    --latest)
      manifest_source=latest
      manifest_path=''
      ;;
    --manifest)
      require_argument "$1" "${2:-}"
      manifest_source=local
      manifest_path="$2"
      shift
      ;;
    --checksums)
      require_argument "$1" "${2:-}"
      checksums_path="$2"
      shift
      ;;
    --instance-name)
      require_argument "$1" "${2:-}"
      instance_name="$2"
      shift
      ;;
    --instance-root)
      require_argument "$1" "${2:-}"
      instance_root="$2"
      shift
      ;;
    --worker-split)
      worker_split=true
      ;;
    --timeout)
      require_argument "$1" "${2:-}"
      wait_seconds="$2"
      shift
      ;;
    --approved-version|--approve-version)
      require_argument "$1" "${2:-}"
      approved_version="$2"
      shift
      ;;
    --approved-manifest-sha256|--approve-manifest-sha256)
      require_argument "$1" "${2:-}"
      approved_manifest_sha256="$2"
      shift
      ;;
    --recovery-evidence|--recovery-evidence-ref)
      require_argument "$1" "${2:-}"
      recovery_evidence="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      [ "$#" -eq 0 ] || usage_error "Positional arguments are not supported."
      break
      ;;
    -*)
      usage_error "Unknown option: $1"
      ;;
    *)
      usage_error "Positional arguments are not supported: $1"
      ;;
  esac
  shift
done

case "$instance_name" in
  ''|[!a-z0-9]*|*[!a-z0-9-]*)
    usage_error "Instance name may contain lowercase letters, numbers, and hyphens only."
    ;;
esac
[ "${#instance_name}" -le 40 ] ||
  usage_error "Instance name must not exceed 40 characters."
case "$wait_seconds" in
  ''|*[!0-9]*)
    usage_error "Timeout must be a number from 30 to 900."
    ;;
esac
if [ "$wait_seconds" -lt 30 ] || [ "$wait_seconds" -gt 900 ]; then
  usage_error "Timeout must be a number from 30 to 900."
fi
if [ "$manifest_source" = latest ] && [ -n "$checksums_path" ]; then
  usage_error "--checksums can be used only with --manifest."
fi
case "$github_repository" in
  *[!A-Za-z0-9._/-]*|''|/*|*/|*..*)
    fail "CMDB_GITHUB_REPOSITORY is invalid."
    ;;
esac
if [ "$action" = apply ]; then
  [ -n "$approved_version" ] ||
    usage_error "--approved-version is required with --apply."
  printf '%s' "$approved_manifest_sha256" | grep -Eq '^[0-9a-f]{64}$' ||
    usage_error "--approved-manifest-sha256 requires an exact lowercase SHA-256."
  if [ -n "$recovery_evidence" ]; then
    evidence_length="$(printf '%s' "$recovery_evidence" | wc -c | tr -d ' ')"
    [ "$evidence_length" -le 512 ] ||
      usage_error "Recovery evidence must not exceed 512 bytes."
    carriage_return="$(printf '\r')"
    case "$recovery_evidence" in
      *'
'*|*"$carriage_return"*)
        usage_error "Recovery evidence must be one line."
        ;;
    esac
  fi
fi

command -v docker >/dev/null 2>&1 ||
  fail "Docker with the Compose v2 plugin is required."
compose_version="$(docker compose version --short 2>/dev/null || true)"
compose_major="$(printf '%s' "$compose_version" | sed 's/^v//' | cut -d. -f1)"
case "$compose_major" in
  ''|*[!0-9]*)
    fail "Docker with the Compose v2 plugin is required."
    ;;
esac
[ "$compose_major" -ge 2 ] ||
  fail "Docker Compose v2 or newer is required; detected $compose_version."

if command -v python3 >/dev/null 2>&1; then
  json_parser=python3
elif command -v python >/dev/null 2>&1; then
  json_parser=python
elif command -v jq >/dev/null 2>&1; then
  json_parser=jq
else
  fail "python3, python, or jq is required to validate release metadata."
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256_file() {
    sha256sum "$1" | awk '{ print tolower($1) }'
  }
  sha256_text() {
    printf '%s' "$1" | sha256sum | awk '{ print tolower($1) }'
  }
elif command -v shasum >/dev/null 2>&1; then
  sha256_file() {
    shasum -a 256 "$1" | awk '{ print tolower($1) }'
  }
  sha256_text() {
    printf '%s' "$1" | shasum -a 256 | awk '{ print tolower($1) }'
  }
else
  fail "sha256sum or shasum is required."
fi

temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/ipt-cmdb-update.XXXXXX")" ||
  fail "Could not create a temporary update directory."

append_evidence() {
  [ -n "$evidence_file" ] || return 0
  printf '%s\n' "$1" >> "$evidence_file"
}

compose_with_environment() {
  selected_environment="$1"
  shift
  if [ "$worker_split" = true ]; then
    docker compose \
      --env-file "$selected_environment" \
      -f "$compose_file" \
      -f "$repository_root/compose.worker.yml" \
      "$@"
  else
    docker compose \
      --env-file "$selected_environment" \
      -f "$compose_file" \
      "$@"
  fi
}

compose() {
  compose_with_environment "$environment_path" "$@"
}

compose_with_worker_environment() {
  selected_environment="$1"
  shift
  docker compose \
    --env-file "$selected_environment" \
    -f "$compose_file" \
    -f "$repository_root/compose.worker.yml" \
    "$@"
}

fetch_runtime_endpoint() {
  endpoint="$1"
  request_timeout="$2"
  compose exec -T cmdb python -c \
    "import sys, urllib.request; print(urllib.request.urlopen('http://127.0.0.1:3000/api/' + sys.argv[1], timeout=float(sys.argv[2])).read().decode('utf-8'))" \
    "$endpoint" "$request_timeout" 2>/dev/null
}

fetch_readiness() {
  request_timeout="$1"
  fetch_runtime_endpoint ready "$request_timeout"
}

validate_readiness() {
  readiness_file="$1"
  if [ "$json_parser" = jq ]; then
    jq -e \
      --arg version "$target_version" \
      --arg digest "$target_digest" \
      --arg schema "$target_schema_version" \
      --arg history "$target_schema_history_checksum" '
        .status == "ready" and
        .schemaCurrent == true and
        .applicationVersion == $version and
        .imageDigest == $digest and
        .schemaVersion == $schema and
        .expectedSchemaVersion == $schema and
        .schemaHistorySha256 == $history
      ' "$readiness_file" >/dev/null 2>&1
  else
    "$json_parser" - \
      "$readiness_file" \
      "$target_version" \
      "$target_digest" \
      "$target_schema_version" \
      "$target_schema_history_checksum" <<'PY' >/dev/null 2>&1
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    ready = json.load(source)
expected_version, expected_digest, expected_schema, expected_history = sys.argv[2:]
valid = (
    ready.get("status") == "ready"
    and ready.get("schemaCurrent") is True
    and ready.get("applicationVersion") == expected_version
    and ready.get("imageDigest") == expected_digest
    and ready.get("schemaVersion") == expected_schema
    and ready.get("expectedSchemaVersion") == expected_schema
    and ready.get("schemaHistorySha256") == expected_history
)
raise SystemExit(0 if valid else 1)
PY
  fi
}

wait_for_expected_readiness() {
  readiness_file="$temporary_directory/readiness.json"
  readiness_deadline=$(($(date +%s) + wait_seconds))
  while :; do
    readiness_now="$(date +%s)"
    readiness_remaining=$((readiness_deadline - readiness_now))
    [ "$readiness_remaining" -gt 0 ] || return 1
    request_timeout=3
    if [ "$readiness_remaining" -lt "$request_timeout" ]; then
      request_timeout="$readiness_remaining"
    fi
    if fetch_readiness "$request_timeout" > "$readiness_file" &&
      validate_readiness "$readiness_file"; then
      return 0
    fi
    readiness_now="$(date +%s)"
    readiness_remaining=$((readiness_deadline - readiness_now))
    [ "$readiness_remaining" -gt 0 ] || return 1
    readiness_sleep=2
    if [ "$readiness_remaining" -lt "$readiness_sleep" ]; then
      readiness_sleep="$readiness_remaining"
    fi
    sleep "$readiness_sleep"
  done
}

runtime_expected_schema() {
  runtime_metadata_file="$1"
  if [ "$json_parser" = jq ]; then
    jq -er '.expectedSchemaVersion | select(type == "string")' \
      "$runtime_metadata_file"
  else
    "$json_parser" - "$runtime_metadata_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    metadata = json.load(source)
value = metadata.get("expectedSchemaVersion")
if not isinstance(value, str):
    raise SystemExit(1)
print(value)
PY
  fi
}

schema_is_newer() {
  current_schema="$1"
  candidate_schema="$2"
  awk -v current="$current_schema" -v candidate="$candidate_schema" '
    BEGIN {
      split(current, current_parts, ".")
      split(candidate, candidate_parts, ".")
      for (part = 1; part <= 4; part += 1) {
        current_value = current_parts[part] + 0
        candidate_value = candidate_parts[part] + 0
        if (current_value > candidate_value) {
          exit 0
        }
        if (current_value < candidate_value) {
          exit 1
        }
      }
      exit 1
    }
  '
}

restore_before_migration() {
  rollback_failed=false
  if [ "$environment_changed" = true ] && [ -f "$environment_backup" ]; then
    restore_temp="$(mktemp "$instance_root/${environment_filename}.restore.XXXXXX")" ||
      rollback_failed=true
    if [ "$rollback_failed" = false ]; then
      if cp -p "$environment_backup" "$restore_temp" &&
        mv -f "$restore_temp" "$environment_path" &&
        [ "$(sha256_file "$environment_path")" = "$environment_checksum" ]; then
        environment_changed=false
        environment_restore_verified=true
        append_evidence "environmentRestored=true"
      else
        rm -f "$restore_temp"
        rollback_failed=true
      fi
    fi
  elif [ -f "$environment_backup" ] &&
    [ "$(sha256_file "$environment_path")" = "$environment_checksum" ]; then
    environment_restore_verified=true
  fi

  if [ "$environment_changed" = false ] && [ "$cmdb_stopped" = true ]; then
    if compose up -d --no-deps --no-build --pull never cmdb; then
      cmdb_stopped=false
      append_evidence "previousCmdbRestarted=true"
    else
      rollback_failed=true
    fi
  fi
  if [ "$environment_changed" = false ] &&
    [ "$worker_split" = true ] &&
    [ "$initial_worker_running" = true ] &&
    [ "$worker_stopped" = true ]; then
    if compose up -d --no-deps --no-build --pull never worker; then
      worker_stopped=false
      append_evidence "previousWorkerRestarted=true"
    else
      rollback_failed=true
    fi
  fi
  if [ "$rollback_failed" = true ]; then
    printf 'Automatic pre-migration recovery did not complete; inspect the evidence file.\n' >&2
  else
    printf 'The previous environment and runtime were restored before migration began.\n' >&2
  fi
}

contain_after_migration_failure() {
  append_evidence "postMigrationContainmentStarted=true"
  if [ "$worker_split" = true ]; then
    if compose stop worker; then
      worker_stopped=true
      append_evidence "postMigrationWorkerStop=success"
    else
      append_evidence "postMigrationWorkerStop=failed"
    fi
  else
    append_evidence "postMigrationWorkerStop=not-configured"
  fi
  if compose stop cmdb; then
    cmdb_stopped=true
    append_evidence "postMigrationCmdbStop=success"
  else
    append_evidence "postMigrationCmdbStop=failed"
  fi
}

cleanup() {
  status="$?"
  trap - 0 1 2 15
  set +e

  if [ "$status" -ne 0 ] && [ "$apply_started" = true ]; then
    if [ "$migration_started" = false ]; then
      append_evidence "result=failed-before-migration"
      restore_before_migration
    else
      contain_after_migration_failure
      append_evidence "result=manual-recovery-required"
      printf '%s\n' \
        'Migration was started; the updater will not restore the old image automatically.' \
        'Keep CMDB and its worker stopped until the evidence and database state are reviewed.' \
        'Use the approved recovery point only through the documented recovery procedure.' >&2
    fi
  fi
  if [ "$status" -eq 0 ] && [ "$update_succeeded" = true ]; then
    append_evidence "result=success"
  fi

  if [ -n "$environment_backup" ] && [ -f "$environment_backup" ]; then
    if { [ "$status" -eq 0 ] && [ "$update_succeeded" = true ]; } ||
      [ "$environment_restore_verified" = true ]; then
      rm -f "$environment_backup"
    else
      append_evidence "environmentBackupRetained=$environment_backup"
      printf 'Protected previous environment retained: %s\n' "$environment_backup" >&2
    fi
  fi
  if [ -n "$lock_directory" ] && [ -d "$lock_directory" ]; then
    rm -f "$lock_directory/pid" "$lock_directory/version"
    rmdir "$lock_directory" 2>/dev/null || true
  fi
  [ -z "$temporary_directory" ] || rm -rf "$temporary_directory"
  exit "$status"
}

trap cleanup 0
trap 'exit 130' 1 2 15

if [ "$manifest_source" = latest ]; then
  command -v curl >/dev/null 2>&1 ||
    fail "curl is required to check the latest GitHub release."
  release_base="https://github.com/$github_repository/releases/latest/download"
  manifest_path="$temporary_directory/release-manifest.json"
  checksums_path="$temporary_directory/SHA256SUMS"
  curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    --output "$manifest_path" \
    "$release_base/release-manifest.json" ||
    fail "Could not download the latest release manifest."
  curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    --output "$checksums_path" \
    "$release_base/SHA256SUMS" ||
    fail "Could not download the latest release checksums."
else
  [ -f "$manifest_path" ] ||
    fail "The local release manifest was not found: $manifest_path"
  manifest_directory="$(CDPATH= cd -- "$(dirname -- "$manifest_path")" && pwd)"
  manifest_path="$manifest_directory/$(basename -- "$manifest_path")"
  if [ -z "$checksums_path" ]; then
    checksums_path="$manifest_directory/SHA256SUMS"
  fi
  [ -f "$checksums_path" ] ||
    fail "The release checksum file was not found: $checksums_path"
  checksums_directory="$(CDPATH= cd -- "$(dirname -- "$checksums_path")" && pwd)"
  checksums_path="$checksums_directory/$(basename -- "$checksums_path")"
fi

checksum_matches="$temporary_directory/manifest-checksums"
awk '
  $2 == "release-manifest.json" || $2 == "./release-manifest.json" {
    print tolower($1)
  }
' "$checksums_path" > "$checksum_matches"
match_count="$(wc -l < "$checksum_matches" | tr -d ' ')"
[ "$match_count" -eq 1 ] ||
  fail "SHA256SUMS must contain exactly one release-manifest.json entry."
expected_manifest_checksum="$(sed -n '1p' "$checksum_matches")"
case "$expected_manifest_checksum" in
  *[!0-9a-f]*|'')
    fail "The release manifest checksum is invalid."
    ;;
esac
[ "${#expected_manifest_checksum}" -eq 64 ] ||
  fail "The release manifest checksum is invalid."
actual_manifest_checksum="$(sha256_file "$manifest_path")"
[ "$actual_manifest_checksum" = "$expected_manifest_checksum" ] ||
  fail "The release manifest does not match SHA256SUMS."
if [ -n "$approved_manifest_sha256" ] &&
  [ "$approved_manifest_sha256" != "$actual_manifest_checksum" ]; then
  fail "Approved manifest SHA-256 does not match the verified release manifest."
fi

manifest_values="$temporary_directory/manifest-values"
if [ "$json_parser" = jq ]; then
  jq -er '
    select(
      (.prerelease | type) == "boolean" and
      (.database.backupRequired | type) == "boolean"
    ) |
    [
      .manifestVersion,
      .version,
      .tag,
      .prerelease,
      .image.name,
      .image.digest,
      .image.reference,
      .database.postgresqlMajor,
      .database.schemaVersion,
      .database.schemaHistorySha256,
      .database.backupRequired,
      .database.changeClassification,
      .database.rollbackPolicy,
      .database.migrationPolicy,
      .database.rollbackBoundary,
      .commit
    ] | @tsv
  ' "$manifest_path" > "$manifest_values" ||
    fail "The release manifest is not valid manifest v1 JSON."
else
  if ! "$json_parser" - "$manifest_path" > "$manifest_values" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    manifest = json.load(source)
if not isinstance(manifest["prerelease"], bool):
    raise TypeError("prerelease must be boolean")
if not isinstance(manifest["database"]["backupRequired"], bool):
    raise TypeError("backupRequired must be boolean")
values = [
    manifest["manifestVersion"],
    manifest["version"],
    manifest["tag"],
    manifest["prerelease"],
    manifest["image"]["name"],
    manifest["image"]["digest"],
    manifest["image"]["reference"],
    manifest["database"]["postgresqlMajor"],
    manifest["database"]["schemaVersion"],
    manifest["database"]["schemaHistorySha256"],
    manifest["database"]["backupRequired"],
    manifest["database"]["changeClassification"],
    manifest["database"]["rollbackPolicy"],
    manifest["database"]["migrationPolicy"],
    manifest["database"]["rollbackBoundary"],
    manifest["commit"],
]
if any("\t" in str(value) or "\n" in str(value) for value in values):
    raise ValueError("manifest values must be single-line")
print("\t".join(
    "true" if value is True else "false" if value is False else str(value)
    for value in values
))
PY
  then
    fail "The release manifest is not valid manifest v1 JSON."
  fi
fi

tab="$(printf '\t')"
IFS="$tab" read -r \
  manifest_version \
  target_version \
  target_tag \
  target_prerelease \
  target_image_name \
  target_digest \
  target_image \
  target_postgres_major \
  target_schema_version \
  target_schema_history_checksum \
  target_backup_required \
  target_database_change \
  target_rollback_policy \
  target_migration_policy \
  target_rollback_boundary \
  target_commit < "$manifest_values"

[ "$manifest_version" = 1 ] ||
  fail "Only release manifest version 1 is supported."
printf '%s' "$target_version" |
  grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z][0-9A-Za-z.-]*)?$' ||
  fail "The manifest version is not valid semantic versioning."
[ "$target_tag" = "v$target_version" ] ||
  fail "The manifest tag does not match its version."
case "$target_prerelease" in
  true|false) ;;
  *) fail "The manifest prerelease value must be boolean." ;;
esac
if [ "$manifest_source" = latest ] && [ "$target_prerelease" = true ]; then
  fail "The latest stable release contains a prerelease manifest."
fi
printf '%s' "$target_image_name" |
  grep -Eq '^[a-z0-9][a-z0-9._-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)*$' ||
  fail "The manifest image name is invalid."
case "$target_image_name" in
  *//*|*..*|*@*|*:latest)
    fail "The manifest image name is not an immutable repository path."
    ;;
esac
printf '%s' "$target_digest" |
  grep -Eq '^sha256:[0-9a-f]{64}$' ||
  fail "The manifest image digest is invalid."
[ "$target_image" = "$target_image_name@$target_digest" ] ||
  fail "The manifest exact image does not match its name and digest."
[ "$target_postgres_major" = 16 ] ||
  fail "The release requires PostgreSQL $target_postgres_major; this updater supports PostgreSQL 16."
printf '%s' "$target_schema_version" |
  grep -Eq '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$' ||
  fail "The manifest schema version is invalid."
printf '%s' "$target_schema_history_checksum" |
  grep -Eq '^[0-9a-f]{64}$' ||
  fail "The manifest schema-history checksum is invalid."
case "$target_backup_required" in
  true|false) ;;
  *) fail "The manifest backupRequired value must be boolean." ;;
esac
case "$target_database_change" in
  none|expand-only-compatible|restore-required) ;;
  *) fail "The manifest database change classification is unsupported." ;;
esac
case "$target_rollback_policy" in
  previous-image-compatible|database-restore-required) ;;
  *) fail "The manifest rollback policy is unsupported." ;;
esac
[ "$target_migration_policy" = forward-only ] ||
  fail "The manifest migration policy must be forward-only."
[ "$target_rollback_boundary" = schema-version-change-requires-database-restore ] ||
  fail "The manifest rollback boundary is invalid."
if [ "$target_database_change" = none ]; then
  [ "$target_backup_required" = false ] ||
    fail "The release manifest backup policy is inconsistent."
else
  [ "$target_backup_required" = true ] ||
    fail "The release manifest backup policy is inconsistent."
  [ "$target_rollback_policy" = database-restore-required ] ||
    fail "Every schema-changing release must require database restore for rollback."
fi
printf '%s' "$target_commit" | grep -Eq '^[0-9a-f]{40}$' ||
  fail "The manifest commit is not a full lowercase SHA."

if [ -n "$approved_version" ] && [ "$approved_version" != "$target_version" ]; then
  fail "Approved version $approved_version does not match manifest version $target_version."
fi

if [ -z "$instance_root" ]; then
  instance_root="$repository_root/.appliance/$instance_name"
fi
[ -d "$instance_root" ] ||
  fail "The instance root was not found: $instance_root"
instance_root="$(CDPATH= cd -- "$instance_root" && pwd -P)"

appliance_environment="$instance_root/.env.appliance"
production_environment="$instance_root/.env.production"
appliance_environment_present=false
production_environment_present=false
if [ -e "$appliance_environment" ] || [ -L "$appliance_environment" ]; then
  appliance_environment_present=true
fi
if [ -e "$production_environment" ] || [ -L "$production_environment" ]; then
  production_environment_present=true
fi
if [ "$appliance_environment_present" = "$production_environment_present" ]; then
  fail "InstanceRoot must contain exactly one of .env.appliance or .env.production."
fi
if [ "$appliance_environment_present" = true ]; then
  deployment_mode=appliance
  environment_filename=.env.appliance
  environment_path="$appliance_environment"
  compose_file="$repository_root/compose.appliance.yml"
else
  deployment_mode=external
  environment_filename=.env.production
  environment_path="$production_environment"
  compose_file="$repository_root/compose.production.yml"
fi
[ -f "$environment_path" ] ||
  fail "The selected instance environment is not a regular file: $environment_path"
[ ! -L "$environment_path" ] ||
  fail "The instance environment must not be a symbolic link."
[ -f "$compose_file" ] ||
  fail "The required Compose file was not found: $compose_file"
[ -f "$repository_root/compose.worker.yml" ] ||
  fail "The worker Compose overlay was not found."

environment_entry_count() {
  key="$1"
  grep -c "^${key}=" "$environment_path" || true
}

environment_value() {
  key="$1"
  count="$(environment_entry_count "$key")"
  [ "$count" -eq 1 ] ||
    fail "$environment_path must contain exactly one $key entry."
  sed -n "s/^${key}=//p" "$environment_path"
}

current_image="$(environment_value CMDB_IMAGE)"
printf '%s' "$current_image" |
  grep -Eq '@sha256:[0-9a-fA-F]{64}$' ||
  fail "The current CMDB_IMAGE must be pinned by digest."
image_digest_from_reference="$(
  printf '%s' "$current_image" |
    sed -n 's/^.*@\(sha256:[0-9a-fA-F]\{64\}\)$/\1/p' |
    tr 'A-F' 'a-f'
)"
digest_entry_count="$(environment_entry_count CMDB_IMAGE_DIGEST)"
digest_metadata_present=true
case "$digest_entry_count" in
  0)
    [ "$deployment_mode" = external ] ||
      fail "$environment_path must contain exactly one CMDB_IMAGE_DIGEST entry."
    current_digest="$image_digest_from_reference"
    digest_metadata_present=false
    ;;
  1)
    current_digest="$(environment_value CMDB_IMAGE_DIGEST)"
    printf '%s' "$current_digest" | grep -Eq '^sha256:[0-9a-f]{64}$' ||
      fail "The current CMDB_IMAGE_DIGEST is not an exact lowercase digest."
    [ "$image_digest_from_reference" = "$current_digest" ] ||
      fail "CMDB_IMAGE and CMDB_IMAGE_DIGEST do not match."
    ;;
  *)
    fail "$environment_path must contain at most one CMDB_IMAGE_DIGEST entry."
    ;;
esac
current_image_without_digest="${current_image%%@*}"
current_image_last_component="${current_image_without_digest##*/}"
case "$current_image_last_component" in
  *:*)
    current_image_repository="$(
      printf '%s' "$current_image_without_digest" | sed 's/:[^/:]*$//'
    )"
    ;;
  *)
    current_image_repository="$current_image_without_digest"
    ;;
esac
[ "$(printf '%s' "$current_image_repository" | tr 'A-Z' 'a-z')" = "$target_image_name" ] ||
  fail "The target manifest uses a different container repository than this instance."

backup_directory=''
if [ "$deployment_mode" = appliance ]; then
  current_project="$(environment_value COMPOSE_PROJECT_NAME)"
  backup_directory="$(environment_value CMDB_BACKUP_DIRECTORY)"
  [ "$current_project" = "cmdb-$instance_name" ] ||
    fail "COMPOSE_PROJECT_NAME does not match instance $instance_name."
  case "$backup_directory" in
    /*) ;;
    *) fail "CMDB_BACKUP_DIRECTORY must be an absolute POSIX path." ;;
  esac
  [ -d "$backup_directory" ] ||
    fail "The appliance backup directory was not found: $backup_directory"
fi

if [ "$action" = apply ]; then
  if [ "$deployment_mode" = appliance ] && [ -z "$recovery_evidence" ]; then
    recovery_evidence='backup:automatic-verified-appliance-backup'
  fi
  [ -n "$(printf '%s' "$recovery_evidence" | tr -d '[:space:]')" ] ||
    fail "External PostgreSQL apply requires non-empty --recovery-evidence-ref."
  if printf '%s' "$recovery_evidence" | LC_ALL=C grep -q '[[:cntrl:]]'; then
    fail "Recovery evidence must be one printable line without control characters."
  fi
  if printf '%s' "$recovery_evidence" |
    grep -Eqi \
      '(postgres(ql)?://|authorization[[:space:]]*[:=]|bearer[[:space:]]+|(password|secret|token|private[_ -]?key)[[:space:]]*[:=])'; then
    fail "Recovery evidence must contain only a non-secret ticket or recovery reference."
  fi
  printf '%s' "$recovery_evidence" |
    grep -Eq '^(ticket|change|backup|snapshot|pitr|restore|runbook):[A-Za-z0-9][A-Za-z0-9._/#-]{0,127}$' ||
    fail "Recovery evidence must use TYPE:REFERENCE with an approved non-secret type."
  recovery_evidence_type="${recovery_evidence%%:*}"
  recovery_evidence_sha256="$(sha256_text "$recovery_evidence")"
fi

compose config --quiet ||
  fail "The current $deployment_mode Compose configuration is invalid."
compose_with_worker_environment "$environment_path" config --quiet ||
  fail "The worker topology could not be inspected safely."
worker_containers="$temporary_directory/worker-containers"
compose_with_worker_environment "$environment_path" \
  ps --all --services worker > "$worker_containers" ||
  fail "Could not inspect the existing worker topology."
detected_worker_split=false
if grep -Fx worker "$worker_containers" >/dev/null; then
  detected_worker_split=true
fi
[ "$worker_split" = "$detected_worker_split" ] ||
  fail "Worker topology mismatch: set --worker-split exactly when an existing worker container is present."
if [ "$detected_worker_split" = true ]; then
  running_worker_services="$temporary_directory/running-worker-services"
  compose_with_worker_environment "$environment_path" \
    ps --status running --services worker > "$running_worker_services" ||
    fail "Could not inspect the existing worker runtime state."
  if grep -Fx worker "$running_worker_services" >/dev/null; then
    initial_worker_running=true
  fi
fi
current_health_file="$temporary_directory/current-health.json"
current_runtime_schema=''
if fetch_runtime_endpoint health 3 > "$current_health_file"; then
  if current_runtime_schema="$(runtime_expected_schema "$current_health_file")" &&
    printf '%s' "$current_runtime_schema" |
      grep -Eq '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$'; then
    if schema_is_newer "$current_runtime_schema" "$target_schema_version"; then
      fail "Refusing a schema downgrade: the current runtime expects $current_runtime_schema but the target expects $target_schema_version."
    else
      schema_comparison_status="$?"
      [ "$schema_comparison_status" -eq 1 ] ||
        fail "Could not compare the current and target schema versions safely."
    fi
  else
    current_runtime_schema=''
    if [ "$action" = apply ] && [ "$current_digest" != "$target_digest" ]; then
      fail "Apply requires a valid current /api/health expectedSchemaVersion before changing image digest."
    fi
    printf '%s\n' \
      'Warning: current /api/health has no valid expectedSchemaVersion; downgrade comparison was not possible.' >&2
  fi
else
  if [ "$action" = apply ] && [ "$current_digest" != "$target_digest" ]; then
    fail "Apply requires current /api/health metadata before changing image digest."
  fi
  printf '%s\n' \
    'Warning: current /api/health metadata is unavailable; downgrade comparison was not possible.' >&2
fi
configured_images="$temporary_directory/configured-images"
compose config --images > "$configured_images" ||
  fail "Could not inspect configured Compose images."
postgres_images="$(grep -E '(^|/)postgres:' "$configured_images" || true)"
if [ "$deployment_mode" = appliance ]; then
  [ -n "$postgres_images" ] ||
    fail "The appliance Compose configuration has no PostgreSQL image."
  printf '%s\n' "$postgres_images" |
    while IFS= read -r postgres_image; do
      case "$postgres_image" in
        postgres:16|postgres:16-*|*/postgres:16|*/postgres:16-*) ;;
        *)
          printf 'Unsupported PostgreSQL image in Compose configuration: %s\n' \
            "$postgres_image" >&2
          exit 1
          ;;
      esac
    done ||
    fail "The appliance must remain on PostgreSQL 16 during a CMDB image update."
elif [ -n "$postgres_images" ]; then
  fail "The external PostgreSQL Compose configuration must not manage a database image."
fi

write_candidate_environment() {
  source_environment="$1"
  destination_environment="$2"
  awk \
    -v target_image="$target_image" \
    -v target_digest="$target_digest" '
      /^CMDB_IMAGE=/ {
        print "CMDB_IMAGE=" target_image
        image_seen = 1
        next
      }
      /^CMDB_IMAGE_DIGEST=/ {
        print "CMDB_IMAGE_DIGEST=" target_digest
        digest_seen = 1
        next
      }
      { print }
      END {
        if (!image_seen) {
          exit 20
        }
        if (!digest_seen) {
          print "CMDB_IMAGE_DIGEST=" target_digest
        }
      }
    ' "$source_environment" > "$destination_environment"
}

candidate_environment="$temporary_directory/candidate.env"
write_candidate_environment "$environment_path" "$candidate_environment"
current_non_image="$temporary_directory/current-non-image.env"
candidate_non_image="$temporary_directory/candidate-non-image.env"
grep -Ev '^CMDB_IMAGE(_DIGEST)?=' "$environment_path" > "$current_non_image"
grep -Ev '^CMDB_IMAGE(_DIGEST)?=' "$candidate_environment" > "$candidate_non_image"
cmp -s "$current_non_image" "$candidate_non_image" ||
  fail "Candidate generation attempted to change non-image environment settings."
compose_with_environment "$candidate_environment" config --quiet ||
  fail "The target $deployment_mode Compose configuration is invalid."

environment_checksum="$(sha256_file "$environment_path")"
printf '%s\n' 'IPT CMDB update check passed.'
printf 'Instance: %s\n' "$instance_name"
printf 'Deployment mode: %s\n' "$deployment_mode"
printf 'Current image: %s\n' "$current_image"
if [ "$digest_metadata_present" = false ]; then
  printf 'Digest metadata: absent; apply will derive and persist the exact image digest.\n'
fi
if [ -n "$current_runtime_schema" ]; then
  printf 'Current runtime schema expectation: %s\n' "$current_runtime_schema"
fi
printf 'Target version: %s\n' "$target_version"
printf 'Target image: %s\n' "$target_image"
printf 'Target schema: %s\n' "$target_schema_version"
printf 'Database change: %s\n' "$target_database_change"
printf 'Rollback policy: %s\n' "$target_rollback_policy"
printf 'Backup required by manifest: %s\n' "$target_backup_required"
printf 'Worker split: %s\n' "$worker_split"
if [ "$worker_split" = true ]; then
  printf 'Worker initially running: %s\n' "$initial_worker_running"
fi
printf 'Manifest SHA-256: %s\n' "$actual_manifest_checksum"

if [ "$current_digest" = "$target_digest" ]; then
  printf 'Status: target digest is configured; verifying runtime metadata.\n'
  if ! wait_for_expected_readiness; then
    fail "The configured digest matches the target, but runtime version/digest/schema readiness does not. Keep the instance under review and repair or redeploy the exact approved image."
  fi
  printf 'Status: target runtime version, digest, and schema are verified.\n'
  exit 0
fi
printf 'Status: update available.\n'

if [ "$action" = check ]; then
  printf '%s\n' \
    "No instance state was changed. Apply only after review with:" \
    "  --apply --approved-version $target_version \\" \
    "  --approved-manifest-sha256 $actual_manifest_checksum"
  if [ "$deployment_mode" = external ]; then
    printf '%s\n' \
      "External PostgreSQL also requires:" \
      "  --recovery-evidence-ref change:CHG12345"
  else
    printf '%s\n' \
      "Optionally add --recovery-evidence-ref TYPE:REFERENCE."
  fi
  exit 0
fi

lock_directory="$instance_root/.update-cmdb.lock"
if ! mkdir "$lock_directory" 2>/dev/null; then
  fail "Another update may be active; lock exists: $lock_directory"
fi
apply_started=true
printf '%s\n' "$$" > "$lock_directory/pid"
printf '%s\n' "$target_version" > "$lock_directory/version"

[ "$(sha256_file "$environment_path")" = "$environment_checksum" ] ||
  fail "The instance environment changed after validation."

history_directory="$instance_root/update-history"
mkdir -p "$history_directory" ||
  fail "Could not create the update evidence directory."
update_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
evidence_file="$history_directory/$update_timestamp-$target_version-$$.txt"
{
  printf 'startedAt=%s\n' "$update_timestamp"
  printf 'instance=%s\n' "$instance_name"
  printf 'deploymentMode=%s\n' "$deployment_mode"
  printf 'fromImage=%s\n' "$current_image"
  printf 'fromDigest=%s\n' "$current_digest"
  printf 'toVersion=%s\n' "$target_version"
  printf 'toImage=%s\n' "$target_image"
  printf 'toDigest=%s\n' "$target_digest"
  printf 'targetSchema=%s\n' "$target_schema_version"
  printf 'databaseChange=%s\n' "$target_database_change"
  printf 'rollbackPolicy=%s\n' "$target_rollback_policy"
  printf 'manifestSha256=%s\n' "$actual_manifest_checksum"
  printf 'recoveryEvidenceType=%s\n' "$recovery_evidence_type"
  printf 'recoveryEvidenceSha256=%s\n' "$recovery_evidence_sha256"
  printf 'workerSplit=%s\n' "$worker_split"
  printf 'workerInitiallyRunning=%s\n' "$initial_worker_running"
} > "$evidence_file"
printf 'Recovery evidence: %s\n' "$evidence_file"

printf 'Pre-pulling exact target image while the current service remains online...\n'
docker pull "$target_image" ||
  fail "Could not pull the exact target CMDB image."
append_evidence "targetImagePulled=true"

if [ "$worker_split" = true ] && [ "$initial_worker_running" = true ]; then
  printf 'Stopping worker before web...\n'
  worker_stopped=true
  compose stop worker ||
    fail "Could not stop the CMDB worker."
  append_evidence "workerStopped=true"
elif [ "$worker_split" = true ]; then
  append_evidence "workerStopped=initially-stopped"
fi
printf 'Stopping CMDB web service; PostgreSQL remains online...\n'
cmdb_stopped=true
compose stop cmdb ||
  fail "Could not stop the CMDB web service."
append_evidence "cmdbStopped=true"

backup_path=''
if [ "$deployment_mode" = appliance ]; then
  printf 'Creating a final verified PostgreSQL backup with application writes stopped...\n'
  backup_output="$temporary_directory/backup-output"
  if ! compose --profile tools run --no-deps --pull never --rm backup > "$backup_output"; then
    cat "$backup_output" >&2
    fail "The final appliance backup failed."
  fi
  cat "$backup_output"
  backup_filename="$(
    sed -n \
      's|^Backup created and verified: /backups/\(cmdb-[0-9]\{8\}T[0-9]\{6\}Z\.dump\)$|\1|p' \
      "$backup_output" |
      tail -n 1
  )"
  [ -n "$backup_filename" ] ||
    fail "The backup tool did not report a verified backup filename."
  backup_path="$backup_directory/$backup_filename"
  backup_checksum_path="$backup_path.sha256"
  [ -f "$backup_path" ] && [ -f "$backup_checksum_path" ] ||
    fail "The verified backup or its checksum is missing from the host backup directory."
  backup_checksum_lines="$(awk 'END { print NR + 0 }' "$backup_checksum_path")"
  [ "$backup_checksum_lines" -eq 1 ] ||
    fail "The backup checksum file must contain exactly one entry."
  expected_backup_checksum="$(
    awk -v filename="$backup_filename" '$2 == filename { print tolower($1) }' \
      "$backup_checksum_path"
  )"
  backup_checksum_count="$(
    awk -v filename="$backup_filename" \
      '$2 == filename { count += 1 } END { print count + 0 }' \
      "$backup_checksum_path"
  )"
  [ "$backup_checksum_count" -eq 1 ] ||
    fail "The backup checksum file is invalid."
  case "$expected_backup_checksum" in
    *[!0-9a-f]*|'') fail "The backup checksum file is invalid." ;;
  esac
  [ "${#expected_backup_checksum}" -eq 64 ] ||
    fail "The backup checksum file is invalid."
  actual_backup_checksum="$(sha256_file "$backup_path")"
  [ "$actual_backup_checksum" = "$expected_backup_checksum" ] ||
    fail "The final backup does not match its checksum."
  append_evidence "backupFile=$backup_filename"
  append_evidence "backupSha256=$actual_backup_checksum"
  append_evidence "backupVerified=true"
else
  append_evidence "externalRecoveryEvidenceAccepted=true"
  printf 'External PostgreSQL recovery evidence accepted; no database service was managed.\n'
fi

[ "$(sha256_file "$environment_path")" = "$environment_checksum" ] ||
  fail "The instance environment changed while the update was being prepared."
environment_backup="$(mktemp "$instance_root/${environment_filename}.previous.XXXXXX")" ||
  fail "Could not create a protected environment recovery file."
cp -p "$environment_path" "$environment_backup" ||
  fail "Could not preserve the previous instance environment."
replacement_environment="$(mktemp "$instance_root/${environment_filename}.update.XXXXXX")" ||
  fail "Could not create the instance environment replacement."
cp -p "$environment_path" "$replacement_environment" ||
  fail "Could not preserve instance environment permissions."
cat "$candidate_environment" > "$replacement_environment" ||
  fail "Could not write the instance environment replacement."
mv -f "$replacement_environment" "$environment_path" ||
  fail "Could not atomically replace the instance environment."
environment_changed=true
append_evidence "environmentUpdated=true"

compose config --quiet ||
  fail "The updated $deployment_mode Compose configuration is invalid."

printf 'Starting the target-image migration. Automatic image rollback is now disabled.\n'
migration_started=true
append_evidence "migrationStarted=true"
if ! compose run --no-deps --pull never --rm cmdb python scripts/migrate_postgres.py; then
  fail "The target-image migration failed; manual recovery is required."
fi
append_evidence "migrationCompleted=true"

printf 'Starting CMDB web on the target image...\n'
compose up -d --no-deps --no-build --pull never cmdb ||
  fail "The target CMDB web service did not start."
cmdb_stopped=false

wait_for_expected_readiness ||
  fail "Target readiness metadata did not match version, digest, and schema within the timeout."
append_evidence "readinessVerified=true"

if [ "$worker_split" = true ] && [ "$initial_worker_running" = true ]; then
  printf 'Starting worker on the verified target image...\n'
  compose up -d --no-deps --no-build --pull never worker ||
    fail "The target CMDB worker did not start."
  worker_stopped=false
  running_services="$temporary_directory/running-services"
  compose ps --status running --services > "$running_services" ||
    fail "Could not verify the CMDB worker state."
  grep -Fx worker "$running_services" >/dev/null ||
    fail "The target CMDB worker is not running."
  append_evidence "workerRunning=true"
elif [ "$worker_split" = true ]; then
  append_evidence "workerRunning=left-initially-stopped"
fi

append_evidence "completedAt=$(date -u +%Y%m%dT%H%M%SZ)"
update_succeeded=true
printf '%s\n' \
  "IPT CMDB update completed." \
  "Version: $target_version" \
  "Image: $target_image" \
  "Schema: $target_schema_version"
if [ "$deployment_mode" = appliance ]; then
  printf 'Verified backup: %s\n' "$backup_path"
else
  printf '%s\n' 'External recovery evidence: accepted and stored as type plus SHA-256 only.'
fi
printf 'Evidence: %s\n' "$evidence_file"
