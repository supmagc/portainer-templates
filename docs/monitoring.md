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

Several dashboards show one row per entity (disk, ZFS pool, cron job, scheduled task,
DAG run) by combining multiple Prometheus queries into a table. Grafana's `merge`
transform does this by matching on a frame's full field signature — which silently breaks
(duplicate or garbled rows, or stray `__name__`/`__name__1`/`__name__2...` columns) as soon
as the queries being combined have asymmetric extra labels. **`joinByField` with an
explicit `byField` key is the correct, robust pattern for every multi-target table panel in
this repo.**

**The `__name__`/stray-label gotcha, and the fix that actually works:** every target's raw
result carries `__name__` (the metric name) plus whatever labels that specific metric has —
these are never identical across different targets (different metric names, and often
different label sets even for the *same* metric filtered two different ways, e.g.
`metric{status="a"}` vs `metric{status="b"}`). `joinByField` only merges the one field named
in `byField`; every other field that collides by name across targets survives as a separate,
unmergeable column, and Grafana disambiguates the collision by renaming them `__name__ 1`,
`__name__ 2`, `status 1`, `status 2`, etc. **An `organize` step's `excludeByName` placed
*after* `joinByField` can no longer catch these** — it's looking for a field literally named
`__name__`, and that field doesn't exist anymore, only the renamed `__name__ 1`/`__name__ 2`
does. The fix is to run `organize` (stripping `__name__` and any other non-shared label)
*as a separate transformation step before* `joinByField`, so the collision never happens:

```json
"transformations": [
  { "id": "organize", "options": { "excludeByName": { "__name__": true, "datid": true, "job": true, "instance": true } } },
  { "id": "joinByField", "options": { "byField": "datname", "mode": "outer" } },
  { "id": "organize", "options": { "renameByName": { "datname": "Database", "Value #A": "Size", "Value #B": "Connections" } } }
]
```

