#!/bin/sh
# Builds the same tar TrueNAS's "Save Config" (with secret seed) produces: a
# consistent sqlite copy of freenas-v1.db plus pwenc_secret, from the host's
# /data mounted read-only. Checks the copy's integrity, chowns it to
# OUTPUT_UID:OUTPUT_GID, then prunes backups older than RETENTION_DAYS.
# Restore with: System → General Settings → Manage Configuration → Upload File
# (same or newer TrueNAS version only).
set -eu

apk add --no-cache -q sqlite

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUT_FILE="${BACKUP_TARGET}/truenas-config_${TIMESTAMP}.tar"
TMP_FILE="${OUT_FILE}.partial"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK" "$TMP_FILE"' EXIT

echo "Copying config database..."
sqlite3 -readonly /truenas-data/freenas-v1.db ".backup '${WORK}/freenas-v1.db'"
[ "$(sqlite3 "${WORK}/freenas-v1.db" 'PRAGMA integrity_check')" = "ok" ]
cp /truenas-data/pwenc_secret "${WORK}/pwenc_secret"

tar -cf "$TMP_FILE" -C "$WORK" freenas-v1.db pwenc_secret
chown "${OUTPUT_UID}:${OUTPUT_GID}" "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
echo "Wrote ${OUT_FILE} ($(wc -c < "$OUT_FILE") bytes)"

echo "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_TARGET" -maxdepth 1 -name "truenas-config_*.tar" -mtime "+${RETENTION_DAYS}" -print -delete
