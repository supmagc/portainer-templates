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
# state/datetime/schedule - nothing here has credentials to begin with
# (unlike cloudsync-tasks-metrics.sh, which does and is careful about the
# same thing).
#
# In addition to the original snapshot_task_* metrics (kept for the "Periodic
# Snapshot Tasks" state string), also writes the shared `scheduled_job_*`
# family every scheduled-job source in this repo writes into - see
# docs/monitoring.md "Scheduled jobs (unified)" and
# ScheduledJobStale/ScheduledJobFailed in grafana/provisioning/alerting/rules.yml.
# scheduled_job_expected_interval_seconds is derived from each task's own
# `schedule` cron dict via a best-effort heuristic (see estimate_interval()
# below) - it only recognizes the common shapes TrueNAS's UI actually
# produces (every-N-minutes, every-N-hours, daily, weekly, monthly). Anything
# it doesn't recognize falls back to a conservative 8-day window rather than
# risk a false alert - check snapshot_task_state_info's schedule fields by
# hand if a task's staleness threshold looks wrong.
#
# Install: chmod +x this file (cron-wrapper.sh must be alongside it), then a
# TrueNAS Cron Job running as ROOT (midclt needs the local middleware socket):
#   Command:  /mnt/fastpool/system/scripts/cron-wrapper.sh snapshot-tasks 1800 -- /mnt/fastpool/system/scripts/snapshot-tasks-metrics.sh
#   Schedule: */15 * * * *  (these tasks run far less often than that)

set -eu

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/snapshot_tasks.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

midclt call pool.snapshottask.query | python3 -c '
import json, sys


def esc(s):
    return str(s).replace("\\", "\\\\").replace("\"", "\\\"")


def estimate_interval(schedule):
    """Best-effort seconds-between-runs from a TrueNAS cron-style schedule
    dict ({minute, hour, dom, month, dow}, each "*", "*/N", a single value,
    or a comma list). Only handles the common UI-generated shapes; anything
    else falls back to 8 days (conservative - would rather under-alert than
    false-positive on a schedule shape it misread)."""
    FALLBACK = 8 * 24 * 3600
    if not schedule:
        return FALLBACK
    minute = schedule.get("minute", "*")
    hour = schedule.get("hour", "*")
    dom = schedule.get("dom", "*")
    month = schedule.get("month", "*")
    dow = schedule.get("dow", "*")

    if month != "*":
        return FALLBACK   # yearly/complex - not worth guessing at

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
        return int(8 * 24 * 3600 / max(n, 1))   # weekly, with slack

    if dom != "*":
        return 32 * 24 * 3600   # monthly, with slack for month-length variance

    if "," in hour or "-" in hour:
        return FALLBACK   # multiple/ranged fixed hours - not worth guessing precisely

    return 26 * 3600   # single fixed hour, dom/dow/month all "*" - daily, +2h slack


tasks = json.load(sys.stdin)

print("# HELP snapshot_task_enabled Whether this Periodic Snapshot Task is enabled (1) or disabled (0).")
print("# TYPE snapshot_task_enabled gauge")
print("# HELP snapshot_task_last_success 1 if the most recent run FINISHED - 0 if disabled, still PENDING its first run, or actually failed. See snapshot_task_state_info for the real state string.")
print("# TYPE snapshot_task_last_success gauge")
print("# HELP snapshot_task_last_run_timestamp_seconds Unix timestamp of the most recently completed run - absent if the task has never finished one.")
print("# TYPE snapshot_task_last_run_timestamp_seconds gauge")
print("# HELP snapshot_task_state_info Standard Prometheus info-metric pattern - one row per (dataset, state), value always 1, so the actual state string shows up as a label rather than being collapsed into a boolean.")
print("# TYPE snapshot_task_state_info gauge")
print("# HELP scheduled_job_last_run_timestamp_seconds See cron-wrapper.sh - shared scheduled-job metric family.")
print("# TYPE scheduled_job_last_run_timestamp_seconds gauge")
print("# HELP scheduled_job_last_exit_code See cron-wrapper.sh - shared scheduled-job metric family. 0 = last run FINISHED, 1 = otherwise.")
print("# TYPE scheduled_job_last_exit_code gauge")
print("# HELP scheduled_job_expected_interval_seconds See cron-wrapper.sh - shared scheduled-job metric family. Estimated from this task'"'"'s own schedule - see script header.")
print("# TYPE scheduled_job_expected_interval_seconds gauge")
print("# HELP scheduled_job_enabled See cron-wrapper.sh - shared scheduled-job metric family.")
print("# TYPE scheduled_job_enabled gauge")

for t in tasks:
    dataset = esc(t["dataset"])
    enabled = 1 if t.get("enabled") else 0
    state_obj = t.get("state") or {}
    state = esc(state_obj.get("state", "UNKNOWN"))
    success = 1 if state == "FINISHED" else 0
    print(f"snapshot_task_enabled{{dataset=\"{dataset}\"}} {enabled}")
    print(f"snapshot_task_last_success{{dataset=\"{dataset}\"}} {success}")
    print(f"snapshot_task_state_info{{dataset=\"{dataset}\", state=\"{state}\"}} 1")

    name = dataset
    print(f"scheduled_job_enabled{{source=\"nas-snapshot\", name=\"{name}\"}} {enabled}")
    print(f"scheduled_job_last_exit_code{{source=\"nas-snapshot\", name=\"{name}\"}} {0 if success else 1}")
    interval = estimate_interval(t.get("schedule"))
    print(f"scheduled_job_expected_interval_seconds{{source=\"nas-snapshot\", name=\"{name}\"}} {interval}")

    dt = state_obj.get("datetime")
    if dt and "$date" in dt:
        dt_ts = dt["$date"] // 1000
        print(f"snapshot_task_last_run_timestamp_seconds{{dataset=\"{dataset}\"}} {dt_ts}")
        print(f"scheduled_job_last_run_timestamp_seconds{{source=\"nas-snapshot\", name=\"{name}\"}} {dt_ts}")
' > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
