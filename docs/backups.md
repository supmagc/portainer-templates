# Backups

A [per-service coverage matrix](#backup-coverage-matrix) for the whole homelab, followed by
the openHABian host's two backup mechanisms and why they're hand-managed here. The NAS
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

The `disklist` covers `/boot`, `/etc`, `/var/lib/openhab`, `/home/openhabian`, `/usr/local`
and `/var/backups` (dpkg state), all as `comp-user-tar`; Mosquitto, Samba, zigbee2mqtt and
cron are present but commented out until installed, because a missing path fails the
nightly dump. Node-RED's data is under `/home/openhabian`. All entries
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

**Missing log dirs (2026-10-16):** after the 10-15 reboot `/var/log/amanda` (and
`/var/log/exim4`) didn't exist, so `amdump-openhab-dir` died with "create debug directory
... Permission denied" (`backup` can't create dirs in `/var/log`). zram-config is active, so
the dirs likely vanished with an unclean shutdown. `etc/tmpfiles.d/amanda-exim-logs.conf`
recreates them at boot; the unit sends amdump output to `/dev/null`, so the journal only shows
that one line.

## Mail relay (exim4)

Amanda's nightly report and cron/Caddy mail go out through the ISP relay `relay.edpnet.be`
(config type `smarthost`, port 25, opportunistic TLS, no login needed). Direct delivery to Gmail
from the home IP fails with TLS errors. Tracked: `etc/exim4/update-exim4.conf.conf`,
`etc/email-addresses` (rewrites senders to `bellecerise@edpnet.be`, which the relay accepts) and
`etc/aliases` (all system users → `supmagc@gmail.com`). After deploying, run `sudo update-exim4.conf
&& sudo systemctl restart exim4` — `oh-deploy-apply` does not. `/etc/exim4/passwd.client` holds a
stale `relay.edpnet.be` login (only causes `501` log noise) and stays off-repo.
`dc_other_hostnames` must include `bellecerise.local` (the `/etc/mailname` domain) or local mail
to `root@bellecerise.local` is "Unrouteable".

## Data exports to the NAS

`pi-data-backup@.{service,timer}` is a systemd template: one instance per dataset
(`pi-data-backup@influxdb`, `@grafana`, `@persistence`), each running
`/usr/local/sbin/pi-data-backup.sh <name>` nightly at 03:30 (+15m jitter) so each is
monitored and re-runnable on its own. They write dated exports to the NAS mount, keeping
the newest 14 of each:

- `/mnt/backup/influxdb/<date>/` — `influx backup` (InfluxDB 2.x, whole instance)
- `/mnt/backup/grafana/<date>/grafana.db.gz` — `sqlite3 .backup`, safe while Grafana runs
- `/mnt/backup/persistence/<date>/persistence.tar.gz`

It refuses to run if `/mnt/backup` isn't a mounted filesystem (so it can't fill the SD card)
and publishes a dated dir only after its export succeeds, so a failed night never replaces
or prunes a good one. A failed export fails only its own unit; all three are watched like
the other timers through `systemd-tasks-metrics.sh`.

Setup on the Pi: create `/etc/default/pi-data-backup` (root, mode 600) with `INFLUX_TOKEN=`
and optionally `INFLUX_HOST=`; install `sqlite3`; `chmod +x` the script; then
`systemctl enable --now pi-data-backup@{influxdb,grafana,persistence}.timer`.
Amanda's `disklist` doesn't cover any of this.

**Influx damage (2026-10-13):** the SSD has unreadable sectors and many Influx shards are
unreadable, so `influx backup` is expected to fail until the data is on a healthy disk.
An emergency raw copy lives in `/mnt/backup/emergency/` (from `rsync -a`; files that hit
read errors were discarded by rsync, not kept truncated).

## Router and TrueNAS config exports

Two Dagu DAGs (`openwrt-backup` 03:40, `truenas-config-backup` 03:45) write dated config
exports into `rightpool/backup/automated/{openwrt,truenas}`, owned `nas_backup` (940),
mode 600, and delete anything older than `RETENTION_DAYS` (30). Each file is first written
as `*.partial` and checked, then renamed, so a failed night never replaces or prunes a
good copy. A failure fails the DAG run, which fires `DaguDagRunFailed`. Both files hold
secrets (Wi-Fi PSKs and the router's root hash; TrueNAS's secret seed, which decrypts
every credential in its DB, including the B2 key), so they reach B2 only as safely as the
Cloud Sync task's own encryption allows.

