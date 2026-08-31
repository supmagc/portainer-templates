#!/bin/sh
# Generic cron-job wrapper for node-exporter's textfile collector - the standard
# "dead man's switch" pattern for Prometheus-monitored cron jobs. Records
# last-run time, last-success time, last exit code, and duration for whatever
# command it wraps, so Prometheus/Alertmanager can catch a cron job that stops
# running entirely (not just one that errors loudly and gets noticed by
# TrueNAS's own cron-failure email). See CronJobStale/CronJobFailed in
# grafana/provisioning/alerting/rules.yml.
#
# Usage in a TrueNAS Cron Job's Command field - wrap the existing command,
# don't replace it:
#   /path/to/cron-wrapper.sh <job-name> -- <actual command and args...>
#
# Example (see zpool-metrics.sh's own header for the full cron setup):
#   /mnt/fastpool/system/scripts/cron-wrapper.sh zpool-metrics -- /mnt/fastpool/system/scripts/zpool-metrics.sh
#
# Each wrapped job gets its own file (cron_<job-name>.prom) rather than a
# shared one, so concurrent cron jobs never race on the same file - matches
# node-exporter's textfile collector, which merges every *.prom file in the
# directory automatically, and mirrors zpool-metrics.sh's own atomic
# write-then-rename pattern.
#
# Install: chmod +x this file. It has no cron entry of its own - it wraps
# whatever job's command line you point it at.

set -eu

JOB_NAME="$1"
shift
if [ "${1:-}" = "--" ]; then shift; fi

OUT_DIR="/mnt/fastpool/system/processes/node-exporter/textfile"
OUT_FILE="${OUT_DIR}/cron_${JOB_NAME}.prom"
TMP_FILE="${OUT_FILE}.tmp"
OUTPUT_OWNER="nas_processes:nas_processes"   # matches ${APP_USER}:${APP_GROUP} elsewhere in this repo

START_TS=$(date +%s)

# Carry the previous success timestamp forward across a failed run, so "time
# since last SUCCESS" stays meaningful through a losing streak - otherwise a
# job that's been failing every run since it broke looks identical to one
# that's healthy, since last_run_timestamp updates either way.
PREV_SUCCESS=0
if [ -f "$OUT_FILE" ]; then
  PREV_SUCCESS=$(awk '/^cron_job_last_success_timestamp_seconds/ {print $2}' "$OUT_FILE" | tail -1)
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
  echo "# HELP cron_job_last_run_timestamp_seconds Unix timestamp of the last time this job ran, regardless of outcome."
  echo "# TYPE cron_job_last_run_timestamp_seconds gauge"
  echo "cron_job_last_run_timestamp_seconds{job=\"${JOB_NAME}\"} ${END_TS}"
  echo "# HELP cron_job_last_success_timestamp_seconds Unix timestamp of the last time this job exited 0."
  echo "# TYPE cron_job_last_success_timestamp_seconds gauge"
  echo "cron_job_last_success_timestamp_seconds{job=\"${JOB_NAME}\"} ${SUCCESS_TS}"
  echo "# HELP cron_job_last_exit_code Exit code of the most recent run (0 = success)."
  echo "# TYPE cron_job_last_exit_code gauge"
  echo "cron_job_last_exit_code{job=\"${JOB_NAME}\"} ${EXIT_CODE}"
  echo "# HELP cron_job_last_run_duration_seconds How long the most recent run took, in seconds."
  echo "# TYPE cron_job_last_run_duration_seconds gauge"
  echo "cron_job_last_run_duration_seconds{job=\"${JOB_NAME}\"} ${DURATION}"
} > "$TMP_FILE"

chown "$OUTPUT_OWNER" "$TMP_FILE"
chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"

exit "$EXIT_CODE"
