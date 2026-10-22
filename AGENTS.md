# AGENTS.md

This file provides guidance to Antigravity (`agy`) when working with code in this repository. Keep both `AGENTS.md` and `CLAUDE.md` (plus folder `CLAUDE.md` files, symlinked as `AGENTS.md`) updated with important changes; keep this root file under 24 KB (`24,000` bytes) so `agy` never truncates it.

AI chat-agent system: Node.js/TypeScript clients (Baileys + Meta Cloud API + Telegram/grammY) + Python/FastAPI API (Pydantic AI + Gemini). `README.md` has the setup guide, API endpoint tables, and full environment variable list.

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
└── instance_check.py  # several bots on one host: port / name / volume checks behind setup.sh
```

**Key entry points**: `whatsapp.ts` (Baileys message router), `routes/webhook.ts` (Cloud API + Telegram webhook handlers), `telegram-client/src/updates.ts` (grammY dispatch table), `agent/core.py` (AI agent definition + system prompt), `streams/processor.py` (processing pipeline), `api-client.ts` (inter-service HTTP client).

## Message Flow

1. Baileys WebSocket → `whatsapp.ts` `messages.upsert` event
2. `normalizeMessageContent()` unwraps viewOnce/ephemeral message wrappers
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts` (transcribe first), image → `handlers/image.ts`, document → `handlers/document.ts`
4. All handlers funnel into `handleTextMessage()` with optional base64 image/document
5. `api-client.ts` sends POST `/chat/enqueue` to AI API → returns `job_id`
6. `routes/chat.py` intercepts slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/broadcast`, `/help`) before queuing
7. Non-command messages: saved to PostgreSQL, enqueued to Redis Stream (`stream:user:{user_id}`)
8. `streams/processor.py`: fetches conversation history → runs Pydantic AI agent with tools → streams response chunks to Redis
9. `api-client.ts` polls GET `/chat/job/{id}` (500ms interval, max 120s) until complete
10. WhatsApp client sends text reply; optionally generates TTS audio if user preference enabled

**Group messages**: non-@mentioned messages are saved as history only (`saveOnly=true`), never processed by AI. Bot checks both JID and LID formats for mentions.

**On-demand history retrieval**: each run auto-injects only the last `HISTORY_LIMIT_PRIVATE`/`HISTORY_LIMIT_GROUP` messages. The `get_chat_history` agent tool (`agent/tools/history.py`) fetches more of the CURRENT chat chronologically (`limit` and/or `since_hours`) via `get_conversation_messages` in `database.py` (keyed by the run's own `users.id`, naive-UTC `since`/`until` cutoffs, hard-capped at `MAX_FLAT_MESSAGES=200`). Distinct from `get_conversation_history` (auto-injection). Transcripts render via `format_transcript` in `rag/conversation.py` (bot lines labelled with `BOT_NAME`, default `Assistant`, hot via `/admin`; group sender names not double-prefixed; private user lines get `User:`). For topic recall use `search_conversation_history`.

**Shared-group tools** (`SHARED_GROUP_TOOLS_ENABLED`, default `false`): `get_group_context` / `send_group_message` in `agent/tools/group_context.py` let a private chat read, search, or post into a group the user shares with the bot. Off by default because group transcripts reach the LLM inside a private chat. Details: `packages/ai-api/src/ai_api/agent/AGENTS.md`.

## Subsystem Docs (Load on Demand)

Deep detail lives in `AGENTS.md` (symlinked to `CLAUDE.md`) next to the code. **These subsystems cross folders, so read the file before changing any part of one**, even when editing from another folder:

| File | Covers |
|------|--------|
| `packages/ai-api/src/ai_api/services/AGENTS.md` | Broadcasts (`/admin/broadcasts`: audience, routing, opt-out, claims, lease, Baileys pacing, FleetView enums), bug reports (`/admin/bug-reports`: dedup, privacy scrub), cross-platform identity (`/link`, `/linkphone`) |
| `packages/ai-api/src/ai_api/agent/AGENTS.md` | DeepSeek → Gemini model chain, `GuardedModel`, fallback rules, model-error fallback reply, shared-group tools |
| `packages/ai-api/src/ai_api/streams/AGENTS.md` | PDF processing queue (concurrency, retry, dead letter, crash recovery) |
| `packages/whatsapp-client/AGENTS.md` | Baileys gotchas (WA version, LID vs phone, whitelist placement), handler + multipart route workflows |
| `packages/telegram-client/AGENTS.md` | Telegram flow, polling mode, reactions, privacy mode, 4096-char limit, formatting |
| `packages/whatsapp-cloud/AGENTS.md` | Cloud API flow, 24h window, typing indicator, handler workflow |
| `scripts/AGENTS.md` | Several bots on one host: `setup.sh --check/--fix`, ports, `SERVICE_NAME`, Compose project name, `*_BIND` |
| `packages/ai-api/docs/logfire.md` | Logfire instrumentation details |

## Tooling & Commands

- **Monorepo**: pnpm workspaces (`pnpm-workspace.yaml`) | **TS**: ES2022, NodeNext, strict | **Python**: >=3.11 (`uv`)
- **Formatting & Linting**: Prettier + ESLint flat config (TS) and Ruff (Python). Husky pre-commit hook runs `pnpm format` automatically; `.agents/hooks.json` auto-formats after `edit_file`/`write_file`.
- **Testing**: Vitest 4 (TS) + pytest 9 / pytest-asyncio (Python) — `pnpm test` runs all.

```bash
# Setup & Host Checks
./setup.sh                              # Interactive: generates .env, installs Node + Python deps
./setup.sh --check                      # Report clashes with other bots on this host (read-only)
./setup.sh --fix                        # Move clashing ports in existing .env (backup first)
./setup.sh --update                     # Add settings new in .env.example to existing .env (backup first)

