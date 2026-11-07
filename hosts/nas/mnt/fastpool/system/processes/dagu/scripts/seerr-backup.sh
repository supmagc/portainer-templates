#!/bin/sh
# Backs up Seerr's settings.json plus a consistent sqlite copy of
# db/db.sqlite3 (safe while Seerr runs) as one tar.gz, chowned to
# OUTPUT_UID:OUTPUT_GID, then prunes backups older than RETENTION_DAYS.
# Restore: stop Seerr, extract into its config dir, start it.
set -eu

apk add --no-cache -q sqlite

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUT_FILE="${BACKUP_TARGET}/seerr_${TIMESTAMP}.tar.gz"
TMP_FILE="${OUT_FILE}.partial"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK" "$TMP_FILE"' EXIT

echo "Copying database..."
mkdir "${WORK}/db"
sqlite3 /seerr/db/db.sqlite3 ".backup '${WORK}/db/db.sqlite3'"
[ "$(sqlite3 "${WORK}/db/db.sqlite3" 'PRAGMA integrity_check')" = "ok" ]
[ -s /seerr/settings.json ]
cp /seerr/settings.json "${WORK}/settings.json"

tar -czf "$TMP_FILE" -C "$WORK" settings.json db/db.sqlite3
chown "${OUTPUT_UID}:${OUTPUT_GID}" "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
echo "Wrote ${OUT_FILE} ($(wc -c < "$OUT_FILE") bytes)"

echo "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_TARGET" -maxdepth 1 -name "seerr_*.tar.gz" -mtime "+${RETENTION_DAYS}" -print -delete
