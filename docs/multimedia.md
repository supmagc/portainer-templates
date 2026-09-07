# Multimedia

The media-library and download stacks, and the exporter quirks specific to them. Most of
the "why" for these lives in the compose files themselves and in
[monitoring.md](monitoring.md); this file collects what's specific to the media apps.

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr |
| `downloads` | SABnzbd (+ cleanup sidecar), qBittorrent, Prowlarr, Spotweb |
| `extras` | stash, namer, whisparr |

tdarr runs in the `utilities` stack, not `multimedia`.

## Exporter notes

- **`exportarr`** — one instance per *arr app in the `monitoring` stack.
  `exportarr-prowlarr` is **known-broken on the currently-published `:latest` image**: a
  VIP expiration date field from one indexer fails Go's `time.Parse`. It's fixed upstream
  on `master` but was never released to the tag this repo pulls. Not yet resolved —
  options are fixing the offending field in Prowlarr's indexer settings, building
  exportarr from source, or dropping that one exporter.
- **Seerr** — no native Prometheus metrics, and the one community exporter targets the
  pre-merge Jellyseerr API with unconfirmed compatibility. Deliberately reachability-only
  via blackbox, not a speculative exporter deployment.
- **Emby / SABnzbd / qBittorrent** each have a dedicated exporter in the `monitoring`
  stack (alongside the nextcloud one).
