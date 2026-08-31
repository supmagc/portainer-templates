#!/bin/sh
# Exposes TrueNAS's own Cloud Sync Task state as node-exporter textfile
# metrics - same approach as snapshot-tasks-metrics.sh (which see for the
# general rationale: midclt over the local middleware socket, no API key).
#
# IMPORTANT: `midclt call cloudsync.query` includes each task's full
# `credentials` block, which holds real provider secrets (e.g. a Backblaze B2
# account/key pair) in plaintext. This script's parser touches ONLY
# id/description/enabled/job.state/job.time_finished - never .credentials -
# so nothing from that block can end up in the output .prom file. Keep it
# that way if this script is ever extended.
#
# Field names below were confirmed live against this box's actual
# `midclt call cloudsync.query` output (TrueNAS 25.10.3) - see
# SCHEDULED-TASKS-MONITORING.md.
#
# Install: chmod +x this file (cron-wrapper.sh must be alongside it), then a
# TrueNAS Cron Job running as ROOT (midclt needs the local middleware socket):
#   Command:  /mnt/fastpool/system/scripts/cron-wrapper.sh cloudsync-tasks -- /mnt/fastpool/system/scripts/cloudsync-tasks-metrics.sh
#   Schedule: */15 * * * *  (these tasks run far less often than that)

set -eu

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/cloudsync_tasks.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

midclt call cloudsync.query | python3 -c '
import json, sys

tasks = json.load(sys.stdin)


def esc(s):
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"")


print("# HELP cloudsync_task_enabled Whether this Cloud Sync Task is enabled (1) or disabled (0).")
print("# TYPE cloudsync_task_enabled gauge")
print("# HELP cloudsync_task_last_success 1 if the most recent run reported job.state SUCCESS - 0 otherwise (never run, still running, or actually failed). See cloudsync_task_state_info for the real state string.")
print("# TYPE cloudsync_task_last_success gauge")
print("# HELP cloudsync_task_last_run_timestamp_seconds Unix timestamp the most recent run finished - absent if the task has never completed one.")
print("# TYPE cloudsync_task_last_run_timestamp_seconds gauge")
print("# HELP cloudsync_task_state_info Standard Prometheus info-metric pattern - one row per (task, state), value always 1, so the actual job state string shows up as a label rather than being collapsed into a boolean.")
print("# TYPE cloudsync_task_state_info gauge")

for t in tasks:
    # deliberately not reading t["credentials"] - see header comment
    task_id = t["id"]
    task = esc(t.get("description") or "id-" + str(task_id))
    enabled = 1 if t.get("enabled") else 0
    job = t.get("job") or {}
    state = esc(job.get("state", "NEVER_RUN"))
    success = 1 if state == "SUCCESS" else 0
    print(f"cloudsync_task_enabled{{task=\"{task}\"}} {enabled}")
    print(f"cloudsync_task_last_success{{task=\"{task}\"}} {success}")
    print(f"cloudsync_task_state_info{{task=\"{task}\", state=\"{state}\"}} 1")
    finished = job.get("time_finished")
    if finished and "$date" in finished:
        finished_ts = finished["$date"] // 1000
        print(f"cloudsync_task_last_run_timestamp_seconds{{task=\"{task}\"}} {finished_ts}")
' > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
