# Endpoint probes

Blackbox-based liveness signals: ICMP reachability for network hosts, and Traefik-endpoint
discovery feeding the HTTP/TLS probes. General monitoring conventions are in
[monitoring.md](monitoring.md).

## ICMP reachability probes (network hosts)

The goal is that *every* endpoint has a liveness signal, including hosts that run no
exporter of their own — the openHAB field-bus boxes (Wago Modbus, Entec DMX, OneWire),
the switch/APs (SNMP covers their interfaces, not "is the box up"), and each of the
router's per-VLAN interface IPs. Those get a plain ping via blackbox-exporter's `icmp`
prober.

- **`icmp` module** in `blackbox/config.yml` — `preferred_ip_protocol: ip4`,
  `ip_protocol_fallback: false`, same shape as the HTTP modules.
- **`blackbox-exporter` needs `cap_add: [NET_RAW]`** (`compose/docker-compose-monitoring.yml`).
  The `prom/blackbox-exporter` image runs as an unprivileged user and the `icmp` prober
  opens raw sockets — without the cap every ICMP probe fails with a permission error.
  Adding the cap is a **container recreate**, not a reload.
- **`blackbox-icmp` job** (`prometheus/config.yml`) — one static list, each target
  labelled `role` (`router`/`nas`/`switch`/`ap`/`host`/`field-device`) plus `name`, and
  `vlan`/`iface` where one box has several IPs (the router has four, the NAS has four).
  No new alert rule needed: the existing `ProbeFailing` rule matches any `probe_success`
  series with no job filter.

**Reachability caveat — some targets are expected down until a firewall rule is added.**
blackbox-exporter runs on the NAS, which is on `lan` (192.168.1.0/24):

- `192.168.1.x` targets are same-subnet and probe directly.
- `192.168.0.x` (admin VLAN: router, switch, APs) ride the same `lan`→`admin` path SNMP
  uses, but that rule is UDP/161-scoped — ICMP echo needs its own `lan`→`admin` allow or
  these page as down (see [network.md](network.md)).
- The `guest` / `work` router interface IPs (`192.168.2.1` / `192.168.3.1`) are in
  isolated zones with no `lan`→`*` forward at all — down until one is added. They're left
  in the target list on purpose so it's literally "all endpoints"; comment them out to
  silence.

## Traefik-endpoint discovery for blackbox

The blackbox HTTP/TLS target lists in `prometheus/config.yml` are otherwise
hand-maintained. Traefik is the source of truth for what it's actually routing instead:
`dagu/scripts/traefik-endpoints-metrics.py`, run every 15m by the Dagu
`traefik-endpoints` DAG (see [dagu.md](dagu.md)), polls
`traefik:8080/api/http/routers` and `traefik-edge:8080/api/http/routers`, pulls the
`Host(...)` rule(s) out of each enabled, non-`internal`-provider router, and writes three
Prometheus `file_sd` JSON files to a bind-mounted dir
(`/mnt/fastpool/system/processes/prometheus/file_sd`, mounted read-only into the
`prometheus` container): `traefik-http.json` / `traefik-https.json` (split by whether the
router has TLS, feeding the `blackbox-redirect` / `blackbox-https` jobs and
the `http_2xx_redirect` / `http_2xx_insecure` modules) and `traefik-tls-cert.json` (TLS
routers only, `host:443` targets, feeding `blackbox-tls-cert` +  `tls_connect` —
this is the "possibly certificates" half: `tls_connect`'s `probe_ssl_earliest_cert_expiry`
covers cert-expiry alerting for free, no separate cert-parsing metric needed). Add a
`traefik.enable=true` service to any stack and it gets probed within 15 minutes, no
Prometheus edit.

**One job per blackbox module (2026-10-04).** `params.module` is job-scoped, so there is one
blackbox job per module (`blackbox-http`, `-redirect`, `-https`, `-tls-cert`, `-icmp`); the
static targets and the file_sd targets of a module share its job, told apart by the
`source` label (`static` / `traefik` / `traefik-edge`). Previously there were separate
`blackbox-traefik-*` jobs. Keep the `tls-cert` suffix on the job name — the alert rules match
`job=~".*tls-cert"`.

**Per-router probe path (2026-10-04).** The discovery script probes `/` unless the router is
listed in `PROBE_PATHS` (currently `loki-secure` → `/ready` and `step-ca` → `/health`; both 404 on `/`). Add an entry
for any other backend with no root page rather than widening the module's `valid_status_codes`.

