# Dagu

The container-native scheduler and the jobs that run on it. General monitoring conventions
and the unified scheduled-job metrics are in [monitoring.md](monitoring.md).

## Dagu (container-native scheduler)

`compose/docker-compose-utilities.yml`'s `dagu` service (`ghcr.io/dagucloud/dagu`,
exposed internally as `dagu.${NETWORK_DOMAIN}` like `phpmyadmin`/`tdarr`) replaces
containers whose whole job was "run something on a schedule" with one-shot containers
Dagu spins up itself via `docker.sock` (same blast-radius tradeoff as `watchtower`'s own
socket mount). DAG definitions live in `hosts/nas/.../dagu/dags/*.yaml`, tracked in git
(no secrets — see below); Dagu's run history/state (`/mnt/fastpool/system/processes/dagu`
minus `dags/`) is untracked runtime state, same treatment as Prometheus/Grafana's own
data directories.

Migrated so far: `mariadb-backup` (was `DB_DUMP_CRON` baked into the
`databack/mysql-backup` container's own env) and `bitmagnet-cleanup` (was an untracked
`while true; do psql …; sleep 86400; done` container). Both DAGs bind-mount a
plaintext-single-line secret file directly into the one-shot container rather than using
Docker Compose's `secrets:` mechanism (Dagu isn't Swarm, so that doesn't apply to
DAG-spawned containers) — `mariadb-backup` reuses the existing
`mariadb-backup-pass.txt`; `bitmagnet-cleanup` needed a new
`bitmagnet-cleanup-db-pass.txt` (same convention) since its old compose service got the
password from a plain Portainer-env `${BITMAGNET_CLEANUP_DB_PASSWORD}` substitution,
which doesn't apply to a DAG YAML file tracked in git.

The `bitmagnet_cleanup` Postgres role needs `SELECT` on `torrents_torrent_sources`,
`torrent_contents` and `torrents`, `DELETE` on `torrents`, **and `UPDATE` on at least one
column of `torrents`**. That last one is because the batched delete (2026-10-01) uses
`SELECT … FOR UPDATE SKIP LOCKED` so it skips rows the crawler has locked, and Postgres
requires `UPDATE` privilege for row locks even when nothing gets updated. Without it the
job fails with `permission denied for table torrents`. A column-level grant is enough
(`GRANT UPDATE (info_hash) ON torrents TO bitmagnet_cleanup;`). The FK cascades into the
child tables run as the table owner, so the role needs no privileges on those.