# Docker Compose Profiles
docker compose up -d                                    # Core: postgres, redis, api, worker, whatsapp
docker compose --profile dev up -d                      # + Adminer (port 8080)
docker compose --profile cloud up -d                    # + WhatsApp Cloud API client
docker compose --profile telegram up -d                 # + Telegram client

# Development (from root)
pnpm dev:server                         # AI API (port 8000)
pnpm dev:whatsapp                       # Baileys client (port 3001)
pnpm dev:cloud                          # Cloud API client (port 3002)
pnpm dev:telegram                       # Telegram client (port 3003)
pnpm dev:queue                          # Stream worker (chat + PDF + broadcast consumers)
pnpm install:all                        # Install Node + Python dependencies

# Linting, Formatting & Testing
pnpm lint && pnpm format:check          # Lint + verify formatting
pnpm lint:fix && pnpm format            # Auto-fix lint + format
pnpm test                               # Run all tests (TS + Python)
pnpm test:ts                            # Run TS tests only
pnpm test:python                        # Run Python tests only (uv run pytest)
cd packages/<pkg> && pnpm build         # tsc type-check/build (Docker build fails on type errors)

# Single Test
cd packages/whatsapp-client && pnpm exec vitest run tests/unit/whitelist.test.ts
cd packages/whatsapp-client && pnpm exec vitest run -t "name substring"
cd packages/ai-api && uv run pytest tests/mocked/test_model_chain_fallback.py -k "cooldown"
```

## Security

- **API Key Auth**: All servers require `X-API-Key` header on all routes except `/health*` and `/docs*` (`AI_API_KEY`, `WHATSAPP_API_KEY`, `WHATSAPP_CLOUD_API_KEY` falling back to `WHATSAPP_API_KEY`, `TELEGRAM_API_KEY` falling back to `WHATSAPP_API_KEY`).
- **Webhook verification**: `/webhook` on Cloud (`x-hub-signature-256` HMAC-SHA256 via `META_APP_SECRET`) and Telegram (`X-Telegram-Bot-Api-Secret-Token` via `TELEGRAM_WEBHOOK_SECRET`) skip API-key auth and use platform-native verification.
- **CORS**: `CORS_ORIGINS` (comma-separated; empty = block all cross-origin).
- **Rate Limiting**: `RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min). On TS clients, requests with a valid `X-API-Key` are exempt (`utils/api-key.ts` `hasValidApiKey`, which compares **byte** lengths before `timingSafeEqual` so multibyte headers get 401 instead of 500). Auth hook runs BEFORE the rate limiter.
- **Group admin commands fail closed**: `/clean`, `/tts`, `/stt`, `/settings`, `/memories`, and `/broadcast on|off` in a group run only when `is_group_admin is True` (`commands.py` checks `is_group_admin is not True`). Baileys resolves via `isSenderGroupAdmin` (`utils/message.ts`); Telegram via `getChatMember` (`services/group-admin.ts`); lookup failure = not-admin. Agent tools changing group-wide state enforce the same via `agent/tools/_group.py` `group_admin_refusal` (`clean_user_data`, `update_tts_settings`, `update_stt_settings`, `set_broadcast_subscription`, and `update_core_memory` when wiping with empty content).
- **User Whitelist (`WHITELIST_PHONES`)**: Comma-separated (empty = disabled). Parsed into phone-digits and verbatim sets by `utils/whitelist.ts` (TS ×3, kept byte-identical by `whitelist-copies.test.ts`) and `ai_api/whitelist.py` (must never be more permissive than TS; uses the identical 29-codepoint separator class). Strips `:device@` and trailing `@s.whatsapp.net`; entries still containing `@` or starting with `tg:` are verbatim-only; others ≥5 digits also enter the normalized phone set.
  - Normalized digits are consulted **only** for `@s.whatsapp.net` JIDs (never `@lid`, `@g.us`, `@broadcast`), though bare entries stay namespace-blind for legacy group/LID IDs.
  - The `phone` argument is **never** consulted for a group JID.
  - Checked at both client level (primary) and AI API level (defense in depth, matching `whatsapp_jid` and `phone`). `PATCH /admin/settings` updates only the AI-API layer (removals take effect immediately; newly added JIDs still need client env update + restart if client `WHITELIST_PHONES` is set) and rejects empty/whitespace-only whitelists (`DELETE /admin/settings/whitelist_phones` reverts to env). Exact matching only (no last-N-digits fuzzy fallback).
