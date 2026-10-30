#!/bin/sh
# Pulls an OpenWrt sysupgrade backup (incl. installed-package list) over SSH,
# checks it's a real archive, chowns it to OUTPUT_UID:OUTPUT_GID, then prunes
# backups older than RETENTION_DAYS. The router's authorized_keys entry forces
# the remote command, so nothing is sent here (see docs/backups.md).
# Restore with: LuCI → System → Backup / Flash Firmware → Restore, or
# sysupgrade -r <file>.tar.gz
set -eu

apk add --no-cache -q openssh-client-default

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
OUT_FILE="${BACKUP_TARGET}/openwrt_${TIMESTAMP}.tar.gz"
TMP_FILE="${OUT_FILE}.partial"
trap 'rm -f "$TMP_FILE"' EXIT

echo "Fetching backup from ${ROUTER_HOST}..."
ssh -T -i /ssh/id_ed25519 \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/state/known_hosts \
  "root@${ROUTER_HOST}" > "$TMP_FILE"

if ! tar -tzf "$TMP_FILE" | grep -q "etc/config/network$"; then
  echo "Not a sysupgrade backup ($(wc -c < "$TMP_FILE") bytes), starts with:"
  head -c 200 "$TMP_FILE" | tr -cd '[:print:]\n'
  echo
  exit 1
fi
chown "${OUTPUT_UID}:${OUTPUT_GID}" "$TMP_FILE"
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
echo "Wrote ${OUT_FILE} ($(wc -c < "$OUT_FILE") bytes)"

echo "Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_TARGET" -maxdepth 1 -name "openwrt_*.tar.gz" -mtime "+${RETENTION_DAYS}" -print -delete
