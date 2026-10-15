# Monitoring

The "why" behind the Prometheus + Grafana + Loki stack and the dashboard/exporter
gotchas that took real debugging to find. Read this before making non-trivial changes to
the monitoring stack or dashboards; it'll save you from re-discovering things the hard
way.

Topic docs split out of this file: [dagu.md](dagu.md) (scheduler and its jobs),
[endpoint-probes.md](endpoint-probes.md) (ICMP and Traefik-discovered blackbox probes),
[openhabian-monitoring.md](openhabian-monitoring.md) (Pi host metrics and Loki log
shipping), [multimedia-monitoring.md](multimedia-monitoring.md) (media-app exporters).

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
(`hosts/nas/.../grafana/provisioning/alerting/rules.yml`), as are the contact points
(`contactpoints.yml`) and notification policy (`policies.yml`) in the same directory. The
Pushover token and user key are `{{PUSHOVER_GRAFANA_API_TOKEN}}` / `{{PUSHOVER_USER_KEY}}`
placeholders in `contactpoints.yml`, filled in from `.tokens` by `scripts/deploy.ps1`.

**Routing by severity (2026-10-15).** `severity=critical` → `pushover-critical` (Pushover priority 1,
bypasses the phone's quiet hours), grouped per alert, repeat 2h. `severity=warning` → `pushover-digest`
(priority -1, silent), one grouped push per day: the route is muted by the `outside-digest-window`
mute timing except 08:00–09:00 Europe/Brussels, with a 23h repeat so the flush is due again before the
next window. Everything else falls through to the root `pushover-alerts` (priority 0). Pushover priority
is a fixed per-contact-point setting (not templatable), hence one contact point per tier. Grafana has no
inhibit rules, so one root cause (a dead drive) still fires several rules; keep `for:` generous on
downstream ones. Early-warning rules are trend-based (`DiskFillingUp`, `PoolFillingUp` project days
until full; SMART/write rules in docs/openhabian-monitoring.md); the static 5% / 95% rules stay as critical
backstops. No ZFS checksum/scrub-error metric exists in Prometheus, so that check isn't covered yet.

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
override it with). `systemd-tasks-metrics.sh` (see [openhabian-monitoring.md](openhabian-monitoring.md))
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
- **`openhabian-timer`** / **`openhabian-manual`** — see [openhabian-monitoring.md](openhabian-monitoring.md).
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
  `blackbox-redirect` vs `blackbox-tls-cert`).
- **SNMP (switch/APs)**: works end-to-end now, after a chain of firewall/routing fixes and
  a scrape-timeout bump — see [network.md](network.md#snmp-switchaps).
- **Multimedia exporters** (`exportarr-prowlarr`, Seerr): see
  [multimedia-monitoring.md](multimedia-monitoring.md).

See [../AGENTS.md](../AGENTS.md) for the verify-before-writing discipline that caught
several of these.

## Household dashboard (Mealie / Vikunja / Nextcloud)

`household-overview`. Mealie has no Prometheus metrics, so its row is cAdvisor + blackbox +
`pg_database_size_bytes`. Vikunja has native `/api/v1/metrics` (`VIKUNJA_METRICS_ENABLED=true`,
scraped as `job=vikunja`: `vikunja_{task,project,user}_count`, `vikunja_active_users`); it has no
auth of its own, so `traefik-edge/dynamic/vikunja.yml` denies that path on the public route (same
pattern as Navidrome) and the public router needs an explicit low `priority`. Nextcloud sits there
temporarily; `nextcloud-exporter` scrapes were failing with `too many requests` (HTTP 429 from
Nextcloud's brute-force throttle), so only `nextcloud_up`/`info` exist until that is fixed.
