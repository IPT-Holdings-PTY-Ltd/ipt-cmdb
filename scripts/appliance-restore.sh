#!/bin/sh
set -eu

backup_file="${BACKUP_FILE:-}"
case "$backup_file" in
  ''|*[!A-Za-z0-9._-]*)
    printf 'BACKUP_FILE must be one backup filename from the configured backup directory.\n' >&2
    exit 2
    ;;
esac
case "$backup_file" in
  cmdb-*.dump) ;;
  *)
    printf 'BACKUP_FILE must match cmdb-*.dump.\n' >&2
    exit 2
    ;;
esac

backup_path="/backups/${backup_file}"
checksum_path="${backup_path}.sha256"
test -f "$backup_path" || { printf 'Backup file was not found.\n' >&2; exit 2; }
test -f "$checksum_path" || { printf 'Backup checksum was not found.\n' >&2; exit 2; }

(cd /backups && sha256sum -c "${backup_file}.sha256")
pg_restore --list "$backup_path" >/dev/null

umask 077
host="${POSTGRES_HOST:-postgres}"
database="${POSTGRES_DATABASE:-cmdb}"
user="${POSTGRES_USER:-cmdb}"
password="$(cat /run/secrets/postgres_password)"
printf '%s:%s:*:%s:%s\n' "$host" 5432 "$user" "$password" > /tmp/pgpass
chmod 600 /tmp/pgpass
export PGPASSFILE=/tmp/pgpass

dropdb --host "$host" --username "$user" --if-exists --force "$database"
createdb --host "$host" --username "$user" --owner "$user" "$database"
pg_restore \
  --host "$host" \
  --username "$user" \
  --dbname "$database" \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "$backup_path"

printf 'Database restored and verified from: %s\n' "$backup_path"
