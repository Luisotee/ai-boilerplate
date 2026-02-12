# CLAUDE.md

AI WhatsApp agent system: Node.js/TypeScript client (Baileys) + Python/FastAPI API (Pydantic AI + Gemini).

See @README.md for setup guide and environment variables.

## Structure

```
packages/
├── whatsapp-client/   # TypeScript — Fastify server (port 3001) + Baileys WhatsApp connection
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
└── ai-api/            # Python — FastAPI server (port 8000) + Pydantic AI agent
    └── src/ai_api/    # agent/, routes/, rag/, streams/, queue/, whatsapp/, scripts/
```

**Key entry points**: `whatsapp.ts` (message router), `agent/core.py` (AI agent definition + system prompt), `streams/processor.py` (processing pipeline), `api-client.ts` (inter-service HTTP client).

## Message Flow

How a WhatsApp message traverses the system end-to-end:

1. Baileys WebSocket → `whatsapp.ts` `messages.upsert` event
2. `normalizeMessageContent()` unwraps viewOnce/ephemeral message wrappers
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts` (transcribe first), image → `handlers/image.ts`, document → `handlers/document.ts`
4. All handlers funnel into `handleTextMessage()` with optional base64 image/document
5. `api-client.ts` sends POST `/chat/enqueue` to AI API → returns `job_id`
6. `routes/chat.py` intercepts slash commands (`/settings`, `/tts`, `/clean`, etc.) before queuing
7. Non-command messages: saved to PostgreSQL, enqueued to Redis Stream (`stream:user:{user_id}`)
8. `streams/processor.py`: fetches conversation history → runs Pydantic AI agent with tools → streams response chunks to Redis
9. `api-client.ts` polls GET `/chat/job/{id}` (500ms interval, max 120s) until complete
10. WhatsApp client sends text reply; optionally generates TTS audio if user preference enabled

**Group messages**: non-@mentioned messages are saved as history only (`saveOnly=true`), never processed by AI. Bot checks both JID and LID formats for mentions.

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
pnpm dev:whatsapp                       # Start WhatsApp client (port 3001)
pnpm dev:queue                          # Start background stream worker
pnpm install:all                        # Install Node + Python dependencies

# Linting & Formatting
pnpm lint                               # Check TypeScript (ESLint) + Python (Ruff)
pnpm lint:fix                           # Auto-fix lint issues
pnpm format                             # Format TypeScript (Prettier) + Python (Ruff)
pnpm format:check                       # Verify formatting without changes (CI)
```

## Security

- **API Key Auth**: Both servers require `X-API-Key` header on all routes except `/health` and `/docs*`
  - `AI_API_KEY` — Python AI API (required, app fails to start without it)
  - `WHATSAPP_API_KEY` — TypeScript WhatsApp API (required)
  - Inter-service calls include the key automatically
- **CORS**: `CORS_ORIGINS` env var (comma-separated). Empty = block all cross-origin
- **Rate Limiting**: `RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min)
- **No default passwords**: `POSTGRES_PASSWORD` and `REDIS_PASSWORD` required in `.env`

## Database

- **PostgreSQL + pgvector** (3072-dim vectors via `gemini-embedding-001`)
- **5 tables**: users, conversation_messages, conversation_preferences, knowledge_base_documents, knowledge_base_chunks
- **No Alembic migrations** — uses SQLAlchemy `create_all()`. Schema changes require manual `ALTER TABLE` or table recreation; `create_all()` only adds new tables
- Models: `database.py` (users, messages, preferences) + `kb_models.py` (documents, chunks)
- **Conversation-scoped PDFs** expire after 24h (`CONVERSATION_PDF_TTL_HOURS`). Cleanup task runs in `main.py` lifespan

## Environment Config

- Root `.env` loaded first (shared vars) — see @.env.example for all required variables
- Package-level `.env.local` for overrides (not committed to git)
- TS config loader: `packages/whatsapp-client/src/config.ts`
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

### AI API (Python)
- Slash commands (`/settings`, `/tts`, `/clean`, `/help`) are intercepted in `routes/chat.py` — they never reach the AI agent
- CORS middleware must be added AFTER `APIKeyMiddleware` in `main.py` (Starlette processes middleware LIFO — reversing this breaks CORS preflight)
- pgvector IVFFlat index must be created manually for `knowledge_base_chunks` — without it, similarity search does full table scan
- Redis Streams (`streams/`) supersedes the arq queue (`queue/worker.py`) — both coexist in the codebase
- Embedding task types matter: `RETRIEVAL_DOCUMENT` for storage, `RETRIEVAL_QUERY` for search — mixing them degrades retrieval quality
- Agent tool modules must be imported in `agent/tools/__init__.py` or the `@agent.tool` decorators won't register

### General
- Husky pre-commit hook runs `pnpm format` automatically — do NOT run format manually before committing
- ai-api Dockerfile requires system deps: poppler-utils, tesseract-ocr, libmagic1, ffmpeg
- API docs: http://localhost:8000/docs (AI API) and http://localhost:3001/docs (WhatsApp client)
- DB GUI: http://localhost:8080 (Adminer)
