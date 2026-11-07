#!/bin/sh
# Triggers Jellyfin's built-in backup (POST /Backup/Create, returns once the
# zip is written) and prunes jellyfin-backup-*.zip older than RETENTION_DAYS.
# Jellyfin refuses while a library scan runs, so failures are retried.
# Restore from Dashboard → Backups.
set -eu

apk add --no-cache -q curl

BODY="{\"Database\":true,\"Metadata\":${INCLUDE_METADATA},\"Subtitles\":${INCLUDE_SUBTITLES},\"Trickplay\":${INCLUDE_TRICKPLAY}}"
RESP=$(mktemp)
trap 'rm -f "$RESP"' EXIT

attempt=1
while :; do
  echo "Requesting backup (attempt ${attempt}/${MAX_ATTEMPTS}): ${BODY}"
  code=$(curl -sS -o "$RESP" -w '%{http_code}' --max-time 3600 -X POST \
    -H "Authorization: MediaBrowser Token=\"${JELLYFIN_API_KEY}\"" \
    -H "Content-Type: application/json" \
    -d "$BODY" "${JELLYFIN_URL}/Backup/Create") || code=000
  [ "$code" = 200 ] && break
  echo "HTTP ${code}: $(head -c 500 "$RESP")"
  [ "$attempt" -ge "$MAX_ATTEMPTS" ] && exit 1
  attempt=$((attempt + 1))
  sleep "$RETRY_DELAY"
done

SERVER_PATH=$(sed -n 's/.*"Path":"\([^"]*\)".*/\1/p' "$RESP")
OUT_FILE="${BACKUP_TARGET}/$(basename "$SERVER_PATH")"
case "$OUT_FILE" in
  */jellyfin-backup-*.zip) ;;
  *) echo "Unexpected response: $(head -c 500 "$RESP")"; exit 1 ;;
esac
unzip -l "$OUT_FILE" | grep -q 'manifest.json' || { echo "${OUT_FILE} missing or has no manifest.json"; exit 1; }
echo "Wrote ${OUT_FILE} ($(wc -c < "$OUT_FILE") bytes)"

echo "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_TARGET" -maxdepth 1 -name "jellyfin-backup-*.zip" -mtime "+${RETENTION_DAYS}" -print -delete
