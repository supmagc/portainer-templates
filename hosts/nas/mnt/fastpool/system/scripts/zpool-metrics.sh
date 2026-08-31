#!/bin/sh
# Writes real ZFS pool capacity as a node-exporter textfile-collector metric.
# Run on the TrueNAS host itself (not in a container) via cron - zpool/zfs CLI
# access to the host's pools from inside Docker needs --privileged + /dev/zfs,
# and no maintained pre-built zfs_exporter image exists to justify that (checked
# pdf/zfs_exporter and ncabatoff/zfs-exporter - both source-only, no published
# container image). This is simpler and needs zero extra container privileges.
#
# Why not node_filesystem_* (from node-exporter, already running)? ZFS child
# datasets share the pool's free space, so a dataset with little of its own
# data always reports near-100% free via df-style stats, regardless of how
# full the pool actually is. `zpool list` gives the real, pool-wide number.
#
# Install:
#   1. mkdir -p /mnt/fastpool/system/processes/node-exporter/textfile
#   2. cp this script (and cron-wrapper.sh, alongside it) somewhere on the
#      host, chmod +x both
#   3. TrueNAS UI -> Tasks -> Cron Jobs -> run as ROOT, not nas_processes -
#      `zpool`/`zfs` need root (or a privileged group nas_processes doesn't
#      have). Schedule: */5 * * * *. Command - go through cron-wrapper.sh
#      rather than calling this script directly, so Prometheus can tell if
#      this job stops running or starts failing (see cron-wrapper.sh and
#      CronJobStale/CronJobFailed in grafana/provisioning/alerting/rules.yml):
#        /path/to/cron-wrapper.sh zpool-metrics -- /path/to/zpool-metrics.sh
#   4. mount the textfile dir read-only into node-exporter and add
#      --collector.textfile.directory=/textfile (see docker-compose-monitoring.yml)
#
# Since this runs as root, the output file gets chown'd to OUTPUT_USER below
# so node-exporter's container (running as a non-root image user) can still
# read it through the read-only bind mount.

set -eu

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/zpool.prom"
TMP_FILE="${OUT_FILE}.tmp"   # write-then-rename: avoids node-exporter ever reading a half-written file
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

{
  echo "# HELP zpool_capacity_percent ZFS pool capacity, percent used (zpool list CAP column)"
  echo "# TYPE zpool_capacity_percent gauge"
  echo "# HELP zpool_size_bytes ZFS pool total size in bytes"
  echo "# TYPE zpool_size_bytes gauge"
  echo "# HELP zpool_free_bytes ZFS pool free space in bytes"
  echo "# TYPE zpool_free_bytes gauge"
  echo "# HELP zpool_health ZFS pool health, 1 = ONLINE, 0 = anything else (DEGRADED/FAULTED/OFFLINE/UNAVAIL/REMOVED)"
  echo "# TYPE zpool_health gauge"

  zpool list -Hp -o name,size,free,capacity,health | while IFS="$(printf '\t')" read -r name size free cap health; do
    echo "zpool_capacity_percent{pool=\"${name}\"} ${cap}"
    echo "zpool_size_bytes{pool=\"${name}\"} ${size}"
    echo "zpool_free_bytes{pool=\"${name}\"} ${free}"
    if [ "$health" = "ONLINE" ]; then
      echo "zpool_health{pool=\"${name}\"} 1"
    else
      echo "zpool_health{pool=\"${name}\"} 0"
    fi
  done
} > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
