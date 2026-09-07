# Backups

The openHABian host's two backup mechanisms and why they're hand-managed here. The NAS
(`nas`) box's own data protection — ZFS periodic snapshots and cloud-sync to Backblaze —
is configured in TrueNAS, not this repo; only its *monitoring* is tracked (see
[monitoring.md](monitoring.md#truenas-native-scheduled-task-visibility)).

## openHABian Amanda backup (dual-storage)

Amanda on the Pi is **hand-owned** in this repo —
`hosts/openhabian/etc/amanda/openhab-dir/{amanda.conf,disklist,amanda-client.conf}` plus
the `amdump-openhab-dir` and `amandaBackupDB` systemd units — because it runs a
dual-storage layout that openHABian's `amanda.conf-template` can't express.

**Do not run `openhabian-config` → Backup → Amanda.** It regenerates `amanda.conf` from
`/opt/openhabian/includes/amanda/amanda.conf-template`, which has no concept of a second
storage and drops the `nas` blocks. If it's ever run: redeploy the file
(`scripts\deploy.ps1 -DeployHost openhabian -DeployPath etc/amanda`) and re-run
`sudo -u backup amcheck openhab-dir`.

### Layout

- **storage `ssd`** — `chg-disk` vtapes on the local Transcend SSD
  (`/mnt/ssd/amanda/openhab-dir/slots`). Fast, primary copy, ~2 weeks of history
  (`retention-tapes 14`).
- **storage `nas`** — `chg-disk` vtapes on the NFS mount from the NAS
  (`/mnt/backup/openhab-dir/slots`) → picked up by ZFS snapshots → Backblaze B2. Offsite
  copy, ~3 months of history (`retention-tapes 90`).

`amdump` writes **both** storages in one run, fed from a holding disk
(`/mnt/ssd/amanda/holding`, up to 15 GB; a whole run is ~1.2 GB). openHABian's template
sets `holdingdisk no` — the multi-storage double-write and graceful NAS-down degradation
both need one. If the NAS is down the SSD write still succeeds and the dump waits in
holding for a later `amflush`.

The `disklist` covers `/boot`, `/etc`, and `/var/lib/openhab`, all as `comp-user-tar`
(client-side fast compression, `amgtar` with openHABian's exclude list). One full dump per
7 days spread over 7 daily runs; one vtape slot = 5 GB (a run fits in one).

### Schedule (systemd timers, not cron)

- `amdump-openhab-dir.timer` → `amdump-openhab-dir.service`: nightly at 00:55
  (`RandomizedDelaySec=10m`, `Persistent=true`), runs `amdump openhab-dir` as the `backup`
  user.
- `amandaBackupDB.timer` → `amandaBackupDB.service`: backs up Amanda's own catalog/state
  DB.

Both run as systemd units specifically so `node_exporter --collector.systemd` sees them
for free (no wrapper script).

### Bring-up state

The config ships **SSD-only** (`storage "ssd"`). The `nas` storage is fully defined in
`amanda.conf` but not yet in the active `storage` line, because the NFS mount to
`/mnt/backup` currently hangs and `amcheck` would fail against it.

To add the NAS copy: get `/mnt/backup` mounting cleanly, create
`/mnt/backup/openhab-dir/slots` owned `backup:backup`, switch the `storage` line to
`storage "ssd" "nas"`, redeploy, and run `sudo -u backup amcheck openhab-dir` until clean.
Nothing needs restarting — `amdump` re-reads `amanda.conf` on every run; the next timer
firing (or a manual `sudo -u backup amdump openhab-dir`) picks up the change and
autolabels the `nas-openhab-*` vtapes on first use.

Restores default to the fast local copy (`amrecover_changer "ch_ssd"`).

## SD-card mirroring guard rails

openHABian's stock `sdrawcopy` / `sdrsync` systemd timers mirror the running SD card to a
spare. Tracked here:

- `sdmirror-guard.sh` + `sd{rawcopy,rsync}.service.d/guard.conf` — `ExecStartPre` drop-ins
  that refuse to run the mirroring jobs unless the target really is the SanDisk card
  reader. This guards against a `/dev/sda`↔`/dev/sdb` USB re-enumeration causing a mirror
  job to write over the Transcend SSD (which holds the Amanda vtapes and holding disk)
  instead of the spare card.
- `99-usb-drives.rules` — udev rules giving stable `/dev/sdbackup*` and `/dev/ssd*` names
  so the guard and the Amanda paths don't depend on kernel enumeration order.
