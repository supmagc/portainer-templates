# Monitoring

The "why" behind the Prometheus + Grafana + Loki stack and the dashboard/exporter
gotchas that took real debugging to find. Read this before making non-trivial changes to
the monitoring stack or dashboards; it'll save you from re-discovering things the hard
way.

## The monitoring migration

The `monitoring` stack used to run Zabbix. It broke (bind mounts moved between ZFS pools
without the target dataset being provisioned) and was fully replaced with
**Prometheus + Grafana + Loki + Promtail**, rather than debugged, because every exporter
and dashboard pattern in this space is Prometheus-first anyway.

**Alerting lives in Grafana itself, not Alertmanager.** An earlier design (still visible
in `C:\Users\supma\.claude\plans\i-want-to-replace-cheeky-lark.md` if you go looking) used
Alertmanager + an Apprise relay for Pushover notifications. That was abandoned in favor of
Grafana's own native alerting (it already has a Pushover contact point). **That plan file
is stale — do not treat it as a description of the current system.** Prometheus is
scrape/storage/query only; `compose/docker-compose-monitoring.yml` has no `alertmanager` or
`apprise` service, and shouldn't gain one without a real reason to revisit that decision.

Grafana alert rules are provisioned as YAML
(`hosts/nas/.../grafana/provisioning/alerting/rules.yml`); the contact point and
notification policy are **not** provisioned as files — they're set by hand in the Grafana
UI and hold a real Pushover token, which is exactly why they stay out of git.

## Grafana provisioning "locked" gotcha

Deleting or emptying a provisioning file (e.g. `rules.yml`) does **not** free those
resources back to UI editing — Grafana keeps a DB-side lock on anything it once
provisioned. You need explicit `deleteRules:` / `deleteContactPoints:` / `resetPolicies:`
directives in the YAML to un-provision something. Dashboards are provisioned with
`allowUiUpdates: true` specifically so they *can* be tweaked live in the UI without this
problem — alert rules currently are not set up that way, by choice (they're generic
enough that UI tuning wasn't worth losing file-based history for).

## Table panels: `merge` vs `joinByField`

Several dashboards show one row per entity (disk, ZFS pool, cron job, scheduled task) by
combining multiple Prometheus queries into a table. Grafana's `merge` transform does this
by matching on a frame's full field signature — which silently breaks (duplicate or
garbled rows, or stray `__name__`/`__name__1`/`__name__2...` columns) as soon as the
queries being combined have asymmetric extra labels. **`joinByField` with an explicit
`byField` key is the correct, robust pattern for every multi-target table panel in this
repo** — always exclude the `__name__` field(s) it exposes (they're normally hidden under
`merge`, but real and visible under `joinByField`).

## The `job` / `exported_job` label collision

If a textfile-collector metric uses a label literally named `job` (as `cron-wrapper.sh`'s
output does — `cron_job_last_exit_code{job="<task-name>", ...}`), it collides with
Prometheus's own scrape-config meta-label of the same name (`job: node` for the node
exporter target). Prometheus's default collision handling **keeps its own value and
renames the metric's own label to `exported_job`** — so every cron-job metric ends up with
a useless constant `job="node"` and the real per-task identity lives in `exported_job`
instead. This bit the NAS dashboard's Cron Jobs table (it was joining on the wrong,
constant field) and the matching Grafana alert-rule annotations (`{{ $labels.job }}` was
always "node"). Fixed by joining/templating on `exported_job`. **This isn't a one-off
bug** — it'll recur for any future textfile-collector metric that names a label `job`;
name it something else (`task`, `dataset`, `pool`, ...) to avoid it entirely.

## TrueNAS-native scheduled-task visibility

Two categories of TrueNAS-scheduled work needed monitoring, neither of which is a Linux
cron job:
- **Cron Jobs** (Settings → Tasks → Cron Jobs) genuinely are plain `/etc/cron.d/middlewared`
  crontab entries — `cron-wrapper.sh` wraps the actual command, timing it and recording
  exit code/duration/last-run/last-success as textfile-collector metrics.
