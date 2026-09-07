#!/bin/bash
# /home/openhabian/acme-renew.sh
#
# Usage:
#   acme-renew.sh           normal renewal via `acme.sh --cron`; on failure, one
#                           explicit full-SAN re-issue from DOMAINS below.
#   acme-renew.sh --force   skip --cron and force a full re-issue against DOMAINS.
#                           Run once after editing DOMAINS (e.g. adding a subdomain).
set -uo pipefail

ACME="/home/openhabian/.acme.sh/acme.sh"
CONFIG_HOME="/home/openhabian/.acme.sh"
SERVER="https://step-ca.networking.bellecerise.local:8999/acme/acme/directory"
CA_BUNDLE="/usr/local/share/ca-certificates/step-ca.crt"

CADDY_CERT="/etc/caddy/certs/openhab.crt"
CADDY_KEY="/etc/caddy/certs/openhab.key"
CADDY_RELOAD="chown caddy:caddy /etc/caddy/certs/openhab.* && systemctl reload-or-restart caddy"

# First entry is the cert's "main domain": acme.sh keys the cert on it and names
# its state dir after it. Keep it first and never change it.
DOMAINS=(
    "openhabian.bellecerise.local"
    "grafana.openhabian.bellecerise.local"
    "influx.openhabian.bellecerise.local"
    "openhabian"
    "192.168.1.154"
)

FORCE_ISSUE=0
case "${1:-}" in
    -f|--force|force) FORCE_ISSUE=1 ;;
    "")               ;;
    *) echo "Unknown argument: $1" >&2; echo "Usage: $0 [--force]" >&2; exit 2 ;;
esac

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

d_args=()
for d in "${DOMAINS[@]}"; do d_args+=(-d "$d"); done

issue_full() {
    "$ACME" --issue "${d_args[@]}" \
        --standalone --httpport 80 \
        --home "$CONFIG_HOME" --config-home "$CONFIG_HOME" \
        --server "$SERVER" --ca-bundle "$CA_BUNDLE" --force
}

install_cert() {
    "$ACME" --install-cert -d "${DOMAINS[0]}" \
        --home "$CONFIG_HOME" --config-home "$CONFIG_HOME" \
        --fullchain-file "$CADDY_CERT" \
        --key-file "$CADDY_KEY" \
        --reloadcmd "$CADDY_RELOAD"
}

log "Stopping Caddy to free port 80..."
systemctl stop caddy

RESULT=1
if [ "$FORCE_ISSUE" -eq 1 ]; then
    log "FORCE mode: full-SAN re-issue for [${DOMAINS[*]}] ..."
    issue_full
    RESULT=$?
else
    log "Attempt 1: standard --cron renewal (SAN set as recorded)..."
    "$ACME" --cron --config-home "$CONFIG_HOME" --home "$CONFIG_HOME" --force
    RESULT=$?
    if [ $RESULT -ne 0 ]; then
        log "Attempt 1 failed (exit $RESULT). Attempt 2: explicit full-SAN re-issue..."
        issue_full
        RESULT=$?
    fi
fi

if [ $RESULT -eq 0 ]; then
    log "Installing cert to $CADDY_CERT ..."
    install_cert || log "WARN: install-cert step failed (exit $?)"
else
    log "ERROR: renewal failed (exit $RESULT). Certificate at renewal risk — investigate."
fi

log "Restarting Caddy..."
systemctl start caddy
log "Done (exit $RESULT)."
exit $RESULT
