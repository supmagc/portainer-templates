# Multimedia

The media-library stacks and how their apps interlock. Related docs:
[multimedia-monitoring.md](multimedia-monitoring.md) (exporters and dashboard wiring),
[bitmagnet.md](bitmagnet.md) (classifier), [gpu.md](gpu.md) (shared NVENC GPU),
[multimedia-library-layout.md](multimedia-library-layout.md) (on-disk layout and the
`library-cleanup` DAG).

## Stacks

| Stack | Services |
|---|---|
| `multimedia` | Emby, Jellyfin, Sonarr, Radarr, Lidarr, Bazarr, Organizr, Seerr, Navidrome |
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
