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
6. `routes/chat.py` intercepts slash commands (`/settings`, `/tts`, `/clean`, `/memories`, etc.) before queuing
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
5. Same AI API flow as Baileys: `api-client.ts` sends POST `/chat/enqueue` (includes `callback_url` for routing) → polls for result
6. AI API routes callbacks to the correct client using per-request `callback_url`
7. Responses sent via Meta Graph API (`POST graph.facebook.com/{phone_number_id}/messages`)

## Tooling

- **Monorepo**: pnpm workspaces (`pnpm-workspace.yaml`)
- **TypeScript**: ES2022, NodeNext modules, strict mode
- **Python**: >=3.11, managed with `uv`
- **Formatting**: Prettier (TS) + Ruff (Python) — enforced by Husky pre-commit hook (`pnpm format` runs automatically)
- **Linting**: ESLint flat config (TS) + Ruff (Python)
- **No test framework configured** — human developer handles testing

## Commands

```bash
# Infrastructure
docker-compose up -d                    # PostgreSQL + Redis + Adminer + full stack

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
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **6 tables**: users, conversation_messages, conversation_preferences, core_memories, knowledge_base_documents, knowledge_base_chunks
- **No Alembic migrations** — uses SQLAlchemy `create_all()`. Schema changes require manual `ALTER TABLE` or table recreation; `create_all()` only adds new tables
- Models: `database.py` (users, messages, preferences, core_memories) + `kb_models.py` (documents, chunks)
- **Core memories**: one markdown document per user (`core_memories` table), injected into system prompt via `@agent.system_prompt` in `agent/core.py`
- **Conversation-scoped PDFs** expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). Cleanup task runs in `main.py` lifespan

## Environment Config

- Root `.env` loaded first (shared vars) — see @.env.example for all required variables
- Package-level `.env.local` for overrides (not committed to git)
- TS config loader: `packages/whatsapp-client/src/config.ts` (Baileys), `packages/whatsapp-cloud/src/config.ts` (Cloud API)
- Python config: pydantic-settings in `packages/ai-api/src/ai_api/config.py`

## Guidelines

- Use `pnpm add` / `uv add` for dependencies — NEVER edit package.json/pyproject.toml directly
- Prefer pure functions over classes
- Async throughout both codebases
- Use structured logging (Pino for TS, Python `logging`) — no console.log/print
- Do NOT write tests — the human developer handles testing
- Keep this file updated with important changes

## Common Workflows

### Adding a new message handler (whatsapp-client)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `sendFailureReaction(sock, msg)` + `logger.error` in catch, `sendPresenceUpdate('paused')` in finally
3. Register it in `src/whatsapp.ts` inside the `messages.upsert` event handler
4. Add any new routes in `src/routes/` with Zod schemas in `src/schemas/`

### Adding a new agent tool (ai-api)
1. Add tool function in `agent/tools/` using the `@agent.tool` decorator (see existing tools for patterns)
2. Signature: `async def tool_name(ctx: RunContext[AgentDeps], ...params) -> str`
3. Import the module in `agent/tools/__init__.py` — the import triggers decorator registration
4. Add tool description to the system prompt in `agent/core.py`
5. Tool accesses deps via `ctx.deps` (db, embedding_service, whatsapp_client, etc.)

### Adding a new API endpoint (ai-api)
1. Add route in the appropriate `routes/*.py` file (or create a new router module)
2. Use `APIRouter` with appropriate tags; import `limiter` from `deps.py` for rate-limited endpoints
3. Add Pydantic schemas in `schemas.py`
4. Register new router in `main.py` via `app.include_router()`

### Adding a new message handler (whatsapp-cloud)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `graphApi.sendReaction(senderPhone, messageId, '❌')` + `logger.error` in catch
3. Register in `src/routes/webhook.ts` inside the message type dispatch switch
4. Use `jidToPhone()` / `phoneToJid()` from `utils/jid.ts` when crossing API boundaries
5. Download media via `graphApi.downloadMedia(mediaId)` instead of Baileys `downloadMediaMessage()`

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
- **No typing indicators**: Cloud API doesn't support `composing`/`paused` presence updates — handlers skip this step
- **No message edit/delete**: Cloud API doesn't support editing or deleting sent messages — `operations.ts` routes return 501
- **Media URL expiry**: Downloaded media URLs from Graph API expire in 5 minutes — `downloadMedia()` fetches URL and downloads immediately in one call
- **Webhook routes exempt from API key auth**: `/webhook` GET/POST use HMAC signature verification via `META_APP_SECRET` instead
- **Phone ↔ JID translation**: Cloud API uses plain phone numbers, AI API expects JIDs — conversion happens at the Cloud client boundary via `utils/jid.ts`
- **callback_url routing**: Each client includes its own URL as `callback_url` in enqueue requests so the AI API stream worker calls back the correct client

### AI API (Python)
- Slash commands (`/settings`, `/tts`, `/clean`, `/memories`, `/help`) are intercepted in `routes/chat.py` — they never reach the AI agent
- Core memory is a single markdown document per user (not individual rows) — the AI reads the whole doc and rewrites it via `update_core_memory` tool
- CORS middleware must be added AFTER `APIKeyMiddleware` in `main.py` (Starlette processes middleware LIFO — reversing this breaks CORS preflight)
- pgvector IVFFlat index must be created manually for `knowledge_base_chunks` — without it, similarity search does full table scan
- Redis Streams (`streams/`) supersedes the arq queue (`queue/worker.py`) — both coexist in the codebase
- Embedding task types matter: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search — mixing them degrades retrieval quality
- Agent tool modules must be imported in `agent/tools/__init__.py` or the `@agent.tool` decorators won't register

### General
- Husky pre-commit hook runs `pnpm format` automatically — do NOT run format manually before committing
- ai-api Dockerfile requires system deps: poppler-utils, tesseract-ocr, libmagic1, ffmpeg
- API docs: http://localhost:8000/docs (AI API), http://localhost:3001/docs (Baileys client), http://localhost:3002/docs (Cloud API client)
- DB GUI: http://localhost:8080 (Adminer)
