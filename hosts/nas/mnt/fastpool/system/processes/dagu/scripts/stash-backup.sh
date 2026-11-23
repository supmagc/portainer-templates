#!/bin/sh
# Triggers Stash's built-in database backup (GraphQL backupDatabase, returns
# once the file is written) and prunes stash-go.sqlite.* older than
# RETENTION_DAYS. Stash writes into its "Backup directory path" setting,
# which must be /backup for the file to land in BACKUP_TARGET.
# Restore: stop Stash, replace stash-go.sqlite in /config with the backup.
set -eu

apk add --no-cache -q curl

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# don't use find -newer: whole-second mtimes miss a backup written in the same second
{ echo /; ls "$BACKUP_TARGET"; } > "${WORK}/before"

BODY="{\"query\":\"mutation { backupDatabase(input: {download: false, includeBlobs: ${INCLUDE_BLOBS}}) }\"}"
echo "Requesting backup (includeBlobs: ${INCLUDE_BLOBS})..."
code=$(curl -sS -o "${WORK}/resp" -w '%{http_code}' --max-time 3000 -X POST \
  -H "ApiKey: ${STASH_API_KEY}" -H "Content-Type: application/json" \
  -d "$BODY" "${STASH_URL}/graphql") || code=000
if [ "$code" != 200 ] || grep -q '"errors"' "${WORK}/resp"; then
  echo "HTTP ${code}: $(head -c 500 "${WORK}/resp")"
  exit 1
fi

OUT_FILE=$(ls "$BACKUP_TARGET" | grep '^stash-go\.sqlite\.' | grep -vxF -f "${WORK}/before" | sort | tail -n 1)
OUT_FILE=${OUT_FILE:+${BACKUP_TARGET}/${OUT_FILE}}
if [ -z "$OUT_FILE" ]; then
  echo "Stash reported success but wrote nothing to ${BACKUP_TARGET}: set its Backup directory path to /backup"
  exit 1
fi
case "$OUT_FILE" in
  *.zip) unzip -l "$OUT_FILE" >/dev/null ;;
  *) [ "$(head -c 15 "$OUT_FILE")" = "SQLite format 3" ] ;;
esac
echo "Wrote ${OUT_FILE} ($(wc -c < "$OUT_FILE") bytes)"

echo "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_TARGET" -maxdepth 1 -name "stash-go.sqlite.*" -mtime "+${RETENTION_DAYS}" -print -delete
