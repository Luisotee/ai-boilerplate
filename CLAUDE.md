# CLAUDE.md

AI Whatsapp agent system: Node.js/TypeScript client (Baileys) + Python/FastAPI API (Pydantic AI + Gemini).

## Structure

```
packages/
├── whatsapp-client/              # TypeScript - WhatsApp interface + REST API
│   └── src/
│       ├── main.ts               # Fastify server entry point (port 3001)
│       ├── whatsapp.ts           # Baileys WebSocket connection, QR auth, message events
│       ├── api-client.ts         # HTTP client for AI API with SSE streaming
│       ├── routes/               # API endpoints (health, messaging, media, operations)
│       ├── services/             # Baileys service layer
│       ├── schemas/              # Zod request/response validation
│       └── utils/                # Helpers (JID handling, file validation, vCard)
│
└── ai-api/                       # Python - AI service with RAG + transcription
    └── src/ai_api/
        ├── main.py               # FastAPI app entry point (port 8000)
        ├── agent.py              # Pydantic AI agent + Gemini integration
        ├── database.py           # SQLAlchemy models (User, ConversationMessage)
        ├── schemas.py            # Pydantic request/response models
        ├── embeddings.py         # Vector embedding generation (pgvector)
        ├── transcription.py      # Groq Whisper speech-to-text
        ├── rag/                   # RAG implementations (history + knowledge base search)
        ├── queue/                 # Background jobs (arq + Redis)
        └── streams/              # SSE streaming management
```

## Commands

```bash
# Infrastructure
docker-compose up -d                    # PostgreSQL + Redis + Adminer

# AI API (Python)
cd packages/ai-api
uv sync
uv run uvicorn ai_api.main:app --reload --port 8000

# WhatsApp Client (Node.js)
cd packages/whatsapp-client
pnpm install
pnpm dev                                # Scan QR code when prompted

# Linting & Formatting
pnpm lint                               # Check TypeScript (ESLint) + Python (Ruff)
pnpm format                             # Format TypeScript (Prettier) + Python (Ruff)
```

## Guidelines

- Use `pnpm add` / `uv add` for dependencies (never edit package.json/pyproject.toml directly)
- Prefer pure functions over classes
- Use structured logging (Pino for TS, Python logging) - no console.log/print
- Keep this file updated with important changes

## References

- `README.md` - Setup guide and environment variables
- `PLAN.md` - Architecture and design decisions
- `packages/*/.env.example` - Environment templates
- API docs: http://localhost:8000/docs (Swagger UI)
- DB GUI: http://localhost:8080 (Adminer)