Aggregating a target with `sum by (<join field>) (...)` also avoids the problem for that
target specifically — aggregation drops `__name__` and every label not named in `by (...)`
— but that only works when the value itself should legitimately be summed; it's not a
substitute for the pre-join `organize` step when a target is a plain metric selector (the
`Databases` table below combines both: `Size`/`Connections` are plain selectors that need
the `organize` step, `Commits/s` is pre-aggregated and wouldn't need it on its own).

Hit twice for real: the `Databases` table (PostgreSQL row, `utilities-overview.json`) joined
`pg_database_size_bytes` and `pg_stat_database_numbackends` — the latter also carries a
`datid` label the former doesn't — producing `__name__ 1`/`__name__ 2` columns until the
pre-join `organize` was added. The Dagu `DAG Runs Today` table hit the same thing joining
`dagu_dag_runs_total_by_dag{status="succeeded"}` against the same metric filtered to
`status="failed"` — identical metric name, different `status` value, same collision.

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

## Traefik-endpoint discovery for blackbox

The blackbox HTTP/TLS target lists in `prometheus/config.yml` are otherwise
hand-maintained. Traefik is the source of truth for what it's actually routing instead:
`dagu/scripts/traefik-endpoints-metrics.py`, run every 15m by the Dagu
`traefik-endpoints` DAG (see "Dagu (container-native scheduler)" below), polls
`traefik:8080/api/http/routers` and `traefik-edge:8080/api/http/routers`, pulls the
`Host(...)` rule(s) out of each enabled, non-`internal`-provider router, and writes three
Prometheus `file_sd` JSON files to a bind-mounted dir
(`/mnt/fastpool/system/processes/prometheus/file_sd`, mounted read-only into the
`prometheus` container): `traefik-http.json` / `traefik-https.json` (split by whether the
router has TLS, feeding the `blackbox-traefik-http` / `blackbox-traefik-https` jobs and
the existing `http_2xx` / `http_2xx_insecure` modules) and `traefik-tls-cert.json` (TLS
routers only, `host:443` targets, feeding `blackbox-traefik-tls-cert` +  `tls_connect` —
this is the "possibly certificates" half: `tls_connect`'s `probe_ssl_earliest_cert_expiry`
covers cert-expiry alerting for free, no separate cert-parsing metric needed). Add a
`traefik.enable=true` service to any stack and it gets probed within 15 minutes, no
Prometheus edit.

**Pruned (2026-09-21):** all six now-redundant TLS entries were dropped from the
hand-maintained `blackbox-tls-cert` list — the three `*.media.bellecerise.local` hosts
(`emby`, `nextcloud`, `seerr`, fronted by `traefik`) and the three `*.bellecerise.be`
public hosts (fronted by `traefik-edge`'s dynamic-file routers, e.g. `emby.yml`). All six
are now covered by `blackbox-traefik-tls-cert` file_sd instead. `blackbox-http`'s
`emby`/`rabbitmq`/`seerr` container-DNS targets stay: those probe the app directly
(`http://emby:8096/...`), not the same thing as Traefik's own router-level probe.

The public three initially looked *uncovered* by file_sd (not just duplicated) — turned
out to be a real bug in `traefik-endpoints-metrics.py`, not a missing router. Each of
`traefik-edge`'s dynamic-file services (`emby.yml`, `nextcloud.yml`, `seerr.yml`) defines
**two** routers for the same `Host()`: a TLS one on `web-secure` and a plain redirect one
on `web`. The script deduped candidate targets on `(source, host)` alone, so whichever
router the API happened to return first for that host won — and the redirect router was
winning, silently discarding the TLS router (and its cert) for all three. Fixed by keying
the dedup on `(source, host, has_tls)` instead, so a host's TLS and non-TLS routers are
tracked independently. (`navidrome.yml` has a third, unrelated router — a `PathPrefix`
metrics-block variant — competing for the same host too; harmless since it shares the
same underlying cert, just means the `router` label picked isn't always the "main" one.)

**Known remaining duplication (by design):** every `-secure` router's cert still gets
reported by *two* jobs — `blackbox-traefik-https` (an `https://host/` probe, reachability
is the point, cert-expiry data is a free side effect) and `blackbox-traefik-tls-cert` (a
dedicated `host:443` `tls_connect` probe). `CertExpiringSoon` is scoped to
`job=~".*tls-cert"` so it only counts the dedicated one, but `ProbeFailing` (plain
`probe_success`, no job filter) still fires on both — a real Traefik outage trips two
alert instances per router instead of one. This mirrors the existing intentional
dual-probe pattern for `step-ca` (see `CertExpiringSoon`'s own comment in
`grafana/provisioning/alerting/rules.yml`) and is left alone for the same reason:
redundant confirmation via two independent probe methods, not a bug. Revisit if the
doubled alert-instance volume becomes noisy in practice.

## Dagu (container-native scheduler)

`compose/docker-compose-utilities.yml`'s `dagu` service (`ghcr.io/dagucloud/dagu`,
exposed internally as `dagu.${NETWORK_DOMAIN}` like `phpmyadmin`/`tdarr`) replaces
containers whose whole job was "run something on a schedule" with one-shot containers
Dagu spins up itself via `docker.sock` (same blast-radius tradeoff as `watchtower`'s own
socket mount). DAG definitions live in `hosts/nas/.../dagu/dags/*.yaml`, tracked in git
(no secrets — see below); Dagu's run history/state (`/mnt/fastpool/system/processes/dagu`
minus `dags/`) is untracked runtime state, same treatment as Prometheus/Grafana's own
data directories.

Migrated so far: `mariadb-backup` (was `DB_DUMP_CRON` baked into the
`databack/mysql-backup` container's own env) and `bitmagnet-cleanup` (was an untracked
`while true; do psql …; sleep 86400; done` container). Both DAGs bind-mount a
plaintext-single-line secret file directly into the one-shot container rather than using
Docker Compose's `secrets:` mechanism (Dagu isn't Swarm, so that doesn't apply to
DAG-spawned containers) — `mariadb-backup` reuses the existing
`mariadb-backup-pass.txt`; `bitmagnet-cleanup` needed a new
`bitmagnet-cleanup-db-pass.txt` (same convention) since its old compose service got the
password from a plain Portainer-env `${BITMAGNET_CLEANUP_DB_PASSWORD}` substitution,
which doesn't apply to a DAG YAML file tracked in git.

