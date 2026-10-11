# Stash: Library Organizer plugin

A custom Stash raw plugin (`library-organizer`, Python) that keeps each adult-media library's
video, NFO, poster and fanart named, filed and described from Stash's own data. Source:
`hosts/nas/mnt/fastpool/system/extras/stash/config/plugins/library-organizer/` (deploys to the
Stash `/config/plugins/library-organizer/`). Stash image: `ghcr.io/feederbox826/stash-s6:hwaccel`
(see `compose/docker-compose-extras.yml`).

## What it does

For every scene in a configured library (matched by the video's path prefix):

- Renames the video to `{studio} - {date} - {title} - {performers} [{source}-{height}p].ext`.
  Empty segments are left out with their separator. Performers are alphabetical, except that an
  existing name listing exactly the same performers keeps its order (avoids mass renames).
- With `studio_folders: true` the file lives in `<library>/<direct studio>/`; a studio change
  moves it (folders are created, case-insensitively reused, and an emptied old folder is removed).
  Scenes without a studio stay in their current folder.
- Moves/renames sidecars with the video: `.nfo`, `.nfo-orig`, `-poster.*`, `-fanart.*`, subtitles
  and the like. Legacy `-background.*` art becomes `-fanart.*` (extension kept).
- Writes a Kodi/Jellyfin `<movie>` NFO from Stash data (title, plot, dates, studio chain,
  performers, tags, rating, runtime, play data, ids, fileinfo, and a `<stash>` block with the rest).
  Existing `uniqueid`s (tvdb/whisparr/tpdb/…) are always preserved; unknown elements are copied
  through; a Whisparr `<episodedetails>` NFO is converted. Unchanged NFOs are not rewritten.
- Creates a missing poster (2:3 crop) and fanart from the scene screenshot via the container's ffmpeg.
- Optionally asks Whisparr to rescan the series after a move (`whisparr.enabled`).

Triggers: `Scene.Create/Update.Post`, `Studio.Update.Post`, `Performer.Update.Post` (a renamed
studio/performer re-processes its scenes). Tasks: **Dry run: all libraries** (always dry, writes a
report file), **Reconcile all libraries**, **Clean orphaned sidecars** (quarantines to
`<base_path>/.orphans/<library>/…`, or deletes with `orphan_action: delete`).

## Studio modes (per library)

Each library is one of three valid shapes; anything else is rejected at load with an error naming the library:

| `studio_mode` | `studio_folders` | `studio` / `studios` |
| --- | --- | --- |
| `follow` (default; `free` alias) | must be `true` | neither allowed |
| `fixed` (`folder` alias) | `true` | `studios` optional; `studio` not allowed |
| `fixed` | `false` | `studio` required; `studios` not allowed |

- **`follow`** (e.g. `Named`): Stash decides the studio; each scene is filed under `<library>/<direct studio>/`
  and moves when the studio changes.
- **`fixed` + `studio_folders: true`** (e.g. `Tubes`): the first-level folder below the library root *is*
  the studio (`Tubes/HypnoTube/` → "HypnoTube"). `studios:` maps folder name → studio name (a mapping or
  a list of single-entry mappings) when they should differ. Files directly in the library root are left
  to Stash. No studio folders are created or moved.
- **`fixed` + `studio_folders: false`**: `studio: <name>` forces one studio for the whole library.

In `fixed` libraries a scene whose studio differs is reset to the forced studio (the studio is
created if missing), then the previous studio is deleted when it has no scenes, images, galleries,
groups or child studios left (checked live; otherwise it is kept). Reports: `CREATE-STUDIO`,
`SET-STUDIO old -> new`, `DELETE-STUDIO`. Only organized scenes are touched (`organized_only`), so a
fresh scrape keeps its uploader studio until you flag the scene. Renaming the studio entity itself
in Stash makes the next run recreate the forced one.

Scene processing is serialised by a lock file (`/tmp/library-organizer-<uid>.lock`) and re-reads the scene
inside the lock, so the hook that our own studio/file edit re-fires waits for the first run and then
sees a finished state.

## Settings and config

Plugin settings (Settings → Plugins): `dry_run` (default **on**; unset means on), `organized_only` (default **on**: scenes not flagged Organized in Stash are ignored by hooks and tasks; ticking Organized fires the update hook, so a scene is processed the moment you flag it),
`dry_run_report_path`, `enable_hook`, `library_filter`, `orphan_action`, `orphan_min_age_days`.
The dry-run task writes `/config/library-organizer-dry-run-report.txt` (host: `/mnt/fastpool/system/extras/stash/config/`) when no report path is set.
`libraries.yaml` defines `base_path`, the libraries (`path`, `studio_folders`, `default_source`,
`max_path_len` default 225, `rename`, `whisparr` default false), `poster_crop_x` and the optional top-level
`whisparr` block (connection + master `enabled` switch). A library's Whisparr rescan after moves runs only when
that library has `whisparr: true` **and** the top-level block is enabled.
`{{WHISPARR_API_KEY}}` in `libraries.yaml` is filled from `.tokens` by `scripts/deploy.ps1`
(deploy aborts until the key exists in `.tokens`); Whisparr stays disabled until
`whisparr.enabled: true`.

## Rollout

1. Deploy: `scripts/deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/extras/stash/config/plugins/library-organizer`
   (copy only), then Settings → Plugins → Reload plugins in Stash. mcMetadata's hook/renamer must
   stay off (it also renames files).
2. Run **Dry run: all libraries**, review `library-organizer-dry-run-report.txt` (summary line at the end).
3. Turn `dry_run` off, run **Reconcile all libraries**; later **Clean orphaned sidecars** (dry first).

The first dry run on 2026-10-09 (1698 scenes) planned ~500 moves, mostly real differences between
Stash data and existing names: `[WEBDL-540]`→`[WEBDL-540p]`, titles edited since import, performers
shortened to fit the 200-char path cap, studio/date prefixes added to HypnoTube/Unnamed files,
~24 studio changes. Raise `max_path_len` or set a library's `rename: false` to reduce churn.

## Gotchas

- Stash runs as root, the library is `1001:930` mode `775`: the plugin copies owner/mode from the
  parent for every directory/file it creates. Don't create paths with Stash's own mkdir.
- Stash's `moveFiles` requires the destination to be inside a library path, and
  `destination_basename` works for a single file only → multi-file scenes are skipped (`SKIP … files in one scene`).
- Scenes without a title keep their file name (NFO title falls back to the file name).
- Name sanitising mirrors Whisparr: `:` and `"` are dropped (not replaced with ` - `), `\ / |`
  become `-`, `? * < >` are removed; reserved Windows names get `_`; 255-byte component and
  `max_path_len` budgets shorten performers (`+N`) first, then the title.
- Whisparr v2 `<uniqueid type="whisparr">` is the **episode** id: `GET /api/v3/episode/<id>` →
  `seriesId` → `POST /api/v3/command {"name":"RescanSeries","seriesId":N}`. Whisparr series are
  per-studio folders, so a move into another studio's folder leaves the old series path (a rescan
  only fixes in-folder renames). v3 (`RescanMovie`) is untested — `VERIFY` in `lo/whisparr.py`.
- Whisparr's Kodi/Emby metadata (episode `.nfo` only, no images) can overwrite our NFO on its own
  events; the next hook or reconcile converts it back.
- Untested against a live move: `moveFiles` has not been exercised by the plugin on real files
  (unit tests use a fake Stash); the dry-run planner is verified against the live instance.

## Tests

`tests/` (unittest, no extra deps). Run inside the stash container, e.g. copy the plugin dir to
`/tmp/lo-test` there and `python3 -m unittest discover -s tests`. For a live planner check:
`STASH_API_KEY=… python3 library_organizer.py --standalone --url http://localhost:9999 --mode dry_run_all --report /tmp/r.txt`
(`--live` really changes files; `--scene ID` processes one scene).