- **Group gating (`GROUP_GATING=jid|membership`, default `jid`)**: Truth table in `utils/gating.ts` (`gateMessage` / `decideGroupGating`, byte-identical across `whatsapp-client` and `telegram-client`).
  - `jid`: group admitted only by its own ID; then any member may address the bot.
  - `membership` (Baileys): group in scope if listed, sender whitelisted, or any participant whitelisted (`groupHasWhitelistedMember` in `services/groups.ts` + `group-cache.ts`, 10s bound, fails closed). Saves all in-scope group messages; replies only to whitelisted senders (`gate.suppressResponse` → `saveOnly`) unless the group ID itself is listed. Gate runs between `resolveSenderPhone` and `sock.user!.id` (`resolveGate` in `whatsapp.ts`, pinned by `whatsapp-whitelist-placement.test.ts`).
  - `membership` (Telegram): Bot API cannot enumerate members, so all groups are in scope for saving; replies gated per sender (`tg:<from.id>`) unless group ID is listed.
  - AI API: in `membership` mode `_is_whitelisted` admits all group JIDs (`@g.us` or `tg:-…`); client is authoritative for groups.
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`.

## Observability

- **Sentry**: Opt-in via `SENTRY_DSN_NODE`. Each TS package's `src/instrument.ts` must be imported **first** in `main.ts` (before `config.ts`).
- **Logfire**: Opt-in via `LOGFIRE_TOKEN` (AI API), configured in `instrument.py` `setup_instrumentation()`. Invariants: pass `token=settings.logfire_token` explicitly; call in BOTH `main.py` and `scripts/run_stream_worker.py`; never pass `instrument=` to `Agent(...)`; keep `include_content=False`. Details: `packages/ai-api/docs/logfire.md`.

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`).
- **11 tables**: `users`, `conversation_messages`, `conversation_preferences`, `core_memories`, `bot_prompt`, `runtime_settings`, `broadcasts`, `broadcast_recipients`, `bug_reports` (`database.py`) + `knowledge_base_documents`, `knowledge_base_chunks` (`kb_models.py`).
- **No Alembic migrations**: `create_all()` only creates missing tables, never new columns/indexes. Ship column/index changes as idempotent (`IF NOT EXISTS`) SQL in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql` (use `/db-migration` skill) and apply before rollout:
  ```bash
  docker compose exec -T postgres psql -U aiagent -d aiagent < packages/ai-api/docs/migrations/<file>.sql
  ```
- **Knowledge-base dedup**: `knowledge_base_documents.file_hash` (SHA-256 hex). `POST /knowledge-base/upload` returns **409** on duplicate active global doc; batch route rejects duplicates per file. Upload routes are `@limiter.exempt` (for `./upload-kb.sh <dir>`) and behind `X-API-Key`.
- **Core memories & DB-backed prompt**: One markdown doc per user (`core_memories`), injected via `@agent.instructions inject_core_memory`. Active system prompt lives in single-row `bot_prompt` table (`PUT /admin/prompt`), loaded via `@agent.instructions base_system_prompt` (falling back to `DEFAULT_SYSTEM_PROMPT`).
- **PDFs & STT**: Conversation PDFs expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). All PDFs go through Redis Stream `stream:pdf_processing` (`streams/pdf_consumer.py` in worker, never inline in API). `PDF_PARSER` (`auto` | `llamaparse` | `docling`). `STT_PROVIDER` (`auto` | `groq` | `whisper`).

## Environment Config

- **Shared secrets live in root `.env` only** (never duplicate credentials in package `.env.local`).
- **`.env.local` is for per-developer overrides** (`override: true`; empty `KEY=` blanks out root value; TS config loaders warn on override).
- **Adding a variable** (use `/config-var` skill): place in `.env.example` with a `#` comment directly above it (used by `./setup.sh --update`), leave secret/opt-in values empty, and wire into `config.ts`, `config.py`, and `docker-compose.yml`.

