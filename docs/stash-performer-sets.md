# Stash: Performer Sets plugin

A custom Stash raw plugin (`performer-sets`, Python) that downloads photosets (PornPics, EliteBabes,
Babepedia) and promo images (stash-boxes, Babepedia) for
selected performers, one Stash gallery per set, deduplicated across sources. Source:
`hosts/nas/mnt/fastpool/system/extras/stash/config/plugins/performer-sets/` (deploys to the Stash
`/config/plugins/performer-sets/`). It replaces the `performerGallery` (porngals4, per-performer zips),
`babepediaGalleryScraper` (per-performer zips) and `stashdb-performer-gallery` (`/data/Performers`) plugins.

## Layout

```text
/data/PerformerSets/                                 Stash library, images only
  Abella Danger [6]/                                 "[<performer id>]" is the key; the name is only set on creation
    _promo/                                          every promo source → one gallery "<name> - Promo"
      theporndb-<image id>.jpg, fansdb-<image id>.jpg, babepedia-pics-<file>.jpg, babepedia-upload-<file>.jpg …, set.json
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
| Babepedia (verified 2026-10-26) | model page `/babe/<Name_With_Underscores>` lists **every** gallery, newest first (the oldest has no "older" link); an unknown name redirects to `/search/…` | `h1` (descriptive), `h2.featuring` = models, "Source:" = studio, scene tags, "Published on" | `/galleries/<dir>/NN.jpg`, ~1200×1800; no sizes in the page | release date: "Published on" is Babepedia's date, stored as `posted` only |
| stash-box (promo) | the performer's `stash_ids` × configured stash-boxes (`findPerformer { images }`) | — | TPDB/FansDB posters; many are < 800 px | — |
| Babepedia (promo) | the same model page: `/pics/<Name>N.jpg` profile pics (~4, ~700×1000) and `/user-uploads/<Name>N.jpg` (~20, often < 800 px) | — | sizes only known after download (a `small` one is never fetched again) | — |

Promo sources (`promo_sources`) all write into the one `_promo` folder: file stems carry the source
and every image URL has its own index status, so new images are added on each run, and a photo already
there from another source is a `duplicate` (dHash over the whole index). A Babepedia profile pic that is
replaced under the same URL is not re-fetched.

The search/model slug comes from a performer URL on that site when present (`pornpics.com/pornstars/<slug>`,
`elitebabes.com/model/<slug>`, `babepedia.com/babe/<slug>`), otherwise from the name. Babepedia promo images
are not validated (a model page has no "models" list), so pin the URL for name clashes. Every set is **validated**: the performer's
name or an alias must be among the set's models, else it's recorded as `rejected`. Sites credit
short names ("Abella", "Lexi") — add them as aliases in Stash to accept those sets, or add the site
URL to the performer to pin the right model page (EliteBabes slugs like `ella-7` for name clashes).

Each source runs politely: `request_delay` (default 2 s, ±30 % jitter) per host, retries with backoff on
429/5xx, and a Cloudflare challenge or persistent 429 disables that source for the rest of the run.
Images from hosts that serve no pages (`cdni.pornpics.com`, `cdn.elitebabes.com`, stash-box CDNs) use a
fixed 1 s instead (`IMAGE_DELAY` in `ps/fetch.py`, never above `request_delay`); Babepedia serves its
images from the page host, so they keep `request_delay`. 2026-10-31: with `request_delay` 7 a run spent ~97 % of its time sleeping (8 s median
per set page, ~5 min per 40-image set) — the delay, not scoring or parsing (0.4 s fetch+parse), is the cost.

## Budget and quality (v0.3/v0.4)

The first runs (v0.1/0.2) took every set they found: 2026-10-26 the library held 4776 PornPics sets
(87k images, 12.5 GB, ~68 sets per performer, ~18 images per set at ~146 KB). Image size was not the
problem — 85 % of images are ≥ 1200 px and already heavily compressed, so re-encoding (WebP) would save
little at a visible quality cost — the number of sets was. Zipping sets was rejected too: JPEGs don't
compress, and the dedupe index, resume, Curate and Stash browsing all work on loose files. Since v0.3:

1. **Survey**: for each source, the set pages of up to `max_sets_per_source` new listing entries are
   fetched, validated (models, `min_image_size`) and stored as **candidates** in the index — no images.
   A listing walked to the end is skipped for `recheck_days` (default 7), so reruns are cheap.
2. **Fill** (v0.5: replaces the separate download-then-Curate cycle): a performer has a budget of
   `max_sets_per_performer` sets (default 50, all sources together). Downloaded sets and candidates
   are ranked together; the best candidate inside the budget is downloaded, and only **after** it is on
   disk does the lowest downloaded set fall out of the budget and get **evicted** (folder deleted, URLs
   `evicted`, fingerprints kept — see Curate). Repeat until no candidate makes the budget. Downloaded
   sets that are excluded, rated out or over a lowered budget are evicted up front. Before a full
   download the 3 sample images decide (v0.4):
   - same set as one on disk → `duplicate`, unless this copy is clearly better (≥ 20 % more resolution
     and ≥ 80 % of the images, or ≥ 50 % more images at ≥ 90 % of the resolution): then it **replaces**
     the old copy (`UPGRADE`; a Clean is queued). Rated/organized galleries are never replaced.
   - median long edge < `min_image_size` (default 800) or sharpness below the soft threshold →
     `low-quality` (final).
   A duplicate or low-quality candidate evicts nothing, so a set is never deleted for a replacement
   that doesn't materialise (the flaw of the pre-v0.5 Curate-first order). A freshly downloaded set is
   re-scored with measured values and can itself be the one that falls out (PornPics candidates have
   no known resolution until downloaded). A Stash Clean is queued after evictions/upgrades. Download and
   Curate end by removing every completely empty folder under the root (`PRUNED`; e.g. a `_promo` whose
   images all turned out duplicate/small); a folder with only `set.json` is a resumable partial and stays.
3. **Curate** (task, maintenance): the same budget applied to downloaded sets **only** (no candidates),
   across every performer folder, selected or not — evicts excluded, rated-out and over-budget sets,
   measures sharpness of pre-v0.4 images, re-applies `set.json`. An evicted set's image fingerprints stay in
   the index, so the same set from another source is a duplicate, not a new download. Needed after
   changing scoring settings for performers no longer selected; otherwise Download does it all.

Score (higher is better; `ps/curate.py`): **models** solo +3, otherwise
min(4, |M − F| / 2 + (F < M ? 1 : 0) + (F + M − 2) / 2), −1 when F = 0, where F = credited models whose
Stash gender is Female and M = everyone else (male, trans, no gender, not in Stash — 23 % of co-model
credits on 2026-10-31); e.g. FM 0, FF 1, MM 1, FFM 1, MMF 2, FFF 2, FFMM 1, FMMM 3, FFFF 3, FMMMM 4,
FFFFF 4, 1F + 10M 4 (groups can top solo by 1: exclude with `tag_weights` if unwanted);
genders come from one all-performers query per run; image
count +2 at `ideal_set_size` (default 40), −2 per halving or doubling away from it, floor −3 (40 → +2,
20/80 → 0, 10/160 → −2; 0 = off); median long edge ≥ 2000 px +3, ≥ 1600 +2, ≥ 1200 0, ≥ 1000 −1, below −3
(when known: measured for downloaded sets, from the page for candidates except PornPics, whose stated
sizes are wrong); soft −4; the **Stash studio** (looked up by name/alias) hearted +4 and rating
(stars − 3), a hearted/rated **parent studio** counts for its sub-studios; tag weights (below); an
already downloaded set +0.5 (no churn on ties). `exclude_studios` removes a set entirely. At most
`max_sets_per_studio` (default 5) sets per studio are picked first; the rest only fill slots nothing
else wants.

**Tag weights** (`tag_weights`): `Lingerie=3, Outdoor, Hardcore=-3, Gangbang=x` — no weight = 2,
`x` = exclude, weights add up per set and are capped at ±6. An entry matches a source category of
the same name, or a **Stash tag** that is the entry's tag or one of its sub-tags: the category is
resolved by name/alias (so synonyms are Stash aliases, e.g. "Stockings" with alias "Nylons" under a
parent "Lingerie"), and for downloaded sets the gallery's own tags count too. **Hearted Stash tags** are
implicit entries: each one a set matches (itself or a sub-tag) adds `favorite_tag_weight` (default 2,
0 = off), inside the same ±6 cap; a tag named in `tag_weights` uses that weight instead.

**Soft sets**: every image gets a focus measure (`imaging.sharpness`: Laplacian variance at ≤ 1024 px,
75th percentile of a 4×4 tile grid, so a sharp subject on a blurred background is sharp; real photos
measured 110–360, the same blurred or 3× upscaled 17–82). It depends on content, so the threshold is
relative: a set is **soft** when its median is below the `blur_percentile` (default 10th) of all set
medians in the library (needs ≥ 50 measured sets). Curate measures images indexed before v0.4 once
(~15 ms each, plus reading the file).

The measured quality (long edge, sharpness, soft) is stored in `set.json` `quality` (at download and by
Curate); the v0.4 quality tags (`Performer Sets: Solo` / `Duo` / `Group` / `High-res` / `Soft`) were
removed in v0.5 — delete those tags in Stash if they exist. No gallery needs reviewing — ratings are
optional overrides.

Gallery ratings (optional) feed back: a gallery rated at most `evict_rating` stars (default 1) is evicted by
Curate and never fetched again; a gallery rated higher, or marked organized, is never evicted (and
counts against the budget).

## Dedupe

Index: SQLite `/config/performer-sets.db` (host `/mnt/fastpool/system/extras/stash/config/`).

1. **Source URL**: each set/promo URL gets a status — `done`, `duplicate`, `rejected`, `small`,
   `evicted`, `low-quality` (final, never fetched again) or `partial` (resumed next run; existing `NNN.*` files are kept).
2. **Same set from another source**: the first 3 images are fetched and compared; if ≥ 2/3 already
   belong to one existing set, the new URL is appended to that set's `set.json` `urls` (and the
   gallery's URLs) and nothing is downloaded.
3. **Per image**: sha256 (exact copy) and a 64-bit dHash with the **same aspect ratio** (±3 %) within
   `dedupe_distance` bits (default 4). Within one set only exact copies are skipped, because
   consecutive shots of a set are near-identical by nature. Crops never match (aspect check).
4. `min_image_size` (default 800 px long edge) drops thumbnails before download when the page states
   sizes, and after download otherwise; the same limit applies to a new set's sample median (above).

**Rebuild dedupe index** re-hashes everything on disk and marks every URL in a `set.json` as done;
`rejected`/`small` statuses are lost, so those set pages are fetched once more. Evictions (URLs and
fingerprints) and the candidates survive a rebuild.

## Gallery metadata

Stash runs jobs **one at a time** (`pkg/job/manager.go`), so the download task cannot wait for the
scan it queues. Instead the `Gallery.Create.Post` hook applies `set.json` the moment the scan creates
a gallery under the download folder; **Apply set metadata** does the same for all of them (backstop,
and after fixing studios/aliases).

Applied: `title`, `studio` (only when the gallery has none), `urls` (merged), performers (folder
performer + `other_performers` found in Stash, union with existing). Source categories are kept in
`set.json` `source_tags` for scoring only (`map_source_tags` was removed in v0.5). Manual edits are never
overwritten. Lookups are exact
name/alias first, then a Stash text search compared ignoring case/accents/punctuation (`X Art` →
`X-Art`); ambiguous = not found. Unresolved studios/performers add the tag
**`Performer Sets: needs review`** (created on first need); it is removed again once they resolve.
`date` is never set from current sources (none has a release date).

## Settings and tasks

Settings (Settings → Plugins): `dry_run` (default **on**), `download_root` (default
`/data/PerformerSets`), `include_favorites`, `min_rating`, `tags`, `sources`, `promo_images`, `promo_sources`,
`max_sets_per_performer`, `max_sets_per_studio`, `exclude_studios`, `tag_weights`, `favorite_tag_weight`, `ideal_set_size`, `evict_rating`,
`blur_percentile`, `max_sets_per_source` (set pages surveyed per performer per source
per run, default 50; 0 = unlimited), `recheck_days`, `min_image_size`, `request_delay`,
`dedupe_distance`, `enable_hook`.

Tasks: **Dry run: selected performers** (always dry; surveys set pages into the index like a download
does, then reports `WOULD-GET` per candidate that makes the budget and `EVICT` per set it would push
out, assuming every candidate downloads fine — no images), **Download sets for selected performers**
(the whole survey → rank → download → evict loop; rerun it to keep improving the sets),
**Curate downloaded sets** (maintenance, see above; honours `dry_run`: dry = `EVICT`/`CURATE`/`FREED`
report only; measuring sharpness into the index happens either way),
**Apply set metadata**, **Rebuild dedupe index**. The report goes to `/config/performer-sets-report.txt`.

**Session expiry**: a task talks to Stash with the session cookie Stash hands the plugin, which expires
after `max_session_age` (default 1 h) without a request — the first Curate's hour-long sharpness pass
hit a 401 on its next query. `ps/stash.py` therefore carries the cookie Stash re-issues on every
response and the long loops call `keepalive()` (a cheap query when idle > 10 min).

## Rollout

1. `requirements.txt` adds Pillow; stash-s6's `/opt/entrypoint.sh` collects every plugin's
   `requirements.txt` into `/config/requirements.txt` and installs it at container start
   (`INSTALL_PY_DEPS=true`), so restart the stash container after the first deploy.
2. Create `/mnt/leftpool/extras/PerformerSets` (owner `nas_extras`, like the other libraries) and add
   `/data/PerformerSets` in Stash as a library with **Exclude videos** on.
3. Deploy: `scripts/deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/extras/stash/config/plugins/performer-sets`,
   restart stash, Settings → Plugins → Reload; set the selection.
4. Run the dry run, check the report; turn `dry_run` off and run **Download sets for selected performers**.
5. **v0.3/v0.4 migration** (library already over budget): deploy, Reload plugins, heart favourite
   studios, set `tag_weights` / `exclude_studios` if wanted (no gallery review needed). With `dry_run`
   **on**, run **Curate downloaded sets** — the first run measures sharpness of all ~87k images
   (expect the better part of an hour) — and read the `SOFT-BELOW`/`EVICT`/`CURATE`/`FREED` lines;
   then `dry_run` off and run it again: it deletes the evicted folders, writes quality into set.json,
   tags the galleries and queues the Clean. From v0.5 on, Download alone keeps improving the sets.
6. Remove the old plugins and libraries (decided 2026-10-23): plugin dirs `performerGallery`
   (also holds stray zips) and `babepediaGalleryScraper`; libraries `/data/Galleries-Porngals`,
   `/data/Galleries-Babepedia` and `/data/Performers` (and the leftover `stashdb-performer-gallery`
   settings), then run Clean in Stash.

## Tests

`tests/` (unittest; needs Pillow, requests, bs4). The stash container has no Pillow until the restart,
so run them in a throwaway container: copy the plugin dir to `/tmp/ps-test` on the NAS and
`docker run --rm -v /tmp/ps-test:/p:ro -w /p python:3.13-slim sh -c "pip install -q Pillow requests beautifulsoup4; python -m unittest discover -s tests"`.
Live: `performer_sets.py --standalone --url http://192.168.1.110:9999 --mode dry_run|download|apply|curate|reindex
[--live] [--performer ID] [--set key=value …] [--no-scan]` with `STASH_API_KEY` in the environment
(from `.tokens`, piped via stdin rather than on a command line). Verified 2026-10-23: unit tests,
both parsers against live pages, selection/lookups against live Stash, and a live download of 3 sets +
38 promo images for one performer into a scratch folder (outside Stash; the scan/hook path is not yet
exercised for real).
