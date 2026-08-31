#!/bin/sh
# Exposes TrueNAS's own Periodic Snapshot Task state as node-exporter textfile
# metrics. These are middleware-scheduled (zettarepl), not cron jobs, so
# there's no command of ours to wrap with cron-wrapper.sh directly - instead
# this polls `midclt`, TrueNAS's local CLI (talks to the middleware over a
# Unix socket as root, no API key or network exposure needed), and reports
# what it finds.
#
# Field names below were confirmed live against this box's actual
# `midclt call pool.snapshottask.query` output (TrueNAS 25.10.3) - see
# SCHEDULED-TASKS-MONITORING.md. Deliberately touches only dataset/enabled/
# state/datetime - nothing here has credentials to begin with (unlike
# cloudsync-tasks-metrics.sh, which does and is careful about the same thing).
#
# Install: chmod +x this file (cron-wrapper.sh must be alongside it), then a
# TrueNAS Cron Job running as ROOT (midclt needs the local middleware socket):
#   Command:  /mnt/fastpool/system/scripts/cron-wrapper.sh snapshot-tasks -- /mnt/fastpool/system/scripts/snapshot-tasks-metrics.sh
#   Schedule: */15 * * * *  (these tasks run far less often than that)

set -eu

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/snapshot_tasks.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

midclt call pool.snapshottask.query | python3 -c '
import json, sys

tasks = json.load(sys.stdin)


def esc(s):
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"")


print("# HELP snapshot_task_enabled Whether this Periodic Snapshot Task is enabled (1) or disabled (0).")
print("# TYPE snapshot_task_enabled gauge")
print("# HELP snapshot_task_last_success 1 if the most recent run FINISHED - 0 if disabled, still PENDING its first run, or actually failed. See snapshot_task_state_info for the real state string.")
print("# TYPE snapshot_task_last_success gauge")
print("# HELP snapshot_task_last_run_timestamp_seconds Unix timestamp of the most recently completed run - absent if the task has never finished one.")
print("# TYPE snapshot_task_last_run_timestamp_seconds gauge")
print("# HELP snapshot_task_state_info Standard Prometheus info-metric pattern - one row per (dataset, state), value always 1, so the actual state string shows up as a label rather than being collapsed into a boolean.")
print("# TYPE snapshot_task_state_info gauge")

for t in tasks:
    dataset = esc(t["dataset"])
    enabled = 1 if t.get("enabled") else 0
    state_obj = t.get("state") or {}
    state = esc(state_obj.get("state", "UNKNOWN"))
    success = 1 if state == "FINISHED" else 0
    print(f"snapshot_task_enabled{{dataset=\"{dataset}\"}} {enabled}")
    print(f"snapshot_task_last_success{{dataset=\"{dataset}\"}} {success}")
    print(f"snapshot_task_state_info{{dataset=\"{dataset}\", state=\"{state}\"}} 1")
    dt = state_obj.get("datetime")
    if dt and "$date" in dt:
        dt_ts = dt["$date"] // 1000
        print(f"snapshot_task_last_run_timestamp_seconds{{dataset=\"{dataset}\"}} {dt_ts}")
' > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
