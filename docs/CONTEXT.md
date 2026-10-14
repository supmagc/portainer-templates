# Context

The deeper "why" behind this repo's current shape — architecture decisions and the
gotchas that took real debugging to find. This file is an index; the detail lives in the
topic docs alongside it. Read the relevant one before making non-trivial changes in that
area — it'll save you from re-discovering things the hard way.

- **[monitoring.md](monitoring.md)** — the Zabbix→Prometheus/Grafana/Loki migration, why
  alerting is native-Grafana not Alertmanager, the Grafana provisioning "locked" gotcha,
  the `merge`/`joinByField` table-panel pattern, the `job`/`exported_job` label collision,
  TrueNAS `midclt`-based scheduled-task monitoring, the `ContainerUnhealthy` alert and its
  fragile dependency on a `container_health_state` metric that isn't in stock cAdvisor, and
  openHABian log shipping.
- **[dagu.md](dagu.md)** — the Dagu scheduler, its `docker.run` executor quirks and every
  job (trailer-fetch, backdrop-generate, library-cleanup, ...).
- **[endpoint-probes.md](endpoint-probes.md)** — ICMP reachability probes and Traefik
  endpoint discovery for blackbox.
- **[openhabian-monitoring.md](openhabian-monitoring.md)** — the Pi's host metrics and
  Promtail→Loki log shipping.
- **[network.md](network.md)** — the OpenWrt router's zone/firewall topology, the SNMP
  bring-up chain, and the openHABian Caddy reverse proxy plus its step-ca / acme.sh cert.
- **[backups.md](backups.md)** — the openHABian Amanda dual-storage backup (local SSD +
  NFS→NAS→Backblaze), its phased bring-up, and the SD-card mirroring guard rails.
- **[nas-storage.md](nas-storage.md)** — which ZFS pool owns which data, cross-pool
  replicas, snapshot/B2 policy and the migration steps (2026-10-08 target layout).
- **[photos.md](photos.md)** — Immich plan: external library over the Lightroom/digiKam
  originals, phone uploads, and the rules that keep the old tools working.
- **[multimedia.md](multimedia.md)** — the *arr / Emby / Jellyfin / download stacks, and
  Seerr's Jellyfin link and how the Jellyfin home-screen plugins match Seerr users.
- **[multimedia-monitoring.md](multimedia-monitoring.md)** — exporter quirks for the media
  apps (`exportarr-prowlarr`, Jellyfin, Navidrome, Seerr).
- **[bitmagnet.md](bitmagnet.md)** — BitMagnet's custom classifier and why its database
  grows.
- **[gpu.md](gpu.md)** — the single shared NVENC GPU and the Watchtower/Tdarr restart
  incident.
- **[extras.md](extras.md)** — the custom `library-organizer` Stash plugin (naming, studio
  folders, NFO/poster/fanart, Whisparr rescan, dry-run/reconcile/orphan tasks, rollout).
- **[multimedia-library-layout.md](multimedia-library-layout.md)** — the canonical on-disk
  layout for the movies/series/music libraries (verified against Emby/Jellyfin naming) and
  the cleanup categories the `library-cleanup` Dagu DAG scans for.

## Verification discipline

Several wrong guesses earlier in this project (Docker image tags/names, TrueNAS API
shapes, PromQL label assumptions) were caught only by checking against a live source —
either a web search for the real image/tag, or a live Grafana MCP query
(`query_prometheus`, `list_prometheus_label_values`) against the running stack. Treat that
as the default working method here, not an exception: confirm exporter image names/tags
and metric/label names against something live before writing them into a compose file or
a dashboard query, rather than pattern-matching from memory. See [../AGENTS.md](../AGENTS.md).
