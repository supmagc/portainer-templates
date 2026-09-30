# Multimedia library layout

The canonical on-disk shape for `/mnt/rightpool/multimedia/{movies,series,music}`, and the
cleanup categories the `library-cleanup` Dagu DAG (see
[monitoring.md](monitoring.md#library-cleanup)) scans for. Written 2026-09-28 after
surveying the live library (1633 movie folders / 3.9T, 89 shows / 1.6T, 153 artists / 140G)
and verifying naming against current Emby and Jellyfin documentation — the existing library
already matches these conventions closely; this mostly formalizes what's there and gives
the cleanup DAG a spec to check against.

Two things already write into these trees on a schedule and are **not** cleanup targets:
`themerr-fetch` (theme.mp3, see its section below `monitoring.md`) and `backdrop-generate`
(backdrops/theme.mp4). Both follow an "own what we wrote, never touch what we didn't" rule
via a state file — the cleanup DAG follows the same principle for its own categories.

## Movies — `movies/<Title> (<Year>)/`

```
Movies/
  Sicario (2015)/
    Sicario - Bluray-720p - RARBG.mkv
    Sicario - Bluray-720p - RARBG.nfo
    Sicario - Bluray-720p - RARBG.eng.srt
    poster.jpg  fanart.jpg  banner.jpg  logo.png  clearart.png  landscape.jpg  disc.png
    extrafanart/fanart1.jpg  extrafanart/fanart2.jpg  ...
    backdrops/theme.mp4        # owned by backdrop-generate
    theme.mp3                  # owned by themerr-fetch
    trailers/Sicario (2015) - Official Trailer [<youtube-id>].mp4
```

- **One video file per folder.** Radarr names it `<Title> (<Year>) - <Source>-<Quality> -
  <ReleaseGroup>.<ext>`. A second cut (extended/director's) belongs in the same folder only
  with an edition tag Radarr recognizes; an untagged second video file is a review case,
  not a supported layout.
- **`<video-basename>.nfo`** — Radarr-written, required. Subtitles should follow the same
  `<video-basename>.<lang>.srt` pattern (legacy files named after just the title, without
  the release tag, still work but drift from the video if it's ever re-released/renamed).
- **Images** — `poster`, `fanart`, `banner`, `logo`/`clearlogo`, `landscape`/`thumb`,
  `clearart`, `disc`/`discart`, `.jpg`/`.png` per Emby's documented set (all also read by
  Jellyfin). `extrafanart/fanartN.jpg` holds extra still images Kodi/Emby/Jellyfin rotate
  through — this is a **different thing** from `backdrops/`, not a duplicate of it: one is
  still images, the other is a short looping video clip.
- **`backdrops/`** — theme *video*, `theme.mp4` (or `.mkv` for a handful of pre-automation
  entries that predate `backdrop-generate` and were never touched, per its "don't touch
  what we didn't write" rule). Not a Kodi/Emby convention on its own — it's this repo's own
  automation's output directory name.
- **`trailers/`** — local trailer file(s), any name; Jellyfin/Emby both read the whole
  folder. `trailer-fetch` writes up to 5 as `<Title (Year)> - <TMDB name> [<YouTube id>].mp4`
  and deletes any other trailer here once one of its own is in place (`.notrailer` opts a
  folder out).

### Movies — cleanup categories

| Category | Examples | Action |
|---|---|---|
| OS/tool cruft | `Thumbs.db`, `desktop.ini`, `.DS_Store` | delete |
| Re-scrape backups | `*.nfo-orig`, `*.bak`, `*.orig` | delete |
| Stale partial downloads | `*.part`, `*.crdownload`, `*.!qB` | delete |
| Broken legacy backdrop | `backdrops/theme.html` (an HTML error/landing page saved by a pre-automation downloader, not a video) | delete |
| Oversized backdrop | `backdrops/theme.*` over ~150MB (a real theme clip is single-digit-to-tens of MB; this size suggests a full file landed there by mistake) | report only |
| Static-image backdrop | `backdrops/theme.mp4`/`.mkv` that's frozen throughout (confirmed live: `Matrix Resurrections, The (2021)` — a 60s, 3.8MB clip that's really a still image encoded as video, mean frame-to-frame pixel-diff ≈0.08 against a 2.0 threshold) | report only |
| Loose trailer | `*-trailer.*` video next to the main video (also at a series root) | move into `trailers/` together with its same-named `.nfo` (exact duplicate of one already there: delete); `trailer-fetch` takes it from there |
| Orphaned nfo | `<name>.nfo` with no `<name>.<video>` beside it, in any subfolder — an upgraded/renamed video's old nfo, or a `trailers/` nfo after `trailer-fetch` replaced that trailer. `movie.nfo`/`tvshow.nfo`/`season.nfo` never count; a folder with no video at all is left alone (see "No video file"), except `trailers/` | delete |
| No video file | folder has no `.mkv`/`.mp4`/`.avi` | report only — usually an in-progress Radarr import |
| Multiple video files | more than one `.mkv`/`.mp4`/`.avi` without an edition tag (loose trailers don't count) | report only — could be a legitimate multi-cut |

## Series — `series/<Show Title>/Season NN/`

```
Series/
  The Handmaid's Tale/
    tvshow.nfo  poster.jpg  fanart.jpg  banner.jpg  logo.png  clearart.png
    season01-poster.jpg  season01-banner.jpg  season01-landscape.jpg
    season-all-poster.jpg
    extrafanart/fanart1.jpg ...
    backdrops/theme.mp4        # owned by backdrop-generate, sampled from representative episodes
    theme.mp3                  # owned by themerr-fetch
    trailers/...
    Season 1/
      season.nfo
      The Handmaid's Tale - S01E01 - WEBDL-720p - ITSat.mkv
      The Handmaid's Tale - S01E01 - WEBDL-720p - ITSat.nfo
      The Handmaid's Tale - S01E01 - WEBDL-720p - ITSat-thumb.jpg
      The Handmaid's Tale - S01E01 - WEBDL-720p - ITSat.eng.srt
```

- Series-level images follow the same set as movies, plus **season-scoped** images named
  `seasonNN-<type>.ext` (or `season-specials-<type>.ext` / `season-all-<type>.ext`), which
  is the documented Emby/Jellyfin fallback for when season folders don't carry their own
  artwork — this library uses them at the series root even though season folders exist,
  which both servers support.
- Each `Season NN/` folder holds `season.nfo` plus one video + `.nfo` + `-thumb.jpg` +
  subtitle(s) per episode, named `<Show> - SxxEyy - <quality> - <group>.<ext>` (Sonarr's
  format).
- `backdrops/` and `theme.mp3` sit at the **series root**, not per-season — `backdrop-generate`
  samples a handful of representative episodes across the show, not every one.

### Series — cleanup categories

Same table as movies, applied per episode file for the backup/cruft/partial-download rows,
plus:

| Category | Examples | Action |
|---|---|---|
| Episode missing nfo | video file with no matching `.nfo` | report only — usually a Sonarr metadata-refresh gap |
| Season with no video files | `Season NN/` with only images/nfo | report only |

## Music — `music/<Artist>/<Album>/`

```
Music/
  Blackmore's Night/
    artist.nfo  fanart.jpg  banner.jpg  logo.png
    extrafanart/fanart1.jpg ...
    Secret Voyage/
      album.nfo  folder.jpg  cover.jpg  disc.png  discart.png  cdart.png
      Secret Voyage - 01 - Blackmore's Night - God Save the Keg.mp3
      ...
```

- One folder per album, one folder per artist containing its albums — Jellyfin's own
  requirement is just "one album per folder"; embedded tags (artist/album/track), not the
  folder/file names, drive matching in both Emby and Jellyfin. Track filenames here follow
  `<Album> - NN - <Artist> - <Track>.<ext>` for human browsability, not because the servers
  need it.
- Artist-level: `artist.nfo`, `fanart.jpg`, `banner.jpg`, `logo.png`, `extrafanart/`.
  Album-level: `album.nfo`, `folder.jpg`/`cover.jpg`, `disc.png`/`discart.png`/`cdart.png`.
- **`extrathumbs/`** alongside `extrafanart/` on older artist folders is a leftover from
  the Kodi-era scraper (Ember Media Manager) that generated both; only `extrafanart/` is
  part of the current Emby/Jellyfin-relevant set — an `extrathumbs/` folder that's a byte-
  for-byte duplicate of `extrafanart/` is redundant, but this needs a content comparison,
  not a blind delete (report only, not auto-cleaned).
- **`_imgdb.nfo`** inside `extrafanart/`/`extrathumbs/` is Ember Media Manager's own
  bookkeeping file — not read by Emby, Jellyfin, or anything else in this stack.

### Music — cleanup categories

| Category | Examples | Action |
|---|---|---|
| OS/tool cruft | `Thumbs.db`, `desktop.ini`, `.DS_Store`, `_imgdb.nfo` | delete |
| Re-scrape backups | `*.nfo-orig`, `*.bak`, `*.orig` | delete |
| Album folder with no audio files | album dir with only images/nfo | report only |
| `extrathumbs/` alongside `extrafanart/` | possible duplicate of the same images under the Kodi-only name | report only |

## Case-duplicate folders

Two folders differing only by case (e.g. `Mika` vs `MIKA`) can both exist side by side on
a case-sensitive filesystem, and this library has some — always one fully-populated folder
plus an empty leftover stub (just a lone `artist.nfo`/`album.nfo`), the residue of a rename
or re-scrape that created new-cased folder without removing the old one. Seen at both the
music artist level (`Mika`/`MIKA`) and the album level (e.g. `Sabaton/The Art Of War` vs
`Sabaton/The Art of War`) — checked at the top level of all three libraries plus the album
level within each artist, since that's where the evidence actually is; nothing suggests
per-season case drift under a show, so that's not checked.

The cleanup DAG merges every such group into whichever folder has the most files
(recursively), matching subfolders case-insensitively at every level so a nested folder
(e.g. an album folder repeated under both artist-case variants) merges correctly instead of
sitting side by side. Within a merge: an exact byte-for-byte duplicate is dropped silently;
a same-name file that actually differs is **never** silently discarded if it's audio/video —
it's kept alongside the original with a `(case-merge)` suffix for manual review — and is
dropped only for a differing metadata/image file (nfo, cover art), which is cheap to
re-derive and reported either way. The loser folder is removed once emptied.

## What the cleanup DAG does and doesn't do

`library-cleanup` (weekly, `APPLY=false` by default — unlike the additive `themerr-fetch`/
`backdrop-generate` DAGs, this one defaults to **report-only** because its job is deletion)
auto-deletes the "delete" rows above (OS cruft, re-scrape backup files, stale partial
downloads, broken legacy `backdrops/theme.html` stubs, orphaned nfos), moves loose trailers into
`trailers/`, consolidates loose/duplicate fanart images, and merges case-duplicate folders — all under the same `APPLY` switch, and
all hash-verified or NFO-reference-safe as described above. Every "report only" row is
listed in its output for manual review and never touched automatically — folders with zero
or multiple video files, missing episode nfos, and oversized backdrops all need a human
judgment call the script can't safely make.
