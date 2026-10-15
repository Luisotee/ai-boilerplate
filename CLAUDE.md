# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AI chat-agent system: Node.js/TypeScript clients (Baileys + Meta Cloud API + Telegram/grammY) + Python/FastAPI API (Pydantic AI + Gemini).

`README.md` has the setup guide, the API endpoint tables and the environment variable list (not imported here, to keep this file small).

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

**Key entry points**: `whatsapp.ts` (Baileys message router), `routes/webhook.ts` (Cloud API + Telegram webhook handlers), `telegram-client/src/updates.ts` (grammY dispatch table), `agent/core.py` (AI agent definition + system prompt), `streams/processor.py` (processing pipeline), `api-client.ts` (inter-service HTTP client).

## Message Flow

How a WhatsApp message traverses the system end-to-end:

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

**On-demand history retrieval**: each run auto-injects only the last `HISTORY_LIMIT_PRIVATE`/`HISTORY_LIMIT_GROUP` messages. The `get_chat_history` agent tool (`agent/tools/history.py`) fetches more of the CURRENT chat chronologically — `limit` (flat last-N) and/or `since_hours` (time window) — via `get_conversation_messages` in `database.py` (keyed by the run's own `users.id`, so it can only read the conversation the message came from and never creates a user; naive-UTC `since`/`until` cutoffs; hard-capped at `MAX_FLAT_MESSAGES=200`). It is distinct from `get_conversation_history`, the config-limited auto-injection path. Transcripts render via `format_transcript` in `rag/conversation.py`: the bot's lines are labelled with the `BOT_NAME` setting (default `Assistant`, hot via `/admin`), group sender names are not double-prefixed, private user lines get `User:`. For topic recall use `search_conversation_history` instead.

**Shared-group tools** (`SHARED_GROUP_TOOLS_ENABLED`, default `false`): `get_group_context` / `send_group_message` in `agent/tools/group_context.py` let a private chat read, search or post into a group the user shares with the bot. Off by default because group transcripts reach the LLM inside a private chat. Privacy model and per-platform membership checks: `packages/ai-api/src/ai_api/agent/CLAUDE.md`.

## Subsystem docs (load on demand)

Deep detail lives in a `CLAUDE.md` next to the code, which Claude Code loads when it works in that folder. **These subsystems cross folders, so read the file before changing any part of one**, even from another folder:

| File | Covers |
|------|--------|
| `packages/ai-api/src/ai_api/services/CLAUDE.md` | Broadcasts (`/admin/broadcasts`: audience, routing, opt-out, claims, lease, Baileys pacing, FleetView enums), bug reports (`/admin/bug-reports`: dedup, privacy scrub), cross-platform identity (`/link`, `/linkphone`) |
| `packages/ai-api/src/ai_api/agent/CLAUDE.md` | DeepSeek → Gemini model chain, `GuardedModel`, fallback rules, model-error fallback reply, shared-group tools |
| `packages/ai-api/src/ai_api/streams/CLAUDE.md` | PDF processing queue (concurrency, retry, dead letter, crash recovery) |
| `packages/whatsapp-client/CLAUDE.md` | Baileys gotchas (WA version, LID vs phone, whitelist placement), handler + multipart route workflows |
| `packages/telegram-client/CLAUDE.md` | Telegram flow, polling mode, reactions, privacy mode, 4096-char limit, formatting |
| `packages/whatsapp-cloud/CLAUDE.md` | Cloud API flow, 24h window, typing indicator, handler workflow |
| `scripts/CLAUDE.md` | Several bots on one host: `setup.sh --check/--fix`, ports, `SERVICE_NAME`, Compose project name, `*_BIND` |
| `packages/ai-api/docs/logfire.md` | Logfire instrumentation details |

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
./setup.sh --check                      # Report clashes with other bots on this host (read-only)
./setup.sh --fix                        # Move clashing ports in the existing .env (backup first)
./setup.sh --update                     # Add settings new in .env.example to the existing .env (backup first)

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
pnpm dev:queue                          # Start the stream worker (chat + PDF + broadcast consumers; required for replies, PDF processing AND broadcasts)
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
cd packages/telegram-client && pnpm test        # Telegram client tests
cd packages/ai-api && uv run pytest             # AI API tests
cd packages/ai-api && uv run pytest tests/unit  # AI API unit tests only
cd packages/<pkg> && pnpm build                 # tsc type-check/build (the Docker build fails on type errors)

# Single test
cd packages/whatsapp-client && pnpm exec vitest run tests/unit/whitelist.test.ts   # one file
cd packages/whatsapp-client && pnpm exec vitest run -t "name substring"            # by test name
cd packages/ai-api && uv run pytest tests/mocked/test_model_chain_fallback.py -k "cooldown"
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
- **Group admin commands fail closed**: `/clean`, `/tts`, `/stt`, `/settings`, `/memories` (and `/broadcast on|off`, gated in its handler) in a group run only when the client sends `is_group_admin: true` — `commands.py` checks `is_group_admin is not True`. The old `is False` check let an omitted field through, and the Telegram client never sent it, so any group member could `/clean all` a group. Baileys resolves it lazily from `groupMetadata` (`isSenderGroupAdmin` in `utils/message.ts`, matching `participant`/`participantAlt` against each member's `id`/`lid`/`phoneNumber`); Telegram via `getChatMember` (`services/group-admin.ts`); a lookup failure means not-admin. Cloud API webhooks carry no group context. The agent tools that change a whole group's state apply the same rule through `agent/tools/_group.py` `group_admin_refusal` (a group conversation JID needs `AgentDeps.is_group_admin is True`): `clean_user_data`, `update_tts_settings`, `update_stt_settings`, `set_broadcast_subscription`, and `update_core_memory` with empty content (a wipe; a normal rewrite stays open to every member). Before, any member could ask the bot in plain words to do what the commands reserve for admins
- **User Whitelist**: `WHITELIST_PHONES` env var — comma-separated. Empty = all users allowed (disabled). Each entry matches **either as a phone number or as a verbatim chat id**, parsed into two sets by `utils/whitelist.ts` (TS ×3, kept byte-identical by `tests/unit/whitelist-copies.test.ts`) / `ai_api/whitelist.py` and matched by `isWhitelisted` / `is_whitelisted`. The Python side is a pure mirror imported by both `routes/chat.py` (which reads the effective value via `runtime_config`) and `routes/admin.py` (which validates a proposed one); it must never be *more permissive* than the TS gate it backs up, which is why both spell out the same 29-codepoint separator class instead of using `\s`. An entry is first stripped of a device suffix (`:50@` → `@`) and a trailing `@s.whatsapp.net`; if what remains still contains an `@`, or the entry started with `tg:`, it is kept **verbatim only** (so `tg:-100…` can never be mistaken for a phone). Otherwise it is normalized to bare digits — tolerating `+`, spaces, `-`, `()`, `.` — and stored in the phone set. Entries below 5 digits never become phones. Every entry is *also* kept verbatim, which is what keeps a bare `120363…` matching a group and a bare LID matching its chat on existing installs.
  - **Normalized digits are consulted only for `@s.whatsapp.net` JIDs.** A phone entry must not admit an `@lid`/`@g.us`/`@broadcast` chat that merely shares its digits. A *bare* entry stays namespace-blind, though — that is the pre-existing `jid.split("@")[0] in whitelist` behaviour and the same code path that keeps bare `120363…` working, so it is pinned by a test rather than "fixed"
  - **The `phone` argument is never consulted for a group JID**, so a member's whitelisted phone never admits the whole group. That is narrower than "a group is matchable only by its `…@g.us` JID" — a legacy bare entry equal to the group's id still admits it When set, non-whitelisted messages are silently ignored. Checked at both WhatsApp client level (primary) and AI API level (defense in depth). **Editing the whitelist via `PATCH /admin/settings` updates only the AI-API layer** — the TS clients keep their startup env value until restarted. So a `/admin` change takes full effect for *removals* instantly (the whitelist is read only in `routes/chat.py`; `streams/` and `queue/` never evaluate it, so unlike `gemini_model` there is no worker-side TTL lag) — the AI-API layer rejects them regardless of the client — but a *newly added* JID is still blocked by the client's primary filter until its env is updated + restart, unless that client's `WHITELIST_PHONES` is empty/disabled
  - The AI-API layer matches on the client-supplied `phone` field in addition to `whatsapp_jid` (it must — see the LID gotcha below). That is not new trust: the route is already behind `X-API-Key`, and `get_or_create_user` already trusts the same field for identity. The client-side check remains primary
  - `PATCH /admin/settings` **rejects a whitelist that parses to zero entries** (`""`, `" "`, `","`). An override shadows the env value across restarts, so clearing the field would otherwise disable the gate for good; `DELETE /admin/settings/whitelist_phones` is the way to revert to the env value
  - Accepted compat break from the two-set split: a bare `<digits>:<device>` entry (a device suffix with no `@`) no longer matches, because the matcher strips the suffix off the incoming jid before comparing local parts. Use the full JID or the bare number
  - Matching is exact — there is deliberately **no** "last N digits" fuzzy fallback, which would let `…5945319` whitelist arbitrary countries. If a BR/MX-style number variant (`55DD9XXXXXXXX` vs `55DDXXXXXXXX`) fails to match, read the `phone` value from the client log and paste that
- **Group gating (`GROUP_GATING=jid|membership`, default `jid`)**: selects how a *group* gets in scope when `WHITELIST_PHONES` is set. Read by the AI API (`config.py`, display-only in `runtime_config`) and by the Baileys + Telegram clients (`config.groupGating`) — keep them equal via the root `.env`. The truth table is `utils/gating.ts` (`gateMessage` / `decideGroupGating`), byte-identical in whatsapp-client and telegram-client (enforced by `whitelist-copies.test.ts`); in `jid` mode the callers pass `groupAllowed = groupExplicit`, which reproduces the historical gate exactly
  - `jid`: a group is admitted only by its own id; then every member may address the bot
  - `membership` (Baileys): a group is in scope if it is listed, the sender is whitelisted, or ANY participant is whitelisted (`groupHasWhitelistedMember` in `services/groups.ts`: participant `id`/`lid`/`phoneNumber` through the whitelist matcher, then a quiet `getPNForLID` fallback for LID-only participants; metadata from `group-cache.ts`, bounded at 10s, **fails closed**). The bot saves every message of an in-scope group but replies only to a whitelisted sender (`gate.suppressResponse` → `saveOnly`), unless the group is listed explicitly. The gate still runs between `resolveSenderPhone` and `sock.user!.id` (`resolveGate` in `whatsapp.ts`; pinned by `whatsapp-whitelist-placement.test.ts` for both modes)
  - `membership` (Telegram): the Bot API cannot enumerate members, so every group the bot is in is in scope and saved; replies are gated per sender (`tg:<from.id>`) unless the group id is listed (`resolveGate` in `updates.ts`)
  - AI API: in `membership` mode `_is_whitelisted` admits every group JID (`is_group_jid`: `@g.us` or `tg:-…`) — the client is authoritative for groups because only it can see membership; 1:1 chats are enforced in both modes
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`

## Observability

- **Sentry**: Error tracking is opt-in via `SENTRY_DSN_NODE`. Each TS package has `src/instrument.ts` which must be imported first in `main.ts` (before `config.ts`) so Sentry's OpenTelemetry hooks load before other modules. When `SENTRY_DSN_NODE` is unset, `instrument.ts` is a no-op and `Sentry.setupFastifyErrorHandler` is skipped.
- **Logfire (LLM token + cost tracking)**: opt-in via `LOGFIRE_TOKEN` (AI API only), set up by `instrument.py` `setup_instrumentation()`, a no-op without a token. Invariants: pass `token=settings.logfire_token` explicitly (pydantic-settings does not export `.env` to `os.environ`, so local dev breaks silently otherwise); call it in BOTH `main.py` and `scripts/run_stream_worker.py` (the agent runs in the worker); never pass `instrument=` to `Agent(...)`; keep `include_content=False` (a privacy guarantee, tested against real spans). Details: `packages/ai-api/docs/logfire.md`

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **11 tables**: users, conversation_messages, conversation_preferences, core_memories, knowledge_base_documents, knowledge_base_chunks, bot_prompt, runtime_settings, broadcasts, broadcast_recipients, bug_reports
- **No Alembic migrations** — uses SQLAlchemy `create_all()`, which only creates missing *tables*, never new columns. Column/index changes ship as hand-written, idempotent (`IF NOT EXISTS`) SQL in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql`, named so they match what `create_all()` emits on a fresh DB. Apply them to an existing Docker deployment BEFORE rolling out the code that needs them (safe to re-run):
  ```bash
  docker compose exec -T postgres psql -U aiagent -d aiagent \
    < packages/ai-api/docs/migrations/<file>.sql
  ```
  (run it in the checkout: `docker compose exec` targets this checkout's own `postgres` whatever `SERVICE_NAME` is, so on a host with several bots it can't hit another bot's database; substitute your `POSTGRES_USER` / `POSTGRES_DB` if you changed the defaults.) Current migrations: `2026-09-21-kb-file-hash.sql`, `2026-09-25-broadcast.sql` (`users.broadcast_opt_out`, `last_client_id`, `whatsapp_client_id`, `cloud_last_inbound_at`, and `broadcast_recipients.attempt_started_at` — added later, so a DB that applied an earlier version must apply it again; the `broadcasts`/`broadcast_recipients` tables come from `create_all()`)
- **Knowledge-base dedup**: `knowledge_base_documents.file_hash` (SHA-256 hex of the uploaded bytes, indexed, computed while streaming the upload to disk). `POST /knowledge-base/upload` answers **409** when identical content is already in the KB; the batch route rejects that file (and a repeat of an earlier file in the same batch) while accepting the rest. Only global documents with a status other than `failed` count — conversation-scoped chat PDFs never block an upload, and a failed document can be re-uploaded. Rows from before the migration have `file_hash = NULL` and never match. Both upload routes are `@limiter.exempt` (bulk loads via `./upload-kb.sh <dir>`, which posts a folder of PDFs to the batch route with `AI_API_KEY` from the env or `.env`); they stay behind `X-API-Key`
- Models: `database.py` (users, messages, preferences, core_memories, bot_prompt, runtime_settings, broadcasts, broadcast_recipients, bug_reports) + `kb_models.py` (documents, chunks)
- **Core memories**: one markdown document per user (`core_memories` table), injected into the prompt via `@agent.instructions inject_core_memory` in `agent/core.py`
- **System prompt is DB-backed**: the active prompt lives in the single-row `bot_prompt` table, loaded per-run via `@agent.instructions base_system_prompt` in `agent/core.py`, falling back to the hardcoded `DEFAULT_SYSTEM_PROMPT` when no row exists. Edit it through `PUT /admin/prompt` — takes effect on the next message, no restart. Uses `instructions` (not `system_prompt`) so a changed prompt is never shadowed by one retained in `message_history`
- **Conversation-scoped PDFs** expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). Cleanup task runs in `main.py` lifespan
- **PDF processing queue**: every PDF (KB uploads and chat attachments) goes through the Redis Stream `stream:pdf_processing`, parsed by `streams/pdf_consumer.py` in the stream worker — never in the API process or inline in a chat job. Retries, dead letter and crash recovery: `packages/ai-api/src/ai_api/streams/CLAUDE.md`
- **PDF parsing**: LlamaParse (cloud, primary) via `llama-cloud` SDK, with **optional** Docling fallback behind the `[docling]` extra. Behavior controlled by `PDF_PARSER` (`auto` | `llamaparse` | `docling`). In `auto` mode, LlamaParse runs when `LLAMA_CLOUD_API_KEY` is set and falls back to Docling on any parser error *if* the extra is installed
- **Speech-to-Text**: Groq Whisper (cloud, primary) plus an **optional** self-hosted Whisper server (speaches by default, any OpenAI-compatible endpoint works). Controlled by `STT_PROVIDER` (`auto` | `groq` | `whisper`). In `auto` mode: with `GROQ_API_KEY` set, Groq runs and falls back to self-hosted on recoverable errors when `WHISPER_BASE_URL` is also set; with only `WHISPER_BASE_URL` set, self-hosted runs alone; if neither is configured, `/transcribe` returns 503. Start the self-hosted container with `docker compose --profile whisper up -d`

## Environment Config

- Root `.env` loaded first (shared vars) — see `.env.example` for all variables
- **Adding a variable**: put it in `.env.example` with a `#` comment line directly above it (the comment is copied along and shown as the prompt hint), and leave the value empty when it is a secret or opt-in. Existing deployments pick it up with `./setup.sh --update` (`instance_check.py update`): it appends only the keys `.env` lacks, each with its comment block, under `# Added by ./setup.sh --update (<date>)`, asks for the ones whose template value is empty (hidden input for `*_KEY`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD`/`*_PASS`; Enter = leave empty), adds the rest with the template default, backs up first and never changes an existing line. Keys `.env` has but the template dropped are listed as possibly obsolete, never removed. `--yes` skips the prompts; the interactive `./setup.sh` offers it when you keep an existing `.env`, and `--check` mentions it when keys are missing
- **Shared secrets live in root `.env` only.** Never duplicate credentials (API keys, DB passwords, Meta tokens, etc.) in package-level `.env.local` — they belong in root `.env` only, and `setup.sh` writes them there
- **`.env.local` is for per-developer customization** (log level, port, feature flags). It loads with `override: true`, so any duplicated key silently wins over root — including empty `KEY=` lines that blank out the root value
- The TS config loaders warn at startup when `.env.local` shadows a root key. If you see `[config] .env.local overrides root .env: X`, confirm it's intentional
- TS config loader: `packages/whatsapp-client/src/config.ts` (Baileys), `packages/whatsapp-cloud/src/config.ts` (Cloud API), `packages/telegram-client/src/config.ts` (Telegram)
- Python config: pydantic-settings in `packages/ai-api/src/ai_api/config.py`

## Mandatory Subagent: docs-fetcher

**ALWAYS use the `docs-fetcher` subagent before writing or modifying code that touches any external library, SDK, API, or framework.** Do not rely on training data for API signatures, method names, or behavior — fetch current documentation first. This applies to Baileys, Pydantic AI, FastAPI, Fastify, Meta Cloud API, Gemini, pgvector, SQLAlchemy, Zod, Redis, Docling, Groq, and any other dependency. Launch `docs-fetcher` in parallel with your planning or exploration to avoid blocking.

## Related Projects

Sibling repos under `/home/ubuntu/projects/`, each with its own `CLAUDE.md` (authoritative for that repo; this file's rules don't carry over). Explore them via the read-only subagents rather than reading them inline:

- **curupira-bot** (`curupira-explorer`): fork for fire alerts. Users subscribe to municipalities in Brazil (`fire_poller/`, fire-alert tools); the persona is "Curupira". Features often start here and get ported back to the boilerplate (the shared-group tools, `get_chat_history`)
- **castanha-bot** (`castanha-explorer`): fork for Brazil-nut supply-chain prices. It integrates the external *cadeia-produtiva* platform (`cadeia` tools), sends a weekly recap and uses Portuguese commands (`/regiao`). It has no Telegram client
- **fleetview** (`fleetview-explorer`): Next.js 16 dashboard, not a fork. It is the control plane for the whole bot fleet and keeps an encrypted registry of bots (base URL + `X-API-Key`). It calls each bot's `/admin/*`, `/health` and `/knowledge-base/*` server-side, for health, conversations, KB, prompt, settings and WhatsApp QR pairing. **Its Zod schemas mirror `routes/admin.py`, `schemas.py` and the `runtime_config.py` REGISTRY strictly** (only health is lenient). Renaming or removing a field, route or setting attribute (`hot`/`secret`/`choices`) therefore breaks the dashboard for every bot. So does a new value in a closed enum: setting `type`, KB status, QR `status`, `conversation_type`. Check `fleetview-explorer` before you change that contract. A new field is safe (unknown keys are stripped, not rejected)

## Guidelines

- Use `pnpm add` / `uv add` for dependencies — NEVER edit package.json/pyproject.toml directly
- Prefer pure functions over classes
- Async throughout both codebases
- Use structured logging (Pino for TS, Python `logging`) — no console.log/print
- Write tests for new functionality — follow existing patterns in `tests/` directories
- New TS files may fail `pnpm format:check` even if lint passes — run `pnpm exec prettier --write <path>` on freshly created files
- Keep this file and the folder `CLAUDE.md` files (see "Subsystem docs") updated with important changes; put subsystem detail in the folder file, not here

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

The Baileys handler, Cloud handler and Baileys multipart-route workflows are in the matching package's `CLAUDE.md`.

## Gotchas

### AI API (Python)
- **Admin API (`routes/admin.py`, `/admin/*`)**: management-dashboard contract — `GET/PUT/DELETE /admin/prompt`, `GET/PATCH /admin/settings` + `DELETE /admin/settings/{key}`, read-only `GET /admin/users` and `GET /admin/users/{jid}/messages`, `GET /admin/overview`, `GET /admin/whatsapp/qr`. Protected by the standard `X-API-Key` (NOT in `_AUTH_EXEMPT_PREFIXES`)
- **`GET /admin/whatsapp/qr`** proxies the Baileys client's `GET /whatsapp/qr` (link status + pairing QR) using a short 5s timeout. It targets the Baileys client via `get_whatsapp_client_url(None)`, so in a multi-client deployment where `WHATSAPP_CLIENT_URL` is unset (defaults to `localhost:3001`) or the Baileys client isn't running, it reports `status="unavailable"` (200, not an error)
- **Model chain**: `build_runtime_model()` builds `FallbackModel(DeepSeek → Gemini)` per run when `DEEPSEEK_API_KEY` is set (chat content then goes to DeepSeek, China), bare Gemini otherwise; `gemini_model` / `deepseek_model` are hot settings. **Never run the chat agent with `agent.run_stream`** (it drops tool calls made after a text preamble) — `agent/response.py` uses `run_stream_events`. Never put `extra_body`/`timeout` in agent- or run-level `model_settings`. Model errors produce a fallback reply, not a failed job. Details: `packages/ai-api/src/ai_api/agent/CLAUDE.md`
- **Runtime settings overlay (`runtime_config.py`)**: a curated subset of settings (TTS/STT, semantic/KB search, history limits, `bot_name`, PDF parser, whitelist, PDF TTL, core-memory length, **`gemini_model`**, **`deepseek_model`**) can be overridden at runtime via the `runtime_settings` table. Behavioural code reads them through `runtime_config.get("key")` (env default ← DB override), cached in-process ~10s and busted on write. The `REGISTRY` in `runtime_config.py` is the source of truth: `hot=True` = overridable, `hot=False` = display-only/"needs restart" (bootstrap settings: DB/Redis/pool, CORS, rate limits, log level). **Only mark a setting `hot` if its consumption site actually reads via `runtime_config.get()`** — otherwise the override silently never applies. `PATCH /admin/settings` rejects unknown/non-hot keys with 400
- Slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/broadcast`, `/help`) are intercepted in `routes/chat.py` — they never reach the AI agent
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
