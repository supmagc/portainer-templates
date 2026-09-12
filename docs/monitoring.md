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
original output did — `cron_job_last_exit_code{job="<task-name>", ...}`), it collides with
Prometheus's own scrape-config meta-label of the same name (`job: node` for the node
exporter target). Prometheus's default collision handling **keeps its own value and
renames the metric's own label to `exported_job`** — so every cron-job metric ended up with
a useless constant `job="node"` and the real per-task identity lived in `exported_job`
instead. This bit the NAS dashboard's Cron Jobs table (it was joining on the wrong,
constant field) and the matching Grafana alert-rule annotations (`{{ $labels.job }}` was
always "node"). **This isn't a one-off bug** — it recurs for any textfile-collector metric
that names a label `job`; name it something else instead. The unified `scheduled_job_*`
family below (see "Scheduled jobs (unified)") was designed around this from the start —
its job-identity label is called `name`, not `job`, so it never hits the collision at all.

## Scheduled jobs (unified)

Every scheduled-job source in this repo — NAS cron jobs, TrueNAS's Periodic Snapshot/Cloud
Sync Tasks, and openHABian's systemd-timer backups — writes into one shared
textfile-collector metric family, described below, which feeds the *same* two Grafana
alert rules (`ScheduledJobStale`, `ScheduledJobFailed` in
`grafana/provisioning/alerting/rules.yml`). One alert to know about regardless of source,
and both rules are a single plain PromQL expression each — no per-source branches.

This wasn't always true: openHABian's timers used to be read directly off
node_exporter's own `--collector.systemd` metrics, which forced a `label_replace()` per
unit and a staleness threshold hardcoded into the PromQL itself (no metric existed to
override it with). `systemd-tasks-metrics.sh` (see "openHABian host monitoring" below)
replaced that with the same textfile idiom every other source already used, so the
rules no longer need to know openHABian exists at all.

Adding a new source later is always the same: write the `scheduled_job_*` family below —
no rule changes needed, it's already wired in.

The shared `scheduled_job_*` family:

- `scheduled_job_last_run_timestamp_seconds{source, name}`
- `scheduled_job_last_exit_code{source, name}` — 0 = success, nonzero = failure (exact
  value beyond that is source-dependent, e.g. a real shell exit code for cron jobs vs. a
  generic 1 for anything that only reports a boolean success/fail)
- `scheduled_job_expected_interval_seconds{source, name}` — the per-job **timewindow
  override** `ScheduledJobStale` compares `time() - last_run` against. Each source decides
  its own: `cron-wrapper.sh` takes it as a required argument from the TrueNAS Cron Job's
  command line, the TrueNAS-task scripts estimate it from the task's own schedule (falling
  back to a conservative 8 days if the schedule shape isn't recognized).
- `scheduled_job_enabled{source, name}` — both alert rules `and` against `== 1`, so a
  disabled TrueNAS task never fires either one.

Labels are deliberately just `{source, name}` — see "The `job`/`exported_job` label
collision" above for why `name` and not `job`.

Sources as of this writing:
- **`nas-cron`** — `cron-wrapper.sh` wraps a TrueNAS Cron Job's actual command, timing it
  and recording exit code/duration/last-run as `cron_<job-name>.prom`. Only `zpool-metrics`
  is wrapped so far.
- **`nas-snapshot`** / **`nas-cloudsync`** — Periodic Snapshot Tasks and Cloud Sync Tasks
  are middleware-scheduled (zettarepl), not cron at all — there's no command of ours to
  wrap. Instead, `snapshot-tasks-metrics.sh` / `cloudsync-tasks-metrics.sh` poll `midclt`,
  TrueNAS's local CLI (talks to middleware over a Unix socket as root — no API token, no
  network exposure) and translate `pool.snapshottask.query` / `cloudsync.query` output into
  both the original per-source `snapshot_task_*`/`cloudsync_task_*` metrics (kept for the
  dashboard's state-string column) and the shared `scheduled_job_*` family. **`cloudsync.query`
  includes each task's `credentials` block with real provider secrets in plaintext (e.g. a
  Backblaze B2 key) — the parser must never read that field.** It currently only touches
  `id`/`description`/`enabled`/`schedule`/`job.state`/`job.time_finished`; keep it that way
  if this script is ever extended.
- **`openhabian-timer`** / **`openhabian-manual`** — see "openHABian host monitoring" below.
- A third-party TrueNAS API exporter (`alexlmiller/truenas-grafana`, needs an API token)
  was considered and explicitly rejected in favor of the local `midclt` approach — no
  externally-facing token to manage, no extra moving part.
