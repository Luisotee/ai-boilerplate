# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository. Keep `CLAUDE.md`, `AGENTS.md` and the folder `CLAUDE.md` files (symlinked as `AGENTS.md`) updated with important changes, putting subsystem detail in the folder file or a linked doc, not here. Keep every rules file under 24 KB (`< 24,000` bytes) so Antigravity (`agy`) never truncates it.

AI chat-agent system: Node.js/TypeScript clients (Baileys + Meta Cloud API + Telegram/grammY) + Python/FastAPI API (Pydantic AI + Gemini).

`README.md` has the setup guide, the API endpoint tables and the environment variable list.

## Structure

```
packages/
├── whatsapp-client/   # TypeScript — Fastify server (port 3001) + Baileys WhatsApp connection
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
├── whatsapp-cloud/    # TypeScript — Fastify server (port 3002) + Meta WhatsApp Cloud API
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
├── telegram-client/   # TypeScript — Fastify server (port 3003) + grammY Telegram Bot API
│   └── src/           # handlers/, routes/, services/, schemas/, utils/, bot.ts, updates.ts
└── ai-api/            # Python — FastAPI server (port 8000) + Pydantic AI agent
    └── src/ai_api/    # agent/, routes/, services/, rag/, streams/, queue/, whatsapp/, scripts/
scripts/
└── instance_check.py  # several bots on one host: the port / name / volume checks behind setup.sh
```

**Key entry points**: `whatsapp.ts` (Baileys router), `routes/webhook.ts` (Cloud + Telegram webhooks), `telegram-client/src/updates.ts` (grammY dispatch), `agent/core.py` (agent + system prompt), `streams/processor.py` (pipeline), `api-client.ts` (inter-service HTTP).

## Message Flow