- **Periodic Snapshot Tasks** and **Cloud Sync Tasks** are middleware-scheduled
  (zettarepl), not cron at all — there's no command of ours to wrap. Instead,
  `snapshot-tasks-metrics.sh` / `cloudsync-tasks-metrics.sh` poll `midclt`, TrueNAS's local
  CLI (talks to middleware over a Unix socket as root — no API token, no network exposure)
  and translate `pool.snapshottask.query` / `cloudsync.query` output into the same textfile
  format. **`cloudsync.query` includes each task's `credentials` block with real provider
  secrets in plaintext (e.g. a Backblaze B2 key) — the parser must never read that field.**
  It currently only touches `id`/`description`/`enabled`/`job.state`/`job.time_finished`;
  keep it that way if this script is ever extended.
- A third-party TrueNAS API exporter (`alexlmiller/truenas-grafana`, needs an API token)
  was considered and explicitly rejected in favor of the local `midclt` approach — no
  externally-facing token to manage, no extra moving part.
- No maintained ZFS-exporter *container image* exists (checked more than once —
  `pdf/zfs_exporter` and `ncabatoff/zfs-exporter` are both source-only/unpublished). Pool
  capacity/health instead comes from `zpool-metrics.sh`, a plain host-side script run via
  TrueNAS cron (must run as **root** — `zpool`/`zfs` CLI calls fail for the unprivileged
  `nas_processes` user; the script `chown`s its output back to `nas_processes` afterward so
  node-exporter's non-root container can still read it through the read-only bind mount).

## openHABian host monitoring

The `openhabian` host runs natively on a Raspberry Pi — no Docker. `node_exporter` and
Promtail are both static Go binaries installed by hand and run as systemd units (see
`hosts/openhabian/etc/`). openHABian's own scheduled backups (Amanda-based) are **systemd
timers** (`amdump-*.timer`, `amandaBackupDB.timer`, `sdrawcopy.timer`, `sdrsync.timer`),
not cron — so they're visible for free via `node_exporter --collector.systemd`, no wrapper
script needed (unlike TrueNAS, which has no systemd-timer option for its scheduled tasks).
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
  chatty `HABApp.EventBus` logger goes to `HABApp_events.log`, which is **deliberately not
  shipped**: it rotates all 5×16 MB backups roughly every 7 minutes, and `/var/log` is on
  size-capped zram (openHABian `zram-config`, limit in `/etc/ztab`), so shipping it would
  blow the zram budget and swamp the Pi's Loki. HabApp is the switch away from JSR223 JS
  rules; its file logging is what made Loki analysis viable.
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

## Other resolved investigations, briefly

- **Router traffic panel "triple-counting"**: `network-details.json` was summing
  aggregating interfaces (`eth0`/`wan`/`br-lan`) alongside their physical members —
  excluded now.
- **Traefik metrics**: weren't enabled at all originally; fixed by adding a dedicated
  internal `metrics` entrypoint (`:8082`) to `compose/docker-compose-networking.yml`, not exposed
  via `ports:`.
- **RabbitMQ/nextcloud-web blackbox probe failures** (from an earlier debugging pass):
  RabbitMQ's management listener is IPv4-only but Docker handed blackbox an AAAA record
  too (fixed with `preferred_ip_protocol: ip4`); `nextcloud-web` redirects `/` to a login
  page that a default blackbox module wouldn't follow correctly (fixed with a dedicated
  `http_2xx_redirect` module/job — blackbox's `params.module` is job-scoped, not
  target-scoped, which is why mixed-behavior targets need their own job repeatedly
  throughout `prometheus.yml`, e.g. `blackbox-http` vs `blackbox-https` vs
  `blackbox-http-redirect` vs `blackbox-tls-cert`).
- **SNMP (switch/APs)**: works end-to-end now, after a chain of firewall/routing fixes and
  a scrape-timeout bump — see [network.md](network.md#snmp-switchaps).
- **Multimedia exporters** (`exportarr-prowlarr`, Seerr): see
  [multimedia.md](multimedia.md).

See [../AGENTS.md](../AGENTS.md) for the verify-before-writing discipline that caught
several of these.
