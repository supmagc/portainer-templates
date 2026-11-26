# Multimedia

The media-library stacks and how their apps interlock. Related docs:
[multimedia-monitoring.md](multimedia-monitoring.md) (exporters and dashboard wiring),
[bitmagnet.md](bitmagnet.md) (classifier), [gpu.md](gpu.md) (shared NVENC GPU),
[multimedia-library-layout.md](multimedia-library-layout.md) (on-disk layout and the
`library-cleanup` DAG).

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Jellyfin, Meilisearch, Streamystats (+ jobs), Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr, Navidrome |
| `downloads` | SABnzbd (+ cleanup sidecar), qBittorrent, Cleanuparr, Prowlarr, Spotweb, BitMagnet |
| `extras` | stash, namer, whisparr |

tdarr runs in the `utilities` stack, not `multimedia`.

## qBittorrent: embedded tracker vs. listen port (2026-11-12)

The embedded tracker (`TrackerEnabled`, `Advanced\trackerPort`) was enabled on **6881**,
the same port as the BitTorrent listen port (`Session\Port`). Whichever bound first on
startup won. When the tracker won, libtorrent logged `Failed to listen on IP ...
TCP/6881 Reason: "Address in use"` and dropped the whole listen socket, TCP *and* UDP.
The result was `connection_status: disconnected`, 0 DHT nodes and 0 peers for every
torrent until the next restart. `qbittorrent_connected` showed it connected only ~4.5 of
15 days, flipping only at container restarts. Tell-tale signs: `Http::RequestParser ...
header parsing error` in `docker logs qbittorrent` (peer handshakes hitting the tracker's
HTTP server), and no UDP socket in `docker exec qbittorrent netstat -lntu`. Fixed by
disabling the embedded tracker. Turning it off doesn't make the session bind the port
again; that needs a container restart.

The router has one `wan` DNAT forward for qBittorrent, **6881 tcp+udp** → `192.168.1.110`
(renamed from the misleading "qBittorrent Tracking"). An older 54907 forward was stale and
removed. The IP filter is refreshed daily by the `qbittorrent-blocklist` Dagu job; see
[dagu.md](dagu.md#qbittorrent-blocklist).

## Cleanuparr (stalled-download cleanup)

Added to the `downloads` stack on 2026-11-25. All configuration lives in its web UI
(`cleanuparr.<domain>`, SQLite under `/config`); nothing is templated in this repo, so
redo these steps by hand after a config loss.

1. **Account:** create the admin account on first visit.
2. **Download client:** qBittorrent at `http://qbittorrent:8282`. Both containers see
   downloads at `/downloads`, so leave *Download Directory Source/Target* (path mapping)
   empty.
3. **\*arrs:** `http://sonarr:8989`, `http://radarr:7878`, `http://lidarr:8686`,
   `http://whisparr:6969`, each with its API key. They all join `downloads_default`, which
   is how Cleanuparr reaches them.
4. **Queue Cleaner:** enable the stalled, slow and metadata-stuck checks with strike
   limits. A download is removed only after it fails the check that many times in a row,
   and Cleanuparr then asks the *arr to search for a replacement.
5. **Download Cleaner / Orphaned Files:** optional. Scan directories must be under
   `/downloads` (the only mount). Leave *Purge Orphaned After* empty until you've reviewed
   what lands in the orphaned directory.

Scope: the Queue Cleaner only acts on downloads in an *arr queue. Torrents grabbed by hand
through Prowlarr (category `prowlarr`) are deliberately left alone; they're managed
manually.

## Seerr and the Jellyfin plugins

Seerr supports exactly **one** media server. It was switched from Emby to Jellyfin on
2026-10-01 so the Jellyfin home-screen plugins can use it; Emby accounts can no longer
sign in to Seerr.

- **`JELLYFIN_TYPE=emby` must stay out of the compose file.** The media server type lives
  in Seerr's `settings.json` (`main.mediaServerType`: 2 = Jellyfin, 3 = Emby, 4 = not
  configured), but Seerr re-runs its settings migrations on every start, and one of them
  flips Jellyfin back to Emby whenever that env var is set.
- **Switching servers** has no UI. Stop Seerr, back up its config dir, set
  `main.initialized=false`, `main.mediaServerType=4`, and clear `jellyfin.ip`,
  `jellyfin.apiKey` and `jellyfin.libraries` in `settings.json`. On the next start the
  setup wizard re-links the owner account (user 1) to whichever admin signs in, keeping
  requests and the Sonarr/Radarr config. Then run *Jellyfin Full Library Scan* so stored
  item IDs are rewritten for the new server. (Derived from Seerr's source, not a
  documented procedure.)
- **Jellyfin Enhanced** matches Seerr users by **Jellyfin user ID**. With no match, every
  Seerr feature it has fails (search results, requests, the info popup it opens when a
  Discover card is clicked), and it shows a "log in to Seerr" banner. Turning its Seerr
  integration off doesn't stop it intercepting Discover card clicks.