**`openwrt-backup`** SSHes to the router as root with a key whose `authorized_keys` entry
*forces* the command, so a leaked key can only produce a backup. The forced command streams
`sysupgrade -k -b -` (config plus `/etc/backup/installed_packages.txt`) to stdout. That
needs OpenWrt 24.10+ (the router runs 24.10.8): stdout streaming was broken until a March
2024 fix, so on an older release write to `/tmp` and `cat` it instead. On 25.x (apk instead
of opkg) check that `-k` still exists. The script rejects
any output that isn't a gzip tar containing `etc/config/network`. The router's host key is
pinned on first contact (`accept-new`) in `dagu/job-data/openwrt-backup/known_hosts`;
delete that file after reflashing the router. The backup only covers files in the
`sysupgrade -l` set (see [network.md](network.md) for the `sysctl.d` footgun). Restore:
LuCI → System → Backup / Flash Firmware → Restore, or `sysupgrade -r <file>`.

Setup, as root on `nas` (create the key **before** the first run: Docker turns a missing
bind-mount source into an empty directory):

```sh
ssh-keygen -t ed25519 -N "" -C dagu-openwrt-backup -f /mnt/fastpool/system/processes/secrets/openwrt-backup-ssh-key
chmod 600 /mnt/fastpool/system/processes/secrets/openwrt-backup-ssh-key
mkdir -p /mnt/rightpool/backup/automated/openwrt /mnt/rightpool/backup/automated/truenas /mnt/fastpool/system/processes/dagu/job-data/openwrt-backup
chown 940:940 /mnt/rightpool/backup/automated/openwrt /mnt/rightpool/backup/automated/truenas
chmod 700 /mnt/rightpool/backup/automated/openwrt /mnt/rightpool/backup/automated/truenas
cat /mnt/fastpool/system/processes/secrets/openwrt-backup-ssh-key.pub
```

Then on the router, append one line to `/etc/dropbear/authorized_keys`, which is in the
default sysupgrade preserve set. Replace `<pubkey>` with the `.pub` contents:

```
command="sysupgrade -k -b - 2>/dev/null",no-port-forwarding,no-pty <pubkey>
```

The options go *before* the key type, on one line, with no spaces after the commas.
`logread -e dropbear` shows why a key was refused.

**`truenas-config-backup`** produces the same tar as System → General Settings → Manage
Configuration → Download File *with* the secret seed: `freenas-v1.db` plus `pwenc_secret`.
It bind-mounts the host's `/data` read-only, copies the live DB with `sqlite3 .backup`
(consistent while the middleware writes) and requires `PRAGMA integrity_check` = `ok` on
the copy. It uses no API key: Dagu's `docker.sock` is already root-equivalent on `nas`, so
reading `/data` adds no new exposure. Restore by uploading the `.tar` in that same
Manage Configuration dialog, on the same or a newer TrueNAS version. The read-only open
works (confirmed 2026-10-30); if a TrueNAS upgrade ever switches the DB to WAL journaling
and it starts failing, mount `/data` read-write.

## Jellyfin, Seerr, Navidrome and Stash backups

Multimedia and extras backups are meant to run **every 5 days**, like the *arr apps' own, and keep
about 30 days of copies. Infrastructure (router, TrueNAS, databases) runs daily. Cron can't
express a true 5-day interval, so these use `*/5` on the day of the month: the
1st, 6th, 11th … 31st. The gap is 5 days except at a month's end, where it's shorter.

| Service | Mechanism | When | Keeps | Owner |
|---|---|---|---|---|
| Navidrome | Built-in (`ND_BACKUP_PATH`/`_SCHEDULE`/`_COUNT` in compose) | every 5 days, 04:00 | 6 newest | Navidrome's user |
| Jellyfin | Dagu `jellyfin-backup` → `POST /Backup/Create` | every 5 days, 04:15 | 30 days | Jellyfin's user |
| Seerr | Dagu `seerr-backup`: `sqlite3 .backup` + `settings.json` → tar.gz | every 5 days, 04:30 | 30 days | `nas_backup`, mode 600 |
| Stash | Dagu `stash-backup` → GraphQL `backupDatabase` | every 5 days, 04:45 | 30 days | Stash's user |

