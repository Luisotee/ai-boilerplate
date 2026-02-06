# CLAUDE.md

AI WhatsApp agent system: Node.js/TypeScript client (Baileys) + Python/FastAPI API (Pydantic AI + Gemini).

See @README.md for setup guide and environment variables.

## Structure

```
packages/
├── whatsapp-client/   # TypeScript — Fastify server (port 3001) + Baileys WhatsApp connection
│   └── src/           # handlers/, routes/, services/, schemas/, utils/
└── ai-api/            # Python — FastAPI server (port 8000) + Pydantic AI agent
    └── src/ai_api/    # agent.py, deps.py, routes/, rag/, streams/, queue/, whatsapp/, scripts/
```

## Tooling

- **Monorepo**: pnpm workspaces (`pnpm-workspace.yaml`), pnpm@10.14.0
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
pnpm format:check                       # Check formatting without writing
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

- **PostgreSQL + pgvector** (3072-dim vectors for embeddings)
- **5 tables**: users, conversation_messages, conversation_preferences, knowledge_base_documents, knowledge_base_chunks
- **No Alembic migrations** — uses SQLAlchemy `create_all()`. Schema changes require manual migration or table recreation
- Models: `database.py` (conversations) + `kb_models.py` (knowledge base)

## Environment Config

- Root `.env` loaded first (shared vars) — see @.env.example for all required variables
- Package-level `.env.local` for overrides (not committed to git)
- TS config loader: `packages/whatsapp-client/src/config.ts`
- Python config: pydantic-settings in `packages/ai-api/src/ai_api/config.py`

## Guidelines

- Use `pnpm add` / `uv add` for dependencies — NEVER edit package.json/pyproject.toml directly
- Prefer pure functions over classes
- Use structured logging (Pino for TS, Python logging) — no console.log/print
- Do NOT write tests — the human developer handles testing
- Keep this file updated with important changes

## Common Workflows

### Adding a new message handler (whatsapp-client)
1. Create handler in `src/handlers/` following the pattern in `text.ts`
2. Register it in `src/whatsapp.ts` message event listener
3. Add any new routes in `src/routes/` with Zod schemas in `src/schemas/`

### Adding a new agent tool (ai-api)
1. Add tool function in `agent.py` using the `@agent.tool` decorator (see existing 12 tools)
2. Tool gets access to `RunContext` with user info and dependencies
3. WhatsApp-sending tools use the `whatsapp/client.py` HTTP client

### Adding a new API endpoint (ai-api)
1. Add route in the appropriate `routes/*.py` file (or create a new router module)
2. Use `APIRouter` with appropriate tags; import `limiter` from `deps.py` for rate-limited endpoints
3. Add Pydantic schemas in `schemas.py`
4. Register new router in `main.py` via `app.include_router()`

## Gotchas

- Husky pre-commit hook runs `pnpm format` automatically — do NOT run format manually before committing
- Python CORS middleware must be added AFTER APIKeyMiddleware (Starlette processes middleware LIFO)
- Python Dockerfile requires system deps: poppler-utils, tesseract-ocr, libmagic1, ffmpeg
- pgvector IVFFlat index must be created manually for knowledge_base_chunks
- Redis Streams (per-user sequential processing) supersedes the arq queue — both are in the codebase
- API docs: http://localhost:8000/docs (AI API) and http://localhost:3001/docs (WhatsApp client)
- DB GUI: http://localhost:8080 (Adminer)