Baileys `messages.upsert` (`whatsapp.ts`) → a handler in `handlers/` → `handleTextMessage()` → `api-client.ts` POST `/chat/enqueue` → `routes/chat.py` answers slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/broadcast`, `/help`) itself, so they never reach the agent, and saves and enqueues the rest on `stream:user:{user_id}` → `streams/processor.py` runs the agent and streams chunks to Redis → the client polls GET `/chat/job/{id}` (500ms, max 120s) and replies, plus TTS if enabled. Step-by-step: `docs/architecture.md`.

**Group messages**: non-@mentioned messages are saved as history only (`saveOnly=true`), never processed by AI. The bot checks both JID and LID formats for mentions.

**On-demand history retrieval**: each run auto-injects only the last `HISTORY_LIMIT_PRIVATE`/`HISTORY_LIMIT_GROUP` messages. The `get_chat_history` tool fetches more of the current chat (`limit` and/or `since_hours`, capped at 200) and can never read another conversation; for topic recall use `search_conversation_history`. Details: `packages/ai-api/src/ai_api/agent/CLAUDE.md`.

**Shared-group tools** (`SHARED_GROUP_TOOLS_ENABLED`, default `false`): `get_group_context` / `send_group_message` in `agent/tools/group_context.py` let a private chat read, search or post into a group the user shares with the bot. Off by default because group transcripts reach the LLM inside a private chat. Privacy model and per-platform membership checks: `packages/ai-api/src/ai_api/agent/CLAUDE.md`.

## Subsystem docs (load on demand)

Claude Code loads a folder's `CLAUDE.md` when it works there. **These subsystems cross folders, so read the file before changing any part of one**, even from another folder:

| File | Covers |
|------|--------|
| `packages/ai-api/src/ai_api/services/CLAUDE.md` | Broadcasts (`/admin/broadcasts`), bug reports (`/admin/bug-reports`), cross-platform identity (`/link`, `/linkphone`) |
| `packages/ai-api/src/ai_api/agent/CLAUDE.md` | DeepSeek → Gemini model chain, `GuardedModel`, fallback reply, chat-history and shared-group tools |
| `packages/ai-api/src/ai_api/streams/CLAUDE.md` | PDF processing queue (concurrency, retry, dead letter, crash recovery) |
| `packages/whatsapp-client/CLAUDE.md` | Baileys gotchas (WA version, LID vs phone, whitelist placement), handler + multipart route workflows |
| `packages/telegram-client/CLAUDE.md` | Telegram flow, polling mode, reactions, privacy mode, 4096-char limit, formatting |
| `packages/whatsapp-cloud/CLAUDE.md` | Cloud API flow, 24h window, typing indicator, handler workflow |
| `scripts/CLAUDE.md` | Several bots on one host: `setup.sh --check/--fix`, `--update`, ports, `SERVICE_NAME`, Compose project name, `*_BIND` |
| `docs/access-control.md` | Rate limits, group admins, whitelist matching, group gating |
| `docs/docker.md` | Non-root images, ignore files, volume chown on upgrade, TS logging/shutdown |
| `packages/ai-api/docs/admin-api.md` | `/admin` routes, runtime settings `REGISTRY` |
| `packages/ai-api/docs/media-and-kb.md` | KB upload dedup, PDF parser and STT provider selection |
| `packages/ai-api/docs/markdown-formatting.md` | Model-output Markdown → WhatsApp/Telegram conversion |
| `packages/ai-api/docs/logfire.md` | Logfire instrumentation details |
| `docs/architecture.md`, `docs/commands.md`, `docs/testing.md`, `docs/workflows.md` | Full message flow, command list, test patterns, workflow steps |

## Tooling

- **Monorepo**: pnpm workspaces (`pnpm-workspace.yaml`). **TypeScript**: ES2022, NodeNext modules, strict mode. **Python**: >=3.11, managed with `uv`
- **Formatting**: Prettier (TS) + Ruff (Python), enforced by the Husky pre-commit hook, which runs `pnpm format` itself, so do NOT run format manually before committing. New TS files may fail `pnpm format:check` even when lint passes: run `pnpm exec prettier --write <path>` on freshly created files
- **Linting**: ESLint flat config (TS) + Ruff (Python)
- **Testing**: Vitest (TS) + pytest/pytest-asyncio (Python); `pnpm test` runs all

## Commands

Full list, per-package tests, Compose profiles and local API-docs URLs: `docs/commands.md`.

```bash
./setup.sh                      # Interactive: generates .env, installs Node + Python deps
./setup.sh --check              # Report clashes with other bots on this host (read-only); --fix moves them, --update adds new .env.example keys
docker compose up -d            # Core: postgres, redis, api, worker, whatsapp; add --profile dev / cloud / telegram / whisper
pnpm dev:server                 # AI API (port 8000); also dev:whatsapp (3001), dev:cloud (3002), dev:telegram (3003)
pnpm dev:queue                  # Stream worker: required for replies, PDF processing AND broadcasts
pnpm install:all                # Install Node + Python dependencies
pnpm lint                       # ESLint + Ruff; also lint:fix, format, format:check, check:rules
pnpm test                       # All tests; also test:ts, test:python
cd packages/<pkg> && pnpm build # tsc type-check/build (the Docker build fails on type errors)
cd packages/whatsapp-client && pnpm exec vitest run tests/unit/whitelist.test.ts   # one file; -t "name substring" by name
cd packages/ai-api && uv run pytest tests/mocked/test_model_chain_fallback.py -k "cooldown"
```

## Security

- **API key auth**: every server requires `X-API-Key` on all routes except `/health` and `/docs*`: `AI_API_KEY` (AI API; it fails to start without it), `WHATSAPP_API_KEY` (Baileys, required), `WHATSAPP_CLOUD_API_KEY` and `TELEGRAM_API_KEY` (both fall back to `WHATSAPP_API_KEY`). Inter-service calls include the key automatically
- **Webhook verification**: `/webhook` on the Cloud and Telegram clients skips API-key auth for platform-native checks: `x-hub-signature-256` HMAC-SHA256 with `META_APP_SECRET` (Cloud), `X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET` (Telegram; grammY's `webhookCallback` validates it when `secretToken` is set)
- **CORS**: `CORS_ORIGINS` (comma-separated). Empty blocks all cross-origin requests
- **Rate limiting**: `RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min). A valid `X-API-Key` exempts a request on the TS clients, because all inter-service traffic comes from one IP. The auth hook runs before the limiter, so the limiter does **not** throttle API-key guessing. `/health*` is exempt from both. Details: `docs/access-control.md`
- **Group admin commands fail closed**: in a group, `/clean`, `/tts`, `/stt`, `/settings`, `/memories` and `/broadcast on|off` run only when the client sends `is_group_admin: true` (`commands.py` checks `is_group_admin is not True`, because an omitted field once let any member `/clean all` a group); a failed lookup means not-admin. Group-changing agent tools enforce the same via `agent/tools/_group.py` `group_admin_refusal`, so asking in plain words can't bypass it. Details: `docs/access-control.md`
- **User whitelist (`WHITELIST_PHONES`)**: comma-separated; empty disables it. Non-whitelisted messages are silently ignored by the client (primary) and the AI API (defense in depth). `utils/whitelist.ts` (TS ×3, kept byte-identical by `tests/unit/whitelist-copies.test.ts`) parses entries as phones or verbatim chat ids; `ai_api/whitelist.py` mirrors it and must never be *more permissive*. Matching is exact. A `PATCH /admin/settings` edit changes only the AI-API layer, so a newly added JID stays blocked by a client whose `WHITELIST_PHONES` is set until its env is updated and it restarts. Read `docs/access-control.md` before changing any of it
- **Group gating (`GROUP_GATING=jid|membership`, default `jid`)**: how a group gets in scope under a whitelist. `jid`: only by its own id. `membership`: also through a whitelisted member, saving every message but replying only to whitelisted senders. Keep it equal across the AI API and clients via the root `.env`. Truth table: `utils/gating.ts`; details in `docs/access-control.md`
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` are required in `.env`

## Observability

- **Sentry**: opt-in via `SENTRY_DSN_NODE`. Each TS package's `src/instrument.ts` must be imported first in `main.ts` (before `config.ts`) so Sentry's OpenTelemetry hooks load first. Without the DSN it is a no-op and `Sentry.setupFastifyErrorHandler` is skipped
- **Logfire (LLM token + cost tracking)**: opt-in via `LOGFIRE_TOKEN` (AI API only), set up by `instrument.py` `setup_instrumentation()`. Invariants: pass `token=settings.logfire_token` explicitly (pydantic-settings does not export `.env` to `os.environ`, so local dev breaks silently otherwise); call it in BOTH `main.py` and `scripts/run_stream_worker.py` (the agent runs in the worker); never pass `instrument=` to `Agent(...)`; keep `include_content=False` (a privacy guarantee). Details: `packages/ai-api/docs/logfire.md`

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **11 tables**, models in `database.py` and `kb_models.py` (list in `docs/architecture.md`)
- **No Alembic migrations.** SQLAlchemy `create_all()` only creates missing *tables*, never new columns. Column/index changes ship as hand-written, idempotent (`IF NOT EXISTS`) SQL in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql`, named so they match what `create_all()` emits on a fresh DB. Apply them to an existing Docker deployment BEFORE rolling out the code that needs them (safe to re-run):
  ```bash
  docker compose exec -T postgres psql -U aiagent -d aiagent \
    < packages/ai-api/docs/migrations/<file>.sql
  ```
  Run it in the checkout, so it can't hit another bot's database on a shared host. Notes and the list of current migrations: `packages/ai-api/docs/migrations/README.md`
