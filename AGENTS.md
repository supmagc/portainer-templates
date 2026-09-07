# Working in this repo as an AI agent

Conventions for Claude (or any other agent) picking this repo back up. This is about
*how* to work here — see [README.md](README.md) for what the repo contains and
[docs/CONTEXT.md](docs/CONTEXT.md) (and its per-topic files) for why it's shaped the way
it is.

## Environment

- Dev machine is **Windows**; PowerShell is the primary shell, the Bash tool (Git Bash /
  POSIX) is also available — pick whichever fits the command, don't assume Unix-only
  syntax works in PowerShell or vice versa.
- The actual target is a **TrueNAS SCALE box** (host `nas`) plus a couple of other
  network hosts (currently `openhabian`, a native Raspberry Pi). You cannot reach them
  directly — all host-side work happens via `scripts/deploy.ps1` (scp) plus asking the
  user to run/restart things, or via the Grafana MCP server for live read access to the
  running Prometheus/Grafana instance.
- A **Grafana MCP server** is configured (`.mcp.json`, git-ignored — it holds a real
  service-account token) and connected. Use it to query live Prometheus/Loki data,
  inspect dashboards, and check alert rules *before* writing PromQL or dashboard JSON —
  this caught real bugs (see docs/monitoring.md's `job`/`exported_job` collision) that would have
  been invisible from source alone.

## The core repo convention

`hosts/<HOST>/<absolute-destination-path>` mirrors each host's real filesystem exactly.
There is no separate mapping table — when adding a new tracked file, its path under
`hosts/<HOST>/` *is* its eventual path on that host, so just place it correctly the first
time. Deploy with `scripts/deploy.ps1` (interactive, or `-DeployHost`/`-DeployPath` for a
one-shot). The script only copies files; it never restarts or reloads a service —
say so explicitly when telling the user a change needs one.

## Secret handling — non-negotiable

- **Never commit secrets.** No tokens, passwords, API keys, or connection strings under
  `hosts/`. If a config file needs a secret to function, either template it with a
  placeholder the host fills in, or keep the whole file host-only.
- **Never persist a secret the user pastes into chat**, into a file, memory, or anywhere
  else — flag it immediately instead (e.g. "that TrueNAS output includes a live B2 key —
  I won't write it anywhere"). This has come up for real: a pasted `midclt cloudsync.query`
  response contained a live Backblaze B2 credential; it was flagged and never touched by
  the parser scripts that consume that same command's output.
- **`scratchpad/`** is git-ignored — that's where secrets, host-only files, and anything
  under manual/UI-only control (e.g. Grafana's contact point / notification policy) live
  or get drafted before a secret-free version graduates to `hosts/`. `.mcp.json` and
  `.deploy-state.json` are also git-ignored for the same reason (real tokens, local state).
- Before writing a script that touches TrueNAS's `midclt` output or any similar API that
  returns credentials mixed in with other data, read the *whole* response shape first and
  design the parser to explicitly avoid the credential-bearing fields (see
  `cloudsync-tasks-metrics.sh` for the pattern and its header comment).

## Verify before writing

Don't guess Docker image names/tags, exporter config schemas, or PromQL label names from
memory or pattern-matching — confirm them first, via web search for anything external
(image tags, an app's actual metrics/API surface) or a live Grafana MCP query
(`query_prometheus`, `list_prometheus_label_values`, `list_prometheus_metric_names`) for
anything about this specific running stack. Multiple earlier guesses in this project were
wrong and caught only this way — treat verification as the default step, not a fallback
for when something looks unusual.

## Dashboard/table-panel pattern

When a table panel combines more than one Prometheus query into one row per entity, use
Grafana's `joinByField` transform with an explicit `byField` key — not `merge`, which
breaks silently on asymmetric label sets (see docs/monitoring.md for the specific bugs this
caused). Always exclude the `__name__` field(s) `joinByField` exposes. Watch for any label
literally named `job` on a custom/textfile-collector metric — it collides with
Prometheus's own scrape-config meta-label and gets silently renamed to `exported_job`.

## Git

- Current branch is `master`. Branch before committing unless the user says otherwise;
  don't push unless asked.
- Commit messages / PR bodies: follow whatever the harness's own instructions say for
  attribution trailers — don't hand-roll a different convention.

## Scope discipline

This project has a habit of surfacing latent issues in adjacent code while fixing
something else (e.g. noticing other dashboards use the same `merge` pattern that was just
fixed elsewhere). The established pattern is: **only act on what's explicitly asked**;
mention a spotted-but-out-of-scope issue in passing if it's cheap to flag, but don't fix
it unprompted. If in doubt about scope, ask rather than expanding the change.
