# Network

The OpenWrt router's zone/firewall layout, the SNMP bring-up chain, and the openHABian
reverse proxy plus its step-ca / acme.sh certificate. Read this before touching firewall
rules, the SNMP jobs, or the Pi's Caddy config.

## VLANs and fixed addresses

The OpenWrt router terminates one interface per VLAN; its address on each is `.1`.

| VLAN | Zone | Subnet | Router IP | Holds |
|---|---|---|---|---|
| 0 | `admin` | 192.168.0.0/24 | 192.168.0.1 | switch, APs, admin desktop |
| 1 | `lan` | 192.168.1.0/24 | 192.168.1.1 | NAS, openHABian Pi, openHAB field-bus devices |
| 2 | `guest` | 192.168.2.0/24 | 192.168.2.1 | isolated — no `lan`→`guest` forward |
| 3 | `work` | 192.168.3.0/24 | 192.168.3.1 | isolated — no `lan`→`work` forward |
| — | `iot` | — | — | zone defined but no interface attached (forwarding rules are no-ops) |
| — | `vpn` | — | — | WireGuard |
| — | `wan` | — | — | PPPoE uplink |

> The VLAN numbers above are the user's labels. Earlier notes recorded `admin` as 802.1Q
> **VLAN 99** — confirm the real tag vs. the label before relying on either. Confirmed
> 2026-09-15: `guest`/`work`'s real tags are **20**/**30** (from the SQM config's
> `br-lan.20`/`br-lan.30` interfaces), not the `2`/`3` shorthand used above — same gap.

Fixed device addresses:

| Device | IP | VLAN | Notes |
|---|---|---|---|
| OpenWrt router | 192.168.0.1 / .1.1 / .2.1 / .3.1 | admin / lan / guest / work | one IP per VLAN interface |
| NAS (`nas`) — main | 192.168.1.101 | lan | TrueNAS SCALE host |
| NAS — docker | 192.168.1.110 | lan | container / Traefik (internal) services |
| NAS — nfs | 192.168.1.111 | lan | NFS exports |
| NAS — edge | 192.168.1.112 | lan | Traefik-Edge (WAN-facing) |
| JetStream switch | 192.168.0.2 | admin | TP-Link T1600G-28PS (SNMP: `if_mib`) |
| EAP1 | 192.168.0.3 | admin | TP-Link EAP AP (SNMP: `if_mib`) |
| EAP2 | 192.168.0.4 | admin | TP-Link EAP AP (SNMP: `if_mib`) |
| EAP3 | 192.168.0.5 | admin | TP-Link EAP AP (SNMP: `if_mib`) |
| openHABian Pi | 192.168.1.154 | lan | openHAB host (also `openhabian.bellecerise.local`) |
| Wago Modbus | 192.168.1.150 | lan | openHAB field bus — Modbus/TCP |
| Entec DMX | 192.168.1.151 | lan | openHAB field bus — DMX lighting gateway |
| OneWire | 192.168.1.153 | lan | openHAB field bus — 1-Wire gateway |

All of these are the `blackbox-icmp` target list — see
[monitoring.md](monitoring.md#icmp-reachability-probes-network-hosts).

## Network/firewall topology (OpenWrt router)

Zones (subnets and per-VLAN router IPs are in the table above): `wan`, `lan` (NAS lives
here), `guest`, `work`, `admin`, `iot`, `vpn`. **`admin` is not purely
network-gear** — it also carries the user's administration desktop, which is why some
`lan`→`admin` forwards exist for desktop-specific apps (Phone Link, LocalSend) alongside
the switch/AP-oriented ones (like the SNMP rule below). When a rule in `admin` looks odd
for "just switches/APs," consider the desktop before assuming it's dead weight.

Open items from the last firewall review (not yet acted on): an `iot` zone with no
interface actually attached to it (its forwarding rules are currently no-ops); Emby/Seerr
WAN DNAT rules that may be redundant now that Traefik-Edge fronts 80/443 with real TLS;
and four guest/work DNS rules that are disabled and point at resolvers whose reachability
hasn't been confirmed.

Resolved since that review: the `Allow-NTP-from-LAN` rule's `src 'wan'` (was a copy/paste
bug exposing router NTP to the internet) has been corrected to `lan`.