- **Knowledge-base dedup**: `knowledge_base_documents.file_hash`; an upload of content already in the KB gets **409**. The upload routes are `@limiter.exempt` (for `./upload-kb.sh <dir>`) but stay behind `X-API-Key`. Details: `packages/ai-api/docs/media-and-kb.md`
- **Core memories**: one markdown document per user (`core_memories`), injected via `@agent.instructions inject_core_memory` in `agent/core.py`. The AI rewrites it whole with `update_core_memory` (empty string clears it). There is deliberately no show/clear tool: the document is in every run and `/memories [clear]` covers users
- **System prompt is DB-backed**: the single-row `bot_prompt` table, loaded per run via `@agent.instructions base_system_prompt`, falling back to `DEFAULT_SYSTEM_PROMPT`. `PUT /admin/prompt` applies on the next message. It uses `instructions` (not `system_prompt`) so a prompt retained in `message_history` never shadows a changed one
- **PDFs**: conversation-scoped PDFs expire after 24h (`CONVERSATION_PDF_TTL_HOURS`; cleanup runs in the `main.py` lifespan). Every PDF goes through the Redis Stream `stream:pdf_processing`, parsed by `streams/pdf_consumer.py` in the stream worker, never in the API process or inline in a chat job. Queue details: `packages/ai-api/src/ai_api/streams/CLAUDE.md`
- **PDF parsing and STT**: `PDF_PARSER` (`auto` | `llamaparse` | `docling`; Docling is an optional extra) and `STT_PROVIDER` (`auto` | `groq` | `whisper`; self-hosted via `docker compose --profile whisper up -d`). Fallbacks: `packages/ai-api/docs/media-and-kb.md`

