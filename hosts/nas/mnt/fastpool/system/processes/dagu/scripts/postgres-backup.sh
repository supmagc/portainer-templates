#!/bin/sh
# Dumps each database in DB_DUMP_INCLUDE with pg_dump (custom format, already
# compressed), chowns it to OUTPUT_UID:OUTPUT_GID, then prunes dumps older than
# DB_DUMP_RETENTION_DAYS. Run nightly by the Dagu postgres-backup DAG in a plain
# postgres:17 container (client major version must be >= the server's).
# Restore with: pg_restore --clean --if-exists -d <db> <file>.dump
set -eu

export PGPASSWORD="$DB_PASS"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
for db in $(echo "$DB_DUMP_INCLUDE" | tr "," " "); do
  echo "Dumping ${db}..."
  OUT_FILE="${DB_DUMP_TARGET}/${db}_${TIMESTAMP}.dump"
  pg_dump \
    --host="$DB_SERVER" \
    --port="$DB_PORT" \
    --username="$DB_USER" \
    --format=custom \
    --file="$OUT_FILE" \
    "$db"
  chown "${OUTPUT_UID}:${OUTPUT_GID}" "$OUT_FILE"
done

echo "Pruning dumps older than ${DB_DUMP_RETENTION_DAYS} days..."
find "$DB_DUMP_TARGET" -maxdepth 1 -name "*.dump" -mtime "+${DB_DUMP_RETENTION_DAYS}" -print -delete
