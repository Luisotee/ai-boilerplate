# CLAUDE.md

AI Whatsapp agent system: Node.js/TypeScript client (Baileys) + Python/FastAPI API (Pydantic AI + Gemini).

## Structure

```
packages/
├── whatsapp-client/              # TypeScript - WhatsApp interface + REST API
│   └── src/
│       ├── main.ts               # Fastify server entry point (port 3001)
│       ├── whatsapp.ts           # Baileys WebSocket connection, QR auth, message events
│       ├── api-client.ts         # HTTP client for AI API with job polling
│       ├── config.ts             # Environment configuration loader
│       ├── logger.ts             # Pino logger setup
│       ├── types.ts              # TypeScript type definitions
│       ├── handlers/             # Incoming message processors
│       │   ├── text.ts           # Text handler with AI integration + TTS
│       │   ├── audio.ts          # Audio transcription handler
│       │   ├── image.ts          # Image handler with vision support
│       │   └── document.ts       # PDF/document handler
│       ├── routes/               # API endpoints (health, messaging, media, operations)
│       ├── services/             # Baileys service layer
│       ├── schemas/              # Zod request/response validation
│       └── utils/                # Helpers (JID, message extraction, reactions, file validation, vCard)
│
└── ai-api/                       # Python - AI service with RAG + transcription + TTS
    └── src/ai_api/
        ├── main.py               # FastAPI app entry point (port 8000)
        ├── agent.py              # Pydantic AI agent + Gemini integration
        ├── commands.py           # Command parser (/settings, /tts, /stt, /help)
        ├── config.py             # Settings with pydantic-settings
        ├── database.py           # SQLAlchemy models (User, ConversationMessage, ConversationPreferences)
        ├── kb_models.py          # Knowledge base models (KnowledgeBaseDocument, KnowledgeBaseChunk)
        ├── schemas.py            # Pydantic request/response models
        ├── embeddings.py         # Vector embedding generation (pgvector)
        ├── transcription.py      # Groq Whisper speech-to-text
        ├── tts.py                # Gemini text-to-speech synthesis
        ├── processing.py         # PDF processing with Docling
        ├── logger.py             # Structured logging
        ├── whatsapp/             # WhatsApp REST API client
        │   ├── client.py         # Async HTTP client for messaging
        │   └── exceptions.py     # Custom exceptions
        ├── rag/                   # RAG implementations (history + knowledge base search)
        ├── queue/                 # Background jobs (arq + Redis)
        ├── streams/              # Redis Streams job processing
        └── scripts/              # Worker + cleanup scripts (cleanup_expired_documents.py)
```

## Commands

```bash
# Infrastructure
docker-compose up -d                    # PostgreSQL + Redis + Adminer + full stack

# Development (from root)
pnpm dev:server                         # Start AI API (port 8000)
pnpm dev:whatsapp                       # Start WhatsApp client (port 3001)
pnpm dev:queue                          # Start background stream worker
pnpm install:all                        # Install Node + Python dependencies

# Manual startup
cd packages/ai-api && uv run uvicorn ai_api.main:app --reload --port 8000
cd packages/whatsapp-client && pnpm dev # Scan QR code when prompted

# Linting & Formatting
pnpm lint                               # Check TypeScript (ESLint) + Python (Ruff)
pnpm lint:fix                           # Auto-fix lint issues
pnpm format                             # Format TypeScript (Prettier) + Python (Ruff)
pnpm format:check                       # Check formatting without writing
```

## Security

- **API Key Auth**: Both servers require `X-API-Key` header on all routes except `/health` and `/docs*`
  - `AI_API_KEY` — authenticates requests to the Python AI API (required, app fails to start without it)
  - `WHATSAPP_API_KEY` — authenticates requests to the TypeScript WhatsApp API (required)
  - Inter-service calls include the key automatically (`api-client.ts`, `handlers/audio.ts`, `whatsapp/client.py`)
- **CORS**: Configurable via `CORS_ORIGINS` env var (comma-separated). Empty = block all cross-origin requests
- **Rate Limiting**: `RATE_LIMIT_GLOBAL` req/min (default 30), `RATE_LIMIT_EXPENSIVE` req/min (default 5) on `/chat`, `/chat/enqueue`, `/tts`, `/transcribe`, `/knowledge-base/upload`
- **Redis Auth**: `REDIS_PASSWORD` required in `.env`, enforced via `--requirepass` in docker-compose
- **No default passwords**: `POSTGRES_PASSWORD` has no fallback — docker-compose fails if unset

## Agent Tools

The AI agent (`agent.py`, Pydantic AI + Gemini 2.5 Flash) has 12 tools:

- **RAG**: `search_conversation_history`, `search_knowledge_base`
- **Web**: `web_search` (DuckDuckGo), `fetch_website` (Jina Reader)
- **WhatsApp**: `send_whatsapp_reaction`, `send_whatsapp_location`, `send_whatsapp_contact`, `send_whatsapp_message`
- **Utility**: `calculate`, `get_weather`, `wikipedia_lookup`, `convert_units`

## Capabilities

- **Vision**: Images sent via WhatsApp are base64-encoded and passed to Gemini for analysis
- **Conversation-scoped PDFs**: PDFs sent in chat are processed and available to the agent, auto-expire after `CONVERSATION_PDF_TTL_HOURS`
- **Knowledge Base**: Persistent PDF uploads via `/knowledge-base/upload` (single) and `/knowledge-base/upload/batch`
- **Redis Streams**: Per-user sequential processing with concurrent processing across users (supersedes arq queue)

## Guidelines

- Use `pnpm add` / `uv add` for dependencies (never edit package.json/pyproject.toml directly)
- Prefer pure functions over classes
- Use structured logging (Pino for TS, Python logging) - no console.log/print
- Keep this file updated with important changes
- There is no need for you to write tests for this project, the human developer will handle that.

## References

- `README.md` - Setup guide and environment variables
- `.env.example` - Root environment template (all required vars)
- `packages/*/.env.example` - Per-package environment templates
- API docs: http://localhost:8000/docs (Swagger UI)
- DB GUI: http://localhost:8080 (Adminer)
