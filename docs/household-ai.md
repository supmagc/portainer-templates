# Household AI: Ollama, Mem0, OpenWebUI, SearXNG

| Service | Stack | Role |
|---|---|---|
| `ollama` | `utilities` | Embeddings only (`embeddinggemma`, 768 dims) on the shared P400 |
| `mem0-mcp` | `utilities` | Mem0 long-term memory exposed as an MCP server, `mem0.utilities.bellecerise.local` |
| `searxng` | `utilities` | Private metasearch, OpenWebUI's web search backend, `searxng.utilities.bellecerise.local` |
| `open-webui` | `household` | Chat UI, `openwebui.household.bellecerise.local`; chat via Requesty |

Dashboards: Utilities Overview has Ollama, mem0 and SearXNG rows; Household Overview has
an OpenWebUI row. See Monitoring below.

Chat and Mem0 fact extraction both go to Requesty (`https://router.requesty.ai/v1`). Mem0
uses `mistral/mistral-small-2603` (EU, no retention). Vectors live in the shared utilities Postgres (pgvector), in
databases `mem0` and `openwebui`, which `postgres-backup` dumps nightly.

## mem0-mcp

No self-hosted Mem0 MCP server exists:
- The official MCP server is hosted-only.
- `mem0ai/mem0-mcp` was archived 2026-03.
- Community wrappers hardcode Qdrant, Neo4j or an Ollama LLM.
- The official REST server has no published image and rejects Ollama embedders.

So `mem0-mcp` is a small FastMCP wrapper around the `mem0ai` library
(`processes/mem0/app/server.py`).

**No custom image, self-updating.** The container is stock `python:3.12-slim`:
- **Every start:** `start.sh` runs `pip install -U -r requirements.txt` into a venv on
  `/data` and then starts the server. If PyPI is unreachable, it starts with the packages
  already in the venv.
- **Daily at `MEM0_UPDATE_CHECK_AT` (04:30):** a `pip install --dry-run` checks for updates.
  If `mem0ai` or `fastmcp` would update within the caps in `requirements.txt`, the process
  exits, and `restart: unless-stopped` reinstalls on the way back up.
- **Watchtower (04:00):** still updates the Python base image itself.

Notes:
- One restart a night stays well under `ContainerCrashLooping` (>2 restarts in 15 min).
- Raise the major-version caps by hand after checking the changelog. mem0's 1.x→2.x
  release changed the search API to `filters=`.
- The first start installs everything, which takes a few minutes. That's why the
  healthcheck has a 5-minute `start_period`.

**Identity.** Tokens live in the `mem0-mcp-tokens` secret, one `<token> <identity>` per line:
- A personal token maps to a fixed identity, an email.
- The identity `@header` means "trust the `X-User-Email` header". Only OpenWebUI holds
  that token, and only OpenWebUI admins can see connection settings.

Your Claude Code token's identity must be your OpenWebUI login email so both share one
memory. `update_memory`/`delete_memory` check that the memory belongs to the caller,
because mem0 itself doesn't. Telemetry (PostHog, on by default) is off via
`MEM0_TELEMETRY=False`. mem0 creates its tables `memories` / `memories_entities` itself.

Tools: `add_memory`, `search_memories`, `list_memories`, `update_memory`, `delete_memory`.
Each call logs `tool call: <tool> (header|token)` for the dashboard. The log line has
the tool name and client type only, never the email or memory text.
`add_memory` runs an LLM call that extracts facts and merges them with existing memories.

## Ollama

- **Embedding only:** `ENABLE_OLLAMA_API=false` in OpenWebUI keeps `embeddinggemma` out
  of the chat model list.
- **GPU headroom:** over 30 days (to 2026-11-20) the P400 was idle at the median, with a
  p99 of 1.0 GB and a peak of 1.76 GB of 2.15 GB. The ~0.8 GB embedder fits.
- **Unloading:** `OLLAMA_KEEP_ALIVE=2m` frees VRAM soon after the last request.
- **Watchtower safety:** because of that short keep-alive, Ollama holds no GPU context at
  04:00 when Watchtower recreates it (see [gpu.md](gpu.md) on the Tdarr wedge).
