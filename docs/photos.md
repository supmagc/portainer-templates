# Photos: library layout and import flow

Plan (2026-10-09, nothing built yet) for replacing the Emby pictures library with Immich
while keeping the digiKam/Darktable (and old Lightroom) workflow. Storage and backup policy:
[nas-storage.md](nas-storage.md).

## What exists

| Path | Content |
|---|---|
| `pictures/import` (381G) | Original camera files: the Lightroom projects/catalogue and the digiKam import library; edits done in Darktable |
| `pictures/export` (105G) | Old Lightroom and digiKam *exports* — derived |
| `phones` (134G, rightpool only; leftpool duplicate merged in and removed 2026-10-09) | Phone uploads, probably via Syncthing (unverified) |
| MariaDB `digikam` | digiKam catalogue (already dumped nightly) |

Immich's Postgres image is already the shared `postgres` container
(`docker-compose-utilities.yml:103`, data on `fastpool`); no Immich service exists yet.
How the old `import`/`export` trees map onto the new layout is undecided (see Open).

## Target layout

```
rightpool/pictures                 one dataset: moves between its folders are renames
├── CameraImport                   RAW imports by date: YYYY/YYYY-MM-DD/   (inbox)
└── CameraAlbums                   RAW, moved into logical albums by digiKam
rightpool/pictures-exported        separate dataset, regenerable
└── CameraAlbumsExported           clean JPEGs, same relative path as CameraAlbums
rightpool/pictures-immich          Immich UPLOAD_LOCATION (phone uploads)
```

- **One dataset for Import + Albums.** As separate datasets, `mv` between them is a copy plus
  delete, and snapshots keep the deleted copy until retention expires. Subfolders of one
  dataset make the move a rename.
- **Exports are a separate dataset** because they are derived: snapshots at most; skip the
  replica and B2 (see [nas-storage.md](nas-storage.md)). The RAWs and their XMP are the
  critical data.

## Core rules

1. **Immich identifies an external asset by path.** A moved file is a *new* asset and loses its
   Immich-only data (favourites, Immich albums, stacks). So Immich must only index folders
   whose files don't move, and exported paths must stay stable.
2. **digiKam is the system of record** for rating, tags, geo, faces, description. Settings:
   write metadata to **XMP sidecars only** (RAW untouched), enable **Face Tags (including face
   areas)**; Tools → Maintenance → "Sync metadata and database" exports it. Sidecars are named
   `<file>.<ext>.xmp` — Immich's preferred name.
3. **Immich libraries are read-only (`:ro`).** Otherwise Immich writes rating/tag/description
   edits back into the XMP (merged into the existing file) and fights digiKam. Consequence:
   curate in digiKam, not in Immich.
4. **Darktable always does the export** (`darktable-cli`), including for images never edited.

## What Immich indexes

| Folder | Immich library | Role |
|---|---|---|
| `CameraImport` | "Inbox" (ro) | New RAWs visible right after import (embedded preview). Files leave it when sorted into albums and go to Immich's trash at the next scan (purged after 30 days) — harmless, nothing permanent attaches to them |
| `CameraAlbums` | **not indexed** | Where the moves happen |
| `CameraAlbumsExported` | "Photos" (ro) | The permanent library; people, albums, favourites attach here |

Both libraries belong to my account (an external library has a single owner); others get
access through album/partner sharing. Immich scans nightly by default; the DISCOVER (new
`.xmp`) and SYNC (re-read changed `.xmp`) library jobs can be triggered after a curation
session or after the exporter runs.

## Flow

```
1 Import   camera card ─► digiKam ─► CameraImport/YYYY/YYYY-MM-DD/     Immich "Inbox": RAW visible
2 Curate   digiKam: pick, rate, tag, geo, faces; move into CameraAlbums/<album>/
           writes <file>.<ext>.xmp
3 Develop  Darktable on chosen RAWs (history stays in the sidecar / its DB)
4 Export   Dagu job: CameraAlbums ─► CameraAlbumsExported (same relative path)
5 View     Immich "Photos": JPEGs with rating, tags, GPS, faces from the XMP
```

A RAW sitting in `CameraAlbums` is invisible in Immich until exported, so the exporter must
run soon after curation (hourly/nightly) with the default develop; later Darktable edits just
overwrite the same JPEG.

## The exporter (Dagu job, not written)

Per RAW in `CameraAlbums`:
1. Skip if the JPEG exists and is newer than both the RAW and its `.xmp`.
2. `darktable-cli <RAW> <RAW.xmp> <out.jpg> --core --conf write_sidecar_files=never` — applies
   the edit history in the sidecar and never rewrites it. (Flag names unverified.)
