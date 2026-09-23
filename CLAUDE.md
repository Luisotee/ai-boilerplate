# CLAUDE.md

AI chat-agent system: Node.js/TypeScript clients (Baileys + Meta Cloud API + Telegram/grammY) + Python/FastAPI API (Pydantic AI + Gemini).

See @README.md for setup guide and environment variables.

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
    └── src/ai_api/    # agent/, routes/, rag/, streams/, queue/, whatsapp/, scripts/
```

**Key entry points**: `whatsapp.ts` (Baileys message router), `routes/webhook.ts` (Cloud API + Telegram webhook handlers), `telegram-client/src/updates.ts` (grammY dispatch table), `agent/core.py` (AI agent definition + system prompt), `streams/processor.py` (processing pipeline), `api-client.ts` (inter-service HTTP client).

## Message Flow

How a WhatsApp message traverses the system end-to-end:

1. Baileys WebSocket → `whatsapp.ts` `messages.upsert` event
2. `normalizeMessageContent()` unwraps viewOnce/ephemeral message wrappers
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts` (transcribe first), image → `handlers/image.ts`, document → `handlers/document.ts`
4. All handlers funnel into `handleTextMessage()` with optional base64 image/document
5. `api-client.ts` sends POST `/chat/enqueue` to AI API → returns `job_id`
6. `routes/chat.py` intercepts slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/help`) before queuing
7. Non-command messages: saved to PostgreSQL, enqueued to Redis Stream (`stream:user:{user_id}`)
8. `streams/processor.py`: fetches conversation history → runs Pydantic AI agent with tools → streams response chunks to Redis
9. `api-client.ts` polls GET `/chat/job/{id}` (500ms interval, max 120s) until complete
10. WhatsApp client sends text reply; optionally generates TTS audio if user preference enabled

**Group messages**: non-@mentioned messages are saved as history only (`saveOnly=true`), never processed by AI. Bot checks both JID and LID formats for mentions.

**On-demand history retrieval**: each run auto-injects only the last `HISTORY_LIMIT_PRIVATE`/`HISTORY_LIMIT_GROUP` messages. The `get_chat_history` agent tool (`agent/tools/history.py`) fetches more of the CURRENT chat chronologically — `limit` (flat last-N) and/or `since_hours` (time window) — via `get_conversation_messages` in `database.py` (keyed by the run's own `users.id`, so it can only read the conversation the message came from and never creates a user; naive-UTC `since`/`until` cutoffs; hard-capped at `MAX_FLAT_MESSAGES=200`). It is distinct from `get_conversation_history`, the config-limited auto-injection path. Transcripts render via `format_transcript` in `rag/conversation.py`: the bot's lines are labelled with the `BOT_NAME` setting (default `Assistant`, hot via `/admin`), group sender names are not double-prefixed, private user lines get `User:`. For topic recall use `search_conversation_history` instead.

### Telegram Message Flow (telegram-client)

1. Telegram → webhook `POST /webhook` → grammY `webhookCallback(bot, "fastify", { secretToken })` verifies the `X-Telegram-Bot-Api-Secret-Token` header
2. grammY parses the Update and dispatches on filter queries: `message:text | message:voice | message:audio | message:photo | message:document`
3. Handlers (`handlers/*.ts`) download media via `telegramApi.downloadFile()` — wrapping `bot.api.getFile()` + a fetch of `https://api.telegram.org/file/bot<TOKEN>/<file_path>`
4. Group messages: `utils/mention.ts` checks for `@bot` mention (case-insensitive), `text_mention` entity for `bot.botInfo.id`, a leading `bot_command` (`/cmd` or `/cmd@thisbot`), or reply to a bot message — otherwise save-only. Addressed group messages that look like a command also resolve the sender's admin status via `getChatMember` (`services/group-admin.ts`)
5. Funnel into `handleTextMessage(ctx, text, options)` → `sendMessageToAI()` with `client_id: "telegram"`
6. AI API processes → result polled back → split (`---` bursts, then the 4096-char cap) → converted from WhatsApp markup to Telegram HTML → delivered via `ctx.reply()` / `ctx.replyWithVoice()` inside the grammY handler
7. Typing indicator is maintained via `@grammyjs/auto-chat-action` (set `ctx.chatAction = 'typing'`, middleware refreshes every ~5s until the handler returns)
8. The synthetic JID `tg:<chat_id>` is stored in `users.whatsapp_jid`; chat IDs are integers (supergroup IDs are negative, e.g. `tg:-1001234567890`)

### Cloud API Message Flow (whatsapp-cloud)

1. Meta sends webhook POST → `routes/webhook.ts` verifies HMAC-SHA256 signature
2. Extract messages from `entry[].changes[].value.messages[]`
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts`, image → `handlers/image.ts`, document → `handlers/document.ts`
4. Handlers download media via Graph API (`services/graph-api.ts`), convert phone→JID for AI API compatibility
5. Same AI API flow as Baileys: `api-client.ts` sends POST `/chat/enqueue` (includes `client_id: "cloud"` for routing) → polls for result
6. AI API resolves `client_id` to a pre-configured URL and routes callbacks to the correct client
7. Responses sent via Meta Graph API (`POST graph.facebook.com/{phone_number_id}/messages`)

## Tooling

- **Monorepo**: pnpm workspaces (`pnpm-workspace.yaml`)
- **TypeScript**: ES2022, NodeNext modules, strict mode
- **Python**: >=3.11, managed with `uv`
- **Formatting**: Prettier (TS) + Ruff (Python) — enforced by Husky pre-commit hook (`pnpm format` runs automatically)
- **Linting**: ESLint flat config (TS) + Ruff (Python)
- **Testing**: Vitest (TS) + pytest/pytest-asyncio (Python) — `pnpm test` runs all

## Commands

```bash
# First-time setup
./setup.sh                              # Interactive: generates .env, installs Node + Python deps

# Infrastructure (Docker Compose profiles)
docker compose up -d                                    # Core: postgres, redis, api, worker, whatsapp
docker compose --profile dev up -d                      # + Adminer (DB GUI, opt-in)
docker compose --profile cloud up -d                    # + WhatsApp Cloud API client (opt-in)
docker compose --profile telegram up -d                 # + Telegram client (opt-in)
docker compose --profile dev --profile cloud up -d      # Everything

# Development (from root)
pnpm dev:server                         # Start AI API (port 8000)
pnpm dev:whatsapp                       # Start Baileys WhatsApp client (port 3001)
pnpm dev:cloud                          # Start Cloud API WhatsApp client (port 3002)
pnpm dev:telegram                       # Start Telegram client (port 3003)
pnpm dev:queue                          # Start the stream worker (chat consumer + PDF consumer; required for replies AND PDF processing)
pnpm install:all                        # Install Node + Python dependencies

# Linting & Formatting
pnpm lint                               # Check TypeScript (ESLint) + Python (Ruff)
pnpm lint:fix                           # Auto-fix lint issues
pnpm format                             # Format TypeScript (Prettier) + Python (Ruff)
pnpm format:check                       # Verify formatting without changes (CI)

# Testing (from root)
pnpm test                               # Run all tests (TS + Python)
pnpm test:ts                            # Run TypeScript tests only (both clients)
pnpm test:python                        # Run Python tests only (uv run pytest)

# Per-package
cd packages/whatsapp-client && pnpm test        # Baileys client tests
cd packages/whatsapp-cloud && pnpm test         # Cloud client tests
cd packages/ai-api && uv run pytest             # AI API tests
cd packages/ai-api && uv run pytest tests/unit  # AI API unit tests only
```

## Security

- **API Key Auth**: All servers require `X-API-Key` header on all routes except `/health` and `/docs*`
  - `AI_API_KEY` — Python AI API (required, app fails to start without it)
  - `WHATSAPP_API_KEY` — Baileys WhatsApp client (required)
  - `WHATSAPP_CLOUD_API_KEY` — Cloud API WhatsApp client (falls back to `WHATSAPP_API_KEY`)
  - `TELEGRAM_API_KEY` — Telegram client (falls back to `WHATSAPP_API_KEY`)
  - Inter-service calls include the key automatically
- **Webhook verification**: `/webhook` routes on the Cloud and Telegram clients skip API-key auth and use platform-native verification instead:
  - Cloud API: `x-hub-signature-256` HMAC-SHA256 using `META_APP_SECRET`
  - Telegram: `X-Telegram-Bot-Api-Secret-Token` compared to `TELEGRAM_WEBHOOK_SECRET` (grammY's `webhookCallback` validates it automatically when `secretToken` is provided)
- **CORS**: `CORS_ORIGINS` env var (comma-separated). Empty = block all cross-origin
- **Rate Limiting**: `RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min). On the TS clients, requests carrying a valid `X-API-Key` are exempt (`utils/api-key.ts` `hasValidApiKey`, shared with the auth hook): all inter-service traffic comes from the AI API's single IP, so a shared per-IP budget would 429 — and lose — legitimate bot traffic in a burst. `hasValidApiKey` compares **byte** lengths before `timingSafeEqual`, so a multibyte header gets a 401 instead of throwing a 500. The auth hook always runs BEFORE the limiter (`@fastify/rate-limit` attaches route-level hooks, which Fastify runs last), so bad-key requests get a 401 without ever being counted: the limiter does **not** throttle API-key guessing. `/health*` (incl. `/health/ready`) stays exempt from both
- **Group admin commands fail closed**: `/clean`, `/tts`, `/stt`, `/settings`, `/memories` in a group run only when the client sends `is_group_admin: true` — `commands.py` checks `is_group_admin is not True`. The old `is False` check let an omitted field through, and the Telegram client never sent it, so any group member could `/clean all` a group. Baileys resolves it lazily from `groupMetadata` (`isSenderGroupAdmin` in `utils/message.ts`, matching `participant`/`participantAlt` against each member's `id`/`lid`/`phoneNumber`); Telegram via `getChatMember` (`services/group-admin.ts`); a lookup failure means not-admin. Cloud API webhooks carry no group context
- **User Whitelist**: `WHITELIST_PHONES` env var — comma-separated. Empty = all users allowed (disabled). Each entry matches **either as a phone number or as a verbatim chat id**, parsed into two sets by `utils/whitelist.ts` (TS ×3, kept byte-identical by `tests/unit/whitelist-copies.test.ts`) / `ai_api/whitelist.py` and matched by `isWhitelisted` / `is_whitelisted`. The Python side is a pure mirror imported by both `routes/chat.py` (which reads the effective value via `runtime_config`) and `routes/admin.py` (which validates a proposed one); it must never be *more permissive* than the TS gate it backs up, which is why both spell out the same 29-codepoint separator class instead of using `\s`. An entry is first stripped of a device suffix (`:50@` → `@`) and a trailing `@s.whatsapp.net`; if what remains still contains an `@`, or the entry started with `tg:`, it is kept **verbatim only** (so `tg:-100…` can never be mistaken for a phone). Otherwise it is normalized to bare digits — tolerating `+`, spaces, `-`, `()`, `.` — and stored in the phone set. Entries below 5 digits never become phones. Every entry is *also* kept verbatim, which is what keeps a bare `120363…` matching a group and a bare LID matching its chat on existing installs.
  - **Normalized digits are consulted only for `@s.whatsapp.net` JIDs.** A phone entry must not admit an `@lid`/`@g.us`/`@broadcast` chat that merely shares its digits. A *bare* entry stays namespace-blind, though — that is the pre-existing `jid.split("@")[0] in whitelist` behaviour and the same code path that keeps bare `120363…` working, so it is pinned by a test rather than "fixed"
  - **The `phone` argument is never consulted for a group JID**, so a member's whitelisted phone never admits the whole group. That is narrower than "a group is matchable only by its `…@g.us` JID" — a legacy bare entry equal to the group's id still admits it When set, non-whitelisted messages are silently ignored. Checked at both WhatsApp client level (primary) and AI API level (defense in depth). **Editing the whitelist via `PATCH /admin/settings` updates only the AI-API layer** — the TS clients keep their startup env value until restarted. So a `/admin` change takes full effect for *removals* instantly (the whitelist is read only in `routes/chat.py`; `streams/` and `queue/` never evaluate it, so unlike `gemini_model` there is no worker-side TTL lag) — the AI-API layer rejects them regardless of the client — but a *newly added* JID is still blocked by the client's primary filter until its env is updated + restart, unless that client's `WHITELIST_PHONES` is empty/disabled
  - The AI-API layer matches on the client-supplied `phone` field in addition to `whatsapp_jid` (it must — see the LID gotcha below). That is not new trust: the route is already behind `X-API-Key`, and `get_or_create_user` already trusts the same field for identity. The client-side check remains primary
  - `PATCH /admin/settings` **rejects a whitelist that parses to zero entries** (`""`, `" "`, `","`). An override shadows the env value across restarts, so clearing the field would otherwise disable the gate for good; `DELETE /admin/settings/whitelist_phones` is the way to revert to the env value
  - Accepted compat break from the two-set split: a bare `<digits>:<device>` entry (a device suffix with no `@`) no longer matches, because the matcher strips the suffix off the incoming jid before comparing local parts. Use the full JID or the bare number
  - Matching is exact — there is deliberately **no** "last N digits" fuzzy fallback, which would let `…5945319` whitelist arbitrary countries. If a BR/MX-style number variant (`55DD9XXXXXXXX` vs `55DDXXXXXXXX`) fails to match, read the `phone` value from the client log and paste that
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`

## Observability

- **Sentry**: Error tracking is opt-in via `SENTRY_DSN_NODE`. Each TS package has `src/instrument.ts` which must be imported first in `main.ts` (before `config.ts`) so Sentry's OpenTelemetry hooks load before other modules. When `SENTRY_DSN_NODE` is unset, `instrument.ts` is a no-op and `Sentry.setupFastifyErrorHandler` is skipped.
- **Logfire (LLM token + cost tracking)**: Opt-in via `LOGFIRE_TOKEN` (Python AI API only). `src/ai_api/instrument.py` is the Python mirror of `instrument.ts` — `setup_instrumentation(service_name)` **returns immediately when `settings.logfire_token` is falsy**, so with no token literally nothing happens: no `configure()`, no global OTel providers, no `atexit` hook, no network
  - **The token MUST be passed as `logfire.configure(token=settings.logfire_token, ...)`.** Logfire only reads `LOGFIRE_TOKEN` from `os.environ`, and **pydantic-settings loads `.env` into the `Settings` object without exporting to the process environment**. Docker happens to work either way (`env_file:` sets real env vars), so omitting `token=` breaks *only* `pnpm dev:server` / `pnpm dev:queue` — silently, while still logging "Logfire enabled". This trap is why the guard reads `settings.*` and the config call passes `settings.*`
  - **Must be invoked in BOTH entrypoints**: `main.py` (service `ai-api`) and `scripts/run_stream_worker.py` (service `ai-api-worker`, inside `main()`). **The agent runs in the worker**, so instrumenting only the API yields zero LLM spans — that's also the end-to-end check: traces appear under `ai-api-worker`
  - **Import order does not matter.** `instrument_pydantic_ai()` sets the `Agent._instrument_default` ClassVar, which is read *per run*, not at construction. The worker in fact builds the `Agent` at import time (`streams.consumer` → `processor` → `agent`) before `main()` calls `setup_instrumentation()`, and works fine. The real invariant is **"active before the first agent run"** — and it holds only while `agent/core.py` never passes `instrument=` to `Agent(...)`, since a per-agent value takes precedence over the global default. There's a comment at that constructor saying so
  - **`include_content=False` is a privacy guarantee, not a preference** — it excludes prompts, completions, and tool args/results while keeping `gen_ai.usage.*` token counts and the `operation.cost` metric. Verified in `tests/unit/test_instrument.py` against *real* spans (in-memory exporter + canary string), not mock kwargs. Consequence: Logfire answers "what is this costing me", *not* "what did the bot say" — use the existing logs for that
  - Dollar costs come from the `genai-prices` dataset, resolved per model name, so a hot `gemini_model` switch via `PATCH /admin/settings` is costed correctly with no local price table. Note `logfire` and `genai-prices` are **not new installs** — `pydantic-ai` already pulls `pydantic-ai-slim[…,logfire,…]`, and `genai-prices` is an unconditional dep of `pydantic-ai-slim`. The `pyproject.toml` entries are version floors that make the reliance explicit
  - Tests force it off via `os.environ["LOGFIRE_TOKEN"] = ""` in `tests/conftest.py`. Note `LOGFIRE_SEND_TO_LOGFIRE` would **not** work there: `load_param` short-circuits on an explicit runtime kwarg before consulting env, and `instrument.py` always passes `send_to_logfire`
  - `LOGFIRE_TOKEN` / `LOGFIRE_ENVIRONMENT` reach both containers through `env_file: .env` — no `docker-compose.yml` entry needed. Both are `hot=False` in the `runtime_config` REGISTRY (read once at startup; restart to change). `.logfire/` is gitignored — `logfire auth` writes a live write token there

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **8 tables**: users, conversation_messages, conversation_preferences, core_memories, knowledge_base_documents, knowledge_base_chunks, bot_prompt, runtime_settings
- **No Alembic migrations** — uses SQLAlchemy `create_all()`, which only creates missing *tables*, never new columns. Column/index changes ship as hand-written, idempotent (`IF NOT EXISTS`) SQL in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql`, named so they match what `create_all()` emits on a fresh DB. Apply them to an existing Docker deployment BEFORE rolling out the code that needs them (safe to re-run):
  ```bash
  docker exec -i aiagent-postgres psql -U aiagent -d aiagent \
    < packages/ai-api/docs/migrations/<file>.sql
  ```
  (`aiagent-postgres` is the compose `container_name`; substitute your `POSTGRES_USER` / `POSTGRES_DB` if you changed the defaults.) Current migrations: `2026-09-21-kb-file-hash.sql`
- **Knowledge-base dedup**: `knowledge_base_documents.file_hash` (SHA-256 hex of the uploaded bytes, indexed, computed while streaming the upload to disk). `POST /knowledge-base/upload` answers **409** when identical content is already in the KB; the batch route rejects that file (and a repeat of an earlier file in the same batch) while accepting the rest. Only global documents with a status other than `failed` count — conversation-scoped chat PDFs never block an upload, and a failed document can be re-uploaded. Rows from before the migration have `file_hash = NULL` and never match. Both upload routes are `@limiter.exempt` (bulk loads via `./upload-kb.sh <dir>`, which posts a folder of PDFs to the batch route with `AI_API_KEY` from the env or `.env`); they stay behind `X-API-Key`
- Models: `database.py` (users, messages, preferences, core_memories, bot_prompt, runtime_settings) + `kb_models.py` (documents, chunks)
- **Core memories**: one markdown document per user (`core_memories` table), injected into the prompt via `@agent.instructions inject_core_memory` in `agent/core.py`
- **System prompt is DB-backed**: the active prompt lives in the single-row `bot_prompt` table, loaded per-run via `@agent.instructions base_system_prompt` in `agent/core.py`, falling back to the hardcoded `DEFAULT_SYSTEM_PROMPT` when no row exists. Edit it through `PUT /admin/prompt` — takes effect on the next message, no restart. Uses `instructions` (not `system_prompt`) so a changed prompt is never shadowed by one retained in `message_history`
- **Conversation-scoped PDFs** expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). Cleanup task runs in `main.py` lifespan
- **PDF processing queue**: every PDF — knowledge-base uploads (`routes/knowledge_base.py`) and chat attachments (`streams/processor.py`) — is enqueued on the shared Redis Stream `stream:pdf_processing` and parsed by `streams/pdf_consumer.py`, which `scripts/run_stream_worker.py` runs next to the chat consumer (the worker exits non-zero if either stops, so Docker restarts both). Nothing parses in the API process any more (the old FastAPI `BackgroundTasks` path is gone) or inline in a chat job (a minutes-long Docling parse used to block that user's whole stream). Semantics:
  - **Concurrency**: `KB_MAX_CONCURRENT_PROCESSING` (default 2) per worker process; a slot is acquired *before* `XREADGROUP`, so waiting jobs stay in Redis rather than piling up in memory. **Memory trade-off**: the worker reuses the api image, so with `INSTALL_DOCLING=true` each Docling parse (1-2 GB) runs inside the 2G worker container next to the agent — keep this at 1-2 or raise the worker's memory limit. LlamaParse-only deployments are light
  - **Ack / retry / dead letter**: a job is `XACK`ed and `XDEL`eted once settled. Retriable failures (`processing.is_retriable_error`: timeouts, httpx/LlamaParse transport errors, HTTP 408/429/5xx — it walks `__cause__`/`__context__` because the pipeline wraps timeouts in a `ValueError`) and "0 chunks stored" are retried up to `KB_MAX_PDF_RETRIES` (3) times with `KB_RETRY_BASE_DELAY_SECONDS * 4**attempt` backoff, parked in the `pdf_processing:retry` sorted set so a restart doesn't lose them. Everything else, and exhausted retries, is copied to `stream:pdf_processing:dead` with a `reason`
  - **Crash recovery**: a job whose worker died mid-parse (restart, OOM kill) stays pending and is reclaimed by an `XAUTOCLAIM` sweep every 15s once idle > `2 * KB_PROCESSING_TIMEOUT_SECONDS + 60s`; it counts as an attempt, so a PDF that keeps OOM-killing the worker ends in the dead letter instead of looping. `process_pdf_document` deletes the document's existing chunks first, so re-running a job never duplicates them
  - **Chat UX**: the chat job reacts ⏳, enqueues the PDF and tells the agent the document "is not searchable yet" (so it acknowledges rather than inventing a summary); the consumer reacts ✅ when the document is `completed` (searchable via `search_knowledge_base`) or ❌ when it permanently failed or ended `partial` (search only reads `completed` documents). If the enqueue itself fails, the user gets the old "couldn't process your document" reply
  - `file_path` in the job is the API's path; the API and worker must share the upload directory (`knowledge-base-data` volume at `/app/knowledge_base` in compose)
  - New uploads start as `queued` (formerly `pending`; old rows may still say `pending`, and the status filter accepts both). If the enqueue fails, the upload is rolled back (row + file deleted) and the route answers 503 / rejects that batch file
- **PDF parsing**: LlamaParse (cloud, primary) via `llama-cloud` SDK, with **optional** Docling fallback behind the `[docling]` extra. Behavior controlled by `PDF_PARSER` (`auto` | `llamaparse` | `docling`). In `auto` mode, LlamaParse runs when `LLAMA_CLOUD_API_KEY` is set and falls back to Docling on any parser error *if* the extra is installed
- **Speech-to-Text**: Groq Whisper (cloud, primary) plus an **optional** self-hosted Whisper server (speaches by default, any OpenAI-compatible endpoint works). Controlled by `STT_PROVIDER` (`auto` | `groq` | `whisper`). In `auto` mode: with `GROQ_API_KEY` set, Groq runs and falls back to self-hosted on recoverable errors when `WHISPER_BASE_URL` is also set; with only `WHISPER_BASE_URL` set, self-hosted runs alone; if neither is configured, `/transcribe` returns 503. Start the self-hosted container with `docker compose --profile whisper up -d`

## Environment Config

- Root `.env` loaded first (shared vars) — see @.env.example for all required variables
- **Shared secrets live in root `.env` only.** Never duplicate credentials (API keys, DB passwords, Meta tokens, etc.) in package-level `.env.local` — they belong in root `.env` only, and `setup.sh` writes them there
- **`.env.local` is for per-developer customization** (log level, port, feature flags). It loads with `override: true`, so any duplicated key silently wins over root — including empty `KEY=` lines that blank out the root value
- The TS config loaders warn at startup when `.env.local` shadows a root key. If you see `[config] .env.local overrides root .env: X`, confirm it's intentional
- TS config loader: `packages/whatsapp-client/src/config.ts` (Baileys), `packages/whatsapp-cloud/src/config.ts` (Cloud API)
- Python config: pydantic-settings in `packages/ai-api/src/ai_api/config.py`

## Mandatory Subagent: docs-fetcher

**ALWAYS use the `docs-fetcher` subagent before writing or modifying code that touches any external library, SDK, API, or framework.** Do not rely on training data for API signatures, method names, or behavior — fetch current documentation first. This applies to Baileys, Pydantic AI, FastAPI, Fastify, Meta Cloud API, Gemini, pgvector, SQLAlchemy, Zod, Redis, Docling, Groq, and any other dependency. Launch `docs-fetcher` in parallel with your planning or exploration to avoid blocking.

## Guidelines

- Use `pnpm add` / `uv add` for dependencies — NEVER edit package.json/pyproject.toml directly
- Prefer pure functions over classes
- Async throughout both codebases
- Use structured logging (Pino for TS, Python `logging`) — no console.log/print
- Write tests for new functionality — follow existing patterns in `tests/` directories
- New TS files may fail `pnpm format:check` even if lint passes — run `pnpm exec prettier --write <path>` on freshly created files
- Keep this file updated with important changes

## Testing

### Structure
Each package has `tests/` with: `unit/` (pure functions, no I/O), `integration/` (HTTP routes via app injection), `helpers/` (factories + test app builders). ai-api also has `mocked/` (external deps mocked: DB, Redis, Google API).

### Frameworks & Config
- **TypeScript**: Vitest 4 — config in `vitest.config.ts`, tests match `tests/**/*.test.ts`
- **Python**: pytest 9 + pytest-asyncio — config in `pytest.ini`, `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed)

