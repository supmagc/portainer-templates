#!/bin/sh
set -eu

VENV=/data/venv

if [ ! -x "$VENV/bin/python" ]; then
  python -m venv "$VENV"
fi

if ! "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir -U -r /app/requirements.txt; then
  "$VENV/bin/python" -c "import mem0, fastmcp" || exit 1
  echo "start.sh: pip install failed, starting with the already installed packages" >&2
fi

"$VENV/bin/pip" list --disable-pip-version-check 2>/dev/null | grep -iE '^(mem0ai|fastmcp|fastmcp-slim) ' || true

exec "$VENV/bin/python" /app/server.py
