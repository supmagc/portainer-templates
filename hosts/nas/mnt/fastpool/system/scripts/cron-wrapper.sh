#!/bin/sh
# Generic cron-job wrapper for node-exporter's textfile collector - the standard
# "dead man's switch" pattern for Prometheus-monitored cron jobs. Records
# last-run time, last-*success*-time, last exit code, and duration for whatever
# command it wraps, plus the caller-supplied expected interval (the per-job
# "how often should this run" override), as part of the shared
# `scheduled_job_*` metric family every scheduled-job source in this repo
# writes into - see docs/monitoring.md "Scheduled jobs (unified)" and
# ScheduledJobStale/ScheduledJobFailed in grafana/provisioning/alerting/rules.yml.
#
# Deliberately does NOT use a label named "job" - Prometheus's scrape config
# already has a label of that name (job="node" for every textfile-collector
# metric here), and a same-named metric label gets silently renamed to
# "exported_job" on ingestion. Bitten by this once already (see git history);
# this metric family uses "name" instead so it never collides.
#
# Usage in a TrueNAS Cron Job's Command field - wrap the existing command,
# don't replace it:
#   /path/to/cron-wrapper.sh <expected-interval-seconds> -- <actual command and args...>
#
# JOB_NAME is derived from the wrapped command's own basename (minus .sh) -
# not a separate argument - so it can't drift out of sync with what's
# actually being run (a mismatched/missing name argument silently corrupted
# this job's whole metrics file once already - see git history).
#
# Example (see zpool-metrics.sh's own header for the full cron setup):
#   /mnt/fastpool/system/scripts/cron-wrapper.sh 1800 -- /mnt/fastpool/system/scripts/zpool-metrics.sh
#
# Each wrapped job gets its own file (cron_<job-name>.prom) rather than a
# shared one, so concurrent cron jobs never race on the same file - matches
# node-exporter's textfile collector, which merges every *.prom file in the
# directory automatically, and mirrors zpool-metrics.sh's own atomic
# write-then-rename pattern. Wrapping the same script from two different cron
# jobs would collide on this file - give each wrapped script its own path if
# that's ever needed.
#
# Install: chmod +x this file. It has no cron entry of its own - it wraps
# whatever job's command line you point it at.

set -eu

EXPECTED_INTERVAL="$1"
shift
if [ "${1:-}" = "--" ]; then shift; fi
JOB_NAME=$(basename "$1")
JOB_NAME="${JOB_NAME%.sh}"

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/cron_${JOB_NAME}.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo
SOURCE="nas-cron"

START_TS=$(date +%s)

# Carry the previous success timestamp forward across a failed run, so "time
# since last SUCCESS" stays meaningful through a losing streak - otherwise a
# job that's been failing every run since it broke looks identical to one
# that's healthy, since last_run_timestamp updates either way.
PREV_SUCCESS=0
if [ -f "$OUT_FILE" ]; then
  # $NF (not $2) - the metric line now has two comma-separated labels
  # (source="...", name="...") so the value isn't always field 2.
  PREV_SUCCESS=$(awk '/^scheduled_job_last_success_timestamp_seconds/ {print $NF}' "$OUT_FILE" | tail -1)
  [ -n "$PREV_SUCCESS" ] || PREV_SUCCESS=0
fi

set +e
"$@"
EXIT_CODE=$?
set -e

END_TS=$(date +%s)
DURATION=$((END_TS - START_TS))

if [ "$EXIT_CODE" -eq 0 ]; then
  SUCCESS_TS=$END_TS
else
  SUCCESS_TS=$PREV_SUCCESS
fi

{
  echo "# HELP scheduled_job_last_run_timestamp_seconds Unix timestamp of the last time this job ran, regardless of outcome."
  echo "# TYPE scheduled_job_last_run_timestamp_seconds gauge"
  echo "scheduled_job_last_run_timestamp_seconds{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} ${END_TS}"
  echo "# HELP scheduled_job_last_success_timestamp_seconds Unix timestamp of the last time this job exited 0."
  echo "# TYPE scheduled_job_last_success_timestamp_seconds gauge"
  echo "scheduled_job_last_success_timestamp_seconds{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} ${SUCCESS_TS}"
  echo "# HELP scheduled_job_last_exit_code Exit code of the most recent run (0 = success)."
  echo "# TYPE scheduled_job_last_exit_code gauge"
  echo "scheduled_job_last_exit_code{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} ${EXIT_CODE}"
  echo "# HELP scheduled_job_last_run_duration_seconds How long the most recent run took, in seconds."
  echo "# TYPE scheduled_job_last_run_duration_seconds gauge"
  echo "scheduled_job_last_run_duration_seconds{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} ${DURATION}"
  echo "# HELP scheduled_job_expected_interval_seconds Caller-supplied 'how often should this run' - the per-job timewindow override for ScheduledJobStale."
  echo "# TYPE scheduled_job_expected_interval_seconds gauge"
  echo "scheduled_job_expected_interval_seconds{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} ${EXPECTED_INTERVAL}"
  echo "# HELP scheduled_job_enabled Always 1 here - a wrapped cron job only ever writes this file while it's actually being run."
  echo "# TYPE scheduled_job_enabled gauge"
  echo "scheduled_job_enabled{source=\"${SOURCE}\", name=\"${JOB_NAME}\"} 1"
} > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"

exit "$EXIT_CODE"
