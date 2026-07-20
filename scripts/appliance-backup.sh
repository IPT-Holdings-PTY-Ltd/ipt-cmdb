#!/bin/sh
set -eu

umask 077
host="${POSTGRES_HOST:-postgres}"
database="${POSTGRES_DATABASE:-cmdb}"
user="${POSTGRES_USER:-cmdb}"
password="$(cat /run/secrets/postgres_password)"
printf '%s:%s:%s:%s:%s\n' "$host" 5432 "$database" "$user" "$password" > /tmp/pgpass
chmod 600 /tmp/pgpass
export PGPASSFILE=/tmp/pgpass

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
filename="cmdb-${timestamp}.dump"
temporary="/backups/.${filename}.tmp"
destination="/backups/${filename}"

pg_dump \
  --host "$host" \
  --username "$user" \
  --dbname "$database" \
  --format custom \
  --compress 9 \
  --file "$temporary"
pg_restore --list "$temporary" >/dev/null
mv "$temporary" "$destination"
(cd /backups && sha256sum "$filename" > "${filename}.sha256")

printf 'Backup created and verified: %s\n' "$destination"