### Key Patterns
- **TS integration tests**: Use `buildTestApp()` from `tests/helpers/fastify.ts` — builds Fastify with all routes but NO auth, rate limiting, or Swagger
- **TS fixtures**: `makeMockSocket()` / `makeMockGraphApi()` and message factories (`makeTextMsg`, `makeWebhookBody`, etc.) in `tests/helpers/fixtures.ts`
- **Python conftest.py**: Session-scoped patches for `sqlalchemy.create_engine` and `GoogleProvider` — prevents real DB/API connections. Must run before production imports
- **Python integration tests**: Use `httpx.AsyncClient` with `ASGITransport(app=app)` + `app.dependency_overrides[get_db]` for mock DB. Rate limiter disabled via `tests/integration/conftest.py`
- **Python factories**: `tests/helpers/factories.py` — `make_conversation_message()`, `make_user()`, `make_http_response()` return `MagicMock` objects mimicking ORM models

### Gotchas
- TS singleton state (`getBaileysSocket`, `isCloudApiConnected`) needs `vi.resetModules()` in `beforeEach` to reset between tests
- New routes must also be registered in `tests/helpers/fastify.ts` (`buildTestApp()`) — integration tests won't see them otherwise
- TS `fetch` tests use `vi.stubGlobal('fetch', mockFetch)` + `vi.useFakeTimers()` for timeout testing
- Python `conftest.py` sets env vars BEFORE any production code import — order matters, don't rearrange
- No coverage tooling configured — no `pytest-cov` or `@vitest/coverage-*`
- No CI/CD pipeline runs tests — testing is local only