- **Model pull:** pulling the model is manual, and the models are stored under
  `processes/ollama/.ollama/models`.

## OpenWebUI

- **Image:** `:slim` has no bundled embedding or Whisper models; we embed via Ollama.
  `:slim` = `:main-slim` = `:latest-slim`, a rolling main-branch build, not the tagged
  release (verified on GHCR 2026-11-20). That's the price of Watchtower updates.
- **Settings only apply on first boot:** most settings, the Requesty key and the RAG
  settings included, are "ConfigVars". They are read from env on the **first boot only**
  and live in the database after that. Change them in Admin → Settings, not in the
  compose file.
- **Embedding size:** `PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH=768` must match the embedder.
  The default of 1536 breaks inserts.
- **Mem0 connection:** an MCP connection can't be provisioned from env
  (`TOOL_SERVER_CONNECTIONS` only knows OpenAPI). Add it in the UI (see Setup).
- **Forwarded headers:** `ENABLE_FORWARD_USER_INFO_HEADERS` stays `false`. It would add
  `X-OpenWebUI-User-Email` to every outbound request, Requesty included. The
  per-connection header on the Mem0 connection does the job on its own.
- **Non-root:** upstream calls running as non-root "untested". The compose file sets
  `user: 970` and `HOME=/app/backend/data`. This works (confirmed 2026-11-21).
- **Built-in Memory clashes with mem0:** OpenWebUI's own Memory feature (`ENABLE_MEMORIES`,
  on by default) exposes tools with the **same names** as mem0's (`add_memory`,
  `search_memories`, …). Keep it off, or a model may write to OWUI's memory instead.
- **Ollama API connection:** keep it off. Ollama only serves `embeddinggemma`, which can't
  chat. RAG embeddings use `RAG_OLLAMA_BASE_URL`, a separate setting. If it's on, it points
  at the default `host.docker.internal:11434`, which doesn't resolve, and every model-list
  refresh logs a connection error.

## SearXNG

OpenWebUI's web search engine. A model searches through OWUI's built-in web search tool,
so any of the chat models can search. Requesty flags Reader (Gemini) as supporting
provider-side search, but OWUI doesn't send the request field that would trigger it.

- **Image:** `docker.io/searxng/searxng:latest`, a rolling daily build, updated by
  Watchtower. The old `searxng-docker` repo is archived; upstream now ships compose
  files from `container/` in the main repo.
- **Config:** `processes/searxng/config/settings.yml`, mounted at `/etc/searxng`.
  `secret_key` is the `{{SEARXNG_SECRET_KEY}}` deploy token. `search.formats` must
  include `json`, or OWUI gets `403 Forbidden`. The limiter is off, since it needs Valkey
  and only matters for public instances. Image proxy and the public `/stats` pages are
  off. `safe_search: 1` (moderate) because kids use it.
- **Contact URL:** Wikipedia, Wikidata and OpenStreetMap refuse to load without
  `general.contact_url`, because Wikimedia requires contact info in API requests. It's
  the `{{SEARXNG_CONTACT_URL}}` token (e.g. `mailto:…`), kept out of git because it's
  personal.
- **Removed engines:** ahmia and torch are Tor engines that fail to load without a Tor
  proxy. `use_default_settings.engines.remove` drops them.
- **Harmless startup noise:** "not owned by searxng:searxng" comes from running as 920.
  "missing config file: limiter.toml" appears because the limiter is off. A single
  "X-Forwarded-For nor X-Real-IP header is set" can show up too.
- **Base URL:** set by `SEARXNG_BASE_URL` in compose, not in `settings.yml`.
- **User:** runs as 920. Upstream expects uid 977 and only chowns when it runs as root.
  As 920 it just warns, so the config and cache dirs must be writable by 920. If startup
  fails, drop the `user:` line; it then runs as root and chowns both dirs to 977.
- **Healthcheck:** the image ships none. The compose check uses `python3`, which is in
  the image (confirmed healthy 2026-11-22). Running as 920 also works (confirmed
  2026-11-22).
- **No access log, no search counts:** an access log would put every search term in
  Loki. `/metrics` exists behind `general.open_metrics` (HTTP basic auth, password only),
  but it isn't enabled, and its metric names are unverified.
