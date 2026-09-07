# Context

The deeper "why" behind this repo's current shape — architecture decisions and the
gotchas that took real debugging to find. This file is an index; the detail lives in the
topic docs alongside it. Read the relevant one before making non-trivial changes in that
area — it'll save you from re-discovering things the hard way.

- **[monitoring.md](monitoring.md)** — the Zabbix→Prometheus/Grafana/Loki migration, why
  alerting is native-Grafana not Alertmanager, the Grafana provisioning "locked" gotcha,
  the `merge`/`joinByField` table-panel pattern, the `job`/`exported_job` label collision,
  TrueNAS `midclt`-based scheduled-task monitoring, and openHABian log shipping.
- **[network.md](network.md)** — the OpenWrt router's zone/firewall topology, the SNMP
  bring-up chain, and the openHABian Caddy reverse proxy plus its step-ca / acme.sh cert.
- **[backups.md](backups.md)** — the openHABian Amanda dual-storage backup (local SSD +
  NFS→NAS→Backblaze), its phased bring-up, and the SD-card mirroring guard rails.
- **[multimedia.md](multimedia.md)** — the *arr / Emby / download stacks and their
  exporter quirks (`exportarr-prowlarr`, Seerr).

## Verification discipline

Several wrong guesses earlier in this project (Docker image tags/names, TrueNAS API
shapes, PromQL label assumptions) were caught only by checking against a live source —
either a web search for the real image/tag, or a live Grafana MCP query
(`query_prometheus`, `list_prometheus_label_values`) against the running stack. Treat that
as the default working method here, not an exception: confirm exporter image names/tags
and metric/label names against something live before writing them into a compose file or
a dashboard query, rather than pattern-matching from memory. See [../AGENTS.md](../AGENTS.md).
