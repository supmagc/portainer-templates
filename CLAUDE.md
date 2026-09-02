# CLAUDE.md

This file is auto-loaded every session — keep it thin. The real documentation lives in
three files at the repo root; read them in this order when picking up work here:

1. **[README.md](README.md)** — what this repo is, its layout, the Portainer stacks, and
   the `hosts/` deploy convention. Start here for orientation.
2. **[docs./CONTEXT.md](CONTEXT.md)** — the architecture decisions and hard-won gotchas behind
   the current state (the Zabbix→Prometheus/Grafana/Loki migration, why alerting is
   native-Grafana not Alertmanager, the `job`/`exported_job` label collision, TrueNAS
   `midclt`-based scheduled-task monitoring, and more). Read before making non-trivial
   changes to the monitoring stack or dashboards.
3. **[AGENTS.md](AGENTS.md)** — conventions to follow while working here: the secret-
   handling rules (non-negotiable), the deploy workflow, the "verify before writing"
   discipline, and scope-discipline norms.

If something you're about to do is covered by one of those files, follow it — they
represent decisions already made and mistakes already paid for once.
