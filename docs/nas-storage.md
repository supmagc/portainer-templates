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

`fastpool` is live app state and is never the only copy: its backups land on `leftpool`.
Media that can be re-downloaded (movies, series, downloads, caches) gets none of this.

## Target layout

| Dataset | Home | Replica | Snapshots | B2 |
|---|---|---|---|---|
| `leftpool/backup/automated` (app + DB dumps, config harvest) | left | none — already a backup of `fastpool` | yes | yes |
| `leftpool/backup/manual` | left | `rightpool/replica/backup-manual` | yes | yes |
| `leftpool/nextcloud` | left | `rightpool/replica/nextcloud` | yes | yes (new job) |
| `rightpool/pictures` (`CameraImport` + `CameraAlbums`, see [photos.md](photos.md)) | right | `leftpool/replica/pictures` | yes | yes |
| `rightpool/pictures-immich` (phone uploads) | right | `leftpool/replica/pictures-immich` | yes | yes |
| `rightpool/pictures-exported` (regenerable JPEGs) | right | none | optional | none |
| `rightpool/phones` | right | `leftpool/replica/phones` | yes | yes |
| `fastpool/system` | fast | optional — covered by the config harvest into `backup/automated` | yes (existing) | via harvest |
| `leftpool/extras` (Stash, 2.0T) | left | decision pending (`rightpool` has room) | yes | cost decision |
| Movies, series, music, downloads | right | none | none | none (music: decide) |

Space (2026-10-08): `leftpool` 4.6T free, `rightpool` 7.8T free; the replicas above total
roughly 0.9T on `leftpool` and 0.15T on `rightpool`.

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

State as of 2026-10-08: `leftpool/iocage` and `leftpool/system` deleted; nothing below done.

1. **Do not delete `leftpool/backup` yet.** It is the target home of `manual` and is already
   mounted: Syncthing uses `/mnt/leftpool/backup/syncthing` (`docker-compose-syncthing.yml:31`).
   Verify it equals `rightpool/backup/manual`
   (`rsync -rnc --itemize-changes /mnt/leftpool/backup/ /mnt/rightpool/backup/manual/` → empty),
   then retire the right-pool copy.
2. **Move `automated` to leftpool.** `zfs send` a snapshot of `rightpool/backup/automated` into
   `leftpool/backup/automated`, then repoint every writer (about 16 paths, all
   `/mnt/rightpool/backup/automated/<app>`): `docker-compose-multimedia.yml`,
   `-downloads.yml`, `-extras.yml`, `dagu/dags/postgres-backup.yaml`, `mariadb-backup.yaml`.
   Redeploy, confirm a fresh backup lands, keep the old dataset read-only for a week.
3. **Pictures out of `multimedia`:** `zfs rename rightpool/multimedia/pictures rightpool/pictures`
   (instant, keeps children and snapshots; `phones` likewise if wanted). Update the SMB share,
   Cloud Sync job, snapshot task and any compose mount; the Emby mounts are already commented out.
4. Create `rightpool/replica` and `leftpool/replica`, then the snapshot, replication and
   Cloud Sync tasks from the table. Repoint the existing B2 jobs
   (`rightpool/backup` → `leftpool/backup`, pictures/phones to the new paths).
5. Restore test from snapshot, from replica, from B2.

Always `zfs destroy -nv` first, snapshot before destroying, and `readonly=on` + rename for a
week before the real destroy.

## Open

- Pool topology (`zpool status`) not recorded here — mirrors vs stripes changes how much the
  replicas matter.
- `leftpool/multimedia/phones` (154G) was removed 2026-10-09: sha256 compare showed rightpool
  held a subset, the 1,611 left-only files were copied over (`rightpool/.../phones` 130G →
  134G), re-hash confirmed 0 missing, then the leftpool copy was destroyed. `rightpool` is now
  the only copy (B2 covers it). Syncthing mounts all of `/mnt/leftpool/` as `/data2`, the
  likely former writer — unverified; check it doesn't recreate `leftpool/multimedia/phones`,
  and that mount also lets it write anywhere on `leftpool`.
- Whether `leftpool/extras` gets a replica or B2.