- **OWUI side:** `SEARXNG_QUERY_URL=http://searxng:8080/search`. OWUI adds `q`,
  `format=json` and the other parameters itself. It reaches the container over
  `utilities_default`. The `ENABLE_WEB_SEARCH`, `WEB_SEARCH_ENGINE` and
  `SEARXNG_QUERY_URL` env vars only apply on first boot; on this install, set them in
  the admin UI (see Setup).
- **Cost:** search results become model input, often a few thousand tokens per search.
  That's about $0.001 on Quick but $0.02 or more on the €€€ models.

## Monitoring

All panels are in the provisioned dashboards (`grafana/provisioning/dashboards/json`).
None of these apps exposes Prometheus metrics, so the panels come from cAdvisor, blackbox
probes (Traefik-discovered routers), postgres-exporter, the nvidia-smi exporter and Loki
log counts.

| Row | Panels | Log source |
|---|---|---|
| Ollama (Utilities) | Health, restarts, RAM, whole-GPU memory, embedding requests, p95 embedding latency | Ollama's GIN access log: `POST "/v1/embeddings"` with latency |
| mem0 (Utilities) | Health, restarts, HTTPS probe, RAM, DB size, errors; tool calls, add_memory calls, Requesty calls and failures; calls by tool, outbound Requesty/Ollama calls | `tool call:` lines from `server.py`; httpx request lines |
| SearXNG (Utilities) | Health, restarts, HTTPS probe, RAM, CPU, memory | — |
| OpenWebUI (Household) | Health, restarts, HTTPS probe, RAM, DB size, messages; CPU, memory | `POST /api/chat/completions`, one per message sent or regenerated |

Notes:
- **GPU memory is whole-GPU.** The P400 is shared with Tdarr, Jellyfin and Stash.
- **Latency includes cold starts.** p95 embedding latency covers requests that waited for
  the model to load (several seconds).
- **Spend isn't here.** Per-model spend lives only in the Requesty dashboard.

## Models (Requesty)

There are two Requesty keys, **Mem0** and **OpenWebUI**, so spend shows per caller. Each
model must be approved on the key that uses it (Requesty's Model Library or the key's
access list). Otherwise calls fail with 403 "model not approved for this API key".

**How models were chosen:**
- EU-pinned, no training on our data, no retention, tool calling supported.
- Requesty's public `https://router.requesty.ai/v1/models` lists `geolocation`,
  `data_retention`, `data_used_for_training` and prices for each model ID.
- The prefix is the hosting provider. `openai/…` and `anthropic/…` (direct) keep data for
  30 days for abuse monitoring. `azure/…`, `bedrock/…` and `vertex/…` are flagged
  no-retention and can be region-pinned (`@westeurope`, `@eu-central-1`, `@eu`), for about
  10% more.
- These flags describe Requesty's contracts with the providers, not standard provider terms.

**mem0's LLM:** `mistral/mistral-small-2603`.
- mem0 checks the model name after the last `/`. Names starting `gpt-5-`, `gpt-6-`, `o1`,
  `o3` and similar count as reasoning models and get no sampling parameters.
- Every other model gets `temperature` **and** `top_p` together. Newer Claude models reject
  that combination, so don't use Claude for mem0.
- After any model change, recreate the container and run the canary (add, search, delete).

**OpenWebUI picker.** Base models can be renamed in Admin → Settings → Models (click the
title). The picker then shows `Name (ID)`. Tags filter the dropdown. Tick **Vision** under
Capabilities on the vision models.

Shared base system prompt, at the start of every chat model's prompt:

```text
You are a helpful assistant for our household. Reply in the language the user writes in.
Use the memory tools: search them when an answer may depend on the user's preferences or past context, and save lasting facts or preferences the user shares. Never save passwords, codes or other secrets.
```