- No maintained ZFS-exporter *container image* exists (checked more than once —
  `pdf/zfs_exporter` and `ncabatoff/zfs-exporter` are both source-only/unpublished). Pool
  capacity/health instead comes from `zpool-metrics.sh`, a plain host-side script run via
  TrueNAS cron (must run as **root** — `zpool`/`zfs` CLI calls fail for the unprivileged
  `nas_processes` user; the script `chown`s its output back to `nas_processes` afterward so
  node-exporter's non-root container can still read it through the read-only bind mount).
  `zpool-metrics.sh` itself is a data script, not a scheduled-job source — its own
  execution is what `nas-cron`/`zpool-metrics` monitors, via the `cron-wrapper.sh` that
  runs it.

## openHABian host monitoring

The `openhabian` host runs natively on a Raspberry Pi — no Docker. `node_exporter` and
Promtail are both static Go binaries installed by hand and run as systemd units (see
`hosts/openhabian/etc/`). openHABian's own scheduled backups (Amanda-based) are **systemd
timers** (`amdump-openhab-dir.timer`, `amandaBackupDB.timer`) plus a daily `acme-renew.timer`
for the Caddy TLS cert — not cron. `sdrawcopy`/`sdrsync` (SD-card mirroring, see
`99-usb-drives.rules`) are **also systemd timers**, installed by `openhabian-config` option
53 (`sdrawcopy.timer` fires semiannually, Jan 1 + Jul 1; `sdrsync.timer` fires every 2 hours)
— confirmed against openHABian's own source (`github.com/openhab/openhabian`,
`includes/SD/*.timer`), not to be trusted as "manual, no schedule" like an earlier version
of this doc claimed.

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

## ICMP reachability probes (network hosts)

The goal is that *every* endpoint has a liveness signal, including hosts that run no
exporter of their own — the openHAB field-bus boxes (Wago Modbus, Entec DMX, OneWire),
the switch/APs (SNMP covers their interfaces, not "is the box up"), and each of the
router's per-VLAN interface IPs. Those get a plain ping via blackbox-exporter's `icmp`
prober.

- **`icmp` module** in `blackbox/config.yml` — `preferred_ip_protocol: ip4`,
  `ip_protocol_fallback: false`, same shape as the HTTP modules.
- **`blackbox-exporter` needs `cap_add: [NET_RAW]`** (`compose/docker-compose-monitoring.yml`).
  The `prom/blackbox-exporter` image runs as an unprivileged user and the `icmp` prober
  opens raw sockets — without the cap every ICMP probe fails with a permission error.
  Adding the cap is a **container recreate**, not a reload.
- **`blackbox-icmp` job** (`prometheus/config.yml`) — one static list, each target
  labelled `role` (`router`/`nas`/`switch`/`ap`/`host`/`field-device`) plus `name`, and
  `vlan`/`iface` where one box has several IPs (the router has four, the NAS has four).
  No new alert rule needed: the existing `ProbeFailing` rule matches any `probe_success`
  series with no job filter.

**Reachability caveat — some targets are expected down until a firewall rule is added.**
blackbox-exporter runs on the NAS, which is on `lan` (192.168.1.0/24):

- `192.168.1.x` targets are same-subnet and probe directly.
- `192.168.0.x` (admin VLAN: router, switch, APs) ride the same `lan`→`admin` path SNMP
  uses, but that rule is UDP/161-scoped — ICMP echo needs its own `lan`→`admin` allow or
  these page as down (see [network.md](network.md)).
- The `guest` / `work` router interface IPs (`192.168.2.1` / `192.168.3.1`) are in
  isolated zones with no `lan`→`*` forward at all — down until one is added. They're left
  in the target list on purpose so it's literally "all endpoints"; comment them out to
  silence.

## Traefik-endpoint discovery for blackbox (planned, not built)

The blackbox HTTP/TLS target lists in `prometheus/config.yml` are hand-maintained and
already carry `# VERIFY` / `# fill in` TODOs. The intended fix is to make Traefik the
source of truth: a host-side script (same idiom as `zpool-metrics.sh` /
`cloudsync-tasks-metrics.sh`) polls `traefik:8080/api/http/routers` and
`traefik-edge:8080/api/http/routers`, pulls the `Host(...)` rules out of each router, and
writes a Prometheus `file_sd` JSON to a bind-mounted dir. `blackbox-traefik` /
`blackbox-traefik-edge` jobs then take targets from `file_sd_configs` + the existing
`http_2xx*` / `tls_connect` modules — add a service to a compose stack, it gets probed,
no Prometheus edit. Not implemented yet.

## Container health alerting (`ContainerUnhealthy`)

