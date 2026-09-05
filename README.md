# portainer-templates

Docker Compose stack definitions for a home-lab server (a TrueNAS SCALE box, hostname
`nas`), deployed through Portainer, plus version-controlled config files for that box and
a few other hosts on the network. This repo is the source of truth for *what runs* and
*how it's configured*; the actual container lifecycle (pull/up/down) is driven by
Portainer's own stack UI, not by running `docker compose` from here.

See also: [context.md](context.md) for the deeper "why" (architecture decisions, the
monitoring migration, known gotchas) and [agents.md](agents.md) for conventions an AI
assistant should follow when working in this repo.

## Layout

```
compose/docker-compose-*.yml   One Portainer stack per file (see below).
hosts/<HOST>/...        Version-controlled config files, mirroring each host's real
                         filesystem 1:1. Deployed with scripts/deploy.ps1.
scripts/deploy.ps1       Interactive/scriptable scp deployer for hosts/.
scratchpad/               Git-ignored. Secrets, host-only files, and drafts — see
                         agents.md for exactly what belongs here vs. hosts/.
logos/                    A couple of icon assets (Emby, Traefik) — not otherwise wired up.
```

## Stacks (`compose/docker-compose-*.yml`)

Each file is `name:`d and deployed as its own Portainer stack. Cross-stack service
access goes through each stack's `default` network, declared `external: true` in the
consumer. Values come from Portainer's per-stack environment UI, not a committed
`.env` — there is no `.env` file in this repo, and there shouldn't be one.

| File | Stack | Contains |
|---|---|---|
| `compose/docker-compose-networking.yml` | `networking` | Traefik (internal), Traefik-Edge (WAN-facing), step-ca (internal CA), whoami |
| `compose/docker-compose-monitoring.yml` | `monitoring` | Prometheus, Grafana, Loki, Promtail, cAdvisor, node-exporter, blackbox-exporter, snmp-exporter, smartctl-exporter, redis/mysqld exporters, one `exportarr` per *arr app, nextcloud/emby/sabnzbd/qbittorrent exporters |
| `compose/docker-compose-multimedia.yml` | `multimedia` | Emby, Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr |
| `compose/docker-compose-downloads.yml` | `downloads` | SABnzbd (+ cleanup sidecar), qBittorrent, Prowlarr, Spotweb |
| `compose/docker-compose-extras.yml` | `extras` | stash, namer, whisparr |
| `compose/docker-compose-nextcloud.yml` | `nextcloud` | Nextcloud (+ cron/web/onlyoffice/rebuilder sidecars) and its dedicated MariaDB |
| `compose/docker-compose-utilities.yml` | `utilities` | MariaDB, Redis, RabbitMQ, Watchtower, phpMyAdmin, flaresolverr, chromedp, dupeguru, tdarr |
| `compose/docker-compose-syncthing.yml` | `syncthing` | Syncthing |

Alerting is **native Grafana alerting** (contact point + notification policy configured
in the Grafana UI, alert *rules* provisioned as YAML) — there is no Alertmanager or
Apprise service in this repo; don't reintroduce one without a reason.

## `hosts/` — deployed host config

Each top-level directory under `hosts/` is a hostname (matched against `~/.ssh/config`
`Host` entries — see `scripts/deploy.ps1`). Everything below `hosts/<HOST>/` mirrors that
host's real filesystem exactly: `hosts/nas/mnt/fastpool/system/processes/prometheus/config.yml`
deploys to `nas:/mnt/fastpool/system/processes/prometheus/config.yml`. No separate mapping
file — the path *is* the mapping.

Currently tracked:
- **`nas`** — Grafana provisioning (dashboards, datasources, alert rules), Prometheus
  and blackbox-exporter config, and a handful of host-side scripts (`cron-wrapper.sh`, `zpool-metrics.sh`,
  `snapshot-tasks-metrics.sh`, `cloudsync-tasks-metrics.sh`) that expose TrueNAS-native
  state (ZFS pools, SMART, cron jobs, periodic snapshot/cloud-sync tasks) as node-exporter
  textfile-collector metrics.
- **`openhabian`** — a native (non-Docker) Raspberry Pi install (`192.168.1.154`) running
  openHAB. Tracked files are `node_exporter`/`promtail` systemd units, Promtail's scrape
  config, and the Caddy `Caddyfile` (reverse-proxies openHAB plus `grafana.` / `influx.`
  subdomains — see [CONTEXT.md](docs/CONTEXT.md) for the step-ca/acme.sh cert setup).
  Promtail tails the openHAB core / `events.log` / HabApp logs plus the systemd journal
  and **dual-ships** them to the central Loki *and* a second Loki running locally on the
  Pi (feeds the bundled Grafana's log views) — see
  [CONTEXT.md](docs/CONTEXT.md) → "openHABian log shipping".

Deploy with:
```powershell
.\scripts\deploy.ps1                                    # interactive: pick host, then file
.\scripts\deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/processes/prometheus/config.yml
```
It only copies files — restarting/reloading the affected service on the target host is a
manual follow-up step (the script says so on exit, and doesn't guess which service).

## What NOT to commit

No tokens, passwords, API keys, or connection secrets anywhere in this repo. Files under
manual/UI-only control (e.g. Grafana's alert *contact point*, which holds a real Pushover
token) are never provisioned as files at all — they're configured by hand in the Grafana
UI and stay that way. See [agents.md](agents.md) for the full secret-handling convention
and where working files with real credentials actually live (`scratchpad/`, git-ignored).
