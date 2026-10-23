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
| `downloads` | SABnzbd (+ cleanup sidecar), qBittorrent, Prowlarr, Spotweb, BitMagnet |
| `extras` | stash, namer, whisparr |

tdarr runs in the `utilities` stack, not `multimedia`.

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
