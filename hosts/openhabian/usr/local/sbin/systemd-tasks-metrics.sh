#!/bin/bash
# /usr/local/sbin/systemd-tasks-metrics.sh
#
# Exposes all of openHABian's systemd-timer-driven jobs - the Amanda backups
# (amdump-openhab-dir, amandaBackupDB), the Caddy cert renewal (acme-renew),
# and the SD-card-mirroring jobs (sdrawcopy, sdrsync) - as the shared
# `scheduled_job_*` textfile-collector family every scheduled-job source in
# this repo writes into - see docs/monitoring.md "Scheduled jobs (unified)"
# and ScheduledJobStale/ScheduledJobFailed in
# hosts/nas/.../grafana/provisioning/alerting/rules.yml. Replaces reading
# node_systemd_timer_last_trigger_seconds/node_systemd_unit_state directly
# from those alert rules - that required a label_replace() per unit and a
# staleness threshold hardcoded into the PromQL itself, since there was no
# metric to override it with.
#
# sdrawcopy/sdrsync are NOT "manual, no schedule" jobs - openhabian-config
# option 53 installs them with real sdrawcopy.timer/sdrsync.timer units
# (confirmed against github.com/openhab/openhabian's includes/SD/*.timer,
# 2026-09-12 - don't trust an earlier assumption otherwise): sdrawcopy's
# stock default is semiannual (Jan 1 + Jul 1), overridden here to every 2
# months via sdrawcopy.timer.d/schedule.conf; sdrsync fires every 2 hours.
# They get exactly the same treatment as the other three units below.
#
# scheduled_job_expected_interval_seconds is derived from the timer's own
# NextElapseUSecRealtime - LastTriggerUSec, i.e. systemd's actual next-run
# schedule rather than a guess - self-corrects if OnCalendar/
# RandomizedDelaySec ever changes. VERIFY: relies on `date -d` parsing
# systemd's `--value` date-string output (e.g. "Fri 2026-09-12 00:55:10
# CEST") - confirmed GNU-date-compatible on Debian/Raspbian, but check
# `systemctl show amdump-openhab-dir.timer -p LastTriggerUSec --value` by
# hand on this Pi before trusting it if this script ever misbehaves. Also
# means a timer that has never fired yet (fresh install) gets no
# expected_interval_seconds sample at all, not a guessed one - it only
# starts appearing in ScheduledJobStale once the timer has fired at least
# once.
#
# Install: chmod +x this file, then run via systemd-tasks-metrics.timer
# (every 5m) - see that unit for the schedule.

set -uo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="${OUT_DIR}/systemd_tasks.prom"
TMP_FILE="${OUT_FILE}.tmp"
SOURCE_TIMER="openhabian-timer"

TIMER_UNITS="amdump-openhab-dir amandaBackupDB acme-renew sdrawcopy sdrsync"

mkdir -p "$OUT_DIR"

epoch_of() {
  # systemd date-string property -> unix seconds, or 0 if never fired
  # ("n/a"/empty).
  local raw="$1"
  if [ -z "$raw" ] || [ "$raw" = "n/a" ]; then
    echo 0
  else
    date -d "$raw" +%s 2>/dev/null || echo 0
  fi
}

result_to_exit_code() {
  # systemd's Result property -> the 0=success/1=failure boolean the rest of
  # scheduled_job_* uses. Empty (unit has never run) counts as success so a
  # fresh install doesn't false-positive on ScheduledJobFailed.
  [ -z "$1" ] || [ "$1" = "success" ] && echo 0 || echo 1
}

{
  echo "# HELP scheduled_job_last_run_timestamp_seconds Unix timestamp of the last time this job ran, regardless of outcome."
  echo "# TYPE scheduled_job_last_run_timestamp_seconds gauge"
  echo "# HELP scheduled_job_last_exit_code Exit code of the most recent run (0 = success)."
  echo "# TYPE scheduled_job_last_exit_code gauge"
  echo "# HELP scheduled_job_expected_interval_seconds The per-job timewindow override for ScheduledJobStale - how often this job is expected to run, in seconds."
  echo "# TYPE scheduled_job_expected_interval_seconds gauge"
  echo "# HELP scheduled_job_enabled Whether this scheduled job is enabled (1) or disabled (0)."
  echo "# TYPE scheduled_job_enabled gauge"

  for unit in $TIMER_UNITS; do
    last_trigger_raw=$(systemctl show "${unit}.timer" -p LastTriggerUSec --value)
    next_elapse_raw=$(systemctl show "${unit}.timer" -p NextElapseUSecRealtime --value)
    result_raw=$(systemctl show "${unit}.service" -p Result --value)

    last_trigger_epoch=$(epoch_of "$last_trigger_raw")
    next_elapse_epoch=$(epoch_of "$next_elapse_raw")
    exit_code=$(result_to_exit_code "$result_raw")
    enabled=0
    systemctl is-enabled "${unit}.timer" >/dev/null 2>&1 && enabled=1

    echo "scheduled_job_last_run_timestamp_seconds{source=\"${SOURCE_TIMER}\", name=\"${unit}\"} ${last_trigger_epoch}"
    echo "scheduled_job_last_exit_code{source=\"${SOURCE_TIMER}\", name=\"${unit}\"} ${exit_code}"
    echo "scheduled_job_enabled{source=\"${SOURCE_TIMER}\", name=\"${unit}\"} ${enabled}"

    if [ "$last_trigger_epoch" -gt 0 ] && [ "$next_elapse_epoch" -gt "$last_trigger_epoch" ]; then
      interval=$((next_elapse_epoch - last_trigger_epoch))
      echo "scheduled_job_expected_interval_seconds{source=\"${SOURCE_TIMER}\", name=\"${unit}\"} ${interval}"
    fi
  done
} > "$TMP_FILE"

chmod 644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
