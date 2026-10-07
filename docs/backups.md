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
| TrueNAS config | Pool layout, users/groups, shares, network, Snapshot/Cloud Sync task definitions (incl. the B2 key) | Critical | System Settings → General → Save Config on a schedule, written to a path that reaches B2; the export holds secrets, so keep it private | **Gap** — not in git by design, no export seen |
| ZFS snapshots | Point-in-time rollback of the three pools | High | Periodic Snapshot Tasks per dataset | **Partial** — tasks exist (monitored by `snapshot-tasks-metrics.sh`); dataset coverage unverified |
| Cloud Sync → B2 | Offsite copy | Critical | Tasks covering `leftpool`, `fastpool/system` and `rightpool/backup/automated`; bucket versioning or copy-with-retention so a local delete/encrypt isn't mirrored | **Partial** — `fastpool` config only is confirmed; `leftpool` and versioning unverified |
| Portainer | Own DB: stack definitions and the per-stack environment values typed in its UI (no `.env` exists) | Critical | Portainer's own backup (Settings → Backup), or copy its data dir while stopped, into `rightpool/backup/automated/portainer` | **Gap** — without it the compose files in git can't be redeployed as-is |
| Secrets dir (`processes/secrets/*.txt`) | Only copy of DB passwords/API keys; deliberately kept out of git | Critical | Copy into `rightpool/backup/automated/secrets` (owner `nas_backup`, mode 600), or a password manager | **Gap** |
| Dagu `ref:` secrets | Secrets set via Dagu's UI/API live in its own state, not in git | High | Re-enterable from the secrets dir; keep a list of the ref names (`mariadb-backup/db-password`, `postgres-backup/db-password`, `trailer-fetch/*`, `themerr-fetch/*`, …) | **Gap** — list not kept |
| This repo (compose, `hosts/`, dashboards, docs) | Declarative config | High | `git push` to GitHub | **Partial** — only as current as the last push (the working tree has uncommitted changes) |
| `scratchpad/` host-only configs | Gitignored: Grafana contact point (Pushover token), OpenWrt notes, config copies | Medium | Include in the secrets copy | **Gap** |
| step-ca (`processes/step-ca`) | Internal CA keys and DB | Critical | Copy dir to `rightpool/backup/automated/step-ca`; losing the root key means re-trusting a new CA on every client | **Gap** |
| Traefik (internal) `processes/traefik/stepca` | ACME cert state from step-ca | Low | Re-issued automatically | **N/A** |
| Traefik-Edge `acme/` | Let's Encrypt (dynu DNS) certs | Low | Re-issued automatically; copy only to dodge LE rate limits after a rebuild | **N/A** |
| Traefik-Edge `dynamic/*.yml` | WAN routing | — | Git | **Done** |
| OpenWrt router | `/etc/config/*` (firewall zones, VLANs, SQM, SNMP/exporter), package list | High | LuCI → System → Backup, or `sysupgrade -b`, kept in `rightpool/backup/automated/openwrt`; check custom files are in the `sysupgrade -l` set (see [network.md](network.md)) | **Gap** — only setup notes in the gitignored scratchpad |
| Fritzbox (VDSL2 modem) | Line/bridge settings | Medium | Fritz!Box → System → Backup | **Gap** (unverified) |
| Managed switch / APs | VLAN and SSID config | Medium | Vendor config export | **Gap** (unverified) |

