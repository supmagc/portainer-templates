#!/bin/sh
# Dumps each database in DB_DUMP_INCLUDE with mariadb-dump, gzips it, chowns
# it to OUTPUT_UID:OUTPUT_GID, then prunes dumps older than
# DB_DUMP_RETENTION_DAYS. Run nightly by the Dagu mariadb-backup DAG in a
# plain mariadb:lts container (matches the server image, so mariadb-dump's
# version always lines up with mariadb-server's). POSIX sh, not bash - the
# container's /bin/sh is dash.
set -eu

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
for db in $(echo "$DB_DUMP_INCLUDE" | tr "," " "); do
  echo "Dumping ${db}..."
  OUT_FILE="${DB_DUMP_TARGET}/${db}_${TIMESTAMP}.sql"
  mariadb-dump \
    --host="$DB_SERVER" \
    --port="$DB_PORT" \
    --user="$DB_USER" \
    --password="$DB_PASS" \
    --single-transaction \
    --quick \
    "$db" > "$OUT_FILE"
  gzip -f "$OUT_FILE"
  chown "${OUTPUT_UID}:${OUTPUT_GID}" "${OUT_FILE}.gz"
done

echo "Pruning dumps older than ${DB_DUMP_RETENTION_DAYS} days..."
find "$DB_DUMP_TARGET" -maxdepth 1 -name "*.sql.gz" -mtime "+${DB_DUMP_RETENTION_DAYS}" -print -delete