`themerr-fetch` (new, not a migration) downloads theme.mp3 files from ThemerrDB into
each Radarr/Sonarr movie/series folder, nightly at 03:30. No custom image or manual build
step: a `vendor` step copies the static `ffmpeg` (`mwader/static-ffmpeg`) and
`deno` (`denoland/deno:bin`, the JS runtime yt-dlp needs for some YouTube signatures)
binaries out of their upstream images into the shared `/data` volume before `fetch` runs
`dagu/scripts/themerr-fetch.py` against a stock `python:3.12-slim` with `/data` on `PATH`.
Both source images are `FROM scratch` with no shell inside them, so `vendor` can't
`docker.run` a command *in* them — it uses `docker create`+`docker cp` (via a
`docker:cli` helper) to read their filesystem without executing anything inside them.
Every image here is pulled once and cached by Docker, so deploying this DAG is just:
push the script + both DAG files — nothing built by hand on `nas`, and no apt-get cost on
every run. yt-dlp itself is still self-hosted by the script the same way it always was
(`ensure_ytdlp`, downloaded from its own GitHub releases and auto-updated). It joins
`multimedia_default` to reach `radarr`/`sonarr`/`emby` by container name — Dagu itself
only sits on `utilities_default`. Its three API keys use Dagu's `secrets:` + `ref:`
mechanism (`themerr-fetch/radarr-api-key` etc.), same as `mariadb-backup`/
`bitmagnet-cleanup` above — registered by hand once through the Dagu UI/API, the one
manual step this DAG still has. (Dagu's secrets spec confirms `ref:` requires a running
server and can't be satisfied by deploying files alone; the file-backed `provider: file`
alternative would avoid that, but this repo prefers `ref:` for consistency with the other
two DAGs' secrets.) Refreshes Jellyfin's library (`MEDIASERVER_TYPE=jellyfin`, switched from
Emby 2026-10-01).

Ownership: both steps run as root (needed for `vendor`'s docker-socket access and for
the arbitrary-uid `chown`s below — a non-root process can't `chown` to a uid/gid it
doesn't already own or belong to), but the script
hands its own files to specific NAS accounts afterward rather than leaving them
root-owned: `chown_data_dir()` gives everything under `/data` (state, vendored
`yt-dlp`/`ffmpeg`/`deno`) to `nas_processes` (`PROCESS_UID`/`PROCESS_GID`
params, default 920:920), and each written `theme.mp3` is `chown`'d individually to
`nas_multimedia` (`MEDIA_UID`/`MEDIA_GID` params, default 910:910) right after it's
written.

Rate-limiting: `MAX_DOWNLOADS` (a run param, default 100) caps YouTube downloads per
invocation so a cold cache or a post-ThemerrDB-outage run (which clears cached negative
lookups) can't blast through an entire library's worth of yt-dlp downloads in one run and
get the NAS's IP flagged — pass `MAX_DOWNLOADS=0` for a deliberate one-off catch-up run.
`DOWNLOAD_DELAY` (5s, hardcoded in the DAG's env) already throttled downloads themselves;
`THEMERRDB_DELAY` (0.3s, script default) was added alongside the cap to also throttle the
per-item ThemerrDB JSON lookups, which previously ran back-to-back with no delay at all —
lighter than a YouTube download but still someone's small self-hosted API, not a CDN built
for bursts.

(Backdrop/theme-video handling lives in its own DAG, `backdrop-generate` below.)

`backdrop-generate` builds Emby/Jellyfin "theme videos" (`backdrops/theme.mp4`). This is
a separate feature from theme songs, in a separate DAG. Runs nightly at 05:00,
deliberately after `themerr-fetch` (03:30) since both walk the same Radarr/Sonarr
libraries, and after most other NAS activity since it shares the P400 GPU with
Tdarr/Emby/Jellyfin transcoding.

**Source order: ThemerrDB video first, local montage as fallback.** ThemerrDB only stores
a YouTube URL per item (`youtube_theme_url`), picked for its audio. Sometimes that
video is the real opening sequence; often it's a still image with music. So each URL is
downloaded (same link rules as `trailer-fetch`: public, not 18+, 20-300s; best ≤1080p
h264 mp4) and checked for motion. At `STATIC_SAMPLES` (8) evenly spread points it
compares two frames `STATIC_GAP` (1s) apart. A point counts as moving at a mean 32×32
pixel-diff ≥ `STATIC_THRESHOLD` (2.0, the value `library-cleanup` validated live against
a known still-image backdrop). The video is static when fewer than half the points move,
which also catches slideshows of a few stills. A video that passes is remuxed as-is,
**audio kept** (a deliberate choice: it's the theme song, so it can overlap with
`theme.mp3`; set `THEMERR_VIDEOS=false` to go back to muted local montages only).

URLs that fail are recorded in `state.json`'s `rejected` map. Static is permanent;
unavailable (removed/private/geo/age/outside link rules) is retried after
`REJECT_RETRY_DAYS` (90). Rate limiting stops downloads for the rest of the run, and
other failures are just retried the next night. Neither of those two is recorded as
rejected. Every item is looked up in ThemerrDB each run (negative lookups are cached for
`RECHECK_DAYS`, like `themerr-fetch`), so a new URL for an item that currently has a
local montage gets tried and, if it moves, replaces the montage. A kept ThemerrDB video
is only replaced by a *newer* ThemerrDB URL that also passes, never by a local montage.
`MAX_DOWNLOADS` (50) and `DOWNLOAD_DELAY` (5s) throttle YouTube like the other two
yt-dlp DAGs, and a `vendor` step copies `deno` in for yt-dlp the same way `trailer-fetch`
does. When there's no ThemerrDB URL, when it's rejected, or when the download budget is
spent, the item falls through to the local montage below. The budget case gets
ThemerrDB tried again on a later run.

Not used: Emby's paid Movie/TV Theme Video plugins and their "Theme Contributors
Network" (a 2014 shared theme database). It has no public API, and joining it means
auto-uploading local files to a third-party server. Those plugins write
`backdrops/*.mkv`, which matches (unconfirmed) the pre-automation `.mkv` backdrops.

**Local montage fallback.** The two real precedents for this
([emby-theme-maker](https://github.com/Oratorian/emby-theme-maker),
[backdrop-generator](https://hub.docker.com/r/vlx42/backdrop-generator)) both generate
the clip locally from the media file itself.

Clip selection, per source file: one ffmpeg scene-cut scan (`select='gt(scene,X)'`,
downscaled first purely for speed) run *locally* around each of `N_SAMPLES` (default
10) target points spread across the file - not the whole file at once, which would
mean decoding entire multi-hour movies for no reason - then a `SAMPLE_DURATION`s
(default 3s) window is accepted only if it contains no detected cut *and* passes a
motion check (three sampled frames' pixel-diff must clear `MIN_MOTION`, so a technically
cut-free but visually static shot - e.g. a dialogue scene - doesn't get picked either).
If no window near a target passes both checks, search outward up to `SEARCH_RADIUS`
before giving up on that one sample slot rather than forcing a bad clip in. Accepted
clips are extracted silently (`-an` - a jump-cut montage's original audio would clash
with both itself and `theme.mp3`) and concatenated into the final file.

Movies sample directly from their one file. A series has no single "the video" to
sample from, so it picks `SERIES_EPISODES` (default 3) representative episodes spread
across the show (skipping specials and the pilot, which is often visually atypical),
splits the sample budget across them, and concatenates everything into one series-root
`backdrops/theme.mp4` - no per-season backdrops. This needs Sonarr's episode-level API
(`/api/v3/episode` joined with `/api/v3/episodefile` by `episodeFileId`), which
`themerr-fetch` never needed since it only ever wrote into the series *folder*.

GPU: unlike `themerr-fetch`'s vendored `mwader/static-ffmpeg` (confirmed to have zero
NVIDIA/NVENC support - it's a portable static build, and NVENC fundamentally can't be
static since it needs the host's own driver libraries injected at container start),
this DAG runs `jrottenberg/ffmpeg:8.0-nvidia` directly as its image with `host.Runtime:
nvidia` + `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` (the same mechanism
`docker-compose-multimedia.yml`'s `runtime: nvidia` services already use for this same
P400). Because that image is dynamically linked against Ubuntu's own libraries, it
can't be vendored out via `docker cp` the way the static binaries are elsewhere - it
has to be the step's own image, with `apt-get install python3` on every run (no static
alternative exists here, unlike the audio DAG's ffmpeg). The image's own `ENTRYPOINT
["ffmpeg"]` has to be cleared (`container.entrypoint: []`) or the step's command would
run as arguments *to* ffmpeg instead of replacing it. CUDA decode and `h264_nvenc` are
used for every ffmpeg call (scene/motion sampling, clip extraction and, if straight
`-c copy` concat ever fails, the fallback re-encode too), with **no CPU fallback**: the
run starts with a tiny NVENC probe, and any failed GPU call re-runs that probe. If the
GPU is gone, the run aborts with exit 1 (so `DaguDagRunFailed` fires). If the probe still
passes, only that item fails. This replaced a per-call `libx264` fallback after the P400's
driver wedged mid-run on 2026-09-30: CUDA init kept failing slowly, and one run crawled
through ~20 min per clip for 17+ hours.

Ownership and the `.nobackdrop` skip file work exactly like `themerr-fetch`'s
`theme.mp3`/`.nothemerr` (see [[project_nas_users_groups]] for the UID:GID table) -
`chown_data_dir()` to `nas_processes`, each written backdrop `chown`'d to
`nas_multimedia`. Each `owned` entry records its `source` (`themerr` with its `url`, or
`local`; entries written before the ThemerrDB step existed have no `source` and count as
`local`). A local montage is regenerated when the source file(s)' own size/mtime changes
(e.g. a quality re-encode), via a `sources` fingerprint list recorded per folder.

**Not done yet, deliberately out of scope so far:** feeding Dagu's own per-DAG run
status (success/fail/last-run) into the shared `scheduled_job_*` metric family (see
[monitoring.md "Scheduled jobs (unified)"](monitoring.md#scheduled-jobs-unified)) the way every other scheduled-job source does, and a
direct Prometheus scrape of Dagu's own `/api/v1/metrics` for engine-level health
(`dagu_scheduler_running` etc.). Until that's built, a DAG silently failing or Dagu
itself going down has no alert coverage beyond whatever the DAG's own container/job
otherwise produces — check the Dagu UI directly for now.

### trailer-fetch

Replaces Trailarr (removed from the `multimedia` stack 2026-10-01). Trailarr offers no crop option, and its
trailers have letterbox bars baked in. Jellyfin's Media Bar Enhanced plugin plays local
trailers zoomed to fill the slide (`object-fit: cover`), so those bars show up as black
bands across the home screen. `trailer-fetch` writes up to `MAX_TRAILERS` (5)
original-language trailers per Radarr/Sonarr item into `trailers/`, nightly at 02:00, ahead of `themerr-fetch` (03:30) and
`backdrop-generate` (05:00). It shares the P400 GPU with `backdrop-generate`.

**Trailer choice: TMDB, not NFOs.** Sonarr's Kodi NFO writer emits no `<trailer>` at all.
Radarr's writes `plugin://plugin.video.youtube/play/?video_id=<id>`, and that ID is just
the `youTubeTrailerId` field its API already returns, so the script reads it from the API
instead of parsing NFOs. The trailer itself comes from TMDB (`TMDB_API_KEY`, either a v3
API key or a v4 read-access token, detected by the `eyJ` JWT prefix):

- It fetches `original_language` from `/movie|tv/{id}` once and caches it in `state.json`.
- It then fetches `/videos` with `include_video_language=<orig>`.
- It keeps YouTube videos whose `iso_639_1` is the original language and whose type is in
  `VIDEO_TYPES`. TMDB tags a dubbed trailer with the dub's language, which is what makes
  "no dubs" enforceable.
- Ranking: Radarr's own trailer first if it qualifies, then official, then type order,
  then larger size, then oldest. The top `MAX_TRAILERS` that pass the link rules below are
  downloaded; the ranking only decides which ones make the cut, not play order (turn on
  Media Bar's "Randomize Local Trailers" to rotate through them).

With `ORIGINAL_LANGUAGE_ONLY` (default on), an item without an original-language trailer
gets none. It's re-queried after `RECHECK_DAYS`. With it off, the fallback is Radarr's
unverified trailer. Series with `tmdbId` 0 are resolved via `/find/{tvdbId}`.

On YouTube's side, yt-dlp's `-S lang,...` sorts the original audio track above YouTube's
auto-dubbed tracks. `res:1080,vcodec:h264,ext:mp4:m4a` then selects 1080p H.264 plus AAC
(format `137+140` when tested). A trailer without bars is therefore only remuxed, and only
cropped trailers get re-encoded with `h264_nvenc`, falling back to `libx264`.

**Video availability: ThemerrDB's link rules.** ThemerrDB's `src/updater.py`
(`validate_youtube_requirements`) rejects links that aren't public, are age-restricted,
aren't available in the US, run outside 20–300s, or no longer exist. It checks these
through the YouTube Data API. This job applies the same rules without a YouTube API key or
any extra request:

- A yt-dlp `--match-filters "availability=?public & age_limit<?18 & duration>=?20 &
  duration<=?300"` on the download request itself. When a video fails it, yt-dlp prints
  `does not pass filter` and exits 0 with no file. That line only appears without
  `--quiet`, so the job doesn't pass `--quiet`.
- yt-dlp's own errors for private, removed, geo-blocked (checked from the NAS's own region
  instead of the US) and age-gated videos.

A rejected video goes into `state.json` `bad` for `BAD_VIDEO_DAYS` (90), and the next
ranked candidate is tried in the same run. Once every candidate has been tried (or the item
reached `MAX_TRAILERS`), the pass is recorded in `state.json` `checked`, and an item still
below the cap asks TMDB again after `RECHECK_DAYS` (14), which picks up trailers released
later (a final trailer for a new release). A pass cut short by `MAX_DOWNLOADS` isn't
recorded, so the next run resumes it.

**Playing nice with the APIs.**

| Service | Behaviour |
| --- | --- |
| YouTube | `MAX_DOWNLOADS` (50) caps *attempts* per run, including rejected ones, with `DOWNLOAD_DELAY` (5s) between every attempt. A `DRY_RUN` honours the cap too, so it previews exactly what the next real run would do; add `MAX_DOWNLOADS=0` to list the whole backlog. |
| YouTube, rate limit | A bot-check, HTTP 429 or "try again later" error stops all YouTube requests for the rest of the run. That check runs before the "unusable" patterns, because a rate-limited session can get *"Video unavailable … try again later"*. |
| YouTube, other failures | Any other failure puts the item on exponential backoff (1, 2, 4 … up to `MAX_BACKOFF_DAYS` 30 days, in `state.json` `failed`). |
| TMDB | Items that won't be attempted this run are skipped *before* the TMDB lookup (download cap reached, backoff, missing-cache), so a long backlog doesn't hit TMDB for every remaining item nightly. The gap between calls is 0.1s, well under ThemerrDB's own 40/s TMDB limiter. `original_language` is cached permanently. |

**Crop detection.** Two choices, each made after a failure:

- **Not ffmpeg `cropdetect`.** `cropdetect` counts a row as picture once the row's
  *average* luma passes its limit. Fan and aggregator uploads often put the film title or
  a channel logo inside the letterbox bar, and that text lifts the row average, so the bar
  survives the crop. This was reported on a real trailer ("500 Days of Summer" in the
  bottom bar) and reproduced on a synthetic one: `cropdetect` gave `1920:982:0:40`, which
  kept both bars. Instead, `window_box()` pipes 2 fps grayscale frames out of ffmpeg and
  counts a row or column as picture only if more than `CROP_OVERLAY_PCT` (25%) of its
  sampled pixels are brighter than `CROP_LIMIT` (24; black ≈ 16). On the same trailer
  that gives `1920:804:0:138`. Within a window the box is the maximum over its frames, so
  dark frames can't shrink it. Detection costs about 3–4s of CPU per 1080p trailer.
- **Majority vote, not union.** 8 two-second windows are sampled between 10% and 90% of
  the runtime. The crop is the box most windows agree on (within 8px), widened to cover
  those agreeing boxes, and it needs at least half the windows. A union would be cancelled
  by a single full-frame rating card or studio logo. Caught on a synthetic trailer with a
  2s full-frame intro: the union gave `1920:1080`, the majority vote gave the correct
  `1920:800:0:140`.
- Windows that are entirely black produce no box and are dropped.
- Bars thinner than `CROP_MIN_PCT` (2%) are ignored.
- Trailers that switch aspect ratio mid-way (IMAX sections) get cropped to the dominant
  framing.

**Naming and ownership.**

- Files are named `<Title (Year)> - <TMDB video name> [<YouTube id>].mp4`. The id is in
  brackets because YouTube ids themselves contain `-` and `_`; it keeps names unique per
  video. Illegal filename characters are stripped and the name is capped at 200 bytes
  before the id.
- `state.json` `owned` maps each folder to its YouTube ids and file names. A file counts as
  ours if it's listed there, **or** if its name ends in `[<11-char id>].mp4`. The tag rule
  means a rename that keeps the tag is simply followed, and a lost `state.json` doesn't
  cause re-downloads or deletions: everything is re-adopted by tag. The flip side is that
  a hand-downloaded file using yt-dlp's default `Title [id].mp4` naming is adopted too.
  Entries for folders no longer on disk are pruned after every run.
- **Deleting one of our trailers is a "bad" vote.** An id listed in `owned` with no file
  left under either rule can only have been deleted by hand: neither this job nor
  `library-cleanup` ever removes our files, and `library-cleanup` only moves files *into*
  `trailers/`. The job then adds it to `state.json` `bad` with `user: true`, which is
  permanent (unlike link-rule rejections, which expire after `BAD_VIDEO_DAYS`). It also
  clears the folder's `checked` mark, so the next-ranked candidate is fetched on the next
  run rather than after `RECHECK_DAYS`. Votes live only in `state.json`, so losing it loses
  them. To un-vote, delete the id from `bad`. To have a trailer re-fetched instead
  (e.g. after a crop fix), delete its entry from `owned` *before* removing the file.
- Only `trailers/` is looked at. Any other video in there counts as foreign (Trailarr's or
  hand-placed). Foreign files stay until the item has at least one of ours, then they're
  deleted, on that run or any later one. An item whose candidates all fail keeps its
  foreign trailers.
- A `*-trailer.*` next to the main video is `library-cleanup`'s job: it moves it into
  `trailers/`, after which this job treats it like any other foreign trailer.
- A trailer of ours that TMDB later drops or replaces is kept, not deleted.
- `.notrailer` skips a folder, which is the way to protect a hand-placed trailer.
- `UID`/`GID` handling works the same as in the sibling jobs.

**Media-server notify.** The job mounts the library at `/mnt/movies` and `/mnt/series`,
the same container paths Emby and Jellyfin use. That lets it send a targeted
`POST /Library/Media/Updated` with the changed folders (the same call Sonarr/Radarr's own
Emby connector makes, and verified in Jellyfin's `LibraryController`) instead of a full
library scan. Like the sibling jobs it targets one server via `MEDIASERVER_TYPE` (`emby` or
`jellyfin`), `MEDIASERVER_URL` and `MEDIASERVER_API_KEY`. All three DAGs were switched to
Jellyfin together on 2026-10-01; keep them on the same server.

**Deploying needs the TMDB key and API-key secrets registered as Dagu `ref:` secrets.**
Same as the sibling jobs: `trailer-fetch/{radarr,sonarr,tmdb,mediaserver}-api-key`. Also
set Media Bar Enhanced's "Prefer Local Trailers" option.

### library-cleanup

Unlike `themerr-fetch`/`backdrop-generate` (which add files), `library-cleanup` finds and
removes cruft that's accumulated across the library's history — OS artifacts (`Thumbs.db`,
`desktop.ini`), re-scrape backup files (`*.nfo-orig`/`.bak`/`.orig`), stale partial
downloads, and broken pre-automation `backdrops/theme.html` stubs (an old downloader's
saved HTML error page, from before `backdrop-generate` existed and started writing real
`theme.mp4` clips). See [multimedia-library-layout.md](multimedia-library-layout.md) for
the full category table and the canonical layout it's checking against.

Because its job is deletion rather than addition, it inverts the other two DAGs' `DRY_RUN`
default: `APPLY` defaults to `"false"` (report-only) rather than defaulting to live. Beyond
plain deletion it also consolidates fanart (moves loose numbered `fanartN.*` files into
`extrafanart/`, drops exact-hash duplicates including music's legacy `extrathumbs/`, and
patches the handful of Emby-written nfos that store a literal path to one of those images),
moves `*-trailer.*` videos from a movie/series root into `trailers/` (the only place
`trailer-fetch` looks, which then replaces them with its own), and merges folders that differ only by case - both found live in this library (see
[multimedia-library-layout.md](multimedia-library-layout.md) for the concrete examples and
the full conflict-handling rules). Findings that need a human judgment call - folders with
zero or multiple video files, oversized backdrops, a `backdrops/theme.mp4`/`.mkv` that's
actually a frozen image throughout (sampled frame-to-frame pixel-diff, same technique
`backdrop-generate`'s own motion check uses - confirmed live against `Matrix Resurrections,
The (2021)`'s backdrop, a 60s/3.8MB clip that's really a still image), episodes missing an
`.nfo` - are written to the report but never auto-deleted, regardless of `APPLY`.

It's a plain filesystem walk with no Radarr/Sonarr/Emby API calls (nothing here needs their
metadata), so it needs no secrets and no `multimedia_default` network join - the one
exception is the static-backdrop check, which needs `ffmpeg`/`ffprobe` to decode sample
frames, so the step installs it inline (`apt-get install ffmpeg` into the `python:3.12-slim`
image, same inline-install pattern `backdrop-generate` uses for `python3`) rather than
switching the whole DAG to an ffmpeg-based image over a check that's a small fraction of its
job; missing `ffmpeg` degrades to skipping just that one check with a warning; no CUDA/GPU
needed since it's a 32x32 frame probe on already-small clips, not an encode. The check
decodes only keyframes in one pass per file (`-skip_frame nokey -fps_mode passthrough`),
~0.1s/file. The older probe + 4 `-ss` seeks cost 1-3s/file, because each seek decodes up
to a full GOP, so the whole library took ~35 min. The seek method now runs only when a
clip has fewer than 2 keyframes, and to confirm any "static" verdict, so the fast path can
clear a file but never flag one on its own. Fanart dedup hashes only files whose byte size
collides with another candidate. Runs weekly
(Sundays 06:00), after the nightly theme-song/backdrop jobs, since it's a full-library scan
rather than an incremental one.

