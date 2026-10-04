#!/usr/bin/env python3
# Polls Traefik's own API for its current routers and writes Prometheus
# file_sd JSON, so blackbox-exporter always probes whatever Traefik is
# actually routing instead of a hand-maintained target list. Run every 15m
# by the Dagu traefik-endpoints DAG. See docs/monitoring.md
# "Traefik-endpoint discovery for blackbox".
#
# Deliberately stdlib-only (python:3-alpine has no curl/jq preinstalled).

import json
import os
import re
import urllib.request
from urllib.error import URLError

OUT_DIR = "/file_sd"
# root inside the container, but prometheus reads this dir as nas_processes
# (see docker-compose-monitoring.yml) - chown back like mariadb-backup.sh does
OUTPUT_UID = int(os.environ.get("OUTPUT_UID", 940))
OUTPUT_GID = int(os.environ.get("OUTPUT_GID", 940))
SOURCES = {
    "traefik": "http://traefik:8080/api/http/routers",
    "traefik-edge": "http://traefik-edge:8080/api/http/routers",
}
# router name -> https probe path, for backends that 404 on "/"
PROBE_PATHS = {"loki-secure": "/ready", "step-ca": "/health"}
HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")


def fetch_routers(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: could not fetch {url}: {exc}")
        return []


def write(name, data):
    path = f"{OUT_DIR}/{name}"
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chown(tmp, OUTPUT_UID, OUTPUT_GID)
    os.replace(tmp, path)


http_targets = []
https_targets = []
tls_cert_targets = []
seen = set()

for source, url in SOURCES.items():
    for router in fetch_routers(url):
        if router.get("status") != "enabled":
            continue
        if router.get("provider") == "internal":
            # traefik's own built-in api/dashboard/ping routers, not a real endpoint
            continue
        router_name = router.get("name", "").split("@")[0]
        has_tls = router.get("tls") is not None
        for host in HOST_RULE_RE.findall(router.get("rule", "")):
            # keep has_tls in the key - dropping it lets a redirect router shadow the real TLS one
            key = (source, host, has_tls)
            if key in seen:
                continue
            seen.add(key)
            labels = {"source": source, "router": router_name}
            if has_tls:
                path = PROBE_PATHS.get(router_name, "/")
                https_targets.append({"targets": [f"https://{host}{path}"], "labels": labels})
                tls_cert_targets.append({"targets": [f"{host}:443"], "labels": labels})
            else:
                http_targets.append({"targets": [f"http://{host}/"], "labels": labels})

write("traefik-http.json", http_targets)
write("traefik-https.json", https_targets)
write("traefik-tls-cert.json", tls_cert_targets)
