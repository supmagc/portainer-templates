# hosts/

Version-controlled host config files, deployed with `../scripts/deploy.ps1`.

Each top-level directory is a host name, discovered from `~\.ssh\config` (see
`scripts/deploy.ps1` for that convention). Everything under
`hosts/<HOST>/` mirrors that host's real destination filesystem exactly —
`hosts/nas/mnt/fastpool/system/processes/grafana/...` deploys to
`nas:/mnt/fastpool/system/processes/grafana/...` — so there's no separate
mapping to maintain, just add files at the path they belong on the host.

**Only put files here that are safe to commit to git.** No tokens, passwords,
API keys, or other secrets — and nothing under manual/UI control elsewhere
(e.g. Grafana's alert contact points hold a real Pushover token and its
notification policy is currently managed by hand in the Grafana UI; both stay
host-only under `scratchpad/host-configs/`, never here).
