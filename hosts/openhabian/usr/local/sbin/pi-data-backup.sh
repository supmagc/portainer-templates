#!/bin/bash
# /usr/local/sbin/pi-data-backup.sh <influxdb|grafana|persistence>
#
# Exports one SSD-resident dataset to the NAS NFS mount. Run per dataset by
# pi-data-backup@.service. See docs/backups.md "Data exports to the NAS".
#
# influxdb needs /etc/default/pi-data-backup (root, mode 600, NOT in the repo):
#   INFLUX_TOKEN=<operator / all-access token>
#   INFLUX_HOST=http://localhost:8086      (optional)
# VERIFY: GRAFANA_DB below is Grafana's real database path.
# Install: chmod +x this file.

set -uo pipefail

NAME="${1:-}"
DEST="${DEST:-/mnt/backup}"
KEEP="${KEEP:-14}"
GRAFANA_DB="${GRAFANA_DB:-/mnt/ssd/grafana/grafana.db}"
PERSISTENCE_DIR="${PERSISTENCE_DIR:-/mnt/ssd/persistence}"
STAMP=$(date +%F)

log() { echo "pi-data-backup[$NAME]: $*"; }

export_influxdb() {
  influx backup "$1"
}

export_grafana() {
  sqlite3 "$GRAFANA_DB" ".backup '$1/grafana.db'" && gzip "$1/grafana.db"
}

export_persistence() {
  tar -C "$(dirname "$PERSISTENCE_DIR")" -czf "$1/persistence.tar.gz" "$(basename "$PERSISTENCE_DIR")"
  local rc=$?
  [ "$rc" -le 1 ]    # 1 = a file changed while being read
}

case "$NAME" in
  influxdb|grafana|persistence) ;;
  *) echo "usage: $0 <influxdb|grafana|persistence>" >&2; exit 2 ;;
esac

timeout 30 stat "$DEST" >/dev/null 2>&1
if ! mountpoint -q "$DEST"; then
  log "$DEST is not mounted - refusing to write to the local disk"
  exit 1
fi

base="${DEST}/${NAME}"
tmp="${base}/.partial-${STAMP}"

# Stage into a hidden dir and publish only on success, so a failed run never replaces
# or prunes a good export.
rm -rf "$tmp"
if mkdir -p "$tmp" && "export_${NAME}" "$tmp"; then
  rm -rf "${base:?}/${STAMP}"
  mv "$tmp" "${base}/${STAMP}"
  ls -1d "${base}"/????-??-?? | sort | head -n -"$KEEP" | xargs -r rm -rf
  log "ok"
else
  rm -rf "$tmp"
  log "FAILED"
  exit 1
fi
