# Multimedia monitoring

Exporter quirks for the media apps. The exporters themselves live in the `monitoring`
stack; dashboard panels are in `multimedia-overview`. General monitoring conventions are in
[monitoring.md](monitoring.md).

- **`exportarr`** — one instance per *arr app. `exportarr-prowlarr` was broken on the
  published `:latest` image (a VIP expiration date field failed Go's `time.Parse`, fixed
  upstream but never released). It has scraped cleanly since 2026-09-09, so the field was
  cleared in Prowlarr. Watch for recurrence if a new VIP indexer is added.
- **Seerr** — no native metrics, and the one community exporter targets the pre-merge
  Jellyseerr API. Deliberately reachability-only via blackbox.
- **Emby / SABnzbd / qBittorrent** — one dedicated exporter each.
- **Jellyfin** — `rebelcore/jellyfin-exporter` (2026-10-01), not Jellyfin's own `/metrics`
  (no library or session data). Its `transcoding` collector is off by default and enabled
  in the compose file for the *Transcoding Now* and *Peak Transcode Bitrate* panels. Metric
  names came from the v1.5.2 source, not a live scrape — verify after deploying.
  `jellyfin_media_count`'s `type` label is the `/Items/Counts` key minus `Count`
  (`Movie`, `Series`, `Episode`, ...). There is no failed-login metric, so the Jellyfin row
  shows *Pending Restart* where the Emby row shows *Failed Logins*.
- **Navidrome** — native `/metrics` (`ND_PROMETHEUS_ENABLED=true`), scraped at
  `navidrome:4533`. No auth of its own, and Navidrome is also proxied publicly through
  `traefik-edge`, so `/metrics` is blocked at that edge router (`PathPrefix` + an
  unroutable `ipWhiteList`, see `traefik-edge/dynamic/navidrome.yml`). A secret metrics
  path (`ND_PROMETHEUS_METRICSPATH`) was rejected: `prometheus/config.yml` has no env
  substitution, so it would be committed in plaintext. Metrics confirmed live 2026-10-01:
  `db_model_totals`, `navidrome_info`, `http_request_count`, `media_scan_last`,
  `media_scans`.
- **BitMagnet / Spotweb / PostgreSQL** — see
  [monitoring.md](monitoring.md#postgresql--bitmagnet--spotweb-dashboard-data).
