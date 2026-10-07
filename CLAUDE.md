# CLAUDE.md

This file is auto-loaded every session — keep it thin. The real documentation lives at
the repo root and under `docs/`; read them in this order when picking up work here:

1. **[README.md](README.md)** — what this repo is, its layout, the Portainer stacks, and
   the `hosts/` deploy convention. Start here for orientation.
2. **[docs/CONTEXT.md](docs/CONTEXT.md)** — an index into the topic docs under `docs/`.
   Each captures the architecture decisions and hard-won gotchas for one area — read the
   relevant one before non-trivial changes there:
   - **[docs/monitoring.md](docs/monitoring.md)** — the Zabbix→Prometheus/Grafana/Loki
     migration, native-Grafana alerting, the `job`/`exported_job` label collision, TrueNAS
     `midclt`-based scheduled-task monitoring, the `ContainerUnhealthy` alert and its
     non-stock-cAdvisor `container_health_state` dependency, openHABian log shipping.
   - Split out of monitoring.md: **[docs/dagu.md](docs/dagu.md)** (scheduler + jobs),
     **[docs/endpoint-probes.md](docs/endpoint-probes.md)** (ICMP / Traefik-discovered
     probes), **[docs/openhabian-monitoring.md](docs/openhabian-monitoring.md)** (Pi host
     metrics + Loki log shipping).
   - **[docs/network.md](docs/network.md)** — OpenWrt zone/firewall topology, the SNMP
     bring-up chain, the openHABian Caddy reverse proxy and its step-ca / acme.sh cert.
   - **[docs/backups.md](docs/backups.md)** — the openHABian Amanda dual-storage backup
     and the SD-card mirroring guard rails.
   - **[docs/multimedia.md](docs/multimedia.md)** — the *arr / Emby / Jellyfin / download
     stacks and the Seerr↔Jellyfin plugin gotchas. Split out of it:
     [multimedia-monitoring.md](docs/multimedia-monitoring.md) (exporter quirks),
     [bitmagnet.md](docs/bitmagnet.md) (classifier), [gpu.md](docs/gpu.md) (shared NVENC
     GPU, Watchtower/Tdarr restart incident).
3. **[AGENTS.md](AGENTS.md)** — conventions to follow while working here: the secret-
   handling rules (non-negotiable), the deploy workflow, the "verify before writing"
   discipline, and scope-discipline norms.

If something you're about to do is covered by one of those files, follow it — they
represent decisions already made and mistakes already paid for once.
