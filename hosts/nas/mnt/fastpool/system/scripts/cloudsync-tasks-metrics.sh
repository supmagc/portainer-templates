#!/bin/sh
# Exposes TrueNAS's own Cloud Sync Task state as node-exporter textfile
# metrics - same approach as snapshot-tasks-metrics.sh (which see for the
# general rationale: midclt over the local middleware socket, no API key, and
# for the scheduled_job_* shared-metric-family / estimate_interval()
# rationale - see docs/monitoring.md "Scheduled jobs (unified)" and
# ScheduledJobStale/ScheduledJobFailed in grafana/provisioning/alerting/rules.yml).
#
# IMPORTANT: `midclt call cloudsync.query` includes each task's full
# `credentials` block, which holds real provider secrets (e.g. a Backblaze B2
# account/key pair) in plaintext. This script's parser touches ONLY
# id/description/enabled/schedule/job.state/job.time_finished - never
# .credentials - so nothing from that block can end up in the output .prom
# file. Keep it that way if this script is ever extended.
#
# Field names below were confirmed live against this box's actual
# `midclt call cloudsync.query` output (TrueNAS 25.10.3) - see
# SCHEDULED-TASKS-MONITORING.md.
#
# Install: chmod +x this file (cron-wrapper.sh must be alongside it), then a
# TrueNAS Cron Job running as ROOT (midclt needs the local middleware socket):
#   Command:  /mnt/fastpool/system/scripts/cron-wrapper.sh 1800 -- /mnt/fastpool/system/scripts/cloudsync-tasks-metrics.sh
#   Schedule: */15 * * * *  (these tasks run far less often than that)
#
# IMPORTANT: keep each `python3 -c` block below small - see the matching note
# in snapshot-tasks-metrics.sh's header for why (a single combined
# `python3 -c` here was reliably SIGKILLed under TrueNAS's cron/middlewared,
# exit 137, every time - confirmed by bisection, mechanism never identified).

set -eu

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/cloudsync_tasks.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

# mktemp, not a fixed /tmp path - this runs as root, a predictable world-
# writable temp filename is a symlink-attack vector.
MIDCLT_TMP="$(mktemp /tmp/cloudsync-tasks-metrics.midclt.XXXXXX)"
midclt call cloudsync.query > "$MIDCLT_TMP"

python3 -c '
import json, sys


def esc(s):
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"")


tasks = json.load(sys.stdin)

print("# HELP cloudsync_task_enabled Whether this Cloud Sync Task is enabled (1) or disabled (0).")
print("# TYPE cloudsync_task_enabled gauge")
print("# HELP cloudsync_task_last_success 1 if the most recent run reported job.state SUCCESS - 0 otherwise (never run, still running, or actually failed). See cloudsync_task_state_info for the real state string.")
print("# TYPE cloudsync_task_last_success gauge")
print("# HELP cloudsync_task_last_run_timestamp_seconds Unix timestamp the most recent run finished - absent if the task has never completed one.")
print("# TYPE cloudsync_task_last_run_timestamp_seconds gauge")
print("# HELP cloudsync_task_state_info Standard Prometheus info-metric pattern - one row per (task, state), value always 1, so the actual job state string shows up as a label rather than being collapsed into a boolean.")
print("# TYPE cloudsync_task_state_info gauge")
print("# HELP scheduled_job_last_run_timestamp_seconds Unix timestamp of the last time this job ran, regardless of outcome.")
print("# TYPE scheduled_job_last_run_timestamp_seconds gauge")
print("# HELP scheduled_job_last_exit_code Exit code of the most recent run (0 = success).")
print("# TYPE scheduled_job_last_exit_code gauge")
print("# HELP scheduled_job_enabled Whether this scheduled job is enabled (1) or disabled (0).")
print("# TYPE scheduled_job_enabled gauge")

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

    name = task
    print(f"scheduled_job_enabled{{source=\"nas-cloudsync\", name=\"{name}\"}} {enabled}")
    print(f"scheduled_job_last_exit_code{{source=\"nas-cloudsync\", name=\"{name}\"}} {0 if success else 1}")

    finished = job.get("time_finished")
    if finished and "$date" in finished:
        finished_ts = finished["$date"] // 1000
        print(f"cloudsync_task_last_run_timestamp_seconds{{task=\"{task}\"}} {finished_ts}")
    else:
        finished_ts = 0
    # scheduled_job_last_run_timestamp_seconds always printed, even if this
    # task has never completed a run - see the matching note in
    # snapshot-tasks-metrics.sh for why (the ScheduledJobStale alert rule
    # matches all three scheduled_job_* series on labels, so a task missing
    # this one entirely is invisible to it forever, no matter how overdue).
    print(f"scheduled_job_last_run_timestamp_seconds{{source=\"nas-cloudsync\", name=\"{name}\"}} {finished_ts}")
' < "$MIDCLT_TMP" > "$TMP_FILE"

# Separate call, deliberately - see the IMPORTANT note in the header comment.
# This is the only thing estimate_interval() feeds; nothing else needs it.
python3 -c '
import json, sys


def esc(s):
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"")


def estimate_interval(schedule):
    """Best-effort seconds-between-runs from a TrueNAS cron-style schedule
    dict - see snapshot-tasks-metrics.sh for the full rationale, identical
    heuristic (both middleware endpoints use the same schedule dict shape)."""
    FALLBACK = 8 * 24 * 3600
    if not schedule:
        return FALLBACK
    minute = schedule.get("minute", "*")
    hour = schedule.get("hour", "*")
    dom = schedule.get("dom", "*")
    month = schedule.get("month", "*")
    dow = schedule.get("dow", "*")

    if month != "*":
        return FALLBACK

    if minute.startswith("*/") and hour == "*":
        try:
            return int(minute[2:]) * 60
        except ValueError:
            return FALLBACK

    if hour.startswith("*/") and dom == "*" and dow == "*":
        try:
            return int(hour[2:]) * 3600
        except ValueError:
            return FALLBACK

    if dow != "*" and dom == "*":
        n = len([d for d in dow.split(",") if d])
        return int(8 * 24 * 3600 / max(n, 1))

    if dom != "*":
        return 32 * 24 * 3600

    if "," in hour or "-" in hour:
        return FALLBACK

    return 26 * 3600


tasks = json.load(sys.stdin)

print("# HELP scheduled_job_expected_interval_seconds The per-job timewindow override for ScheduledJobStale - how often this job is expected to run, in seconds.")
print("# TYPE scheduled_job_expected_interval_seconds gauge")

for t in tasks:
    # deliberately not reading t["credentials"] - see header comment
    task_id = t["id"]
    name = esc(t.get("description") or "id-" + str(task_id))
    interval = estimate_interval(t.get("schedule"))
    print(f"scheduled_job_expected_interval_seconds{{source=\"nas-cloudsync\", name=\"{name}\"}} {interval}")
' < "$MIDCLT_TMP" >> "$TMP_FILE"

rm -f "$MIDCLT_TMP"
chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