## Task Tracking: Devflow

Work is tracked in **Devflow** (kanban), project **`CHATBOTS`** (org Terra Krya), shared by every bot repo (this one, curupira-bot, castanha-bot, fleetview). Use the `flow-staging` MCP server (`https://staging.devflow.fractopia.org`). The board is in Brazilian Portuguese: write titles, descriptions, comments and test instructions in pt-BR.

- **Board**: default `Desenvolvimento` (id `6aa2e19c6bf0a8f157d443e7`; ignore the second board with the same name). Columns: `Aguardando na fila` → `Aguardando desenvolver` → `Em desenvolvimento` → `Aguardando teste` → `Testando` → `Aguardando publicacao` → `Em producao` (the last two count as done). `Ciclos` groups tasks by sprint.
- **Before non-trivial work** (feature, bug fix, migration): search the project for a matching task and follow its description (often a markdown checklist). If none exists, create one: short title, markdown description, assignee `me`.
- **Always set a priority**: every task you create gets `Prioridade` (`Baixa`/`Media`/`Alta`/`Urgente`), and a task you pick up without one gets it set before you start. Ask the user when unclear.
- **Starting**: move it to `Em desenvolvimento`. **Finishing** (PR open or merged): move it to `Aguardando teste`, passing the required `test_instructions` (concrete steps to check the change). Never move a task to `Aguardando publicacao`/`Em producao` or complete it unless the user says so.
- Do not log time, change sprints or archive tasks unless asked.