**Navidrome** backs up its DB only (users, playlists, play counts, not the music) and prunes
itself. Restore with Navidrome stopped: `navidrome backup restore --backup-file <file>`. It
runs as `${APP_USER}`, so the backup folder must be writable by that user before the
container is recreated. Otherwise Docker creates it root-owned and the backup fails.

**Jellyfin** (v12) has a backup API but no schedule and no pruning, so the DAG calls it and
prunes `jellyfin-backup-*.zip` older than `RETENTION_DAYS` itself. Manual backups made from
the dashboard land in the same folder and are pruned too. The call is synchronous: it
returns the zip's `Path` once the file is written, and the script then checks that the zip
contains `manifest.json`. By default it backs up the database and config only. Metadata,
subtitles and trickplay are rebuildable, and each can be turned on with an
`INCLUDE_*` param. Jellyfin refuses to back up while a library scan runs and needs about
5 GiB free. Any failure is retried up to 6 times, 10 minutes apart. The API key is the Dagu
`ref:` secret `jellyfin-backup/api-key` (Dashboard → API Keys; an API key gets admin
rights, which the endpoint requires). Restore: Dashboard → Backups.

**Seerr** has no backup feature, so the DAG copies the DB with SQLite's online backup
(consistent while Seerr runs) and checks it with `PRAGMA integrity_check`. Seerr's config dir
is mounted **read-write** even though the job only reads it: if the DB uses WAL journaling,
SQLite needs write access to the `-shm` file just to read. `settings.json` holds API keys
for Jellyfin, Sonarr and Radarr, hence mode 600. Restore: stop Seerr, extract the tar into
`/mnt/fastpool/system/multimedia/seerr`, start it. Organizr isn't covered yet.

**Stash** can back itself up (Settings → Tasks → Backup, or the `backupDatabase` GraphQL
mutation) but has no scheduler and doesn't prune. Its backup folder held only three files,
198 days old as of 2026-10-31. The DAG calls the mutation with `download: false`, which
returns once the file is written but doesn't say where. So the script looks for a
`stash-go.sqlite.*` newer than its own start and fails if there is none. Stash writes to its
**Backup directory path** setting (Settings → System), which defaults to the database's own
folder. It must be set to `/backup`, the compose mount onto `automated/stash`. The backup is the
SQLite file only (a `VACUUM INTO` copy); `INCLUDE_BLOBS=true` zips `/blobs` in as well.
It reaches Stash over `utilities_default` with the API key from the Dagu `ref:` secret
`stash-backup/api-key` (Settings → Security → API Key, the same key the plugins use).
Restore: stop Stash, put the file back as `/config/stash-go.sqlite`, start it.

Setup, as root on `nas`, before the first run:

```sh
mkdir -p /mnt/rightpool/backup/automated/navidrome /mnt/rightpool/backup/automated/seerr
chown --reference=/mnt/rightpool/backup/automated/jellyfin /mnt/rightpool/backup/automated/navidrome
chown 940:940 /mnt/rightpool/backup/automated/seerr
chmod 700 /mnt/rightpool/backup/automated/seerr
```

Then recreate Navidrome (`docker compose up -d`, not `restart`: the mount is new), and register
`jellyfin-backup/api-key` in the Dagu UI.

## Stale backup detection

`BackupStale` checks what lands in `rightpool/backup/automated/`, not whether a job ran.
Native app schedules (the *arr apps, Bazarr, Tunarr, Navidrome) aren't Dagu jobs, so
`DaguDagRunFailed` can't see them, and a "successful" job that writes nothing would pass it
too.