## Common Workflows

### Adding a new message handler (whatsapp-client)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `sendFailureReaction(sock, msg)` + `logger.error` in catch, `sendPresenceUpdate('paused')` in finally
3. Register it in `src/whatsapp.ts` inside the `messages.upsert` event handler
4. Add any new routes in `src/routes/` with Zod schemas in `src/schemas/`
5. Add unit tests in `tests/unit/` and integration tests in `tests/integration/` following existing patterns

### Adding a new agent tool (ai-api)
1. Add tool function in `agent/tools/` using the `@agent.tool` decorator (see existing tools for patterns)
2. Signature: `async def tool_name(ctx: RunContext[AgentDeps], ...params) -> str`
3. Import the module in `agent/tools/__init__.py` — the import triggers decorator registration
4. Add tool description to the system prompt in `agent/core.py`
5. Tool accesses deps via `ctx.deps` (db, embedding_service, whatsapp_client, etc.)
6. Add mocked tests in `tests/mocked/` following existing patterns (mock external deps, test tool behavior)
7. Exception handlers that touch the DB must call `safe_rollback(ctx.deps.db)` (`agent/tools/_db.py`)
8. Error return values must be generic — never raw `str(e)` (details belong in `logger.error(..., exc_info=True)`)

### Adding a new API endpoint (ai-api)
1. Add route in the appropriate `routes/*.py` file (or create a new router module)
2. Use `APIRouter` with appropriate tags; import `limiter` from `deps.py` for rate-limited endpoints
3. Add Pydantic schemas in `schemas.py`
4. Register new router in `main.py` via `app.include_router()`
5. Add integration tests in `tests/integration/` using `httpx.AsyncClient` with `ASGITransport`