- **Home Screen Sections** matches Seerr users by **username, case-sensitively**
  (`jellyfinUsername` must equal the Jellyfin username exactly). A mismatch fails silently:
  the Seerr rows are just empty, with nothing in the log. Its Seerr URL needs the port
  (`http://seerr:5055`); without it the plugin connects on port 80 and logs
  `Connection refused (seerr:80)`. Its *My Requests* section throws
  `Unrecognized Guid format` while Seerr holds another server's item IDs.

## Music (Navidrome only)

Jellyfin has no music library (removed 2026-10-23): all listening goes through Navidrome
(Symfonium, Feishin), so there is no favourites/ratings/play-count sync to maintain.
Navidrome mounts the music `:ro` and never writes; it reads tags plus files other apps write.

- **Lidarr writes the sidecars** (Settings → Metadata → Kodi (XBMC) / Emby, all four boxes
  on): `artist.nfo`, `album.nfo`, artist `folder.jpg`/`fanart.jpg`/`banner`/`logo`, album
  `folder.jpg`/`discart`. Images are skipped if the file exists; `.nfo` files are
  **overwritten** even when another app wrote them, so keep Lidarr the only nfo writer.
- **`artist-nfo-metadata` plugin** reads only `<Artist>/artist.nfo` (folder name must equal
  the artist name, case-insensitive): `<biography>` and `<musicbrainzartistid>`. `<thumb>` is
  used only if it's an http(s) URL, which neither Lidarr nor Jellyfin/Emby write. It never
  reads `album.nfo`. Needs the `library` permission on Navidrome's Plugins page; misses log
  at debug level only.
- **Artist images:** `ND_ARTISTARTPRIORITY` puts Lidarr's `folder.*` before the external
  agents (Apple Music). Album covers use the default `cover.*, folder.*, front.*, embedded,
  external`; `discart.*` matches the default disc-art `disc*.*`. Navidrome 0.64 stores
  resolved artwork, so an art-priority change only applies after
  `docker exec navidrome navidrome artwork reprocess --all`.

## Jellyfin search (Meilisearch plugin)

Jellyfin 12 has an external search-provider hook, so search is improved server-side by the
[Meilisearch plugin](https://github.com/Shadowghost/jellyfin-plugin-meilisearch) and every
client (Streamyfin, Pelagica, Wholphin, web) gets it with no proxy routing. JellySearch is
archived and was the pre-12 reverse-proxy workaround; don't use it.

- Plugin repo (unstable feed only): `https://raw.githubusercontent.com/Shadowghost/jellyfin-plugin-meilisearch/metadata/unstable/manifest.json`.
  Plugin settings: URL `http://meilisearch:7700`, API key = the master key, then run
  *Rebuild Meilisearch Index*.
- The `meilisearch` service has no `MEILI_MASTER_KEY_FILE` support, so the compose `command`
  exports the key from the Docker secret. It carries no Watchtower label: a minor-version
  bump can refuse to open the old index (rebuildable, but not unattended).
- BusyBox `wget` resolves `localhost` to `::1` and doesn't fall back to IPv4, so the
  healthcheck uses `127.0.0.1`.
- Tunarr runs its own bundled Meilisearch (hardcoded `localhost`, no key, no remote option);
  it can't share this instance.
- **Semantic search is deliberately off.** The plugin runs Qwen3-Embedding-0.6B *inside the
  Jellyfin process* (ONNX, ~610 MB download, 1-2 GB RAM while loaded) and registers a
  `userProvided` embedder, so Meilisearch never embeds anything and the plugin sends no
  `hybrid` parameter without its own vector. Enabling it means raising Jellyfin's 2G limit.
  There is no setting to use an external embedding endpoint.

## Streamystats

Watch stats and embedding-based recommendations; Streamyfin and Pelagica integrate with it.
Two containers (`streamystats` web/API, `streamystats-jobs` sync + migrations) on the shared
`utilities` Postgres, which is the Immich image with pgvector and VectorChord, so upstream's
own `vchord-postgres` container isn't needed. No volumes: all state is in the database.

- **Database (once, as superuser):** create role + database `streamystats` and, in that
  database, `CREATE EXTENSION vector; CREATE EXTENSION "uuid-ossp"; CREATE EXTENSION pg_trgm;`.
  Upstream's migrator tries `vector` and `uuid-ossp` itself but only warns if it lacks the
  rights, so pre-creating them avoids a half-migrated start.
- **Secret file** `/mnt/fastpool/system/processes/secrets/streamystats-env.txt`, world-readable
  (the images run as their own users). Upstream has no `_FILE` support, so both services
  source it in their `command`:

  ```
  DATABASE_URL=postgresql://streamystats:<hex password>@postgres:5432/streamystats
  SESSION_SECRET=<openssl rand -base64 32>
  INTERNAL_API_KEY=<same value as SESSION_SECRET, or another random>
  NEXT_SERVER_ACTIONS_ENCRYPTION_KEY=<openssl rand -hex 16>
  ```

  Keep the password hex so it needs no URL escaping, and quote nothing: it's `source`d by `sh`.
- Embeddings are off until a provider is configured in the Streamystats UI (any
  OpenAI-compatible endpoint). Not configured, by choice: no extra model service.
- Add `streamystats` to the Dagu `postgres-backup` database list.
