# Context

The deeper "why" behind this repo's current shape — architecture decisions, the state of
the monitoring migration, and gotchas that took real debugging to find. Read this before
making non-trivial changes to the monitoring stack or dashboards; it'll save you from
re-discovering things the hard way.

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

### Grafana provisioning "locked" gotcha

Deleting or emptying a provisioning file (e.g. `rules.yml`) does **not** free those
resources back to UI editing — Grafana keeps a DB-side lock on anything it once
provisioned. You need explicit `deleteRules:` / `deleteContactPoints:` / `resetPolicies:`
directives in the YAML to un-provision something. Dashboards are provisioned with
`allowUiUpdates: true` specifically so they *can* be tweaked live in the UI without this
problem — alert rules currently are not set up that way, by choice (they're generic
enough that UI tuning wasn't worth losing file-based history for).

### Table panels: `merge` vs `joinByField`

Several dashboards show one row per entity (disk, ZFS pool, cron job, scheduled task) by
combining multiple Prometheus queries into a table. Grafana's `merge` transform does this
by matching on a frame's full field signature — which silently breaks (duplicate or
garbled rows, or stray `__name__`/`__name__1`/`__name__2...` columns) as soon as the
queries being combined have asymmetric extra labels. **`joinByField` with an explicit
`byField` key is the correct, robust pattern for every multi-target table panel in this
repo** — always exclude the `__name__` field(s) it exposes (they're normally hidden under
`merge`, but real and visible under `joinByField`).

### The `job` / `exported_job` label collision

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

### TrueNAS-native scheduled-task visibility

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

### openHABian (the `openhabian` host)

Runs natively on a Raspberry Pi — no Docker. `node_exporter` and Promtail are both static
Go binaries installed by hand and run as systemd units (see `hosts/openhabian/etc/`).
openHABian's own scheduled backups (Amanda-based) are **systemd timers**
(`amdump-*.timer`, `amandaBackupDB.timer`, `sdrawcopy.timer`, `sdrsync.timer`), not cron —
so they're visible for free via `node_exporter --collector.systemd`, no wrapper script
needed (unlike TrueNAS, which has no systemd-timer option for its scheduled tasks).
openHABian also bundles a local Grafana (port 3000, native `/metrics`) and InfluxDB
**1.x** (port 8086 — no native Prometheus endpoint; that's a 2.x-only feature). Both are
covered by blackbox reachability probes only (Grafana `/api/health`, InfluxDB `/ping`),
not scraped for internal metrics.

### Other resolved investigations, briefly

- **Router traffic panel "triple-counting"**: `network-details.json` was summing
  aggregating interfaces (`eth0`/`wan`/`br-lan`) alongside their physical members —
  excluded now.
- **Traefik metrics**: weren't enabled at all originally; fixed by adding a dedicated
  internal `metrics` entrypoint (`:8082`) to `compose/docker-compose-networking.yml`, not exposed
  via `ports:`.
- **Seerr**: no native Prometheus metrics, and the one community exporter targets the
  pre-merge Jellyseerr API with unconfirmed compatibility — deliberately reachability-only
  via blackbox, not a speculative exporter deployment.
- **RabbitMQ/nextcloud-web blackbox probe failures** (from an earlier debugging pass):
  RabbitMQ's management listener is IPv4-only but Docker handed blackbox an AAAA record
  too (fixed with `preferred_ip_protocol: ip4`); `nextcloud-web` redirects `/` to a login
  page that a default blackbox module wouldn't follow correctly (fixed with a dedicated
  `http_2xx_noredirect` module/job — blackbox's `params.module` is job-scoped, not
  target-scoped, which is why mixed-behavior targets need their own job repeatedly
  throughout `prometheus.yml`, e.g. `blackbox-http` vs `blackbox-http-insecure` vs
  `blackbox-nextcloud` vs `blackbox-openhabian`).
- **SNMP (switch/APs)**: works end-to-end now. Root causes chained through: missing
  `lan`→`admin` VLAN firewall forwarding for UDP/161, then the switch itself lacking a
  default gateway (JetStream T1600G-28PS: under **L3 FEATURES → Static Routing**, not
  System Info — that's only for the simpler T1500 family), then finally Prometheus's
  default 10s scrape timeout being too short for the switch's full interface-table walk
  (fixed with `scrape_interval: 60s` / `scrape_timeout: 55s` on the `snmp-switch`/`snmp-ap`
  jobs).
- **exportarr-prowlarr**: known-broken on the currently-published `:latest` image (a VIP
  expiration date field from one indexer fails Go's `time.Parse`; fixed upstream on
  `master` but never released to the tag this repo pulls). Not yet resolved — options are
  fixing the offending field in Prowlarr's indexer settings, building exportarr from
  source, or dropping that one exporter.

## Network/firewall topology (OpenWrt router)

Zones: `wan`, `lan` (192.168.1.0/24 — NAS lives here), `guest`, `work`,
`admin` (VLAN99, 192.168.0.0/24), `iot`, `vpn` (WireGuard). **`admin` is not purely
network-gear** — it also carries the user's administration desktop, which is why some
`lan`→`admin` forwards exist for desktop-specific apps (Phone Link, LocalSend) alongside
the switch/AP-oriented ones (like the SNMP rule above). When a rule in `admin` looks odd
for "just switches/APs," consider the desktop before assuming it's dead weight.

Open items from the last firewall review (not yet acted on): an `Allow-NTP-from-LAN` rule
with `src 'wan'` instead of `lan` (likely a copy/paste bug exposing router NTP to the
whole internet); an `iot` zone with no interface actually attached to it (its forwarding
rules are currently no-ops); Emby/Seerr WAN DNAT rules that may be redundant now that
Traefik-Edge fronts 80/443 with real TLS; and four guest/work DNS rules that are disabled
and point at resolvers whose reachability hasn't been confirmed.

## Verification discipline

Several wrong guesses earlier in this project (Docker image tags/names, TrueNAS API
shapes, PromQL label assumptions) were caught only by checking against a live source —
either a web search for the real image/tag, or a live Grafana MCP query
(`query_prometheus`, `list_prometheus_label_values`) against the running stack. Treat that
as the default working method here, not an exception: confirm exporter image names/tags
and metric/label names against something live before writing them into a compose file or
a dashboard query, rather than pattern-matching from memory. See agents.md.