| Model ID | Name | Description | Tags | Role line (after the base prompt) |
|---|---|---|---|---|
| `azure/gpt-6-luna@westeurope` | Quick € | Fast and cheap: everyday questions, rewording, photos. EU · $0.12/$0.60 per 1M | `€` `vision` `fast` | Keep answers short and to the point unless asked for more detail. |
| `melious/deepseek-v4.1-flash-1017` | Thinker € | Cheap step-by-step reasoning: maths, logic, homework. EU · $0.23/$1.14 | `€` `reasoning` | Work through problems step by step and check your result before giving the final answer. When helping with homework, explain the method, not just the answer. |
| `mistral/devstral-latest` | Coder €€ | Scripts, config files, coding help. EU (Mistral) · $0.44/$2.20 | `€€` `code` | Give complete, runnable code. Say which file it goes in and how to run it. Point out anything risky (deleting data, permissions) before the user runs it. |
| `mistral/mistral-large-4` | Default €€ | Everyday default: planning, writing, documents. EU (Mistral) · $0.68/$2.09 | `€€` `default` `vision` | Be practical and clear. Use lists or tables when they make an answer easier to scan. |
| `vertex/gemini-3.8-flash@eu` | Reader €€ | Long PDFs, manuals and images, up to whole books. EU · $0.83/$4.13 | `€€` `long-docs` `vision` | When answering from a document, quote or name the relevant section so the user can check it. Say clearly when the document doesn't contain the answer. |
| `bedrock/claude-sonnet-5-5@eu-central-1` | Writer €€€ | Careful writing, nuance, difficult tasks. EU · $2.20/$11 | `€€€` `writing` `vision` | Take care with tone and wording. For letters and important messages, match the formality the situation needs and offer a short alternative. |
| `azure/gpt-6.2-sol@westeurope` | Second opinion €€€ | Hard questions, or a check on another model's answer. EU · $2.40/$12 | `€€€` `reasoning` `vision` | Think carefully and be candid about uncertainty. When asked to review an answer, point out errors and weak spots specifically, not just general agreement. |
| `mistral/mistral-small-2603` | *(hidden, task model)* | Titles and tags only | — | — |

Prices are $ per 1M tokens in/out, from the Requesty catalogue on 2026-11-21. Thinker is
DeepSeek's open-weights model hosted by Melious in Finland and Ireland, with no prompt
storage, so nothing goes to DeepSeek itself. OWUI only sends sampling parameters set in
Advanced Params, so leave those empty. That way the temperature/top_p issue doesn't
affect Claude here.

**Task prompt tweaks** (Settings → Interface → Tasks):
- The default templates contain placeholders (`{{MESSAGES:…}}`) and a JSON output format
  (`{"tags": […]}`, `{"follow_ups": […]}`) that OWUI parses.
- Don't rewrite them. Copy the default, keep all of it, and add the line(s) below just
  before its output-format section.
- If a new chat's title stays "New Chat" or its tags disappear, the edit broke the
  format. Clear the field to restore the default.

| Template | Add | Why |
|---|---|---|
| Title | `Keep the title to 3–5 words in the language of the conversation. No emojis or quotes. Never put names, addresses, health details or other personal information in the title.` | Titles show in the sidebar; keeps private details off a shared screen |
| Tags | `Use at most 3 tags. Prefer these when they fit: Household, Cooking, Shopping, Travel, School, Work, Health, Money, Tech, Garden, Kids, Fun. Only make up a new tag if none of these fits.` | Without a fixed list, near-duplicate tags make filtering useless. If the list isn't in English, add `Write the tags in <language>.` |
| Follow-ups | `Suggest at most 3 short, practical follow-up questions in the language of the conversation, written the way the user would ask them.` | Only matters when follow-up generation is on |
| Retrieval query, web search, image prompt, autocomplete | Leave the default | Technical prompts; for autocomplete, optionally change `{{MESSAGES:END:6}}` to `END:2` to send less history |

## Setup (one-time)

Secret files live in `/mnt/fastpool/system/processes/secrets/`, owned by the container
user, mode 400:

| File | Owner | Content |
|---|---|---|
| `mem0-postgres-password.txt` | 920 | hex password of the `mem0` role |
| `mem0-requesty-api-key.txt` | 920 | Requesty key named **Mem0** (separate from OpenWebUI's, for usage tracking) |
| `mem0-mcp-tokens.txt` | 920 | `<token> @header` for OpenWebUI, `<token> <your email>` for Claude Code |
| `openwebui-secret-key.txt` | 970 | `openssl rand -hex 32` |
| `openwebui-env.txt` | 970 | `DATABASE_URL=postgresql://openwebui:<hex password>@postgres:5432/openwebui` and `OPENAI_API_KEY=<Requesty key named OpenWebUI>`. Only read on first boot; later changes go in Admin → Connections |

Postgres (as superuser): roles plus databases `mem0` and `openwebui`, each owned by its
role, with `CREATE EXTENSION vector` in both. OpenWebUI runs with
`PGVECTOR_CREATE_EXTENSION=false`, and mem0 would otherwise try to create the extension
as a non-superuser. The `backup` role needs read access to both databases.

**OpenWebUI admin UI, after first login** (the first account becomes admin; later
sign-ups land as `pending`):
1. Admin → Settings → Integrations → External Tool Servers → add a connection:
   - Type: **MCP (Streamable HTTP)**
   - URL: `http://mem0-mcp:8000/mcp`
   - Auth: Bearer, with the `@header` token
   - Headers: `{"X-User-Email": "{{USER_EMAIL}}"}`
   - Access: all users
2. Settings → General → Features: turn **Memories** off (see the clash above).
3. Connections:
   - **Ollama API** off.
   - Requesty → **Model IDs**: only the eight models in the table above.
4. Settings → Models, for each chat model:
   - Name, description, tags and system prompt from the table.
   - mem0 tools ticked under Tools, **then Save**. Without this OWUI never even connects
     to mem0, so models can't remember anything.
   - Advanced Params → Function Calling = **Native**.
   - Sort the models in table order.
   - Make **Default €€** the default model, and hide `mistral-small-2603`.
5. Settings → Interface → Tasks:
   - External Task Model = `mistral/mistral-small-2603`.
   - Title, tags and retrieval query generation on. Autocomplete off: it fires on every
     pause in typing and adds latency. Follow-ups are optional (about $0.0003 per reply
     on the task model).
   - Without a task model set, all of these run on the chat model itself.
   - Apply the task prompt tweaks from the Models section.
   - Leave Task Model Parameters empty. They apply to every background call, including
     compaction summaries.
6. Settings → Documents:
   - Text splitter `token`, chunk size 2000, overlap 200, top K 15. These are the OWUI
     docs' recommendations, not the defaults.
   - Skip hybrid search and reranking: the slim image has no reranker model.
7. Turn off sign-up once the family accounts exist. `DEFAULT_USER_ROLE=pending` covers
   the gap until then.
8. Requesty: set a monthly spending limit on the OpenWebUI key.
9. Canary test: in a chat, say "remember …". Then search for it from Claude Code, which
   confirms the shared identity. Test at least Default, Quick and Thinker, since
   tool-calling quality varies by model.

**Claude Code:**
`claude mcp add --transport http --scope user mem0 https://mem0.utilities.bellecerise.local/mcp --header "Authorization: Bearer <token>"`.
If the step-ca root certificate isn't trusted by Claude Code (Node), point
`NODE_EXTRA_CA_CERTS` at the root cert, or use `http://192.168.1.110:8765/mcp`.

**SearXNG:**
1. Add `SEARXNG_SECRET_KEY` (`openssl rand -hex 32`) and `SEARXNG_CONTACT_URL`
   (e.g. `mailto:you@example.com`) to `.tokens`. Deploy
   `processes/searxng/config/settings.yml`.
2. On the NAS, create `processes/searxng/cache`. Chown `config` and `cache` to 920,
   then `chmod 600` `settings.yml`, since it holds the secret.
3. Redeploy the utilities stack. Check `https://searxng.utilities.bellecerise.local`
   returns results, and the container reports healthy (see the VERIFY in compose).
4. OWUI → Admin → Settings → Web Search:
   - Enable Web Search, engine `searxng`.
   - Query URL `http://searxng:8080/search`.
5. Per chat model, **Features → Web Search** on, so the 🌐 toggle works. Tick
   **Default Features → Web Search** to start it on in new chats. With Native function
   calling the model still decides when to search, so it costs nothing until it does.
   Default on for Quick, Default, Thinker and Reader; off for the €€€ models. Leave
   Settings → Interface → **Web Search in Chat** on Default; Always forces a search on
   every message.
6. Test: ask a model something current, for example today's weather. The reply should
   cite sources.