### Adding a new message handler (whatsapp-cloud)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `graphApi.sendReaction(senderPhone, messageId, '❌')` + `logger.error` in catch
3. Register in `src/routes/webhook.ts` inside the message type dispatch switch
4. Use `jidToPhone()` / `phoneToJid()` from `utils/jid.ts` when crossing API boundaries
5. Download media via `graphApi.downloadMedia(mediaId)` instead of Baileys `downloadMediaMessage()`
6. Add unit tests in `tests/unit/` and integration tests in `tests/integration/` following existing patterns

### Adding a WhatsApp media route (multipart — whatsapp-client)
Multipart routes can't use Zod validation directly. Follow the pattern in `routes/media.ts`:
1. Use plain JSON Schema for `schema.body` (not Zod)
2. Add custom `validatorCompiler: () => (data) => ({ value: data })` to bypass automatic validation
3. Extract fields from `request.body` — **multipart form fields are `{ value: string }` objects**, not raw strings
4. Validate files with `validateMediaFile()` from `utils/file-validation.ts`
5. Get socket via `getBaileysSocket()` from `services/baileys.ts`

## Gotchas

### Baileys / WhatsApp (TS)
- **WA Web version must be fetched, never pinned**: Baileys bundles a hardcoded WhatsApp Web version that goes stale within months; once WhatsApp rejects it, pairing dies with `Connection Failure` / `statusCode: 405` at the handshake, before any QR is emitted (existing sessions keep reconnecting). `services/wa-version.ts` `getWaVersionConfig()` fetches the live version via **`fetchLatestWaWebVersion()`**. Prefer it over `fetchLatestBaileysVersion()`, which returns Baileys' bundled constant while reporting `isLatest: true` — fine until it rots, which is the whole failure mode. Do NOT hardcode a version tuple: a pinned tuple is what left the forks broken. Only `fetchLatestWaWebVersion` forwards a `signal` to `fetch`; both *resolve* (never reject) on failure, handing back the stale bundled version, so branch on the returned `error` field. Successful fetches are cached per process (`initializeWhatsApp()` is also the reconnect path); failures are not, so they retry
- **NEVER pass `version: undefined` to `makeWASocket`**: Baileys merges config as `{ ...DEFAULT_CONNECTION_CONFIG, ...config }`, so an explicit `undefined` key *overwrites* its bundled default instead of falling through to it. `getUserAgent()` then does `config.version[0]` and throws `TypeError`, which surfaces as a non-Boom close event (`statusCode` is `undefined`, so no 405 branch fires) and retries forever. Omit the key entirely — that's why `getWaVersionConfig()` returns a spreadable `{ version?: WAVersion }` fragment (`{}` on failure) rather than `WAVersion | undefined`. Same trap for any other optional `makeWASocket` option
- Must call `normalizeMessageContent()` before type-checking any message — unwraps viewOnce/ephemeral wrappers
- `contextInfo` (mentions, quoted messages) is nested under specific message types (`imageMessage.contextInfo`, `audioMessage.contextInfo`, etc.) — NOT only on `extendedTextMessage`
- Bot identity uses two formats: JID (`@s.whatsapp.net`) and LID (`@lid`) — both must be checked for mentions/replies (see `utils/message.ts`)
- Group admin check is lazy: only fetches `groupMetadata` when message text starts with `/`. `isSenderGroupAdmin` (`utils/message.ts`) matches `key.participant`/`participantAlt` against each member's `id`/`lid`/`phoneNumber` — comparing only `id` to `participant` missed admins whenever LID vs phone addressing differed, and the API now fails closed on anything but `true`
- **Group `sender_jid` is the participant's PHONE JID** (`resolveParticipantJid` in `utils/jid.ts`: direct phone JID → `key.participantAlt` → LID↔PN store, falling back to the device-stripped raw LID), so one human is recorded under one identity regardless of LID or phone addressing. It reuses `resolveSenderPhone`, so an unresolvable LID logs the same warnings
- Only PDF documents are accepted for processing; other types return a user-facing error message
- **The whitelist check must run after `resolveSenderPhone()`**: under Baileys v7 a chat is usually LID-addressed, so `msg.key.remoteJid` is `…@lid` — an anonymized account id whose digits are NOT a phone. Comparing those digits to a phone entry is why a documented bare-phone whitelist silently matched nothing. The check now sits in `whatsapp.ts` *between* identity resolution and content handling, which is a narrow window on both sides: it must be **after** `resolveSenderPhone` (no phone before it) and **before** `sock.user!.id` — if `sock.user` is unset that dereference throws into the per-message `catch`, which calls `sendFailureReaction`, i.e. it would react into a chat that was just blocked. It also stays ahead of `transcribeAudioMessage` / `extractImageData` / `extractDocumentData` / `groupMetadata`, so a blocked message still costs nothing. Enforced by `tests/unit/whatsapp-whitelist-placement.test.ts` — the ordering is invisible to every other test, so without it the gate could silently drift back above `resolveSenderPhone` and restore the original bug
  - Two caveats on "a blocked message costs nothing". It holds only for the **client-side** filter: in the admin-managed mode above (client `WHITELIST_PHONES` empty, enforcement at the AI API), the 403 lands *after* Cloud has sent read receipts + typing and downloaded or transcribed the media. And `resolveSenderPhone` now runs for blocked messages too — cheap (in-process LRU or one local keystore read, no network), but each blocked LID miss emits a `logger.warn`/`logger.error` from `utils/jid.ts`, so a spam wave against a tight whitelist floods the logs at warn/error
