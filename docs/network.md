# Network

The OpenWrt router's zone/firewall layout, the SNMP bring-up chain, and the openHABian
reverse proxy plus its step-ca / acme.sh certificate. Read this before touching firewall
rules, the SNMP jobs, or the Pi's Caddy config.

## Network/firewall topology (OpenWrt router)

Zones: `wan`, `lan` (192.168.1.0/24 — NAS lives here), `guest`, `work`,
`admin` (VLAN99, 192.168.0.0/24), `iot`, `vpn` (WireGuard). **`admin` is not purely
network-gear** — it also carries the user's administration desktop, which is why some
`lan`→`admin` forwards exist for desktop-specific apps (Phone Link, LocalSend) alongside
the switch/AP-oriented ones (like the SNMP rule below). When a rule in `admin` looks odd
for "just switches/APs," consider the desktop before assuming it's dead weight.

Open items from the last firewall review (not yet acted on): an `Allow-NTP-from-LAN` rule
with `src 'wan'` instead of `lan` (likely a copy/paste bug exposing router NTP to the
whole internet); an `iot` zone with no interface actually attached to it (its forwarding
rules are currently no-ops); Emby/Seerr WAN DNAT rules that may be redundant now that
Traefik-Edge fronts 80/443 with real TLS; and four guest/work DNS rules that are disabled
and point at resolvers whose reachability hasn't been confirmed.

## SNMP (switch/APs)

Works end-to-end now. Root causes chained through: missing `lan`→`admin` VLAN firewall
forwarding for UDP/161, then the switch itself lacking a default gateway (JetStream
T1600G-28PS: under **L3 FEATURES → Static Routing**, not System Info — that's only for the
simpler T1500 family), then finally Prometheus's default 10s scrape timeout being too
short for the switch's full interface-table walk (fixed with `scrape_interval: 60s` /
`scrape_timeout: 55s` on the `snmp-switch`/`snmp-ap` jobs).

## openHABian reverse proxy (Caddy) + its cert

The Pi runs **Caddy** (not Docker, not Traefik — Traefik only fronts the NAS stack),
config tracked at `hosts/openhabian/etc/caddy/Caddyfile`. As of this session it serves
three vhosts, all sharing one cert:

- `openhabian.bellecerise.local` (+ `openhabian`, `192.168.1.154`) → openHAB on `:8080`
- `grafana.openhabian.bellecerise.local` → the bundled Grafana on `:3000`
- `influx.openhabian.bellecerise.local` → the bundled InfluxDB 1.x on `:8086`

The subdomains resolve via a single OpenWrt dnsmasq line
`address=/openhabian.bellecerise.local/192.168.1.154`, which matches the parent name and
**every sub-label** of it, so new `*.openhabian.bellecerise.local` names need no further
DNS edits. Grafana additionally needs `[server] domain` / `root_url` set to its subdomain
in `/etc/grafana/grafana.ini`. InfluxDB's HTTP API is unauthenticated on a stock
openHABian install — the `influx.` vhost exposes it to anything that reaches the Pi on
443; add auth in Caddy if that matters.

**The cert** (`/etc/caddy/certs/openhab.{crt,key}`) is issued by `acme.sh` via **HTTP-01,
`--standalone`** against the NAS step-ca's ACME directory
(`https://step-ca.networking.bellecerise.local:8999/acme/acme/directory`), renewed nightly
by `/home/openhabian/acme-renew.sh` (root cron, `0 3 * * *`). Hard-won details:

- The script **stops Caddy** for the run so acme.sh's standalone server can bind `:80`;
  step-ca then connects back to `http://<name>:80/.well-known/acme-challenge/...` for
  each identifier. If Caddy isn't actually stopped (e.g. an interactive run that fails a
  polkit prompt), validation fails with
  `urn:ietf:params:acme:error:connection "could not connect to validation target"` and
  the order sits `pending` until it times out.
- step-ca **does** issue for the bare `openhabian` short-name and the `192.168.1.154` IP
  identifier — both validate over HTTP-01 the same way (confirmed this session).
- `acme.sh --cron` renews against each cert's **recorded** SAN set; a plain
  `acme.sh --issue -d ... -d ...` is what *changes* that set. So after editing the domain
  list you must run one explicit `--issue` (the script's `--force` flag does exactly
  this) before `--cron` will carry the new names forward.
- `acme.sh --cron`/`--renew` auto-run the saved deploy hook (copy fullchain+key to
  `/etc/caddy/certs/`, `chown caddy:caddy`, `systemctl reload-or-restart caddy`); a plain
  `--issue` does **not** — the reworked script calls `--install-cert` explicitly after a
  forced issue to cover that path.
- **Don't run `sudo acme.sh ...` directly** — acme.sh's sudo guard aborts before doing
  anything. Invoke it *from* the script (its `SUDO_COMMAND` is then the script, not
  acme.sh) or from a root login shell (`sudo su -`).
- `acme-renew.sh` logs to `/var/log/acme-renew.log` with a `logrotate.d/acme-renew` rule
  (monthly, keep 6, compressed).

There is a Grafana alert (`CertExpiringSoon`, in `rules.yml`) whose most load-bearing job
is watching these three subdomains' shared cert, since nothing else notices a silent
renewal failure.
