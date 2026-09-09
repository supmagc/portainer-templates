# Multimedia

The media-library and download stacks, and the exporter quirks specific to them. Most of
the "why" for these lives in the compose files themselves and in
[monitoring.md](monitoring.md); this file collects what's specific to the media apps.

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr |
| `downloads` | SABnzbd (+ cleanup sidecar), qBittorrent, Prowlarr, Spotweb, BitMagnet |
| `extras` | stash, namer, whisparr |

tdarr runs in the `utilities` stack, not `multimedia`.

## Exporter notes

- **`exportarr`** — one instance per *arr app in the `monitoring` stack.
  `exportarr-prowlarr` **had been broken on the currently-published `:latest` image**: a
  VIP expiration date field from one indexer failed Go's `time.Parse` (fixed upstream on
  `master`, never released to the pulled tag). As of 2026-09-09 it scrapes cleanly — the
  full per-indexer metric set (`prowlarr_indexer_*`) is flowing — so the offending field
  was cleared or changed in Prowlarr. Watch for recurrence if a new VIP indexer is added.
- **Seerr** — no native Prometheus metrics, and the one community exporter targets the
  pre-merge Jellyseerr API with unconfirmed compatibility. Deliberately reachability-only
  via blackbox, not a speculative exporter deployment.
- **Emby / SABnzbd / qBittorrent** each have a dedicated exporter in the `monitoring`
  stack (alongside the nextcloud one).
- **BitMagnet / Spotweb / PostgreSQL** dashboard wiring — see
  [monitoring.md](monitoring.md#postgresql--bitmagnet--spotweb-dashboard-data). BitMagnet
  self-exposes `/metrics`; Spotweb has no exporter (read via `mysqld-exporter`); Postgres
  gets a new `postgres-exporter`.