- **An unresolvable LID fails closed**: if `key.remoteJidAlt` is absent *and* the LID↔PN store has no mapping (a freshly-paired session), `resolveSenderPhone` returns `undefined` and a phone-whitelisted contact is dropped until the mapping warms. Hosted LIDs (`@hosted`) never resolve at all. The drop logs `phoneResolved: false`; the escape hatch is to whitelist the raw `…@lid` verbatim

### Telegram (TS)
- **Reaction emoji mismatch**: Telegram's allowed standard-emoji reactions (Bot API 7.x) do NOT include ⏳, ✅, or ❌ — the three status emojis the WhatsApp clients use. `services/telegram-api.ts` maps them to `🤔 / 👍 / 👎` on the way out. `400 BAD_REQUEST: REACTION_INVALID` is the only error `sendReaction` swallows; 401/403/429/network errors re-throw so the `bot.catch` boundary can log them. Callers that treat reactions as best-effort (`handlers/text.ts`, voice transcription fallback) wrap the call in their own try/catch
- **Privacy mode must be OFF** (via `@BotFather` → `/setprivacy` → Disable) for the bot to see non-addressed group messages. After toggling, **the bot must be removed and re-added** to existing groups — Telegram caches privacy state on join
- **`bot.init()` is required on startup in webhook mode** to populate `bot.botInfo.id` / `bot.botInfo.username`. Mention detection in `utils/mention.ts` relies on this; `main.ts` awaits `bot.init()` before calling `markBotReady()`
- **`bot.botInfo` THROWS when uninitialised** (grammY ≥ 1.4x) — it is not merely undefined, so every read must be guarded by `bot.isInited()` (`botIdentity()` in `updates.ts`). A misordered bootstrap then degrades to "does not answer in groups" instead of throwing on every update
- **`ctx.chatAction = 'typing'`** (from `@grammyjs/auto-chat-action`) is the canonical "keep typing alive for the duration of a long handler" idiom — the middleware refreshes every ~5s until the handler returns. Unlike Meta Cloud API (one-shot per wamid), Telegram keeps refreshing across multi-burst AI replies. Do NOT roll your own `setInterval`
- **20 MB download limit** (Bot API cloud): oversize files fail with a `GrammyError` (400 "file is too big"). `handlers/document.ts` returns a discriminated `DocExtraction` (`ok | wrong-type | too-large | download-error`) and `updates.ts` surfaces a distinct user-facing reply per kind. Voice and photo handlers still collapse failures to null and show a generic "try again" reply. For larger uploads you'd need to run a self-hosted `tdlib/telegram-bot-api` server (not done)
- **Photos arrive as an array of sizes** (`message.photo[]` from thumb → largest); `handlers/photo.ts` always picks the last entry
- **Chat IDs are integers**, and supergroup/channel IDs are **negative** (e.g. `-1001234567890`). `utils/telegram-id.ts` renders them verbatim as `tg:-1001234567890`
- **Path naming is intentional**: the Telegram client serves the same `/whatsapp/*` paths as the WhatsApp clients (`send-text`, `send-reaction`, `typing`, `send-location`, `send-contact`) so the Python `WhatsAppClient` works unchanged for all three platforms. `send-location` uses `sendVenue` when both `name` and `address` are given (Telegram's `sendLocation` has no label parameters, and `sendVenue` requires both), otherwise `sendLocation`. `send-contact` uses `sendContact` and adds a vCard (`utils/vcard-builder.ts`) only when email or org is set
- **4096-character limit**: `sendMessage` accepts 1–4096 characters after entity parsing. A longer message is a 400 that the plain-text fallback does NOT catch, so the whole reply would be lost. `splitByLength` in `utils/message-split.ts` enforces the cap on every path — including when bursting is disabled (the group path) — and `telegramApi.sendText` applies it too. Split the RAW text before `waMarkupToHtml` so tags stay balanced in each chunk
- **Formatting**: the global `bot_prompt` is WhatsApp-flavoured, so `utils/wa-markup-to-html.ts` converts `*bold*`/`_italic_`/`~strike~`/`` `code` `` to Telegram HTML at send time (`handlers/text.ts` replies and `telegramApi.sendText`), with one plain-text retry on `can't parse entities`. HTML needs 3 escaped characters and MarkdownV2 18, where one stray `.` or `-` rejects the whole message. Don't add per-platform syntax rules to the prompt
- **`/cmd@BotName` is a `bot_command` entity, not a mention.** Telegram sends it as one entity with no separate `mention`, so `isAddressedToBot` checks `bot_command` too (a bare `/cmd` also counts; `/cmd@OtherBot` does not) — otherwise the form Telegram's own command menu produces in groups is saved as chatter. The AI API's `normalize_command` (`commands.py`) strips the `@BotName` suffix before matching commands
- **Group admin status must be sent explicitly** — the AI API refuses group admin commands unless `is_group_admin` is exactly `true` (see Security). `updates.ts` looks it up lazily, only for addressed group messages that look like commands
- **Route exempt from API-key auth**: `/webhook` verifies via Telegram's `X-Telegram-Bot-Api-Secret-Token` header
- **Voice notes must be OGG/Opus** for `sendVoice` to render them as voice (not audio). The AI API's `/tts` endpoint defaults to `format=ogg` so no re-encoding is needed — pipe the returned bytes into `ctx.replyWithVoice(new InputFile(buffer, 'reply.ogg'))`
- **Whitelist format**: `WHITELIST_PHONES` entries for Telegram must be the full synthetic JID `tg:<chat_id>` (users) or `tg:-<group_id>` (groups). The Bot API exposes no phone for a chat, so `passesWhitelist` passes no phone and Telegram matches on the id set only — phone-shaped entries live in a separate set it never consults (and a `tg:` id has no `@`, so the local-part clauses are skipped too), so a bare number can never admit a chat with the same digits. See `tests/unit/whitelist.test.ts` and `tests/unit/test_whitelist.py`

### Cloud API / WhatsApp (TS)
- **24-hour messaging window**: Can only send free-form messages within 24h of customer's last message — outside this window, template messages are required (not implemented)
- **Typing indicators**: Cloud API supports typing via the mark-as-read endpoint with `typing_indicator: { type: 'text' }` — auto-dismisses after 25s or on first outbound reply (whichever is first). Fired from `routes/webhook.ts` before the type-dispatch switch so media messages (audio/image/document) show typing before Graph download or transcription starts. Meta limits it to **one-shot per inbound wamid** — cannot be refreshed mid-response (no typing between multi-burst chunks; no `audio`/"recording" type). Default Graph API version is `v23.0`; `paused` state is a no-op
- **No message edit/delete**: Cloud API doesn't support editing messages; deletion is supported but not implemented — `operations.ts` routes return 501 for both
- **Media URL expiry**: Downloaded media URLs from Graph API are temporary — `downloadMedia()` fetches URL and downloads immediately in one call
- **Webhook routes exempt from API key auth**: `/webhook` GET/POST use HMAC signature verification via `META_APP_SECRET` instead
- **Phone ↔ JID translation**: Cloud API uses plain phone numbers, AI API expects JIDs — conversion happens at the Cloud client boundary via `utils/jid.ts`
- **client_id routing**: Each client sends a `client_id` (`"baileys"` or `"cloud"`) in enqueue requests — the AI API maps this to a pre-configured URL (`WHATSAPP_CLIENT_URL` / `WHATSAPP_CLOUD_CLIENT_URL`) to route callbacks
- **Whitelist group JID limitation**: `WHITELIST_PHONES` supports group JIDs (e.g. `120363...@g.us`) only on the Baileys client. The Cloud API webhook payload does not include group context — only the individual sender's phone number — so group JID entries in the whitelist have no effect for Cloud API messages. Cloud does accept every phone entry format (`+49…`, spaced, `…@s.whatsapp.net`) — `passesWhitelist` in `routes/webhook.ts` feeds the matcher both `phoneToJid(senderPhone)` and `+<senderPhone>`

### AI API (Python)
- **Admin API (`routes/admin.py`, `/admin/*`)**: management-dashboard contract — `GET/PUT/DELETE /admin/prompt`, `GET/PATCH /admin/settings` + `DELETE /admin/settings/{key}`, read-only `GET /admin/users` and `GET /admin/users/{jid}/messages`, `GET /admin/overview`, `GET /admin/whatsapp/qr`. Protected by the standard `X-API-Key` (NOT in `_AUTH_EXEMPT_PREFIXES`)
- **`GET /admin/whatsapp/qr`** proxies the Baileys client's `GET /whatsapp/qr` (link status + pairing QR) using a short 5s timeout. It targets the Baileys client via `get_whatsapp_client_url(None)`, so in a multi-client deployment where `WHATSAPP_CLIENT_URL` is unset (defaults to `localhost:3001`) or the Baileys client isn't running, it reports `status="unavailable"` (200, not an error)
- **Live model switching**: `deepseek_model` and `gemini_model` are hot settings (`gemini_model` defaults to `gemini-3.1-flash-lite`, `deepseek_model` to `deepseek-flash`, set at boot by `GEMINI_MODEL`/`DEEPSEEK_MODEL` in root `.env`). `agent/response.py` calls `build_runtime_model()` (in `agent/core.py`) on every `agent.run_stream`, which calls `model_chain.build_model()` with the current `runtime_config` values: `FallbackModel(DeepSeek → Gemini)` when `DEEPSEEK_API_KEY` is set, a bare `GoogleModel` otherwise. Edit through `PATCH /admin/settings {"overrides":{"gemini_model":"..."}}` — takes effect on the next message in the API process, ≤ ~10s in the stream worker via the runtime_config TTL cache. PATCH rejects empty strings and values over 200 chars for both keys (`_MODEL_NAME_KEYS` in `routes/admin.py`); bad names surface as a normal agent error on the next call (no allowlist). A typo'd DeepSeek name is a 404, so every message silently falls back to Gemini with DeepSeek skipped in 60s cooldowns — look for the `DeepSeek (...) failed, falling back to Gemini` warning in the worker logs
- **Model chain: DeepSeek → Gemini, DeepSeek optional** (`agent/model_chain.py` `build_model`). Without `DEEPSEEK_API_KEY` the agent runs on Gemini alone, exactly as before. With it, the primary is `deepseek-flash` (`DEEPSEEK_MODEL`; reads images — `deepseek-v4-pro` does not, so image messages would fail over to Gemini) called directly on `api.deepseek.com` via pydantic-ai's `DeepSeekProvider`, and `GEMINI_MODEL` is the fallback. **Chat content goes to DeepSeek's servers in China whenever the key is set.** TTS (`TTS_MODEL`) and embeddings (`gemini-embedding-001`, 3072 dims) stay on Gemini either way. **Scheduled shutdowns**: `gemini-3.1-flash-lite` on 2027-05-07 (replacement `gemini-3.5-flash-lite` — a `gemini_model` setting change) and `gemini-embedding-001` on 2028-05-14 (replacement `gemini-embedding-2` — requires re-embedding every stored vector)
- **DeepSeek thinking is disabled via `extra_body`**: `deepseek-flash` reasons by default, and those hidden tokens are billed as output and delay the reply. `DEEPSEEK_MODEL_SETTINGS` sends `{"thinking": {"type": "disabled"}}` in the DeepSeek model's OWN `settings=`, which pydantic-ai merges per model — never move `extra_body` (or `timeout`) into agent- or run-level `model_settings`: those override every model's own value and would be sent to Gemini too
- **DeepSeek fallback mechanics (`agent/model_chain.py`)** — each point is reproduced with a fake transport in `tests/mocked/test_model_chain_fallback.py`:
  - **`fallback_on=FALLBACK_ON` = `(ModelAPIError, openai.APIError, httpx.TransportError)`, not the default.** pydantic-ai converts OpenAI SDK errors to `ModelAPIError` only around `create()`, which returns as soon as the stream's HEADERS arrive. The first chunk is read afterwards, unconverted: a `httpx.ReadTimeout`/`RemoteProtocolError` or an SSE error event (`openai.APIError`) escapes raw
  - **The DeepSeek model is wrapped in `GuardedModel` (a `WrapperModel`)**, which adds three things. (1) A wall-clock `asyncio.timeout(DEEPSEEK_TIMEOUT_SECONDS)` around OPENING the stream (including the first-chunk peek): while a request waits in DeepSeek's queue it streams `: keep-alive` lines for up to ~10 min, and each resets httpx's per-read timeout, so the httpx timeout alone never fires. (2) A `DeepSeek (<model>) failed, falling back to Gemini` warning with the traceback — `FallbackModel` otherwise discards the errors it falls back from. (3) A per-process 60s `Cooldown` that skips DeepSeek entirely: fallback is decided per model REQUEST and a reply makes one per tool round trip, so without it an outage costs a timeout on every step. The cooldown trips on timeouts/transport/SSE errors and HTTP 401/402/403/404/429/5xx, NOT on other 4xx (400/422 — an oversized context or rejected image is one request's problem)
  - **No fallback after the first chunk** (pydantic-ai design) — a stream that drops mid-reply raises the raw error, which is why `MODEL_ERRORS` (below) includes `openai.APIError`/`httpx.TransportError`. Nothing partial has been delivered by then: chunks are published only when the reply completes
  - **Never run the chat agent with `agent.run_stream`**: it treats the first text it receives as the final output and does NOT execute tool calls made after that text in the same response. DeepSeek often writes a preamble ("Let me check…") and then calls a tool, so the user got only the preamble and the tool never ran. `get_ai_response` (`agent/response.py`) uses `agent.run_stream_events`, which runs every tool call and yields only the final output once. Model requests are still streamed, so `GuardedModel`'s first-chunk timeout and the fallback are unaffected
  - The client keeps `max_retries=0` (the SDK's default 2 retries would triple the wait); `httpx.Timeout(DEEPSEEK_TIMEOUT_SECONDS)` still bounds gaps between chunks once the reply is flowing
  - Tests force `DEEPSEEK_API_KEY=""` in `tests/conftest.py` (the provider is built at import); tests that need one install it via `model_chain.deepseek_provider`
- **Model errors get a fallback reply, not a failed job** (`streams/processor.py`): `MODEL_ERRORS` (`agent/model_chain.py` — `ModelAPIError`, `UnexpectedModelBehavior`, `FallbackExceptionGroup`, `openai.APIError`, `httpx.TransportError`) around the `get_ai_response` loop → log, ❌ reaction on the user's message (sent by the API), publish `MODEL_ERROR_FALLBACK_TEXT` as a normal COMPLETED job (metadata carries `model_error: true`, no `status`), and do NOT save it to history (it would replay to the model as a prior assistant turn). Deliberately not `status: "failed"`: the TS clients answer a failed job with their OWN error text + ❌, so doing both would double both. Consequence: a user with TTS on also gets the fallback as a voice note. Every other exception keeps the generic path (`status: "failed"` metadata + re-raise). The sync `POST /chat` maps the same `MODEL_ERRORS` to 503 `AI model temporarily unavailable` (other errors stay a generic 500)
- **Runtime settings overlay (`runtime_config.py`)**: a curated subset of settings (TTS/STT, semantic/KB search, history limits, `bot_name`, PDF parser, whitelist, PDF TTL, core-memory length, **`gemini_model`**, **`deepseek_model`**) can be overridden at runtime via the `runtime_settings` table. Behavioural code reads them through `runtime_config.get("key")` (env default ← DB override), cached in-process ~10s and busted on write. The `REGISTRY` in `runtime_config.py` is the source of truth: `hot=True` = overridable, `hot=False` = display-only/"needs restart" (bootstrap settings: DB/Redis/pool, CORS, rate limits, log level). **Only mark a setting `hot` if its consumption site actually reads via `runtime_config.get()`** — otherwise the override silently never applies. `PATCH /admin/settings` rejects unknown/non-hot keys with 400
- Slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/help`) are intercepted in `routes/chat.py` — they never reach the AI agent
- Core memory is a single markdown document per user (not individual rows) — the AI reads the whole doc and rewrites it via `update_core_memory` tool (an empty string clears it; there is deliberately no separate show/clear tool — the document is already injected into every run and `/memories [clear]` covers the user-driven path)
- **Model output is Markdown-sanitised**: models drift into Markdown (`**bold**`, `[label](url)`, `#` headings) despite the prompt, and WhatsApp renders it literally. `formatting.markdown_to_whatsapp` (pure, idempotent, never touches code spans or URLs, leaves `---` burst lines byte-for-byte) converts every model-authored text: in `streams/processor.py` before delivery/embedding/save and on the `[Partial - Error]` history row (saved Markdown would be replayed as history and reinforce the habit), in the sync `/chat` route, and in the `send_whatsapp_message` tool. The formatting *rule* lives in the `formatting_guidance` instructions hook (`agent/core.py`), sent on every run and to every client, because a `bot_prompt` DB override replaces `DEFAULT_SYSTEM_PROMPT` wholesale. Keep `**` out of the default prompt itself (pinned by `tests/mocked/test_formatting_guidance.py`) — it demonstrates the syntax it forbids. Telegram composes on top: model Markdown → WhatsApp markup (ai-api) → `---` bursts + Telegram HTML (client). `packages/ai-api/tests/fixtures/markdown_pipeline.json` pins both hops and is asserted from both sides (`tests/unit/test_formatting.py`, telegram-client `tests/unit/markdown-pipeline.test.ts`) — regenerate the expected columns if either converter changes on purpose
- CORS middleware must be added AFTER `APIKeyMiddleware` in `main.py` (Starlette processes middleware LIFO — reversing this breaks CORS preflight)
- pgvector IVFFlat index must be created manually for `knowledge_base_chunks` — without it, similarity search does full table scan
- **Redis Streams (`streams/`) is the only job pipeline.** The dead arq worker (`queue/worker.py`, `scripts/run_worker.py`) and the `arq` dependency were removed; `queue/` now holds only the Redis plumbing that outlived it — `queue/connection.py` (plain `redis.asyncio`), job metadata and response-chunk storage. The `get_arq_redis` / `close_arq_redis` / `create_arq_pool` names and the `ARQ_KEEP_RESULT` env var (still the job-metadata TTL) keep their legacy names on purpose: renaming them would break callers, test patches and existing overrides
- Embedding task types matter: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search — mixing them degrades retrieval quality
- Agent tool modules must be imported in `agent/tools/__init__.py` or the `@agent.tool` decorators won't register
- Agent tools that touch `ctx.deps.db` must call `safe_rollback(ctx.deps.db)` (`agent/tools/_db.py`) in their except blocks — all tools share one DB session per agent run, so a failed flush/commit leaves it dirty and later tool calls hit `PendingRollbackError`. `safe_rollback` swallows (and logs) a failing rollback, so it can never replace the tool's reply with a crash
- Agent tool error returns must NOT include raw `str(e)` — SQLAlchemy/httpx exceptions can leak DB hostnames, table names, SQL or internal URLs to the LLM → user. Return a generic English message; details go to `logger.error(..., exc_info=True)`. Pinned by `tests/mocked/test_tool_error_hygiene.py`

### General
- Husky pre-commit hook runs `pnpm format` automatically — do NOT run format manually before committing
- **Logging/shutdown (TS)**: pino-pretty is used only when `NODE_ENV !== 'production'` (the Dockerfiles set `production`, so containers emit plain JSON). All three clients handle SIGTERM/SIGINT with `app.close()` in try/catch, flush Sentry and exit 0; startup failures and `unhandledRejection` still go through `shutdownWithError` (Sentry flush, exit 1)
- **Docker images run as non-root.** The three TS clients are multi-stage builds: `tsc` → `dist/` in a builder stage, production-only deps in the runtime stage, `USER node` (uid 1000), started with `node --import ./dist/instrument.js dist/main.js` so Sentry's ESM hooks register before `main.js`'s imports are linked (`main.js` still imports `./instrument.js`; it resolves to the same module and is not evaluated twice). **Type errors fail the image build** — never add `|| true` to the build step. The ai-api image runs as `appuser` (uid/gid 1000); code and venv are root-owned/read-only, the writable paths are `/app`, `/app/knowledge_base` and `/home/appuser/.cache` (`HF_HOME`, `EASYOCR_MODULE_PATH`, `TIKTOKEN_CACHE_DIR` for Docling/tiktoken models; the worker mounts the `model-cache` volume there)
- **Build-context ignore files**: the TS clients build from the repo root, so a `packages/<pkg>/.dockerignore` would be ignored. Each uses an allowlist `packages/<pkg>/Dockerfile.dockerignore` (BuildKit uses it *instead of* the root `.dockerignore`), which is why their contexts are a few hundred KB. The ai-api context is `packages/ai-api`, so its own `.dockerignore` applies. Keep the `.logfire/` exclusions in all of them
- **Upgrading an existing deployment to the non-root images**: named volumes created by the old root images keep root ownership, so the new processes can't write them (Baileys can't save its session; uploads fail). Once, after building the new images:
  ```bash
  docker compose build
  docker compose stop whatsapp api worker
  docker compose run --rm --no-deps --user root --entrypoint chown whatsapp -R node:node /app/packages/whatsapp-client/auth_info_baileys
  docker compose run --rm --no-deps --user root --entrypoint chown api -R appuser:appuser /app/knowledge_base
  docker compose up -d
  ```
  New volumes need nothing: Docker copies the image directory's ownership on first mount
- ai-api Dockerfile installs `ffmpeg` always (used by pydub for TTS/STT). `poppler-utils`, `tesseract-ocr`, and `libmagic1` are only installed when `INSTALL_DOCLING=true` (build arg) — the default image uses LlamaParse only and skips them to stay lean. Docker Compose forwards `${INSTALL_DOCLING}` from the shell environment as a build arg
- API docs: http://localhost:8000/docs (AI API), http://localhost:3001/docs (Baileys client), http://localhost:3002/docs (Cloud API client)
- DB GUI: http://localhost:8080 (Adminer)
