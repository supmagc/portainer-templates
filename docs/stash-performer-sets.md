# Stash: Performer Sets plugin

A custom Stash raw plugin (`performer-sets`, Python) that downloads photosets and promo images for
selected performers, one Stash gallery per set, deduplicated across sources. Source:
`hosts/nas/mnt/fastpool/system/extras/stash/config/plugins/performer-sets/` (deploys to the Stash
`/config/plugins/performer-sets/`). It replaces the `performerGallery` (porngals4, per-performer zips),
`babepediaGalleryScraper` (per-performer zips) and `stashdb-performer-gallery` (`/data/Performers`) plugins.

## Layout

```text
/data/PerformerSets/                                 Stash library, images only
  Abella Danger [6]/                                 "[<performer id>]" is the key; the name is only set on creation
    _promo/                                          stash-box performer images → one gallery "<name> - Promo"
      theporndb-<image id>.jpg, fansdb-<image id>.jpg …, set.json
    Evil Angel - <title> [pornpics-62556155]/        one folder = one set = one folder-based gallery
      001.jpg … 016.jpg, set.json
    Fitting Room - Studio Session Vol 07 [elitebabes-93068]/
```

- A set with several performers is stored once, under the performer it was found for; the gallery
  gets all of them that exist in Stash.
- Renaming a performer in Stash keeps the old folder (found by `[id]`); rename the folder by hand if
  wanted, then run a scan.
- `set.json` is the source of truth for a gallery's metadata (survives a database rebuild):
  `kind` (`set`/`promo`), `source`, `source_id`, `urls` (every source the set was seen on),
  `title`, `studio`, `date`, `posted`, `performer_id`, `performer`, `other_performers`,
  `source_tags`, `source_image_count`; promo: `images` (file stem → source URL).

## Selection

A performer is processed when **any** enabled criterion matches (union): **favorite**
(`include_favorites`, default on), **rating ≥ `min_rating`** (stars as in the UI, half stars allowed;
converted to `rating100`), or **one of `tags`** (comma-separated names/aliases, sub-tags included).
Nothing enabled = nothing selected.

## Sources (verified 2026-10-23)

