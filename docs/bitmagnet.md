# BitMagnet

BitMagnet (`downloads` stack) only indexes DHT-crawled metadata (name, size, file list) in
Postgres; it never downloads torrent content. "Disk usage" therefore means the `bitmagnet`
database, not the media library. About 90% of that database is per-torrent file-list rows,
which is why the compose file caps `DHT_CRAWLER_SAVE_FILES_THRESHOLD` (default 100) instead
of relying on classifier cleanup alone. Dashboard wiring is in
[monitoring.md](monitoring.md#postgresql--bitmagnet--spotweb-dashboard-data); its DHT
crawler also caused a conntrack exhaustion incident, see [network.md](network.md).

## Classifier

`hosts/nas/.../downloads/bitmagnet/.config/bitmagnet/classifier.yml` extends the built-in
`default` workflow (`CLASSIFIER_WORKFLOW=custom`). It closes gaps found 2026-09-12 by
sampling live "unknown" results. None were TMDB problems: `content_type` is decided by file
extensions and name parsing, and TMDB only attaches metadata to an already-typed torrent.

- **Software bundles with large bonus archives** (soundtrack or art-book `.zip`) can
  outweigh the installer in the default size-weighted software match, because archive
  formats are in no extension list. Fixed by classifying on "has a software file and no
  video files".
- **Whole-series/season releases** ("Seasons 1 to 8 Complete Box Set", "S03 Complete") are
  not recognised by the built-in name parser. Keyword/regex rules force `tv_show` and add a
  `full-series` or `full-season` tag. Because this bypasses the parser there is no base
  title, so **these never get a TMDB match, even after a reprocess**.
- **Image-only torrents** have no `content_type` in bitmagnet's enum. They get an
  `image-only` tag instead of a guessed `xxx`.
- **JAV releases** use studio-code names (`STARS-207`, `GVH646`, `FC2PPV-...`) that the
  default `xxx` matcher misses. `content_type` is a fixed enum, so they are set to `xxx`
  with an extra `jav` tag. The condition calls `.matches()` with a literal RE2 regex; the
  regex is loose, so spot-check a reprocess batch before trusting it broadly.

**`keywords:` is not regex.** It is a simplified glob language (`( ) | * ? + # `` ` `` only),
with no `[a-z]`, `\d` or `{n,m}`. The first JAV rule (2026-09-13) used regex syntax there,
compiled to literal punctuation, and silently matched 0 of ~250K `xxx` torrents (confirmed
2026-09-14). Use `.matches()` with a regex string when real regex is needed.

Changes only affect torrents processed after the deploy. Existing "unknown" torrents need a
manual reprocess (admin UI → "Enqueue torrent processing batch", force rematch).