## Environment Config

- The root `.env` loads first (shared vars; all listed in `.env.example`). Loaders: `packages/whatsapp-client/src/config.ts`, `packages/whatsapp-cloud/src/config.ts`, `packages/telegram-client/src/config.ts`, pydantic-settings in `packages/ai-api/src/ai_api/config.py`
- **Adding a variable**: put it in `.env.example` with a `#` comment line directly above it (copied along and shown as the prompt hint), and leave the value empty when it is a secret or opt-in. Existing deployments pick it up with `./setup.sh --update` (details in `scripts/CLAUDE.md`)
- **Shared secrets live in root `.env` only**, never duplicated in a package-level `.env.local`
- **`.env.local` is for per-developer customization** (log level, port, feature flags). It loads with `override: true`, so a duplicated key silently wins over root, even an empty `KEY=`. If you see `[config] .env.local overrides root .env: X` at startup, confirm it's intentional

## Library docs

**Before writing or modifying code that touches any external library, SDK, API or framework, look up its current documentation with the Context7 MCP server** (resolve the library id, then query the docs). Do not rely on training data for signatures or behavior. This covers every dependency (Baileys, Pydantic AI, FastAPI, Fastify, grammY, Meta Cloud API, Gemini, pgvector, SQLAlchemy, Zod, Redis, Docling, Groq…). Fall back to web search when Context7 has no entry.

## Task tracking: Devflow

Work is tracked in **Devflow**, project **`CHATBOTS`** (org Terra Krya), through the Devflow MCP server. Write in pt-BR, because the board is in Brazilian Portuguese. Board id, columns and connector names: `docs/devflow.md`.

- **Before non-trivial work** (a feature, a bug fix, a migration): find the matching task and follow its description, often a markdown checklist. If none exists, create one: short title, markdown description, assignee `me`
- **Always set a priority** (`Prioridade`: `Baixa`, `Media`, `Alta`, `Urgente`) on every task you create, and before starting a task that has none. Pick it from the urgency the user expressed; ask when it is unclear
- **Starting**: move the task to `Em desenvolvimento`. **Finishing** (code done, PR open or merged): move it to `Aguardando teste`, passing the required `test_instructions` (concrete steps to check the change). Do not move it to `Aguardando publicacao` or `Em producao`, or complete it, unless the user says so, because testing and deploy are theirs
- Do not log time, change sprints or archive tasks unless asked

## Related Projects

Sibling repos under `/home/ubuntu/projects/` have their own authoritative `CLAUDE.md` / `AGENTS.md`; this file's rules don't carry over.