### openHABian (Raspberry Pi, `192.168.1.154`)

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| `/etc`, `/boot`, `/var/lib/openhab` | openHAB conf, items/things (JSONDB), HabApp, Caddy, systemd units | Critical | Amanda `amdump` nightly (above) | **Done** on the local SSD (14 days); **Partial** — the NAS/B2 leg (90 days) is inactive until the NFS mount works |
| Amanda catalog | Index needed to `amrecover` | High | `amandaBackupDB` timer | **Done** (unit tracked) |
| Boot SD card | Whole OS image | Medium | `sdrawcopy` (every 2 months) + `sdrsync` to the spare card | **Done**, guarded; same room as the Pi, no offsite |
| InfluxDB 1.x (`/var/lib/influxdb`) | openHAB persistence history | Medium | `influxd backup -portable` on a timer to the SSD/NAS (don't tar the live DB: inconsistent) | **Gap** — not in the Amanda `disklist` |
| Pi-bundled Grafana (`/var/lib/grafana`) | Local dashboards, Loki datasource | Low | Add to `disklist`, or recreate | **Gap** (low) |
| Pi Loki data, journal | Logs | — | Disposable; central Loki gets a copy | **N/A** |
| Caddy cert (`~/.acme.sh`) | step-ca-issued cert | Low | Re-issued by `acme-renew` | **N/A** |
| Tracked config (units, Caddyfile, Promtail, Amanda conf) | — | — | Git + `deploy.ps1` | **Done** |

### Multimedia stack

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| Sonarr, Radarr, Lidarr, Bazarr | `/config` (DB, settings) | High | App's native backup → `rightpool/backup/automated/<app>` | **Partial** — export folder is mounted; that backups are scheduled and recent is unverified |
| Emby | Config, users, watch state | High | Native backup → `rightpool/backup/automated/emby` | **Partial** — as above (being retired for Jellyfin) |
| Jellyfin | Config, users, watch state; `metadata`/`cache` rebuildable | High | Native backup → `rightpool/backup/automated/jellyfin` | **Partial** — as above; trial deployment |
| Seerr | `settings.json`, request DB | Medium | Copy `/app/config` (stop it, or SQLite `.backup`) → `rightpool/backup/automated/seerr` | **Gap** — a reset means redoing the Jellyfin setup wizard by hand |
| Navidrome | `navidrome.db` (users, playlists, play counts); music is separate | Medium | Built-in scheduled backup (`ND_BACKUP_PATH` / `ND_BACKUP_SCHEDULE`) → `rightpool/backup/automated/navidrome` | **Gap** — verify option names against your version |
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
| Stash | DB (`/config`); `generated`/`cache` rebuildable; media is in `leftpool/extras` | Medium | Native backup → `rightpool/backup/automated/stash` | **Partial** (schedule unverified) |
| Whisparr | `/config` | Medium | Native backup → `rightpool/backup/automated/whisparr` | **Partial** (schedule unverified) |
| Namer | `/config` (`namer.cfg`) | Low | Repo holds a placeholder copy; the real token is host-only → secrets copy | **Gap** (low) |
| `leftpool/extras` | Extras library | Medium | Snapshots; Cloud Sync is a cost decision | **Partial** (unverified) |

### Nextcloud and household

| Item | What to protect | Priority | How | Status |
|---|---|---|---|---|
| Nextcloud data (`leftpool/nextcloud`) | Personal files, photos, documents | Critical | Snapshots + Cloud Sync with versioning | **Partial** — Cloud Sync scope unverified |
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
| MariaDB | `digikam`, `nextcloud`, `onlyoffice` (`spotweb` excluded, see above) | Critical | Dagu `mariadb-backup`, 04:00 daily, gzip dumps to `rightpool/backup/automated/mariadb`, 7-day retention | **Done** — offsite only if Cloud Sync covers `rightpool/backup` |
| PostgreSQL | `mealie`, `vikunja` (+ `bitmagnet`, rebuildable) | High | Dagu `postgres-backup`, 04:30 daily, `pg_dump -Fc` to `rightpool/backup/automated/postgres`, 7-day retention | **Partial** — committed 2026-10-07, deployment unverified |
| Dagu | DAG files and scripts; run history/state | Low | Git for definitions; history is disposable | **Done** |
| Grafana (`processes/grafana/data`) | `grafana.db`: users, UI-managed contact points and notification policy, API keys | Medium | Copy `grafana.db` (SQLite `.backup`) → `rightpool/backup/automated/grafana`. Provisioned dashboards/rules/datasources are in git | **Gap** — contact points/policy exist only here |
| Prometheus, Loki, Promtail positions | Time-series and log data | — | Disposable; config in git | **N/A** |
| Exporters (cAdvisor, node/blackbox/snmp/smartctl/gpu, redis/mysqld/postgres, exportarr, per-app) | Stateless | — | Config in git (`mysqld-exporter.cnf` is a secret → secrets copy) | **N/A** |
| Redis, RabbitMQ (`data`, `log`) | Cache/sessions, transient queues | — | Disposable; `rabbitmq.conf` / `enabled_plugins` are tiny and host-only | **N/A** |
| Syncthing (`processes/syncthing`) | Device identity (keys, certs), folder config | Medium | Copy `/config` → `rightpool/backup/automated/syncthing` (losing the keys means re-pairing every device) | **Gap** |
| Tdarr (`server`, `configs`, flows, custom plugins) | Library DB, flows, node config | Low–Medium | Copy `server`/`configs`; `temp`/`cache`/`logs` excluded; local flow plugins belong in git | **Gap** (low) |
| dupeGuru, chromedp, phpMyAdmin, Watchtower, whoami | Stateless / trivial config | — | — | **N/A** |

### Highest-value gaps, in order

1. **Cloud Sync / snapshot scope** — confirm in the TrueNAS UI that `leftpool` (Nextcloud,
   phones, pictures, home videos) and `rightpool/backup/automated` are covered, and whether
   B2 keeps versions. This decides the status of ~15 rows above.
2. **Portainer DB, secrets dir, step-ca, TrueNAS config export** — small, Critical, and a
   rebuild from git alone is impossible without them. One scheduled "config harvest"
   copying these into `rightpool/backup/automated/` (a Dagu DAG fits the existing pattern)
   puts them on the same path to B2 as the DB dumps.
3. **Activate the Amanda NAS leg** (fix the `/mnt/backup` NFS mount) and add an InfluxDB backup.
4. **OpenWrt export**, plus Grafana `grafana.db`, Seerr, Syncthing and Navidrome.
5. **Restore test** — nothing here counts as proven until a file has been restored from a
   snapshot, from B2, and with `amrecover`.
