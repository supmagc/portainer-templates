# Multimedia

The media-library and download stacks, and the exporter quirks specific to them. Most of
the "why" for these lives in the compose files themselves and in
[monitoring.md](monitoring.md); this file collects what's specific to the media apps.

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr, Navidrome, Ampache (trial, alongside Navidrome) |
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
- **Navidrome** — native `/metrics` via `ND_PROMETHEUS_ENABLED=true`, no separate
  exporter, scraped directly at `navidrome:4533` (Prometheus is already on
  `multimedia_default`). The endpoint has no auth of its own, and Navidrome is also
  proxied publicly through `traefik-edge` (see
  `hosts/nas/.../traefik-edge/dynamic/navidrome.yml`), so `/metrics` is blocked at that
  edge router (`PathPrefix` + an unroutable `ipWhiteList`) rather than hidden behind
  Navidrome's own secret-path option (`ND_PROMETHEUS_METRICSPATH`) — this repo has no
  env-substitution for `prometheus/config.yml`, so a secret path would have to be
  committed there in plaintext. Metric names confirmed against Navidrome's own docs and
  the community "Navidrome Observability" Grafana dashboard (`db_model_totals`,
  `navidrome_info`, `http_request_count`, `media_scan_last`, `media_scans`, plus standard
  Go/process metrics) — none of this has been checked against a live scrape yet.
- **BitMagnet / Spotweb / PostgreSQL** dashboard wiring — see
  [monitoring.md](monitoring.md#postgresql--bitmagnet--spotweb-dashboard-data). BitMagnet
  self-exposes `/metrics`; Spotweb has no exporter (read via `mysqld-exporter`); Postgres
  gets a new `postgres-exporter`.

## BitMagnet classifier

`hosts/nas/.../downloads/bitmagnet/.config/bitmagnet/classifier.yml` extends the
built-in `default` workflow (`CLASSIFIER_WORKFLOW=custom` in the compose file) to close
three real gaps found 2026-09-12 by sampling live "unknown" results, none of which were a
TMDB problem (content_type is decided by file extensions + name-parsing; TMDB only
attaches metadata to an already-typed movie/tv_show/xxx torrent):

- **Game/software bundles with large bonus archives** (soundtrack/art-book `.zip`, etc.)
  can outweigh the installer's size in the default classifier's software match, since
  archive formats aren't in any extension list — pushing the sum negative and leaving an
  unambiguous GOG/repack release unclassified. Fixed by classifying on "has a software
  file and no video files" instead of the size-weighted sum.
- **Whole-series/whole-season releases** with names like "Seasons 1 to 8 Complete Box
  Set" or "S03 Complete" aren't recognized by the built-in video-name parser (it expects
  `S01-S08` / "Complete Series" style grammar), so they fall through despite being
  obvious video content. Fixed with keyword/regex rules that force-set `tv_show` — but
  since this bypasses the name parser, it never gets a parsed base title, so it **won't
  pick up a TMDB match even after a reprocess**. Filterable/browsable, but no
  poster/metadata — split (2026-09-14, was a single `boxset` tag) into `full-series` and
  `full-season` tags so the two are distinguishable, same idea as `image-only`/`jav`
  below.
- **Image-only torrents** (photo dumps, etc.) have no dedicated content_type in
  bitmagnet's enum at all — the only way to leave "unknown" is an explicit adult-keyword
  match on the name. Rather than guess, these get an `image-only` tag instead of a
  content_type, so they're findable without risking a wrong `xxx` label.
- **JAV releases** use studio-code naming (`STARS-207`, `GVH646`, `FC2PPV-...`) with no
  generic keyword, so they fell through the default `xxx` matcher despite ~170K other
  torrents already being correctly classified as `xxx`. content_type is a fixed enum
  with no custom values, so this still sets `xxx`; a `jav` tag is added alongside it so
  these stay separately filterable. **The first version of this rule (2026-09-13) never
  matched anything** — bitmagnet's `classifier.yml` `keywords:` block is a simplified
  glob language (`( ) | * ? + # `` ` `` only), documented as "a simpler alternative to
  regular expressions", not actual regex — it has no `[a-z]` character classes, `\d`, or
  `{n,m}` quantifiers. The original rule's regex-syntax keyword entries were being
  compiled as literal punctuation that never appears in a torrent name, so it silently
  matched 0 torrents (confirmed 2026-09-14: 0 `jav` tags despite ~250K `xxx` torrents).
  Fixed by calling `.matches()` with a literal regex string directly in the condition —
  documented separately as full RE2 regex support, bypassing `keywords:` entirely. The
  regex is loose (studio-code format, not a strict allowlist) — spot-check a reprocess
  batch before trusting it broadly.

Changes here only affect torrents crawled/processed *after* the deploy — existing
"unknown" torrents need a manual reprocess batch (admin UI → "Enqueue torrent processing
batch", force rematch) to be re-evaluated against the new workflow.

BitMagnet only indexes DHT-crawled metadata (name/size/file list) in Postgres — it never
downloads torrent content — so "disk usage" here means the `bitmagnet` Postgres database,
not the media library. ~90% of that DB is per-torrent file-list rows, which is why the
compose file caps `DHT_CRAWLER_SAVE_FILES_THRESHOLD` (default 100) rather than relying on
classifier cleanup alone to control growth.
