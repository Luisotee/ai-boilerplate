# CLAUDE.md

AI WhatsApp agent system: Node.js/TypeScript clients (Baileys + Meta Cloud API) + Python/FastAPI API (Pydantic AI + Gemini).

See @README.md for setup guide and environment variables.

## Structure

```
packages/
├── whatsapp-client/   # TypeScript — Fastify server (port 3001) + Baileys WhatsApp connection
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
├── whatsapp-cloud/    # TypeScript — Fastify server (port 3002) + Meta WhatsApp Cloud API
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
└── ai-api/            # Python — FastAPI server (port 8000) + Pydantic AI agent
    └── src/ai_api/    # agent/, routes/, rag/, streams/, queue/, whatsapp/, scripts/
```

**Key entry points**: `whatsapp.ts` (Baileys message router), `routes/webhook.ts` (Cloud API webhook handler), `agent/core.py` (AI agent definition + system prompt), `streams/processor.py` (processing pipeline), `api-client.ts` (inter-service HTTP client).

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
docker compose --profile dev --profile cloud up -d      # Everything

# Development (from root)
pnpm dev:server                         # Start AI API (port 8000)
pnpm dev:whatsapp                       # Start Baileys WhatsApp client (port 3001)
pnpm dev:cloud                          # Start Cloud API WhatsApp client (port 3002)
pnpm dev:queue                          # Start background stream worker
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
  - Inter-service calls include the key automatically
- **Webhook HMAC**: Cloud API `/webhook` routes skip API key auth — verified via `x-hub-signature-256` HMAC-SHA256 using `META_APP_SECRET`
- **CORS**: `CORS_ORIGINS` env var (comma-separated). Empty = block all cross-origin
- **Rate Limiting**: `RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min)
- **User Whitelist**: `WHITELIST_PHONES` env var — comma-separated phone numbers and/or group JIDs. Empty = all users allowed (disabled). When set, non-whitelisted messages are silently ignored. Checked at both WhatsApp client level (primary) and AI API level (defense in depth)
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`

## Observability

- **Prometheus metrics**: Both TypeScript clients expose `GET /metrics` (Prometheus exposition format). The route is exempt from API-key auth and rate limiting so scrapers can reach it directly. Counters: `whatsapp_messages_received_total{type,conversation_type}`, `whatsapp_messages_sent_total{type}`. Histogram: `ai_api_poll_duration_seconds{status}`. Default Node process metrics are also included. Note: `whatsapp_messages_sent_total{type="text"}` counts outbound chunks, not AI responses — a single AI reply split into N bursts produces N increments
- **Sentry**: Error tracking is opt-in via `SENTRY_DSN_NODE`. Each TS package has `src/instrument.ts` which must be imported first in `main.ts` (before `config.ts`) so Sentry's OpenTelemetry hooks load before other modules. When `SENTRY_DSN_NODE` is unset, `instrument.ts` is a no-op and `Sentry.setupFastifyErrorHandler` is skipped.

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **6 tables**: users, conversation_messages, conversation_preferences, core_memories, knowledge_base_documents, knowledge_base_chunks
- **No Alembic migrations** — uses SQLAlchemy `create_all()`. Schema changes require manual `ALTER TABLE` or table recreation; `create_all()` only adds new tables
- Models: `database.py` (users, messages, preferences, core_memories) + `kb_models.py` (documents, chunks)
- **Core memories**: one markdown document per user (`core_memories` table), injected into system prompt via `@agent.system_prompt` in `agent/core.py`
- **Conversation-scoped PDFs** expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). Cleanup task runs in `main.py` lifespan
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
- Module-scoped prom-client counters leak across tests — call `metricsRegistry.resetMetrics()` in `beforeEach` when asserting counter values
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
- Must call `normalizeMessageContent()` before type-checking any message — unwraps viewOnce/ephemeral wrappers
- `contextInfo` (mentions, quoted messages) is nested under specific message types (`imageMessage.contextInfo`, `audioMessage.contextInfo`, etc.) — NOT only on `extendedTextMessage`
- Bot identity uses two formats: JID (`@s.whatsapp.net`) and LID (`@lid`) — both must be checked for mentions/replies (see `utils/message.ts`)
- Group admin check is lazy: only fetches `groupMetadata` when message text starts with `/`
- Only PDF documents are accepted for processing; other types return a user-facing error message

