# openHABian monitoring

Host metrics and log shipping for the natively-run `openhabian` Raspberry Pi. General
monitoring conventions and the Zabbix migration are in [monitoring.md](monitoring.md).

## openHABian host monitoring

The `openhabian` host runs natively on a Raspberry Pi — no Docker. `node_exporter` and
Promtail are both static Go binaries installed by hand and run as systemd units (see
`hosts/openhabian/etc/`). openHABian's own scheduled backups (Amanda-based) are **systemd
timers** (`amdump-openhab-dir.timer`, `amandaBackupDB.timer`) plus a daily `acme-renew.timer`
for the Caddy TLS cert — not cron. `sdrawcopy`/`sdrsync` (SD-card mirroring, see
`99-usb-drives.rules`) are **also systemd timers**, installed by `openhabian-config` option
53 (`sdrawcopy.timer` stock default fires semiannually, Jan 1 + Jul 1; `sdrsync.timer` fires
every 2 hours) — confirmed against openHABian's own source (`github.com/openhab/openhabian`,
`includes/SD/*.timer`), not to be trusted as "manual, no schedule" like an earlier version
of this doc claimed. `sdrawcopy.timer` is overridden here to run every 2 months instead — see
`sdrawcopy.timer.d/schedule.conf`.

`systemd-tasks-metrics.sh` (`hosts/openhabian/usr/local/sbin/`, run every 5m via its own
`systemd-tasks-metrics.timer`) translates that state into the shared `scheduled_job_*`
textfile family — same idiom as `snapshot-tasks-metrics.sh` on the NAS, so
`ScheduledJobStale`/`ScheduledJobFailed` don't need to know openHABian is a different kind
of source at all. All five units (`amdump-openhab-dir`, `amandaBackupDB`, `acme-renew`,
`sdrawcopy`, `sdrsync`) are treated identically under a single `openhabian-timer` source:
last-run comes from each `.timer`'s `LastTriggerUSec`, exit code from the paired `.service`'s
`Result`. `scheduled_job_expected_interval_seconds` is computed as
`NextElapseUSecRealtime - LastTriggerUSec` — systemd's own next-scheduled-fire time minus
its last actual fire, rather than a value hardcoded per unit — so it self-corrects if a
`.timer`'s `OnCalendar`/`RandomizedDelaySec` ever changes, and needs no special-casing for
`sdrawcopy`'s much longer interval. A timer that hasn't fired even once yet (fresh install)
gets no `expected_interval_seconds` sample until it has — see the script's header comment.

node_exporter's own `--collector.systemd` is no longer used here — dropped in favor of the
textfile collector (`--collector.textfile.directory`) once `systemd-tasks-metrics.sh` covered
everything it was reading, which also retires the unit-include regex that flag required
(see git history if `node_systemd_*` metrics are ever needed again for something else).