- **Run or resume a peer agent in that repo** (Antigravity `agy` or Claude Code `claude`) instead of editing its files from here, so its own rules, skills and hooks apply and the user can resume it (`agy -c` or `claude -c`): `cd /home/ubuntu/projects/<repo> && agy -c -p "<prompt>" --dangerously-skip-permissions` or `cd /home/ubuntu/projects/<repo> && claude -c -p "<prompt>" --dangerously-skip-permissions`. **Always pass `--dangerously-skip-permissions`**, resumed (`-c -p`) or new (`-p`), because headless print mode needs it to auto-approve tools. Drop `-c` for a fresh session (`agy` needs one when an open TUI holds the latest conversation's lock); in `agy`, `send_message` to an open session's conversation ID queues into its inbox
- **curupira-bot**: fire-alert fork (municipality subscriptions, `fire_poller/`, persona "Curupira"). Features often start here and get ported back (the shared-group tools, `get_chat_history`)
- **castanha-bot**: Brazil-nut price fork (external *cadeia-produtiva* platform via `cadeia` tools, weekly recap, Portuguese commands like `/regiao`, no Telegram client)
- **fleetview**: Next.js 16 control plane for the fleet, not a fork. It keeps an encrypted registry of bots (base URL + `X-API-Key`) and calls each bot's `/admin/*`, `/health` and `/knowledge-base/*` server-side. **Its Zod schemas mirror `routes/admin.py`, `schemas.py` and the `runtime_config.py` REGISTRY strictly** (only health is lenient). Renaming or removing a field, route or setting attribute (`hot`/`secret`/`choices`) breaks the dashboard for every bot, and so does a new value in a closed enum (setting `type`, KB status, QR `status`, `conversation_type`, broadcast enums). A new field is safe (unknown keys are stripped)
  - **Contract check**: the contract files are `packages/ai-api/src/ai_api/routes/{admin,broadcasts,bug_reports,health,knowledge_base}.py`, `schemas.py` and the `runtime_config.py` REGISTRY. After changing any of them, ask a fresh peer session in fleetview (no `-c`) for the verdict, since only fleetview knows its current schemas: `cd /home/ubuntu/projects/fleetview && claude -p "Run git -C /home/ubuntu/projects/ai-boilerplate diff origin/main...HEAD -- <the changed contract files>. Does it break a Zod schema in lib/bot-api/schemas.ts or a Server Action in lib/actions/? Read-only check: do not modify fleetview files, run write commands, or read .env* files. Answer Safe, Breaking or Needs a FleetView update, with file:line in both repos." --dangerously-skip-permissions` (or the same prompt with `agy -p ... --dangerously-skip-permissions`)

## Guidelines

- Use `pnpm add` / `uv add` for dependencies. NEVER edit package.json/pyproject.toml directly
- Prefer pure functions over classes. Async throughout both codebases
- Use structured logging (Pino for TS, Python `logging`), no console.log/print
- Write tests for new functionality, following the existing patterns in `tests/` directories

## Testing

Each package has `tests/` with `unit/`, `integration/` and `helpers/`; ai-api also has `mocked/`. Vitest 4 (TS) and pytest 9 with `asyncio_mode = auto` (Python). No coverage tooling, and no CI runs tests. The rules that bite:
- New routes must also be registered in `buildTestApp()` (`tests/helpers/fastify.ts`), or integration tests won't see them
- TS singleton state (`getBaileysSocket`, `isCloudApiConnected`) needs `vi.resetModules()` in `beforeEach`
- Python `conftest.py` sets env vars and patches before any production import; order matters, so don't rearrange it

Fixtures, factories and the integration-test setup: `docs/testing.md`.

## Common Workflows

Step lists: `docs/workflows.md`. Baileys handler, Cloud handler and Baileys multipart-route workflows: the matching package's `CLAUDE.md`.

- **New agent tool** (use the `/agent-tool` skill): `async def tool_name(ctx: RunContext[AgentDeps], ...params) -> str` with `@agent.tool` in `agent/tools/`, imported in `agent/tools/__init__.py` (or the decorator never registers it), described in the system prompt in `agent/core.py`, with mocked tests in `tests/mocked/`
  - Exception handlers that touch `ctx.deps.db` must call `safe_rollback(ctx.deps.db)` (`agent/tools/_db.py`), because all tools share one DB session per run and a failed flush/commit would make later tool calls hit `PendingRollbackError`
  - Error returns must be generic English, never raw `str(e)`, because SQLAlchemy/httpx exceptions can leak hostnames, SQL or internal URLs to the user; log details with `logger.error(..., exc_info=True)`. Pinned by `tests/mocked/test_tool_error_hygiene.py`
- **New API endpoint** (use the `/api-route` skill): route in `routes/*.py` with `APIRouter` (`limiter` from `deps.py`), schemas in `schemas.py`, router registered in `main.py` via `app.include_router()`, integration tests in `tests/integration/`

## Gotchas

### AI API (Python)
- **Admin API (`routes/admin.py`, `/admin/*`)** is the dashboard contract, behind the standard `X-API-Key`. `GET /admin/whatsapp/qr` reports `status="unavailable"` (200) when it can't reach the Baileys client. Route list: `packages/ai-api/docs/admin-api.md`
- **Model chain**: `build_runtime_model()` builds `FallbackModel(DeepSeek → Gemini)` per run when `DEEPSEEK_API_KEY` is set (chat content then goes to DeepSeek, China), bare Gemini otherwise. **Never run the chat agent with `agent.run_stream`**, because it drops tool calls made after a text preamble; `agent/response.py` uses `run_stream_events`. Never put `extra_body`/`timeout` in agent- or run-level `model_settings`. Details: `packages/ai-api/src/ai_api/agent/CLAUDE.md`
- **Runtime settings overlay (`runtime_config.py`)**: some settings can be overridden at runtime via the `runtime_settings` table, read through `runtime_config.get("key")` (cached ~10s). In the `REGISTRY`, **only mark a setting `hot` if its consumption site actually reads via `runtime_config.get()`**, otherwise the override silently never applies. Details: `packages/ai-api/docs/admin-api.md`
- **Model output is Markdown-sanitised** by `formatting.markdown_to_whatsapp` before delivery, embedding and save, because WhatsApp renders Markdown literally. The rule for the model lives in the `formatting_guidance` instructions hook (`agent/core.py`), because a `bot_prompt` override replaces `DEFAULT_SYSTEM_PROMPT` wholesale. Keep `**` out of the default prompt (pinned by `tests/mocked/test_formatting_guidance.py`). Details: `packages/ai-api/docs/markdown-formatting.md`
- CORS middleware must be added AFTER `APIKeyMiddleware` in `main.py`: Starlette processes middleware LIFO, and reversing this breaks CORS preflight
- The pgvector IVFFlat index must be created manually for `knowledge_base_chunks`; without it, similarity search does a full table scan
- Embedding task types matter: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search. Mixing them degrades retrieval quality
- **Redis Streams (`streams/`) is the only job pipeline**; `arq` is gone (history in `docs/architecture.md`). `get_arq_redis` / `close_arq_redis` / `create_arq_pool` and `ARQ_KEEP_RESULT` (the job-metadata TTL) keep their legacy names on purpose, because renaming them would break callers, test patches and existing overrides

### General
- **Logging/shutdown (TS)**: pino-pretty only outside production (containers emit plain JSON). SIGTERM/SIGINT exit 0; startup failures and `unhandledRejection` go through `shutdownWithError` (exit 1). Details: `docs/docker.md`
- **Docker images run as non-root** (TS clients as `node`, ai-api as `appuser`, both uid 1000). **Type errors fail the image build**, so never add `|| true` to the build step. TS clients use an allowlist `packages/<pkg>/Dockerfile.dockerignore`, not the root `.dockerignore`; keep `.logfire/` excluded in all ignore files. When upgrading, volumes from the old root images stay root-owned and unwritable: chown them once as `docs/docker.md` shows
