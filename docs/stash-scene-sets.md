# Stash: Scene Sets plugin

A custom Stash raw plugin (`scene-sets`, Python) that downloads the **photo set of selected scenes** from their
source site — the members' set for Kink when a session cookie is configured, else the public photos — into one
gallery folder per scene, and links that gallery to the scene. Source:
`hosts/nas/mnt/fastpool/system/extras/stash/config/plugins/scene-sets/` (deploys to the Stash
`/config/plugins/scene-sets/`). Sibling of [performer-sets](stash-performer-sets.md); it copies that plugin's
Stash client (session-cookie keepalive), polite fetcher and ownership-preserving writes rather than importing them,
like `library-organizer` does.

## Layout

```text
/data/SceneSets/                                         Stash library, images only (host /mnt/leftpool/extras/SceneSets)
  <Studio>/
    <Studio> - <date> - <title> [<site>-<id>]/           one folder = one scene's set = one folder-based gallery
      001.jpg … 015.jpg, set.json
```

- `[<site>-<id>]` is the key (`kink-35455`, `kinkmen-123`, `deeper-101342`): a folder renamed or made by hand is
  still found. The name comes from the Stash scene (studio, date, title), so it matches the `/data/Named` video.
- **Hand-made sets** (the 10 Kink member sets moved in 2026-10-30: `set.json` without an `images` map) are never
  touched: their scene is recorded as `exists` without fetching anything.
- `set.json` (`kind: "scene"`, so performer-sets' Curate never sees these): `source`, `site`, `source_id`, `urls`,
  `title`, `studio`, `date`, `scene_id`, `performers`, `director`, `details` (all from the Stash scene),
  `source_tags`, `source_image_count`, VMG `pictures_in_set` (size of the members' set), `member` (Kink members'
  set), `archives` (the zips it came from), `images` (file stem → source URL without the signature, or
  `<zip>/<entry>`).

## Selection

A scene is processed when **either** criterion matches (union): rating ≥ `min_rating` (stars, half stars allowed;
off when empty) or **a favorite performer** in the scene (`include_favorite_performers`, default on). It also needs
a URL a source recognises, and is skipped when it already has a gallery (any gallery: the photos are there already).
2026-10-30: 256 scenes with a favorite performer (166 on kink.com), 85 rated ≥ 4★.

## Sources (verified 2026-10-30)

| Source | Scene URL | How | Photos | Not available |
| --- | --- | --- | --- | --- |
| `kink` | `kink.com` / `kinkmen.com` / `kinktrans.com` `/shoot/<id>` (only kink.com tested) | Page is behind Cloudflare: loaded in Stash's **chromedp** (Stash's `scraperCDPPath`, or `cdp_url`) with the `age_gate_accepted` cookie — what the community Kink scraper does with `useCDP: true` | `#galleryImagesContainer[data-images]` JSON, ~15 public photos; `fullPath` `cdnp.kink.com/imagedb/<id>/i/h/830/<n>.jpg` is really 1400×934; the CDN answers plain requests; other size presets 404 | the members' set (~250 photos); a removed shoot (e.g. 36120) redirects to `/channels` → `gone` |
| `vixen` | `<site>.com/videos/<slug>` for vixen, blacked, blackedraw, tushy, tushyraw, deeper, slayed, milfy, wifey | Public GraphQL `https://www.<site>.com/graphql` `findOneVideo` (what the vixenNetwork scraper uses; introspection is off) | `carousel[].main[]`: 8 posed tour photos, 1200×800, CloudFront-signed URLs (plain requests work) | the members' set (`picturesInSet`, e.g. 89) |

### Kink members' sets (verified 2026-10-30 with a subscription)

With `kink_cookie` set (the `kinky.sess` cookie of a browser logged in to kink.com; ~30-day lifetime), the shoot
page is loaded with that session and also lists `a.download-zip-images-file`: signed, short-lived
`cdn.kink.com/…zip?nva=…&token=…` links (the dropdown is rendered twice; deduplicated). What they hold:

| Era (shoot tested) | Zips | Kept |
| --- | --- | --- |
| 2013 (29616) | `publicdisgrace_29616_hi_res_pictures_1 Glamour.zip` + `…_Scene 1/2/3.zip` | all four: Glamour = 32 posed photos, 1200×800 (public: 15), then the `Scene N` video frame grabs — 249 images, 53 MB (= the old hand-downloaded `Pics 29616`) |
| 2014–2020 (35455, 45609) | `<id>_images.zip` | the same 15 photos as the public gallery (1400×934); `performer-N.jpg` headshots skipped |
| 2026 (108174) | `v2/members/shoots/<id>/private/zipfile/<id>_images.zip` | 18 PNGs at 3840×2160 (~2 MB each, 37 MB per set) |