The Dagu DAG `backup-freshness` runs hourly (`:07`). Its script,
`backup-freshness-metrics.py`, walks every subfolder of `automated/` (recursively, so the
*arr apps' `scheduled/` subfolders count). It ignores `*.partial`/`*.tmp` and dotfiles, and
writes `backups.prom` to node-exporter's textfile dir:

- `backup_newest_file_timestamp_seconds{name}`: mtime of the newest file, `0` if there is none.
- `backup_newest_file_size_bytes{name}`, `backup_files{name}`
- `backup_max_age_seconds{name}`: the per-folder threshold.

The thresholds are in the script's `MAX_AGE` map, set to each folder's schedule plus slack:

| Schedule | Max age | Folders |
|---|---|---|
| Daily | 30 h | mariadb, postgres, openwrt, truenas, openhabian |
| Every 5 days | 6 d | sonarr, radarr, lidarr, jellyfin, seerr, navidrome, whisparr, stash, tunarr |
| Weekly | 8 d | bazarr |
| Not in the map | 8 d (default) | anything else, e.g. prowlarr, sabnzbd |
| `None` | not checked | emby (retired) |

A folder that's in the map but doesn't exist is reported as never backed up, so a job whose
target was never created still alerts. A new folder is picked up automatically with the
8-day default. Add it to `MAX_AGE` if it runs more often.

The rule lists every checked folder. Its value is hours past the max age, so a negative
number is the headroom left. If the detector itself stops running, its last file stays in
place with fixed timestamps, and every folder eventually goes stale. That alerts too, rather
than going quiet. `noDataState: NoData` covers the file never being written at all.

## SD-card mirroring guard rails

openHABian's stock `sdrawcopy` / `sdrsync` systemd timers mirror the running SD card to a
spare. Tracked here:

- `sdmirror-guard.sh` + `sd{rawcopy,rsync}.service.d/guard.conf` — `ExecStartPre` drop-ins
  that refuse to run the mirroring jobs unless the target really is the SanDisk card
  reader. This guards against a `/dev/sda`↔`/dev/sdb` USB re-enumeration causing a mirror
  job to write over the Transcend SSD (which holds the Amanda vtapes and holding disk)
  instead of the spare card.
- `sdrawcopy.timer.d/schedule.conf` — overrides openhabian-config's stock semiannual
  `OnCalendar` to run every 2 months instead.
- `99-usb-drives.rules` — udev rules giving stable `/dev/sdbackup*` and `/dev/ssd*` names
  so the guard and the Amanda paths don't depend on kernel enumeration order.

## Backup coverage matrix

One row per data store or config in the homelab, with whether it's worth backing up, how it
should be done, and where it stands. Compiled 2026-10-07 **from this repo only** — the
TrueNAS UI (snapshot/Cloud Sync scope), the router and the Portainer host weren't inspected,
so anything depending on them is marked *unverified*, not assumed.

**Status:** **Done** = a working mechanism exists in the repo. **Partial** = exists but
incomplete, or its scope/schedule is unverified. **Gap** = relevant and nothing covers it.
**N/A** = deliberately not backed up (cache, logs, rebuildable, stateless).
**Priority:** Critical (irreplaceable, or blocks recovery of everything else) > High >
Medium > Low.

Cross-cutting layers the rows below refer to:

- **ZFS redundancy** protects against a failed disk only — not deletion, ransomware or pool loss.
- **Snapshots** — TrueNAS Periodic Snapshot Tasks (TrueNAS UI; monitored, not defined, here).
- **Cloud Sync** — TrueNAS Cloud Sync → Backblaze B2 (offsite). Scope per dataset is
  **unverified**; a row that says "via Cloud Sync" only reaches B2 if that dataset is in a task.
- **`rightpool/backup/automated/<app>`** — the second-pool landing zone for per-app native
  exports and DB dumps (a different pool from the live data on `fastpool`).
- **Git** — this repo; offsite only for what's committed *and* pushed.

### Platform, secrets and network

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| TrueNAS config | Pool layout, users/groups, shares, network, Snapshot/Cloud Sync task definitions (incl. the B2 key) | Critical | Dagu `truenas-config-backup`, 03:45 daily → `rightpool/backup/automated/truenas`, 30-day retention ([above](#router-and-truenas-config-exports)) | **Done** — deployed, first run OK 2026-10-30; offsite via Cloud Sync |
| ZFS snapshots | Point-in-time rollback of the three pools | High | Periodic Snapshot Tasks per dataset | **Partial** — tasks exist (monitored by `snapshot-tasks-metrics.sh`); dataset coverage unverified |
| Cloud Sync → B2 | Offsite copy | Critical | Tasks covering `leftpool`, `fastpool/system` and `rightpool/backup/automated`; bucket versioning or copy-with-retention so a local delete/encrypt isn't mirrored | **Partial** — `fastpool` config and all of `rightpool/backup` (2026-10-30) confirmed; `leftpool` and versioning unverified |
| Portainer | Own DB: stack definitions and the per-stack environment values typed in its UI (no `.env` exists) | Critical | Portainer's own backup (Settings → Backup), or copy its data dir while stopped, into `rightpool/backup/automated/portainer` | **Gap** — without it the compose files in git can't be redeployed as-is |
| Secrets dir (`processes/secrets/*.txt`) | Only copy of DB passwords/API keys; deliberately kept out of git | Critical | Copy into `rightpool/backup/automated/secrets` (owner `nas_backup`, mode 600), or a password manager | **Gap** |
| Dagu `ref:` secrets | Secrets set via Dagu's UI/API live in its own state, not in git | High | Re-enterable from the secrets dir; keep a list of the ref names (`mariadb-backup/db-password`, `postgres-backup/db-password`, `trailer-fetch/*`, `themerr-fetch/*`, …) | **Gap** — list not kept |
| This repo (compose, `hosts/`, dashboards, docs) | Declarative config | High | `git push` to GitHub | **Partial** — only as current as the last push (the working tree has uncommitted changes) |
| `scratchpad/` host-only configs | Gitignored: Grafana contact point (Pushover token), OpenWrt notes, config copies | Medium | Include in the secrets copy | **Gap** |
| step-ca (`processes/step-ca`) | Internal CA keys and DB | Critical | Copy dir to `rightpool/backup/automated/step-ca`; losing the root key means re-trusting a new CA on every client | **Gap** |
| Traefik (internal) `processes/traefik/stepca` | ACME cert state from step-ca | Low | Re-issued automatically | **N/A** |
| Traefik-Edge `acme/` | Let's Encrypt (dynu DNS) certs | Low | Re-issued automatically; copy only to dodge LE rate limits after a rebuild | **N/A** |
| Traefik-Edge `dynamic/*.yml` | WAN routing | — | Git | **Done** |
| OpenWrt router | `/etc/config/*` (firewall zones, VLANs, SQM, SNMP/exporter), package list | High | Dagu `openwrt-backup`, 03:40 daily, `sysupgrade -k -b` over a forced-command SSH key → `rightpool/backup/automated/openwrt`, 30-day retention ([above](#router-and-truenas-config-exports)); check custom files are in the `sysupgrade -l` set (see [network.md](network.md)) | **Done** — deployed, first run OK 2026-10-30; offsite via Cloud Sync |
| Fritzbox (VDSL2 modem) | Line/bridge settings | Medium | Fritz!Box → System → Backup | **Gap** (unverified) |
| Managed switch / APs | VLAN and SSID config | Medium | Vendor config export | **Gap** (unverified) |

### openHABian (Raspberry Pi, `192.168.1.154`)

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| `/etc`, `/boot`, `/var/lib/openhab` | openHAB conf, items/things (JSONDB), HabApp, Caddy, systemd units | Critical | Amanda `amdump` nightly (above) | **Done** on the local SSD (14 days); **Partial** — the NAS/B2 leg (90 days) is inactive until the NFS mount works |
| Amanda catalog | Index needed to `amrecover` | High | `amandaBackupDB` timer | **Done** (unit tracked) |
| Boot SD card | Whole OS image | Medium | `sdrawcopy` (every 2 months) + `sdrsync` to the spare card | **Done**, guarded; same room as the Pi, no offsite |
| InfluxDB **2.x** (`/mnt/ssd/influxdb`) | openHAB persistence history | Medium | `influx backup` to the NAS via `pi-data-backup` (don't tar the live DB: inconsistent) | **Partial** — script written, not deployed; the SSD copy has unreadable shards (see below) |
| Pi-bundled Grafana (`/mnt/ssd/grafana`) | Local dashboards, Loki datasource | Low | `sqlite3 .backup` of `grafana.db` via `pi-data-backup` | **Partial** — written, not deployed |
| openHAB persistence (`/mnt/ssd/persistence`) | rrd4j/MapDB; `/var/lib/openhab/persistence` is a symlink to it, so Amanda's tar skips it | Medium | tar via `pi-data-backup` | **Partial** — written, not deployed |
| Pi Loki data, journal | Logs | — | Disposable; central Loki gets a copy | **N/A** |
| Caddy cert (`~/.acme.sh`) | step-ca-issued cert | Low | Re-issued by `acme-renew` | **N/A** |
| Tracked config (units, Caddyfile, Promtail, Amanda conf) | — | — | Git + `deploy.ps1` | **Done** |

### Multimedia stack

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| Sonarr, Radarr, Lidarr | `/config` (DB, settings) | High | App's native backup, every 5 days, ~30 days kept → `rightpool/backup/automated/<app>/scheduled` | **Done** — Radarr/Lidarr zips checked 2026-10-30 (Sonarr's folder updated the same day). Pre-switch copies in `/config/Backups` stop at 2025-09-17 and can be deleted |
| Bazarr | `/config` | High | Native backup, weekly (Sat 03:00) → `rightpool/backup/automated/bazarr` | **Done** — checked 2026-10-30 |
| Tunarr | `db.db`, channel lineups, `xmltv.xml`, search snapshot | Medium | Native backup, every 5 days 03:00 (a true 5-day interval, set in Tunarr's UI), 7 kept → `rightpool/backup/automated/tunarr` | **Done** — checked 2026-10-30; daily until 2026-11-01. ~1 GB per tar, 752 MB of it `cache/`: Tunarr always archives `cache/`, images, lineups and `ms-snapshots` (no per-item exclusion in its source); gzip (tar only) is the way to shrink it |
| Emby | Config, users, watch state | — | Retired (commented out of compose); old exports may remain in `backup/automated/emby` | **N/A** |
| Jellyfin | Config, users, watch state; `metadata`/`cache` rebuildable | High | Dagu `jellyfin-backup`, every 5 days 04:15, native `POST /Backup/Create` → `rightpool/backup/automated/jellyfin`, 30-day retention ([above](#jellyfin-seerr-navidrome-and-stash-backups)) | **Done** — first run OK 2026-10-31 |
| Meilisearch | Jellyfin search index | — | Rebuildable from Jellyfin | **N/A** |
| Seerr | `settings.json`, request DB | Medium | Dagu `seerr-backup`, every 5 days 04:30, SQLite `.backup` + `settings.json` → `rightpool/backup/automated/seerr`, 30-day retention | **Done** — first run OK 2026-10-31 |
| Navidrome | `navidrome.db` (users, playlists, play counts); music is separate | Medium | Built-in scheduled backup, every 5 days 04:00, 6 kept → `rightpool/backup/automated/navidrome` | **Partial** — no backup yet as of 2026-10-31; needs the container recreated |
| Organizr | Tab/user config | Low | Copy `/config` | **Gap** (low) |
| Media libraries (`rightpool/multimedia/{movies,series,music,games}`) | Replaceable media | Low | ZFS redundancy only, by design | **N/A** |
| `rightpool/multimedia/pictures`, `leftpool/multimedia/phones` | Personal photos/video | Critical | Snapshots + Cloud Sync | **Partial** — Cloud Sync scope unverified |

### Downloads and extras

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| SABnzbd | `/config` (servers, categories) | High | Native backup → `rightpool/backup/automated/sabnzbd`, pruned by `sabnzbd-backup-cleaner` (7 days) | **Done** |
| Prowlarr | Indexers, app links | High | Native backup → `rightpool/backup/automated/prowlarr` | **Partial** (schedule unverified) |
| qBittorrent | `/config` | Low | Copy `/config` | **Gap** (low) |
| BitMagnet | Postgres DB `bitmagnet` (DHT index) | Low | Rebuildable; intentionally not dumped | **N/A** |
| Spotweb | MariaDB `spotweb` (~380K spots, users/settings) | Low | Re-retrievable from NNTP but slow; add `spotweb` to `DB_DUMP_INCLUDE` if its settings matter | **N/A** (decide) |
| `downloads/` | In-flight and finished downloads | — | Disposable | **N/A** |
| Stash | DB (`/config`); `generated`/`cache` rebuildable; media is in `leftpool/extras` | Medium | Dagu `stash-backup`, every 5 days 04:45, GraphQL `backupDatabase` → `rightpool/backup/automated/stash`, 30-day retention ([above](#jellyfin-seerr-navidrome-and-stash-backups)) | **Partial** — written 2026-10-31, not deployed; nothing had run for 198 days |
| Whisparr | `/config` | Medium | Native backup, every 5 days → `rightpool/backup/automated/whisparr` | **Done** — set up 2026-10-31 |
| Namer | `/config` (`namer.cfg`) | Low | Repo holds a placeholder copy; the real token is host-only → secrets copy | **Gap** (low) |
| `leftpool/extras` | Extras library | Medium | Snapshots; Cloud Sync is a cost decision | **Partial** (unverified) |

### Nextcloud and household

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| Nextcloud data (`rightpool/nextcloud`, moving from `leftpool` 2026-10-18) | Personal files, photos, documents | Critical | Snapshots + Cloud Sync with versioning | **Partial** — Cloud Sync scope unverified |
| Nextcloud app dir (`nextcloud/app/html`) | `config.php` (instance secrets), installed apps | High | Copy `config.php` (or the dir) → `rightpool/backup/automated/nextcloud` | **Gap** |
| Nextcloud DB (`nextcloud` in MariaDB) | Metadata and shares | Critical | Nightly dump (MariaDB row below) | **Done** |
| OnlyOffice | DB `onlyoffice`, `data`/`log` | Low | DB is dumped; documents live in Nextcloud, the rest is rebuildable | **Done** (DB) |
| Nextcloud Caddy, `cron`, `rebuilder` | Caddyfile; otherwise stateless | Low | Copy the Caddyfile if non-trivial | **N/A** |
| digiKam DB (`digikam` in MariaDB) | Album/tag/face catalogue | High | Nightly dump (below). Does **not** cover the image files | **Done** (DB only) |
| Home videos | Irreplaceable | Critical | Consolidate into one known dataset inside Cloud Sync scope | **Gap** — scattered across Nextcloud/phones/pictures |
| Mealie | Postgres `mealie` + `/app/data` (images, uploads) | High | DB: nightly `pg_dump`. Files: copy `household/mealie` (or Mealie's built-in backup) → `rightpool/backup/automated/mealie` | **Partial** — DB dump committed 2026-10-07 (deploy and `postgres-backup/db-password` ref unverified); files **Gap** |
| Vikunja | Postgres `vikunja` + `files/` (attachments) | High | DB: as above. Files: copy `household/vikunja/files` | **Partial** — same as Mealie |

### Utilities, databases and monitoring

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| MariaDB | `digikam`, `nextcloud`, `onlyoffice` (`spotweb` excluded, see above) | Critical | Dagu `mariadb-backup`, 04:00 daily, gzip dumps to `rightpool/backup/automated/mariadb`, 7-day retention | **Done** — offsite via Cloud Sync |
| PostgreSQL | `mealie`, `vikunja`, `mem0`, `openwebui` (+ `bitmagnet`, rebuildable) | High | Dagu `postgres-backup`, 04:30 daily, `pg_dump -Fc` to `rightpool/backup/automated/postgres`, 7-day retention | **Partial** — committed 2026-10-07, deployment unverified |
| Dagu | DAG files and scripts; run history/state | Low | Git for definitions; history is disposable | **Done** |
| Grafana (`processes/grafana/data`) | `grafana.db`: users, UI-managed contact points and notification policy, API keys | Medium | Copy `grafana.db` (SQLite `.backup`) → `rightpool/backup/automated/grafana`. Provisioned dashboards/rules/datasources are in git | **Gap** — contact points/policy exist only here |
| Prometheus, Loki, Promtail positions | Time-series and log data | — | Disposable; config in git | **N/A** |
| Exporters (cAdvisor, node/blackbox/snmp/smartctl/gpu, redis/mysqld/postgres, exportarr, per-app) | Stateless | — | Config in git (`mysqld-exporter.cnf` is a secret → secrets copy) | **N/A** |
| Redis, RabbitMQ (`data`, `log`) | Cache/sessions, transient queues | — | Disposable; `rabbitmq.conf` / `enabled_plugins` are tiny and host-only | **N/A** |
| Syncthing (`processes/syncthing`) | Device identity (keys, certs), folder config | Medium | Copy `/config` → `rightpool/backup/automated/syncthing` (losing the keys means re-pairing every device) | **Gap** |
| Tdarr (`server`, `configs`, flows, custom plugins) | Library DB, flows, node config | Low–Medium | Copy `server`/`configs`; `temp`/`cache`/`logs` excluded; local flow plugins belong in git | **Gap** (low) |
| dupeGuru, chromedp, phpMyAdmin, Watchtower, whoami | Stateless / trivial config | — | — | **N/A** |

### Built-in backup mechanisms

What each service can do on its own, to prefer over a raw file copy. Written from memory of
each project, not checked against the pinned versions (all images are `:latest`) — verify
the *unverified* entries before relying on them.

#### Built-in, already mounted in compose

| Service | Mechanism | Caveat |
|---|---|---|
| Sonarr, Radarr, Lidarr, Whisparr, Prowlarr | Scheduled zip of config DB and settings (System → Backup) | Default folder is under `/config`; the `/backup` mount only helps if the backup folder is pointed there (done for Sonarr/Radarr/Lidarr) |
| Bazarr | Scheduled backup of config and DB | Pointed at `/backup` (confirmed) |
| SABnzbd | Config backup via UI/API | Believed on-demand, not scheduled (unverified) — something must trigger it or `sabnzbd-backup-cleaner` has nothing to prune |
| Jellyfin (12, running) | Dashboard → Backups / `POST /Backup/Create`: DB + config always, optionally metadata/subtitles/trickplay | No scheduled task and no pruning in the server (checked in `release-12.z` source); Dagu `jellyfin-backup` adds both (the `Filtik/Jellyfin-AutoBackup` plugin was rejected: one release, unmaintained, fails silently). Mount targets its backup dir |
| Navidrome | Scheduled DB backup (`ND_BACKUP_PATH`, `ND_BACKUP_SCHEDULE`, `ND_BACKUP_COUNT`) | Wired up 2026-10-31 (every 5 days, 6 kept) |
| Stash | Settings → Tasks → Backup / GraphQL `backupDatabase` (DB, optionally blobs) | No scheduler and no pruning in core (checked in `develop` source); Dagu `stash-backup` adds both |

#### Built-in, not wired up yet

| Service | Mechanism |
|---|---|
| Mealie | Admin → Backups: zip of DB and data, API-triggerable |
| Vikunja | `vikunja dump` CLI (config, DB, files) |
| Portainer | Settings → Backup (encrypted archive, API-triggerable; scheduling is Business Edition only) |
| Tdarr | Automatic DB backups in `server/Backups`, restorable from the UI |
| Emby | Plugin-based, not core — don't rely on it |

#### Tool-level backups (not the app's own)

| Service | Mechanism |
|---|---|
| openHAB | `openhab-cli backup` (already covered by Amanda) |
| OpenWrt | `sysupgrade -b` / LuCI → Backup |
| TrueNAS | System Settings → Save Config |
| Fritzbox | System → Backup |
| Prometheus | Snapshot API, if metrics history is ever wanted (currently treated as disposable) |
| Grafana | No real native backup — copy `grafana.db` or export via API |
| Postgres, MariaDB | `pg_dump` / `mariadb-dump` (done) |

**No native mechanism — copy files:** Syncthing (keys, `config.xml`), Seerr (done, see above), Organizr,
step-ca, Nextcloud (needs maintenance mode + DB dump + data dir; no single command),
qBittorrent, Traefik, BitMagnet, Dagu.

The SABnzbd/Prowlarr/Stash/Whisparr rows above stay **Partial** until someone checks on
the NAS that backups are actually being created on a schedule and landing in
`rightpool/backup/automated/<app>`. Once `backup-freshness` is deployed, `BackupStale`
answers that: each of them is checked against the 8-day default.

### Highest-value gaps, in order

1. **Cloud Sync / snapshot scope** — confirm in the TrueNAS UI that `leftpool` (Nextcloud,
   phones, pictures, home videos) and `rightpool/backup/automated` are covered, and whether
   B2 keeps versions. This decides the status of ~15 rows above.
2. **Portainer DB, secrets dir, step-ca, TrueNAS config export** — small, Critical, and a
   rebuild from git alone is impossible without them. One scheduled "config harvest"
   copying these into `rightpool/backup/automated/` (a Dagu DAG fits the existing pattern)
   puts them on the same path to B2 as the DB dumps.
3. **Activate the Amanda NAS leg** (fix the `/mnt/backup` NFS mount) and add an InfluxDB backup.
4. Grafana `grafana.db` and Syncthing (OpenWrt, Seerr and Navidrome done).
5. **Restore test** — nothing here counts as proven until a file has been restored from a
   snapshot, from B2, and with `amrecover`.