`themerr-fetch` (new, not a migration) downloads theme.mp3 files from ThemerrDB into
each Radarr/Sonarr movie/series folder, nightly at 03:30. No custom image or manual build
step: a `vendor` step copies the static `ffmpeg`/`ffprobe` (`mwader/static-ffmpeg`) and
`deno` (`denoland/deno:bin`, the JS runtime yt-dlp needs for some YouTube signatures)
binaries out of their upstream images into the shared `/data` volume before `fetch` runs
`dagu/scripts/themerr_fetch.py` against a stock `python:3.12-slim` with `/data` on `PATH`.
Both source images are `FROM scratch` with no shell inside them, so `vendor` can't
`docker.run` a command *in* them — it uses `docker create`+`docker cp` (via a
`docker:cli` helper) to read their filesystem without executing anything inside them.
Every image here is pulled once and cached by Docker, so deploying this DAG is just:
push the script + both DAG files — nothing built by hand on `nas`, and no apt-get cost on
every run. yt-dlp itself is still self-hosted by the script the same way it always was
(`ensure_ytdlp`, downloaded from its own GitHub releases and auto-updated). It joins
`multimedia_default` to reach `radarr`/`sonarr`/`emby` by container name — Dagu itself
only sits on `utilities_default`. Its three API keys use Dagu's `secrets:` + `ref:`
mechanism (`themerr-fetch/radarr-api-key` etc.), same as `mariadb-backup`/
`bitmagnet-cleanup` above — registered by hand once through the Dagu UI/API, the one
manual step this DAG still has. (Dagu's secrets spec confirms `ref:` requires a running
server and can't be satisfied by deploying files alone; the file-backed `provider: file`
alternative would avoid that, but this repo prefers `ref:` for consistency with the other
two DAGs' secrets.) Defaults to refreshing Emby's library (`MEDIASERVER_TYPE=emby`); switch to
Jellyfin by changing that one env line if the trial becomes primary. Written files land
root-owned (container runs as root, `UMASK=002` keeps them world-readable) — add a
`container.user` override in the DAG if that ever needs to match the media UID/GID
instead.

Rate-limiting: `MAX_DOWNLOADS` (a run param, default 100) caps YouTube downloads per
invocation so a cold cache or a post-ThemerrDB-outage run (which clears cached negative
lookups) can't blast through an entire library's worth of yt-dlp downloads in one run and
get the NAS's IP flagged — pass `MAX_DOWNLOADS=0` for a deliberate one-off catch-up run.
`DOWNLOAD_DELAY` (5s, hardcoded in the DAG's env) already throttled downloads themselves;
`THEMERRDB_DELAY` (0.3s, script default) was added alongside the cap to also throttle the
per-item ThemerrDB JSON lookups, which previously ran back-to-back with no delay at all —
lighter than a YouTube download but still someone's small self-hosted API, not a CDN built
for bursts.

**Not done yet, deliberately out of scope so far:** feeding Dagu's own per-DAG run
status (success/fail/last-run) into the shared `scheduled_job_*` metric family (see
"Scheduled jobs (unified)" above) the way every other scheduled-job source does, and a
direct Prometheus scrape of Dagu's own `/api/v1/metrics` for engine-level health
(`dagu_scheduler_running` etc.). Until that's built, a DAG silently failing or Dagu
itself going down has no alert coverage beyond whatever the DAG's own container/job
otherwise produces — check the Dagu UI directly for now.

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