Rule (user's choice, 2026-10-30): a shoot with **several zips** (more than one page of images) gets all of them,
the photo set first; a single zip is used as is, except a lone `Scene N` frame-grab zip (then the public photos).
Logged in = the page has a logout link; a rejected cookie logs `NOT-LOGGED-IN` once and the run continues with
public photos. Every page load runs in its **own throwaway browser context** (`Target.createBrowserContext`),
cookies set only there: a plain `Network.setCookie` lands in chromedp's shared profile, which Stash's scrapers use
— that leaked the session there once during testing (cleared with `Storage.clearCookies`). Signed URLs are
redacted (`?…`) in the log/report and never stored in set.json.

Statuses in the index (`/config/scene-sets.db`, per canonical source URL): `done`, `exists` (hand-made set),
`gone`, `empty` (no public photos), `small` (none ≥ `min_image_size`) are final; `partial` resumes on the next
run (existing `NNN.*` files are kept); `preview` = a kink.com set from the public photos — final while no
`kink_cookie` is set, otherwise replaced by the members' set on the next run (its photos are deleted first).
Delete the database to re-check everything.

## Gallery metadata

Like performer-sets: the `Gallery.Create.Post` hook applies `set.json` when the scan creates a gallery under the
download folder; **Apply set metadata** does it for all of them (also links the hand-made sets to their scenes).
Applied: the **scene link** (`scene_id`), `title`/`date`/`details` and `studio` when the gallery has none, `urls`
(merged), performers (union). Manual edits are never overwritten. Unresolved scene/studio/performers add the tag
**`Scene Sets: needs review`**, removed again once they resolve.

## Settings and tasks

Settings: `dry_run` (default **on**), `download_root` (default `/data/SceneSets`), `min_rating`,
`include_favorite_performers`, `sources` (`kink`, `vixen`), `min_image_size` (800), `request_delay` (2 s per host,
±30 % jitter; a Kink page load also waits for it), `cdp_url`, `kink_cookie` (lives in Stash's config, not the
repo; the local copy for tests is `KINK_COOKIE` in `.tokens`), `enable_hook`.

Tasks: **Dry run: selected scenes** (always dry; fetches each new set page, reports `WOULD-GET`/`GONE`/`NO-PHOTOS`,
records the final statuses), **Download sets for selected scenes** (then queues a scan of the touched studio
folders), **Apply set metadata**. The report goes to `/config/scene-sets-report.txt`.

## Rollout

1. `requirements.txt` adds `websocket-client` (CDP) next to Pillow; stash-s6 installs every plugin's requirements at
   container start, so **restart the stash container** after the first deploy.
2. Add `/data/SceneSets` in Stash as a library with **Exclude videos** on (the folder exists since 2026-10-30).
3. Deploy: `scripts/deploy.ps1 -DeployHost nas -DeployPath mnt/fastpool/system/extras/stash/config/plugins/scene-sets`,
   restart stash, Settings → Plugins → Reload; set `min_rating` and `kink_cookie` (renew it when the report says
   `NOT-LOGGED-IN`).
4. Scan `/data/SceneSets`, run **Apply set metadata** (links the hand-made sets), then the dry run; read the report;
   turn `dry_run` off and run **Download sets for selected scenes**.

## Tests

`tests/` (unittest; needs Pillow, requests, websocket-client). Run in a throwaway container: copy the plugin dir to
`/tmp/ss-test` on the NAS and
`docker run --rm -v /tmp/ss-test:/p:ro -w /p python:3.13-slim sh -c "pip install -q -r requirements.txt requests; python -B -m unittest discover -s tests"`.
Live: the same container with `--network utilities_default` (reaches `chromedp` and `stash`) and
`scene_sets.py --standalone --url http://stash:9999 --mode dry_run|download|apply [--live] [--scene ID] [--set key=value …] [--no-scan]`,
`STASH_API_KEY` from `.tokens` piped via stdin (the Kink cookie too: never on a command line). Verified
2026-10-30: 18 unit tests; live dry runs (Kink 35455 → 15 photos, Kink 36120 → gone, Deeper "Anger" → 8 photos);
live public downloads of 35455 + Anger and live members' downloads of 29616 (32), 108174 (18 PNG) and 35455 (15)
into a scratch folder (outside Stash; the scan/hook path is not yet exercised for real).