**Monitoring ICMP allows:** the `blackbox-icmp` job (see
[monitoring.md](monitoring.md#icmp-reachability-probes-network-hosts)) pings infra hosts
from the NAS on `lan`. The `lan`→`admin` **ICMP** allow needed for the `admin` VLAN
targets (router `192.168.0.1`, switch, APs) has been added — the SNMP rule only permitted
UDP/161. The `guest` / `work` router IPs (`192.168.2.1` / `192.168.3.1`) still have no
`lan`→`*` forward; decide per target whether it's worth a rule or should be dropped from
the job.

## SQM (cake) — WAN and guest/work shaping

`/etc/config/sqm` on the router (not tracked in this repo — router-local config) runs
three `cake` queues:

- **`eth1`/`wan`**: the actual internet-facing queue. The physical path is
  **VDSL2 → Fritzbox (modem mode, does the DSL/ATM work) → OpenWrt `eth1` over plain
  Ethernet → PPPoE**. Because the Fritzbox absorbs the DSL/ATM layer, the router never
  sees ATM framing — `linklayer` must be `ethernet` (not `atm`), with `overhead 34` (the
  standard PPPoE-over-VDSL2 value). An earlier config had `linklayer atm` / `overhead 0`,
  which is internally inconsistent (ATM cell-quantization with no accounted overhead) and
  wrong for this modem setup regardless — fixed 2026-09-15.
- **`br-lan.20` (`guest`) and `br-lan.30` (`work`)**: separate per-VLAN queues, not there
  for Wi-Fi bufferbloat but to cap how much `guest`/`work` can consume of the shared WAN
  link so `lan`/`admin` traffic can't be starved. On a LAN-side (non-`wan`) SQM interface
  the `download`/`upload` fields are reversed from the WAN instance: `download` throttles
  ingress-to-router (that VLAN's client *uploads*), `upload` throttles egress-from-router
  (that VLAN's client *downloads*). Current split: `guest` 6000/18000 (up/down),
  `work` 18000/55000 — `work` intentionally gets a much higher ceiling than `guest`
  (confirmed by design 2026-09-15), even though at 78-85% of the WAN's own
  23000/65000 up/down it leaves little headroom if `work` actually maxes out.

## SNMP (switch/APs)

Works end-to-end now. Root causes chained through: missing `lan`→`admin` VLAN firewall
forwarding for UDP/161, then the switch itself lacking a default gateway (JetStream
T1600G-28PS: under **L3 FEATURES → Static Routing**, not System Info — that's only for the
simpler T1500 family), then finally Prometheus's default 10s scrape timeout being too
short for the switch's full interface-table walk (fixed with `scrape_interval: 60s` /
`scrape_timeout: 55s` on the `snmp-switch`/`snmp-ap` jobs).

## SQM (traffic shaping)

`/etc/config/sqm` runs `cake` on three queues, one per uplink/VLAN pair:

| Queue | Interface | Download | Upload |
|---|---|---|---|
| `eth1` | `wan` | 65000 kbit | 23000 kbit |
| (unnamed) | `br-lan.20` (`guest`) | 6000 kbit | 18000 kbit |
| (unnamed) | `br-lan.30` (`work`) | 18000 kbit | 55000 kbit |

The `wan` queue uses `linklayer 'atm'` with `overhead '0'` — worth revisiting if the WAN
link type ever changes (ATM overhead accounting is PPPoA/ADSL-era; a fiber/PPPoE-only line
wouldn't need it). SQM/cake itself was **ruled out** as a cause of the router slowdown
below — cake doesn't do connection tracking, it only shapes/queues packets already past
netfilter.

## nf_conntrack exhaustion from BitMagnet's DHT crawler (2026-09-15)

**Symptom:** internet became "excruciatingly slow" over a couple of days, never happened
before, only fixed by a router reboot. Coincided with standing up `bitmagnet` (see
[multimedia.md](multimedia.md#bitmagnet-classifier)).

**Root cause:** `bitmagnet`'s entire function is crawling the mainline DHT network — it
opens far more short-lived UDP sessions to random internet peers than a normal torrent
client's incidental DHT participation. Its DHT port (`3334/udp`) is NAT'd straight out
through the router's `wan`, so every one of those sessions is a `nf_conntrack` entry. This
matches a well-documented OpenWrt failure mode: DHT churn overflows
`nf_conntrack_max`, the router drops packets and degrades until reboot clears the table.
Not SQL, not SQM — both were initially suspected and ruled out.

**Diagnostics that matter here** (checked 2026-09-15, router has ~496MB RAM):

- `sysctl net.netfilter.nf_conntrack_max` / `_count` / `hashsize` — compare live count to
  max to see how close to the wall you are; `hashsize` came back numerically equal to
  `max` (`64512`/`64512`) rather than the kernel's usual 4x ratio.
- **`nf_conntrack_max`/`hashsize` are the kernel's own RAM-scaled automatic default, not
  set anywhere on this router** — confirmed absent from `/etc/modules.d/nf-conntrack`
  (loads bare `nf_conntrack`, no `hashsize=` option), `uci show firewall`/`system`, and
  every installed package (`sqm-scripts`/`luci-app-sqm` present, but SQM doesn't touch
  conntrack sizing; no `nlbwmon`/accounting package installed despite `nf_conntrack_acct=1`
  being set — that's just the stock default file, not an installed tool acting on it).
- `logread`/`dmesg` are useless for retroactively confirming "table full" — the log ring
  buffer is RAM-only and a reboot wipes it regardless of buffer size.
- `/etc/sysctl.d/11-nf-conntrack.conf` **ships from `base-files` and says so in its own
  header** (`Do not edit, changes to this file will be lost on upgrades` /
  `/etc/sysctl.conf can be used to customize sysctl settings`) — sets
  `nf_conntrack_udp_timeout=60`/`udp_timeout_stream=180` among others. Confirmed via
  `/etc/init.d/sysctl` that `/etc/sysctl.d/*.conf` is applied **before** `/etc/sysctl.conf`
  in the same boot loop, so a `sysctl.conf` override of the same keys genuinely wins — the
  header comment is accurate, not just a suggestion.
- `/etc/sysctl.d/*` is **not** in the `sysupgrade` preserve set on this router
  (`sysupgrade -l | grep sysctl.d` returns nothing) — don't put custom tuning there even
  though upstream OpenWrt supports reading that directory; on this install it's
  package-managed/ephemeral. `/etc/sysctl.conf` is the correct customization point.

**Fix applied:**

- `bitmagnet` service: `DHT_CRAWLER_SCALING_FACTOR=5` (default `10`, higher = more
  aggressive) in `compose/docker-compose-downloads.yml` — throttles crawl concurrency at
  the source. **Not yet redeployed** as of this writing.
- Router `/etc/sysctl.conf`: raise `nf_conntrack_max` to `131072` and shorten
  `nf_conntrack_udp_timeout`/`_udp_timeout_stream` (`30`/`120`) so DHT probe entries expire
  faster than they can accumulate. Persistence needs verifying per-device: confirm via
  `opkg files base-files | grep sysctl.conf` (auto-tracked conffile) and/or an explicit
  line in `/etc/sysupgrade.conf`, then `sysupgrade -l | grep sysctl.conf` to prove it
  before trusting the next firmware upgrade. **Not yet applied** — pending.

If the slowdown recurs after both land, next step is watching `nf_conntrack_count` live
under load (`watch -n1 'cat /proc/sys/net/netfilter/nf_conntrack_count'`) to see whether
it's climbing again — that'd point at a lower scaling factor still, or routing bitmagnet's
DHT egress through a VPN container instead of the home WAN directly.

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