| Source | Listing | Set page gives | Images | Not available |
| --- | --- | --- | --- | --- |
| PornPics | `/search/srch.php?q=<name>&limit=20&offset=N` JSON (`g_url`, `gid`); the site pages max 25×20 | `h1` (descriptive, not the studio's title), Channel = studio, Models, Categories | `cdni.pornpics.com/1280/…`, ~900–1500 px long edge (`data-size` under-reports) | dates |
| EliteBabes | model page `/model/<slug>/` → `apiUrl` → `/gridapi/?content=model&nr=<id>&sort=latest&mpage=N` (empty body = end) | `h1` "`<models> in <studio> set <title>`" = real set title, `a.overlay-b` = studio + models, `a.visible-tag` | `cdn.elitebabes.com/content/…`, ~1066–1800 px | release date: the grid date is EliteBabes' **upload** date (2026-10-04 on an old X-Art set), stored as `posted` only |
| stash-box (promo) | the performer's `stash_ids` × configured stash-boxes (`findPerformer { images }`) | — | TPDB/FansDB posters; many are < 800 px | — |

The search/model slug comes from a performer URL on that site when present (`pornpics.com/pornstars/<slug>`,
`elitebabes.com/model/<slug>`), otherwise from the name. Every set is **validated**: the performer's
name or an alias must be among the set's models, else it's recorded as `rejected`. Sites credit
short names ("Abella", "Lexi") — add them as aliases in Stash to accept those sets, or add the site
URL to the performer to pin the right model page (EliteBabes slugs like `ella-7` for name clashes).

Each source runs politely: `request_delay` (default 2 s, ±30 % jitter) per host, retries with backoff on
429/5xx, and a Cloudflare challenge or persistent 429 disables that source for the rest of the run.

## Dedupe

Index: SQLite `/config/performer-sets.db` (host `/mnt/fastpool/system/extras/stash/config/`).

1. **Source URL**: each set/promo URL gets a status — `done`, `duplicate`, `rejected`, `small` (final,
   never fetched again) or `partial` (resumed next run; existing `NNN.*` files are kept).
2. **Same set from another source**: the first 3 images are fetched and compared; if ≥ 2/3 already
   belong to one existing set, the new URL is appended to that set's `set.json` `urls` (and the
   gallery's URLs) and nothing is downloaded.
3. **Per image**: sha256 (exact copy) and a 64-bit dHash with the **same aspect ratio** (±3 %) within
   `dedupe_distance` bits (default 4). Within one set only exact copies are skipped, because
   consecutive shots of a set are near-identical by nature. Crops never match (aspect check).
4. `min_image_size` (default 800 px long edge) drops thumbnails before download when the page states
   sizes, and after download otherwise.

**Rebuild dedupe index** re-hashes everything on disk and marks every URL in a `set.json` as done;
`rejected`/`small` statuses are lost, so those set pages are fetched once more.

## Gallery metadata

Stash runs jobs **one at a time** (`pkg/job/manager.go`), so the download task cannot wait for the
scan it queues. Instead the `Gallery.Create.Post` hook applies `set.json` the moment the scan creates
a gallery under the download folder; **Apply set metadata** does the same for all of them (backstop,
and after fixing studios/aliases).

Applied: `title`, `studio` (only when the gallery has none), `urls` (merged), performers (folder
performer + `other_performers` found in Stash, union with existing), and with `map_source_tags` the
source categories that already exist as tags. Manual edits are never overwritten. Lookups are exact
name/alias first, then a Stash text search compared ignoring case/accents/punctuation (`X Art` →
`X-Art`); ambiguous = not found. Unresolved studios/performers add the tag
**`Performer Sets: needs review`** (created on first need); it is removed again once they resolve.
`date` is never set from current sources (none has a release date).

## Settings and tasks

Settings (Settings → Plugins): `dry_run` (default **on**), `download_root` (default
`/data/PerformerSets`), `include_favorites`, `min_rating`, `tags`, `sources`, `promo_images`,
`max_sets_per_source` (default 50 new sets per performer per source per run; 0 = unlimited),
`min_image_size`, `request_delay`, `dedupe_distance`, `map_source_tags`, `enable_hook`.

Tasks: **Dry run: selected performers** (always dry; listing pages only, no set pages or images;
counts are candidates before model validation), **Download sets for selected performers**,
**Apply set metadata**, **Rebuild dedupe index**. The report goes to `/config/performer-sets-report.txt`.

## Rollout

1. `requirements.txt` adds Pillow; stash-s6's `/opt/entrypoint.sh` collects every plugin's
   `requirements.txt` into `/config/requirements.txt` and installs it at container start
   (`INSTALL_PY_DEPS=true`), so restart the stash container after the first deploy.
2. Create `/mnt/leftpool/extras/PerformerSets` (owner `nas_extras`, like the other libraries) and add
   `/data/PerformerSets` in Stash as a library with **Exclude videos** on.
3. Deploy: `scripts/deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/extras/stash/config/plugins/performer-sets`,
   restart stash, Settings → Plugins → Reload; set the selection.
4. Run the dry run, check the report; turn `dry_run` off and run **Download sets for selected performers**.
5. Remove the old plugins and libraries (decided 2026-10-23): plugin dirs `performerGallery`
   (also holds stray zips) and `babepediaGalleryScraper`; libraries `/data/Galleries-Porngals`,
   `/data/Galleries-Babepedia` and `/data/Performers` (and the leftover `stashdb-performer-gallery`
   settings), then run Clean in Stash.

## Tests

`tests/` (unittest; needs Pillow, requests, bs4). The stash container has no Pillow until the restart,
so run them in a throwaway container: copy the plugin dir to `/tmp/ps-test` on the NAS and
`docker run --rm -v /tmp/ps-test:/p:ro -w /p python:3.13-slim sh -c "pip install -q Pillow requests beautifulsoup4; python -m unittest discover -s tests"`.
Live: `performer_sets.py --standalone --url http://192.168.1.110:9999 --mode dry_run|download|apply|reindex
[--live] [--performer ID] [--set key=value …] [--no-scan]` with `STASH_API_KEY` in the environment
(from `.tokens`, piped via stdin rather than on a command line). Verified 2026-10-23: unit tests,
both parsers against live pages, selection/lookups against live Stash, and a live download of 3 sets +
38 promo images for one performer into a scratch folder (outside Stash; the scan/hook path is not yet
exercised for real).