## External Docs & Related Projects

- **Always verify external library/SDK APIs before coding**: Look up current documentation with the Context7 MCP server (resolve the library id, then query the docs) for Baileys, Pydantic AI, FastAPI, Fastify, grammY, Meta Cloud API, Gemini, pgvector, SQLAlchemy, Zod, Redis, Docling, Groq, etc. Fall back to `search_web` when Context7 has no entry. Never rely solely on training data.
- **Sibling repos under `/home/ubuntu/projects/`** (each has its own authoritative `CLAUDE.md`/`AGENTS.md`):
  - **Run or resume a peer agent in that repo (`agy -p` / `claude -p`)**: Invoke the CLI agent directly inside the target repo (`cd /home/ubuntu/projects/<repo> && agy -c -p "<prompt>" --dangerously-skip-permissions`, or `claude -c -p "<prompt>" --dangerously-skip-permissions`) so work runs with that repo's own `AGENTS.md`/`CLAUDE.md`, skills, and hooks, and persists a conversation the user can resume interactively (`agy -c` / `claude -c`). When spawning a new chat instead of resuming (e.g. if an interactive TUI is already open and locking the latest conversation in that repo), omit `-c` (`agy -p "<prompt>" --dangerously-skip-permissions` / `claude -p "<prompt>" --dangerously-skip-permissions`) — **always keep `--dangerously-skip-permissions`** because headless `-p` mode requires the CLI flag to auto-approve tools. (Alternatively, use `send_message` by conversation ID in `agy`, which queues into the open TUI's inbox and dequeues on the next user prompt in that terminal.)
  - **The sibling repos**:
    - **curupira-bot**: fire-alert fork (`fire_poller/`, municipality subscriptions).
    - **castanha-bot**: Brazil-nut supply-chain fork (`cadeia` tools, weekly recap, no Telegram client).
    - **fleetview**: Next.js 16 control-plane dashboard for the bot fleet. Calls `/admin/*`, `/health`, and `/knowledge-base/*` server-side. **Its Zod schemas in `lib/bot-api/schemas.ts` strictly mirror `routes/admin.py`, `schemas.py`, and `runtime_config.py` `REGISTRY`**. Renaming/removing fields or adding values to closed enums (`type`, `source`, `conversation_type`, `role`, QR `status`, KB `status`, broadcast enums) breaks the dashboard. A new field is safe.
      - **Contract check**: the contract files are `packages/ai-api/src/ai_api/routes/{admin,broadcasts,bug_reports,health,knowledge_base}.py`, `schemas.py` and the `runtime_config.py` `REGISTRY`. After changing any of them, ask a fresh peer session in fleetview (no `-c`) for the verdict instead of reading its schemas from here: `cd /home/ubuntu/projects/fleetview && agy -p "Run git -C /home/ubuntu/projects/ai-boilerplate diff origin/main...HEAD -- <the changed contract files>. Does it break a Zod schema in lib/bot-api/schemas.ts or a Server Action in lib/actions/? Read-only check: do not modify fleetview files, run write commands, or read .env* files. Answer Safe, Breaking or Needs a FleetView update, with file:line in both repos." --dangerously-skip-permissions` (or the same prompt with `claude -p ... --dangerously-skip-permissions`).

## Guidelines

- Use `pnpm add` / `uv add` for dependencies — NEVER edit `package.json` or `pyproject.toml` by hand.
- Prefer pure functions over classes; async throughout both codebases.
- Structured logging only (Pino in TS, `logging` in Python) — no `console.log` or `print`.
- Write tests for new functionality following `tests/` patterns (`unit/`, `integration/`, `mocked/`, `helpers/`).

## Testing & Common Workflows