`ContainerCrashLooping` only fires on containers that are *restarting*
(`changes(container_start_time_seconds[15m]) > 2`). A container whose Docker
healthcheck is failing while its process stays up never restarts on its own —
`restart: unless-stopped` reacts to the process exiting, not to health status (only
Swarm acts on `unhealthy`, and there's no autoheal sidecar here). So an
unhealthy-but-alive container sat silently until `ContainerUnhealthy` was added.

That rule queries **`container_health_state{name!=""} >= 0`** (threshold fires at
`< 1`, i.e. exactly `0`) with `for: 10m`. Value encoding, confirmed against the
live instance: `-1` = no `HEALTHCHECK` defined (most containers here), `0` =
unhealthy **or** still in `starting`, `1` = healthy. The `>= 0` (rather than a
PromQL `== 0` filter) means **every health-checked container produces an alert
instance** — Normal at `1`, Alerting at `0` — the same all-series listing you get
from `ProbeFailing`, instead of the rule only ever showing the one bad container.
The `-1` (no-healthcheck) containers drop out since there's nothing to alert on.
The `0` state is hit routinely on every healthchecked container's restart, so the
10m `for:` is load-bearing — it's what separates a genuinely stuck container from
one that's still inside its `start_period`. Nothing in this stack takes >10m to go
healthy. `noDataState` is `NoData` (not `OK`): the query returns a row per
health-checked container whenever cAdvisor is up, so "no data" means cAdvisor is
down (TargetDown covers that) or the forked image has lost the metric — both
worth surfacing.

Why the difference in behaviour vs. a rule like `ProbeFailing`: both use the
identical `query → reduce → threshold` node structure. `ProbeFailing`'s query is
bare `probe_success`, so Prometheus returns a series for every target and the
*Grafana threshold* decides firing per-series. A PromQL filter like
`container_health_state == 0` instead drops every non-matching series before it
ever reaches Grafana, so only the firing ones exist as instances.

**Gotcha:** `container_health_state` is **not in any stock cAdvisor release**
(checked `master` and v0.49–v0.53). It matches an unmerged upstream PR, so the
running `cadvisor` must be a patched/forked build despite
`docker-compose-monitoring.yml` pinning `gcr.io/cadvisor/cadvisor:latest`. A plain
image repull (watchtower, or a manual pull) would silently drop the metric, and
the rule's `noDataState: OK` would hide that. If this rule ever goes quiet,
check the metric still exists before trusting the silence. The durable fix, if it
comes to that, is a `docker inspect`-based textfile-collector script in the
`cron-wrapper.sh` mould rather than depending on the forked image.

**"Only spotweb shows a health status" was a misread**, not a coverage gap
(investigated 2026-09-08). Roughly half the fleet reports `1` — the `-1`s are
just third-party images with no `HEALTHCHECK`; the LinuxServer.io images all
bundle one. spotweb is simply the only container sitting at `0`, so it's the only
row `ContainerUnhealthy` (or any "not healthy" filter) has to show. It has been
continuously unhealthy since before 2026-09-07 — the alert is doing its job; the
container or its healthcheck is genuinely broken. The `container-overview`
dashboard now surfaces the full picture: a **Container Health** piechart
(healthy / unhealthy / no-healthcheck counts), a **Needs Attention** table
(`container_health_state=0` or `changes(container_start_time_seconds[15m])>2`,
sitting next to "Containers with Errors"), and per-container **Health** /
**Restarts (15m)** stat tiles in each repeated row.

## PostgreSQL / BitMagnet / Spotweb dashboard data

Added 2026-09-09 alongside the BitMagnet indexer and its shared Postgres cluster.

- **PostgreSQL** (`utilities-overview`, new row): `postgres-exporter` in the
  `monitoring` stack, on `utilities_default`, pointed at the shared `postgres` (utilities
  stack) via `DATA_SOURCE_URI` + `DATA_SOURCE_PASS_FILE` (secret `postgres-exporter-pass`).
  Needs a dedicated login role — `CREATE ROLE postgres_exporter LOGIN PASSWORD '…'; GRANT
  pg_monitor TO postgres_exporter;` — run once by hand (same as BitMagnet's own role).
  `--collector.database` is set explicitly so `pg_database_size_bytes` (the per-DB
  "Databases" table + BitMagnet's "Index Size") is populated; without it that collector
  can be off and those panels read empty.
- **BitMagnet** (`multimedia-overview`, new row): native `/metrics` on `:3333` (the
  `prometheus` component of its `http_server`), scraped directly as `job=bitmagnet`.
  BitMagnet exposes only runtime/HTTP metrics there — **torrent count and DHT crawl rate
  live in its GraphQL API, not Prometheus** — so the section tracks up/health/resources
  plus `pg_database_size_bytes{datname="bitmagnet"}` as the index-growth proxy. If
  `bitmagnet_*` counters do show up on the live endpoint, add crawl panels then.
- **Spotweb** has no exporter and never will; its panels read the `spotweb` schema
  through `mysqld-exporter` (`--collect.info_schema.tables` was already on):
  `mysql_info_schema_table_rows{schema="spotweb",table="spots"}` is the spot count,
  `mysql_info_schema_table_size{…,component=~"data_length|index_length"}` the DB size.
- **MariaDB "Databases by Size" table**: same `mysql_info_schema_table_*` metrics, summed
  `by (schema)`; `component="data_free"` surfaced as the "Reclaimable" column
  (fragmentation / pending `OPTIMIZE TABLE`).

## Other resolved investigations, briefly

- **spotweb stuck `container_health_state=0`** (2026-09-08): the app was fine all
  along — the compose `healthcheck` ran `curl -f`, but the `jgeusebroek/spotweb`
  image ships only `php-curl`, not the `curl` CLI (it has `wget`). `["CMD", curl…]`
  exits 127 every probe. Switched the test to `wget -q --spider`.
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
