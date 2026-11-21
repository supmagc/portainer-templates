"""Mem0 memory as an MCP server (Streamable HTTP). See docs/household-ai.md."""

import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

os.environ.setdefault("MEM0_TELEMETRY", "False")

from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.auth import StaticTokenVerifier  # noqa: E402
from fastmcp.server.dependencies import get_access_token, get_http_headers  # noqa: E402
from mem0 import Memory  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mem0-mcp")

HEADER_TRUST = "@header"
USER_HEADER = os.environ.get("MEM0_USER_HEADER", "x-user-email").lower()
WATCHED_PACKAGES = {"mem0ai", "fastmcp", "fastmcp-slim"}


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"missing required env var {name}")
    return value


def read_secret(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def load_tokens(path):
    """One `<token> <identity>` pair per line; identity `@header` trusts the user header."""
    tokens = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                sys.exit(f"{path}: expected '<token> <identity>' per line")
            token, identity = parts
            identity = identity if identity == HEADER_TRUST else identity.lower()
            tokens[token] = {"client_id": identity, "scopes": []}
    if not tokens:
        sys.exit(f"{path}: no tokens defined")
    return tokens


def build_memory():
    return Memory.from_config({
        "llm": {
            "provider": "openai",
            "config": {
                "model": env("MEM0_LLM_MODEL"),
                "api_key": read_secret(env("MEM0_LLM_API_KEY_FILE")),
                "openai_base_url": env("MEM0_LLM_BASE_URL"),
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": env("MEM0_EMBED_MODEL"),
                "api_key": "ollama",
                "openai_base_url": env("MEM0_EMBED_BASE_URL"),
            },
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": env("MEM0_PG_HOST"),
                "port": int(env("MEM0_PG_PORT", "5432")),
                "dbname": env("MEM0_PG_DB"),
                "user": env("MEM0_PG_USER"),
                "password": read_secret(env("MEM0_PG_PASSWORD_FILE")),
                "collection_name": env("MEM0_COLLECTION", "memories"),
                "embedding_model_dims": int(env("MEM0_EMBED_DIMS")),
            },
        },
        "history_db_path": os.path.join(env("MEM0_DIR"), "history.db"),
    })


memory = build_memory()
mcp = FastMCP(
    name="mem0",
    instructions=(
        "Long-term memory for the current user. Search it before answering questions that "
        "depend on the user's preferences, history or personal context. Add durable facts "
        "and preferences the user shares; don't store one-off chatter or secrets."
    ),
    auth=StaticTokenVerifier(tokens=load_tokens(env("MEM0_TOKENS_FILE"))),
)


def current_user():
    token = get_access_token()
    if token is None:
        raise PermissionError("not authenticated")
    if token.client_id != HEADER_TRUST:
        return token.client_id
    user = get_http_headers().get(USER_HEADER, "").strip().lower()
    if not user or "{{" in user:
        raise PermissionError(f"missing {USER_HEADER} header")
    return user


def owned(memory_id, user):
    item = memory.get(memory_id)
    if not item or item.get("user_id") != user:
        raise ValueError(f"memory {memory_id} not found")
    return item


def slim(item):
    keep = ("id", "memory", "score", "event", "created_at", "updated_at")
    return {k: item[k] for k in keep if item.get(k) is not None}


@mcp.tool
def add_memory(text: str) -> list[dict]:
    """Store a fact, preference or piece of context about the user. Mem0 extracts the
    durable facts and merges them with what it already knows (may add, update or delete)."""
    user = current_user()
    result = memory.add(text, user_id=user)
    return [slim(r) for r in result.get("results", [])]


@mcp.tool
def search_memories(query: str, limit: int = 10) -> list[dict]:
    """Semantic search over the user's memories, best match first."""
    user = current_user()
    result = memory.search(query, filters={"user_id": user}, top_k=max(1, min(limit, 50)))
    return [slim(r) for r in result.get("results", [])]


@mcp.tool
def list_memories(limit: int = 100) -> list[dict]:
    """List the user's stored memories."""
    user = current_user()
    result = memory.get_all(filters={"user_id": user}, top_k=max(1, min(limit, 500)))
    return [slim(r) for r in result.get("results", [])]


@mcp.tool
def update_memory(memory_id: str, text: str) -> dict:
    """Replace the text of one of the user's memories."""
    user = current_user()
    owned(memory_id, user)
    memory.update(memory_id, text=text)
    return slim(memory.get(memory_id) or {"id": memory_id})


@mcp.tool
def delete_memory(memory_id: str) -> dict:
    """Delete one of the user's memories."""
    user = current_user()
    owned(memory_id, user)
    memory.delete(memory_id)
    return {"id": memory_id, "deleted": True}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def pending_updates():
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--disable-pip-version-check",
         "--no-cache-dir", "-U", "-r", "/app/requirements.txt", "--report", "-"],
        capture_output=True, text=True, errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        log.warning("update check failed: %s", proc.stderr.strip()[-500:])
        return []
    report = json.loads(proc.stdout or "{}")
    return [
        f"{i['metadata']['name']} {i['metadata']['version']}"
        for i in report.get("install", [])
        if i["metadata"]["name"].lower().replace("_", "-") in WATCHED_PACKAGES
    ]


def update_watcher():
    tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    hour, minute = (int(x) for x in env("MEM0_UPDATE_CHECK_AT", "04:30").split(":"))
    while True:
        now = datetime.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            updates = pending_updates()
        except Exception:
            log.exception("update check crashed")
            continue
        if updates:
            log.info("updates available (%s), exiting so the container restarts and reinstalls", ", ".join(updates))
            logging.shutdown()
            os._exit(0)
        log.info("update check: nothing to update")


if __name__ == "__main__":
    threading.Thread(target=update_watcher, name="update-watcher", daemon=True).start()
    mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp", show_banner=False)