- **TS tests (Vitest 4)**: `buildTestApp()` in `tests/helpers/fastify.ts` (no auth/rate-limit/Swagger; **register new routes there too**), fixtures in `tests/helpers/fixtures.ts`. Reset singletons (`getBaileysSocket`, `isCloudApiConnected`) with `vi.resetModules()` in `beforeEach`.
- **Python tests (pytest 9, `asyncio_mode = auto`)**: `tests/conftest.py` sets env vars and patches `create_engine` / `GoogleProvider` **before** production imports (do not reorder). Integration tests use `httpx.AsyncClient` with `ASGITransport(app=app)` + `app.dependency_overrides[get_db]`.
- **Adding an agent tool (`ai-api`)**:
  1. Define `async def tool_name(ctx: RunContext[AgentDeps], ...) -> str` with `@agent.tool` in `agent/tools/`.
  2. Import module in `agent/tools/__init__.py` (triggers registration) and describe in `agent/core.py`.
  3. Call `safe_rollback(ctx.deps.db)` (`agent/tools/_db.py`) in DB exception blocks; return generic English errors (never raw `str(e)`, pinned by `test_tool_error_hygiene.py`); log details with `logger.error(..., exc_info=True)`.
  4. Add mocked tests in `tests/mocked/`.
- **Adding an API endpoint (`ai-api`)**: Add route in `routes/*.py` with `APIRouter` + `limiter` from `deps.py`, schemas in `schemas.py`, register in `main.py` via `app.include_router()`, and add integration tests in `tests/integration/`.

## Gotchas

- **Admin API (`routes/admin.py`, `/admin/*`)**: Protected by `X-API-Key`. `GET /admin/whatsapp/qr` proxies Baileys `GET /whatsapp/qr` (5s timeout) and returns `status="unavailable"` (HTTP 200) if Baileys is unreachable.
- **Model chain**: `build_runtime_model()` builds `FallbackModel(DeepSeek → Gemini)` when `DEEPSEEK_API_KEY` is set. **Never use `agent.run_stream`** (drops tool calls after text preamble) — `agent/response.py` uses `run_stream_events`. Never put `extra_body`/`timeout` in agent/run `model_settings`. Details: `packages/ai-api/src/ai_api/agent/AGENTS.md`.
- **Runtime settings (`runtime_config.py`)**: Only mark a `REGISTRY` setting `hot=True` if its consumption site actually reads via `runtime_config.get("key")`.
- **Markdown sanitisation (`formatting.markdown_to_whatsapp`)**: Converts model Markdown to WhatsApp markup across `streams/processor.py` (including `[Partial - Error]` rows), sync `/chat`, and `send_whatsapp_message`. Formatting rule is injected via `formatting_guidance` in `agent/core.py`; keep `**` out of `DEFAULT_SYSTEM_PROMPT` (pinned by `test_formatting_guidance.py`). Telegram composes on top (WhatsApp markup → `---` bursts + Telegram HTML), pinned by `packages/ai-api/tests/fixtures/markdown_pipeline.json`.
- **Starlette middleware order**: Add `CORSMiddleware` **after** `APIKeyMiddleware` in `main.py` (LIFO execution).
- **pgvector IVFFlat index**: Must be created manually on `knowledge_base_chunks` after data load.
- **Embedding task types**: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search.
- **Redis Streams (`streams/`) is the only job pipeline**: `queue/connection.py` retains legacy `get_arq_redis` / `ARQ_KEEP_RESULT` names for compatibility.
- **Non-root Docker images & `.dockerignore`**: TS clients run as `node` (uid 1000) via `node --import ./dist/instrument.js dist/main.js` (type errors fail the image build; never add `|| true`). `ai-api` runs as `appuser` (uid 1000). Each TS client uses `packages/<pkg>/Dockerfile.dockerignore` (allowlist); keep `.logfire/` excluded in all ignore files.