### Cloud API / WhatsApp (TS)
- **24-hour messaging window**: Can only send free-form messages within 24h of customer's last message — outside this window, template messages are required (not implemented)
- **Typing indicators**: Cloud API supports typing via the mark-as-read endpoint with `typing_indicator: { type: 'text' }` — auto-dismisses after 25s or on first outbound reply (whichever is first). Fired from `routes/webhook.ts` before the type-dispatch switch so media messages (audio/image/document) show typing before Graph download or transcription starts. Meta limits it to **one-shot per inbound wamid** — cannot be refreshed mid-response (no typing between multi-burst chunks; no `audio`/"recording" type). Default Graph API version is `v23.0`; `paused` state is a no-op
- **No message edit/delete**: Cloud API doesn't support editing messages; deletion is supported but not implemented — `operations.ts` routes return 501 for both
- **Media URL expiry**: Downloaded media URLs from Graph API are temporary — `downloadMedia()` fetches URL and downloads immediately in one call
- **Webhook routes exempt from API key auth**: `/webhook` GET/POST use HMAC signature verification via `META_APP_SECRET` instead
- **Phone ↔ JID translation**: Cloud API uses plain phone numbers, AI API expects JIDs — conversion happens at the Cloud client boundary via `utils/jid.ts`
- **client_id routing**: Each client sends a `client_id` (`"baileys"` or `"cloud"`) in enqueue requests — the AI API maps this to a pre-configured URL (`WHATSAPP_CLIENT_URL` / `WHATSAPP_CLOUD_CLIENT_URL`) to route callbacks
- **Whitelist group JID limitation**: `WHITELIST_PHONES` supports group JIDs (e.g. `120363...@g.us`) only on the Baileys client. The Cloud API webhook payload does not include group context — only the individual sender's phone number — so group JID entries in the whitelist have no effect for Cloud API messages

### AI API (Python)
- Slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/help`) are intercepted in `routes/chat.py` — they never reach the AI agent
- Core memory is a single markdown document per user (not individual rows) — the AI reads the whole doc and rewrites it via `update_core_memory` tool
- CORS middleware must be added AFTER `APIKeyMiddleware` in `main.py` (Starlette processes middleware LIFO — reversing this breaks CORS preflight)
- pgvector IVFFlat index must be created manually for `knowledge_base_chunks` — without it, similarity search does full table scan
- Redis Streams (`streams/`) supersedes the arq queue (`queue/worker.py`) — both coexist in the codebase
- Embedding task types matter: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search — mixing them degrades retrieval quality
- Agent tool modules must be imported in `agent/tools/__init__.py` or the `@agent.tool` decorators won't register
- Agent tools that call `ctx.deps.db.commit()` must call `ctx.deps.db.rollback()` in their except blocks — otherwise a failed write leaves the shared session dirty and poisons subsequent tool calls

### General
- Husky pre-commit hook runs `pnpm format` automatically — do NOT run format manually before committing
- ai-api Dockerfile installs `ffmpeg` always (used by pydub for TTS/STT). `poppler-utils`, `tesseract-ocr`, and `libmagic1` are only installed when `INSTALL_DOCLING=true` (build arg) — the default image uses LlamaParse only and skips them to stay lean. Docker Compose forwards `${INSTALL_DOCLING}` from the shell environment as a build arg
- API docs: http://localhost:8000/docs (AI API), http://localhost:3001/docs (Baileys client), http://localhost:3002/docs (Cloud API client)
- DB GUI: http://localhost:8080 (Adminer)
