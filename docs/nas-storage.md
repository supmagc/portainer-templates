# NAS storage and protection strategy

Which ZFS pool owns which data, where each critical dataset is replicated, and what goes to
Backblaze B2. This is the **target** layout agreed 2026-10-08; the migration state is tracked
at the bottom. Per-app backup coverage lives in [backups.md](backups.md); the photo flow in
[photos.md](photos.md).

## Principle

Every critical dataset has one **home pool** and a **replica on the other pool**, plus daily
snapshots and an offsite B2 copy taken from the *source* (not the replica).

| Layer | Protects against | Not enough alone because |
|---|---|---|
| Snapshots (same pool) | Deletion, ransomware, bad edits | Die with the pool |
| Replica on the other pool | One pool failing, quick local restore | Same box, same site |
| B2 (Cloud Sync) | Site loss | Sync mode propagates deletions unless the bucket keeps versions |

**Pool roles (decided 2026-10-18):** `rightpool` holds all primary data; `leftpool` holds only
`extras` and the `replica/*` datasets. Nothing else is planned for `leftpool`.

`fastpool` is live app state and is never the only copy: its backups land on `rightpool`.
Media that can be re-downloaded (movies, series, downloads, caches) gets none of this.

## Target layout

| Dataset | Home | Replica | Snapshots | B2 |
|---|---|---|---|---|
| `rightpool/backup/automated` (app + DB dumps, config harvest) | right | none — already a backup of `fastpool` | yes | yes |
| `rightpool/backup/manual` | right | `leftpool/replica/backup-manual` | yes | yes |
| `rightpool/nextcloud` (moved from `leftpool` 2026-10-18) | right | `leftpool/replica/nextcloud` | yes | yes (new job) |
| `rightpool/pictures` (`CameraImport` + `CameraAlbums`, see [photos.md](photos.md)) | right | `leftpool/replica/pictures` | yes | yes |
| `rightpool/pictures-immich` (phone uploads) | right | `leftpool/replica/pictures-immich` | yes | yes |
| `rightpool/pictures-exported` (regenerable JPEGs) | right | none | optional | none |
| `rightpool/phones` | right | `leftpool/replica/phones` | yes | yes |
| `fastpool/system` | fast | optional — covered by the config harvest into `backup/automated` | yes (existing) | via harvest |
| `leftpool/extras` (Stash, 2.0T) | left | decision pending (`rightpool` has room) | yes | cost decision |
| Movies, series, music, downloads | right | none | none | none (music: decide) |

Space (2026-10-08): `leftpool` 4.6T free, `rightpool` 7.8T free; with every replica now on
`leftpool` it takes roughly 1.0T of that (the old figure split it across both pools).

## Snapshot and replication policy

- One schedule for every dataset in the table: **daily, keep 14 daily / 8 weekly / 6 monthly**.
  No hourly — these datasets are append-mostly and nothing here needs sub-day recovery.
- Replication: local→local in the TrueNAS Replication UI, snapshot-based incrementals, with
  *longer* retention on the replica than on the source.
- Cloud Sync to B2 from the source datasets. The bucket must keep file versions
  (lifecycle "keep all versions" or ~30 days), otherwise a synced deletion is permanent.
- Optional client-side encryption for pictures/phones; store the passphrase off the NAS
  (the TrueNAS config export already holds the B2 key — see backups.md "Highest-value gaps").

## Migration

State as of 2026-10-18: `leftpool/iocage` and `leftpool/system` deleted; `leftpool/multimedia/phones`
retired (see Open). The steps below are otherwise not done.

**Sequencing (agreed 2026-10-18):**

1. **Done 2026-10-18:** `leftpool/nextcloud` moved to `rightpool/nextcloud` and its snapshot
   task moved with it (compose paths repointed, not yet deployed). Still to do: the
   `leftpool/replica/nextcloud` replica, and the old `leftpool/nextcloud` stays `readonly=on`
   for a week before destroy.
2. **Next:** scheduled replication of the important `rightpool` datasets (pictures,
   pictures-immich, phones, backup-manual) → `leftpool/replica/*`.
3. **After that:** re-create the Backblaze B2 Cloud Sync tasks from the *source* datasets
   (bucket versioning on first), repointing the old jobs per step 3 below.

Until step 3 is done, B2 offsite coverage is **absent or stale** — treat the replica as the
only second copy, and it shares the box.

**Snapshot tasks that exist today** (everything else in the table is still unsnapshotted):

| Dataset | Why it has snapshots |
| --- | --- |
| `rightpool/nextcloud` | Ransomware / mass-delete rollback |
| `backup/manual` (pool not recorded) | Old irreplaceable material; must not be lost or silently corrupted |
| `fastpool/system` | Roll back a big misconfiguration of app state |

Steps:

1. **`backup` stays on `rightpool`** (superseding the 2026-10-08 plan to move it to leftpool),
   so no `automated` path edits are needed. Syncthing is unused (user, 2026-10-18), so nothing
   needs repointing; if `leftpool/backup` still exists, verify it equals
   `rightpool/backup/manual`
   (`rsync -rnc --itemize-changes /mnt/leftpool/backup/ /mnt/rightpool/backup/manual/` → empty),
   then retire it.
2. **Pictures out of `multimedia`:** `zfs rename rightpool/multimedia/pictures rightpool/pictures`
   (instant, keeps children and snapshots; `phones` likewise if wanted). Update the SMB share,
   Cloud Sync job, snapshot task and any compose mount; the Emby mounts are already commented out.
3. Create `leftpool/replica` (the only new dataset needed), then the snapshot, replication and
   Cloud Sync tasks from the table. Repoint the existing B2 jobs
   (`rightpool/backup` stays; pictures/phones to the new paths).
4. Restore test from snapshot, from replica, from B2.

Always `zfs destroy -nv` first, snapshot before destroying, and `readonly=on` + rename for a
week before the real destroy.

## Open

- Pool topology (`zpool status`) not recorded here — mirrors vs stripes changes how much the
  replicas matter.
- `leftpool/multimedia/phones` (154G) was removed 2026-10-09: sha256 compare showed rightpool
  held a subset, the 1,611 left-only files were copied over (`rightpool/.../phones` 130G →
  134G), re-hash confirmed 0 missing, then the leftpool copy was destroyed. `rightpool` is now
  the only copy (B2 covers it). Syncthing (now unused) mounted all of
  `/mnt/leftpool/` as `/data2` and was the likely former writer; if its stack is still
  deployed, stop it so nothing recreates `leftpool/multimedia/phones`.
- Whether `leftpool/extras` gets a replica or B2.
