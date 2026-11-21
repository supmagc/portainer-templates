# Household AI: Ollama, Mem0, OpenWebUI

| Service | Stack | Role |
|---|---|---|
| `ollama` | `utilities` | Embeddings only (`embeddinggemma`, 768 dims) on the shared P400 |
| `mem0-mcp` | `utilities` | Mem0 long-term memory exposed as an MCP server, `mem0.utilities.bellecerise.local` |
| `open-webui` | `household` | Chat UI, `openwebui.household.bellecerise.local`; chat via Requesty |

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
   - mem0 tools on, Advanced Params → Function Calling = **Native**.
   - Sort the models in table order.
   - Make **Default €€** the default model, and hide `mistral-small-2603`.
5. Settings → Interface → Tasks:
   - External Task Model = `mistral/mistral-small-2603`.
   - Title and tags generation on, **follow-up and autocomplete generation off**. Each
     costs an extra call, and by default they run on the chat model itself.
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