See [backups.md](backups.md) for what those backup units actually do. openHABian also
bundles a local Grafana (port 3000, native `/metrics`) and InfluxDB **1.x** (port 8086 —
no native Prometheus endpoint; that's a 2.x-only feature). Both are covered by blackbox
reachability probes only (Grafana `/api/health`, InfluxDB `/ping`), not scraped for
internal metrics.

The Pi's address is **`192.168.1.154`** (on `lan`, same subnet as the NAS). The
`# fill in actual Pi address` TODOs still in `prometheus/config.yml` for the `openhabian`
node/blackbox jobs can be closed with that — they currently use the
`openhabian.bellecerise.local` name, which resolves fine, so it's cosmetic.

**InfluxDB `/ping` returns HTTP 204**, and the shared `http_2xx` blackbox module sets an
explicit `valid_status_codes` list (originally `[200, 401]` — 401 for token-secured
OpenHAB REST). An explicit list *disables* blackbox's default "any 2xx" acceptance, so
204 was being treated as a failure and the `blackbox-openhabian` probe for `:8086/ping`
alerted constantly. Fixed by adding `204` to that list (`[200, 204, 401]` in
`blackbox/config.yml`). The `blackbox/config.yml` was also graduated from `scratchpad/`
to `hosts/nas/.../blackbox/config.yml` at the same time (no secrets in it).

## openHABian log shipping (Promtail → Loki)

Promtail on the Pi (`hosts/openhabian/etc/promtail/config.yml`, static binary, systemd
unit, runs as **root**) tails openHAB's file logs and the systemd journal and
**dual-ships** everything to two Loki endpoints:

- `https://loki.monitoring.bellecerise.local/loki/api/v1/push` — the central NAS stack,
  through Traefik's step-ca cert. The Pi must have the **step-ca root in its system trust
  store** (`/usr/local/share/ca-certificates/…` + `update-ca-certificates`) or the push
  fails with `x509: certificate signed by unknown authority`. Promtail reads the system
  store, so no `tls_config` block is needed once the root is trusted.
- `http://127.0.0.1:3100/loki/api/v1/push` — a **second Loki running locally on the Pi**.
  openHABian does *not* bundle Loki (only Grafana + InfluxDB 1.x); it was installed by
  hand as a single-binary, filesystem-storage instance (`/usr/local/bin/loki`,
  `/etc/loki/config.yml`, `loki.service`, data under `/var/lib/loki`, retention ~14d) and
  added as a datasource to the bundled Grafana so its log panels work offline of the NAS.
  *(These `loki.service` / `/etc/loki/config.yml` / Grafana-datasource files are not yet
  tracked under `hosts/openhabian/`.)*

Scrape jobs and the reasons they're shaped the way they are:

- `openhab` (`/var/log/openhab/openhab.log`) and `events` (`/var/log/openhab/events.log`)
  — same openHAB log4j2 format (`multiline` on the `yyyy-MM-dd HH:mm:ss.SSS` line start +
  `regex` pulling `level`/`logger` + `timestamp`). `events.log` gets its **own `job`
  label** (not folded into `openhab`) because it's high-volume and you'll want to
  mute/drop it independently.
- `habapp` (`/var/log/openhab/HABApp.log`) — HabApp's Python-logging format, which per the
  install's `logging.yml` has **no milliseconds** and remaps `WARNING`→`WARN`; the regex
  and `timestamp` format account for both. This file is low-volume by design — the
  chatty `HABApp.EventBus` logger goes to `HABApp_events.log`, shipped separately as its
  own `habapp_events` job (see below). HabApp is the switch away from JSR223 JS rules;
  its file logging is what made Loki analysis viable.
- `habapp_events` (`/var/log/openhab/HABApp_events.log`) — the `HABApp.EventBus` firehose:
  rotated at 5×16 MB, roughly every 7 minutes at INFO. The local file's size was never
  actually at risk — `RotatingFileHandler`'s `maxBytes`/`backupCount` cap it regardless of
  volume, so the Pi's size-capped zram `/var/log` (`zram-config`, limit in `/etc/ztab`)
  isn't the constraint; the real cost of shipping it is Loki *storage* (both the local Pi
  Loki and the NAS one) growing with line volume, unbounded by that local rotation cap.
  Originally left unshipped over that concern; shipped as of 2026-09-12 at the user's
  request. `HABApp.EventBus` was briefly dropped to `WARNING` the same day (HABApp isn't
  running real rules yet — just connected ahead of a future migration off JSR223 — so the
  firehose had no signal worth the ingest cost), then reverted back to `INFO` by the user:
  a manual "remember to flip this back to INFO once the migration starts" step is a footgun
  they'd rather not carry. Measured over one hour at unfiltered INFO: ~60%
  `ThingStatusInfoEvent` + ~40% `ItemStateUpdatedEvent` (both fire on every poll, changed or
  not) vs. ~0.5% `ItemStateChangedEvent` (an actual change) — unlike openHAB core's
  `events.log`, HABApp's `EventBus` logger has no equivalent changed-only allow-list;
  per-rule `EventFilter`s (`ValueUpdateEventFilter` vs `ValueChangeEventFilter`) exist but
  don't apply to this global logger. A `HABAppUser.py` startup module (HABApp's own
  documented hook for registering a `logging.Filter` programmatically, since `logging.yml`
  has no declarative way to do it) was tried and then **reverted** — it dropped
  `ItemStateUpdatedEvent`/`ThingStatusInfoEvent` at INFO on the assumption that any real
  change is always also captured by the corresponding `ItemStateChangedEvent`. That
  assumption doesn't reliably hold: openhab-core#1092 documents `ItemStateEvent` (the
  `ItemStateUpdatedEvent` class) and `ItemStateChangedEvent` reporting genuinely different
  values for the same transition when a command is involved (e.g. a color item's `OFF`
  command shows as `OFF` in the updated-event but `0,0,0` in the changed-event) — so a
  blanket drop of `ItemStateUpdatedEvent` risks losing information the changed-event never
  carries, not just deduplicating it. Revisit only with a narrower rule (e.g. drop
  `ThingStatusInfoEvent` alone, which doesn't have this discrepancy) if the volume becomes
  a real problem rather than a cosmetic one; a promtail `drop` stage remains the
  lower-risk fallback (still writes the full firehose locally first, but at least doesn't
  discard anything HABApp itself might one day read off its internal event bus). Its
  `logging.yml` formatter (`HABApp_events_format`) still drops the fixed-width padding
  (`%(name)25s`/`%(levelname)8s`) that `HABApp_format` uses, to keep per-line cost down at
  this volume. Own `job` label for the same mute/drop-independently reason as `events`. The
  "Log Line Rate by File" panel on the `home-automation-overview` Grafana dashboard plots
  this alongside `openhab`/`events`/`habapp` on a log-scaled Y axis for exactly this reason
  — at full volume habapp_events outnumbers the other three by 2-3 orders of magnitude and
  flattens them on a linear axis.
- `journal` (systemd journal via the `sd-journal` API, **not** a file). This install is
  journald-only — there is **no `/var/log/syslog`** — so a file-based syslog job would
  tail nothing. The journal target also sidesteps any inotify-on-overlayfs quirks since
  it doesn't watch files at all.

Because logs now land in Loki in near-real-time, Loki is the durable copy — keep the
on-Pi `maxBytes`/`backupCount` (openHAB `log4j2.xml`, HabApp `logging.yml`) modest to
stay inside the zram budget; losing unsynced `/var/log` on a power cut no longer loses
data that already shipped.

Note: Promtail is deprecated upstream (folded into Grafana Alloy, removed from Loki as of
3.7.3). The standalone binary still works and matches the NAS stack's `grafana/promtail`
— an eventual Alloy migration is the long-term path for both hosts.

## Pi storage health (SSD SMART, SD cards)

- **SSD:** `smartctl_exporter` runs natively on the Pi (`smartctl_exporter.service`, root, `:9633`,
  scrapes only `/dev/ssd;sat`, the stable name from `99-usb-drives.rules`). Its target is part of the
  existing `smartctl` Prometheus job, so `DiskSmartFailure` and `DiskTemperatureHigh` cover it with no
  rule change. Install: `smartmontools` package plus the `linux-armv7`/`linux-arm64` release binary of
  `prometheus-community/smartctl_exporter` (v0.15.0 ships both) in `/usr/local/bin`; check
  `smartctl -a /dev/ssd` and drop `;sat` if the bridge doesn't need it.
- **SD cards expose no SMART.** The running card (`mmcblk0`, SanDisk `SN32G`) has no `life_time` /
  `pre_eol_info` either, so there is no wear percentage. Failure is caught indirectly:
  `PiStorageIoErrors` (Loki: kernel `mmcblk0` errors, `EXT4-fs error`, `Buffer I/O error`,
  `blk_update_request`, forced read-only remounts in the last 10m). ext4's "last error at" /
  "initial error at" lines print at every mount and are deliberately not matched. The mirror card
  in the USB reader (`/dev/sdbackup`) is covered by the `sdrawcopy`/`sdrsync` scheduled-job alerts.
- **Write load (2026-10-14).** The SSD was taking ~178 GB/day, almost all from openHAB's own MapDB
  persistence (`everyUpdate`; fixed in the openHAB config, which is outside this repo). The boot SD card
  takes a steady ~70 KB/s (~6 GB/day), mostly the Pi-local Loki (`/var/lib/loki` is on the card), so
  its WAL is disabled in `loki/config.yml`: a crash loses the last few minutes of the Pi's local logs,
  the copy shipped to the NAS is unaffected. Compare `rate(node_disk_written_bytes_total{device="mmcblk0"})`
  and the dashboard's "Written (24h)" panel before/after. Moving Loki to the SSD is not worth it while
  the SSD has bad sectors.
- The `EXT4-fs error (device sdb1)` lines from 2026-10-13 (the damaged SSD) match `PiStorageIoErrors`.
