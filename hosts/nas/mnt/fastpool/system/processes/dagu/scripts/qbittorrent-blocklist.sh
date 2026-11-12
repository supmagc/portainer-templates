#!/bin/sh
set -eu

: "${BLOCKLIST_URL:?}" "${MIN_RULES:?}" "${OWNER_UID:?}" "${OWNER_GID:?}"
CONTAINER=${QBT_CONTAINER:-qbittorrent}
API=http://localhost:${QBT_WEBUI_PORT:-8282}/api/v2
DIR=/qbt/blocklist
TARGET=$DIR/bt_blocklists.p2p
QBT_PATH=/config/blocklist/bt_blocklists.p2p

mkdir -p "$DIR"
chown "$OWNER_UID:$OWNER_GID" "$DIR"
tmp=$(mktemp "$DIR/.download.XXXXXX")
trap 'rm -f "$tmp" "$tmp.gz" "$tmp.raw"' EXIT

wget -q -T 120 -O "$tmp.gz" "$BLOCKLIST_URL"
gunzip -c "$tmp.gz" > "$tmp.raw"
grep -v -E "^(${EXCLUDE_CATEGORIES:-Bogon}):" "$tmp.raw" > "$tmp" || true
rules=$(grep -c -E '^[^#].*:.+-.+' "$tmp" || true)
if [ "$rules" -lt "$MIN_RULES" ]; then
  echo "downloaded list has only $rules rules (< MIN_RULES=$MIN_RULES); keeping the current one" >&2
  exit 1
fi
chown "$OWNER_UID:$OWNER_GID" "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$TARGET"
echo "installed $TARGET ($rules rules)"

qbt() { docker exec "$CONTAINER" curl -fsS "$@"; }

last_id=$(qbt "$API/log/main" | grep -o '"id":[0-9]*' | tail -n 1 | cut -d: -f2)
qbt -X POST "$API/app/setPreferences" --data-urlencode 'json={"ip_filter_enabled":false}'
qbt -X POST "$API/app/setPreferences" \
  --data-urlencode "json={\"ip_filter_enabled\":true,\"ip_filter_path\":\"$QBT_PATH\"}"

i=0
while [ "$i" -lt 36 ]; do
  sleep 5
  i=$((i + 1))
  log=$(qbt "$API/log/main?last_known_id=${last_id:--1}")
  if echo "$log" | grep -q 'Failed to parse the IP filter file'; then
    echo "qBittorrent failed to parse $QBT_PATH" >&2
    exit 1
  fi
  applied=$(echo "$log" | grep -o 'IP filter file. Number of rules applied: [0-9]*' | tail -n 1 | grep -o '[0-9]*$' || true)
  if [ -n "$applied" ]; then
    echo "qBittorrent applied $applied rules"
    if [ "$applied" -lt "$MIN_RULES" ]; then
      echo "applied rule count below MIN_RULES=$MIN_RULES" >&2
      exit 1
    fi
    exit 0
  fi
done
echo "no IP filter reload confirmation in qBittorrent's log after 3 minutes" >&2
exit 1
