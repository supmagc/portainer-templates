# Multimedia

The media-library and download stacks, and the exporter quirks specific to them. Most of
the "why" for these lives in the compose files themselves and in
[monitoring.md](monitoring.md); this file collects what's specific to the media apps.

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Jellyfin (trial, alongside Emby), Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr, Navidrome, WatchState |
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
- **Jellyfin** — `rebelcore/jellyfin-exporter` (added 2026-10-01). It doesn't use
  Jellyfin's own `/metrics`, which has no library or session data. The `transcoding`
  collector is off by default and is enabled in the compose file because the dashboard's
  *Transcoding Now* and *Peak Transcode Bitrate* panels need it. Metric names were taken
  from the v1.5.2 source, not a live scrape, so check them once it's deployed.
  `jellyfin_media_count`'s `type` label is Jellyfin's `/Items/Counts` key minus `Count`
  (`Movie`, `Series`, `Episode`, ...). There is no failed-login metric, so the Jellyfin row
  shows *Pending Restart* where the Emby row shows *Failed Logins*.
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
  Go/process metrics) — all five confirmed present in a live scrape (2026-10-01).
- **BitMagnet / Spotweb / PostgreSQL** dashboard wiring — see
  [monitoring.md](monitoring.md#postgresql--bitmagnet--spotweb-dashboard-data). BitMagnet
  self-exposes `/metrics`; Spotweb has no exporter (read via `mysqld-exporter`); Postgres
  gets a new `postgres-exporter`.

## Seerr and the Jellyfin plugins

Seerr supports exactly **one** media server. It was switched from Emby to Jellyfin on
2026-10-01 so the Jellyfin home-screen plugins can use it; Emby accounts can no longer
sign in to Seerr.

- **`JELLYFIN_TYPE=emby` must stay out of the compose file.** The media server type lives
  in Seerr's `settings.json` (`main.mediaServerType`: 2 = Jellyfin, 3 = Emby, 4 = not
  configured), but Seerr re-runs its settings migrations on every start, and one of them
  flips Jellyfin back to Emby whenever that env var is set.
- **Switching servers** has no UI. Stop Seerr, back up its config dir, set
  `main.initialized=false`, `main.mediaServerType=4`, and clear `jellyfin.ip`,
  `jellyfin.apiKey` and `jellyfin.libraries` in `settings.json`. On the next start the
  setup wizard re-links the owner account (user 1) to whichever admin signs in, keeping
  requests and the Sonarr/Radarr config. Then run *Jellyfin Full Library Scan* so stored
  item IDs are rewritten for the new server. (Derived from Seerr's source, not a
  documented procedure.)
- **Jellyfin Enhanced** matches Seerr users by **Jellyfin user ID**. With no match, every
  Seerr feature it has fails (search results, requests, the info popup it opens when a
  Discover card is clicked), and it shows a "log in to Seerr" banner. Turning its Seerr
  integration off doesn't stop it intercepting Discover card clicks.
- **Home Screen Sections** matches Seerr users by **username, case-sensitively**
  (`jellyfinUsername` must equal the Jellyfin username exactly). A mismatch fails silently:
  the Seerr rows are just empty, with nothing in the log. Its Seerr URL needs the port
  (`http://seerr:5055`); without it the plugin connects on port 80 and logs
  `Connection refused (seerr:80)`. Its *My Requests* section throws
  `Unrecognized Guid format` while Seerr holds another server's item IDs.

## WatchState (Emby → Jellyfin play state)

Added 2026-10-01 to carry watch history over during the Jellyfin trial.

- **Copies only played/unplayed state and resume progress.** Favourites, ratings and
  play counts aren't covered by its docs; they would need a separate script.
- **One way:** turn on *Import* for the Emby backend and *Export* for the Jellyfin backend
  (not the other way round). The first full import pulls from Emby, then export pushes the
  result to Jellyfin.
- **Multiple users go through identities** (*Configuration → Identities → Match &
  Provision*). Users are paired across servers by username, so check the pairings before
  creating them. WatchState's own FAQ says it is built for a single user first, so
  spot-check each user in Jellyfin afterwards.
- Backends need a server **API key** (Dashboard → Advanced → API Keys), not a user token.
  Within the stack the URLs are `http://emby:8096` and `http://jellyfin:8096`.

## Library layout and cleanup

See [multimedia-library-layout.md](multimedia-library-layout.md) for the canonical on-disk
shape of `movies`/`series`/`music` and what the `library-cleanup` Dagu DAG treats as safe
to delete vs. report-only.

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