**step-ca's Traefik router (2026-10-04).** step-ca's port 9000 is HTTPS-only, so the router
needs `loadbalancer.server.scheme=https` + `port=9000` and Traefik needs the global
`--serversTransport.insecureSkipVerify=true` (docker labels can't define a per-service
transport; Traefik dials the container IP, which step-ca's cert doesn't cover). ACME
clients (Traefik's own resolver, `acme-renew.sh`) never use this router — they hit the
published `:8999` directly. Also: a multi-port image (rabbitmq) needs an explicit
`loadbalancer.server.port` label or Traefik may pick the wrong port and 502.

**Pruned (2026-09-21):** all six now-redundant TLS entries were dropped from the
hand-maintained `blackbox-tls-cert` list — the three `*.media.bellecerise.local` hosts
(`emby`, `nextcloud`, `seerr`, fronted by `traefik`) and the three `*.bellecerise.be`
public hosts (fronted by `traefik-edge`'s dynamic-file routers, e.g. `emby.yml`). All six
are now covered by the file_sd half of `blackbox-tls-cert` instead. `blackbox-http`'s
`emby`/`rabbitmq`/`seerr` container-DNS targets stay: those probe the app directly
(`http://emby:8096/...`), not the same thing as Traefik's own router-level probe.

The public three initially looked *uncovered* by file_sd (not just duplicated) — turned
out to be a real bug in `traefik-endpoints-metrics.py`, not a missing router. Each of
`traefik-edge`'s dynamic-file services (`emby.yml`, `nextcloud.yml`, `seerr.yml`) defines
**two** routers for the same `Host()`: a TLS one on `web-secure` and a plain redirect one
on `web`. The script deduped candidate targets on `(source, host)` alone, so whichever
router the API happened to return first for that host won — and the redirect router was
winning, silently discarding the TLS router (and its cert) for all three. Fixed by keying
the dedup on `(source, host, has_tls)` instead, so a host's TLS and non-TLS routers are
tracked independently. (`navidrome.yml` has a third, unrelated router — a `PathPrefix`
metrics-block variant — competing for the same host too; harmless since it shares the
same underlying cert, just means the `router` label picked isn't always the "main" one.)

**Known remaining duplication (by design):** every `-secure` router's cert still gets
reported by *two* jobs — `blackbox-https` (an `https://host/` probe, reachability
is the point, cert-expiry data is a free side effect) and `blackbox-tls-cert` (a
dedicated `host:443` `tls_connect` probe). `CertExpiringSoon` is scoped to
`job=~".*tls-cert"` so it only counts the dedicated one, but `ProbeFailing` (plain
`probe_success`, no job filter) still fires on both — a real Traefik outage trips two
alert instances per router instead of one. This mirrors the existing intentional
dual-probe pattern for `step-ca` (see `CertExpiringSoon`'s own comment in
`grafana/provisioning/alerting/rules.yml`) and is left alone for the same reason:
redundant confirmation via two independent probe methods, not a bug. Revisit if the
doubled alert-instance volume becomes noisy in practice.

**HTTP routers don't follow the redirect (2026-10-01).** the Traefik-discovered http targets (`blackbox-redirect`) use
`http_2xx_redirect` (`no_follow_redirects`), not `http_2xx`. Every internal `http://`
router redirects to https. When `http_2xx` followed that redirect, it failed certificate
verification against step-ca (`x509: certificate signed by unknown authority`), so about
30 targets were failing all the time. Traefik's `redirectScheme` answers a GET with a 302,
or a 301 if `permanent` is set (`pkg/middlewares/redirect/redirect.go`), and both codes are
already in that module's accepted list. Whether the https side works is
`blackbox-https`'s job.

**tls-cert jobs need two failed probes in a row (2026-10-01).** The tls-cert job scrapes
every 5m, so a single failed sample stayed the latest `probe_success` value for 5 minutes,
long enough to satisfy `ProbeFailing`'s `for: 3m`. Two short nightly blips fired this
almost every day:

- **~03:00–03:30:** openHABian's `acme-renew.sh` stops Caddy to free port 80 for the
  renewal (`connection refused` on `*.openhabian…:443`).
- **05:55:** the OpenWrt adblock list refresh restarts DNS for a few seconds (`lookup …
  on 127.0.0.11:53: server misbehaving`).

The rule now reads the tls-cert jobs through `max_over_time(…[11m])`. An 11-minute window
always spans at least two 5-minute samples, so only a failure that lasts two probes in a
row (~10+ min) alerts. The 30-second probe jobs are unchanged.