3. `exiftool -tagsFromFile <RAW.xmp> …` to copy digiKam's tags, rating, GPS and MWG face
   regions into the JPEG, because `darktable-cli`'s own metadata carry-over was not verified.
   The exact tag list needs testing.
4. After the run, trigger an Immich library scan via its API (endpoint unverified).

Open implementation points: an image with `darktable-cli` + `exiftool` (probably custom or
installed at run time — see the Dagu `docker.run` quirks in [dagu.md](dagu.md)), a default
style so un-edited RAWs don't look flat, CPU-only throttling next to the shared GPU
([gpu.md](gpu.md)).

## Carrying digiKam data into Immich

| Data | Path into Immich |
|---|---|
| Rating | `xmp:Rating` |
| Tags | `digiKam:TagsList`, then `lr:HierarchicalSubject`, then `IPTC:Keywords` |
| Geo | `exif:GPSLatitude` / `GPSLongitude` |
| Date, description | `exif:DateTimeOriginal` etc., `dc:description` |
| Faces | Immich setting Administration → Settings → Metadata → **"Import faces from metadata"** (reads digiKam/Lightroom MWG regions) |

Risks to test before trusting it:
- **Cropped/rotated Darktable edits**: MWG regions are relative to the original image, so the
  boxes land in the wrong place on the exported JPEG.
- **Immich issue #31629**: updating metadata on external assets removes faces imported from
  digiKam/MWG; check it on the version in use.
- **Fallback**: let Immich recognise faces on the exports and name people once; names persist
  in its database.

## Known conflicts and gaps

- **digiKam and Darktable both want `<file>.<ext>.xmp`.** Test on copies whether Darktable's GUI
  overwrites digiKam's tags/rating and whether digiKam preserves Darktable's history. Darktable
  can read tags, ratings and GPS from other apps' sidecars at import.
- **Old Lightroom sidecars** are named `IMG_0001.xmp` — Immich accepts it as a fallback, but a
  `<file>.<ext>.xmp` takes precedence if both exist.
- **Renaming an album folder** re-creates its exports at a new path = new Immich assets
  (Immich-only data lost; XMP-derived data returns). Rename rarely.
- **Immich re-reading an overwritten JPEG** (changed content, same path): believed to follow
  modification time, unverified.
- RAW formats: Immich shows the embedded preview; confirm the cameras' files render.

## Phones and household

- **One uploader per phone.** Immich auto-upload *or* Syncthing/Nextcloud/PhotoSync for
  photos, not several — otherwise duplicates. Nextcloud stays for documents/files.
- My phone → Immich mobile app → `pictures-immich` (writable library, my account).
- Household members: their own Immich accounts or Nextcloud; not merged into my library.
- Old `phones/` archive: frozen; index once as a read-only library if wanted.

## Backup

- `CameraImport` + `CameraAlbums` (with their sidecars) and `pictures-immich` are Critical:
  snapshots, replica, B2 per [nas-storage.md](nas-storage.md). `CameraAlbumsExported` is
  regenerable.
- Immich-only data (favourites, albums, stacks, person names) is in its **database**. Immich
  writes its own DB dumps to `UPLOAD_LOCATION/backups` (default: daily 02:00, keep 14) —
  **metadata only**; originals are `library/`, `upload/`, `profile/`. Add the `immich` database
  to the `postgres-backup` Dagu DAG so it lands in `backup/automated`.
- digiKam's DB is dumped nightly already. Darktable's library DB (if sidecars are not the only
  store) is **not** covered yet.

## Steps

1. Test on copies: digiKam + Darktable XMP interplay, the exiftool carry-over, face import
   with a cropped and an uncropped export.
2. Create the datasets, decide what happens to the old `import`/`export`
   ([nas-storage.md](nas-storage.md) migration step 3) and what writes `phones`.
3. Add an `immich` compose stack (server + machine-learning, DB = shared `postgres`, create
   database and extensions first). Pin versions; verify image tags before writing them.
4. Two read-only libraries (`CameraImport`, `CameraAlbumsExported`), enable "Import faces from
   metadata", my phone auto-upload on, first scan.
5. Write the exporter Dagu job; add `immich` to `postgres-backup`.
6. Remove the pictures/phones library from Emby/Jellyfin once Immich is the viewer.
7. GPU: Immich ML competes with Jellyfin NVENC — read [gpu.md](gpu.md) first.

## Open

- Old `import` (381G, Lightroom projects) and `export` (105G): treat as already-processed
  albums, or keep as a legacy library outside the new flow?
- Index `phones` as a library, or just archive it?
- Do other household members want Immich accounts, or only Nextcloud uploads?
- Where Darktable's library DB/config lives and whether it is backed up.
