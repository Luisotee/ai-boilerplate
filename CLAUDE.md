# CLAUDE.md - AI WhatsApp Agent System

This file provides Claude Code with project-specific context, development guidelines, and best practices for working effectively on this codebase.

## Repository Structure

```
ai-boilerplate/
├── packages/
│   ├── whatsapp-client/           # Node.js/TypeScript - Baileys WhatsApp interface
│   │   ├── src/
│   │   │   ├── index.ts          # Entry point, environment validation
│   │   │   ├── whatsapp.ts       # Baileys connection & message handling
│   │   │   ├── api-client.ts     # HTTP client + SSE parser for AI API
│   │   │   ├── logger.ts         # Pino structured logging configuration
│   │   │   └── types.ts          # TypeScript type definitions
│   │   ├── package.json          # Dependencies: baileys, pino, dotenv
│   │   ├── tsconfig.json         # ES2022, NodeNext modules
│   │   ├── .env.example          # Environment template
│   │   └── auth_info_baileys/    # Baileys session storage (gitignored)
│   │
│   └── ai-api/                   # Python/FastAPI - AI service
│       ├── src/ai_api/
│       │   ├── main.py           # FastAPI app with auto-generated docs
│       │   ├── agent.py          # Pydantic AI agent + Gemini integration
│       │   ├── database.py       # SQLAlchemy models & session management
│       │   ├── schemas.py        # Pydantic request/response models
│       │   └── logger.py         # Python structured logging
│       ├── pyproject.toml        # uv project config, dependencies, scripts
│       └── .env.example          # Environment template
│
├── docker-compose.yml            # PostgreSQL 16 with health checks
├── .env.example                  # Root environment template
├── pnpm-workspace.yaml           # Monorepo workspace configuration
├── package.json                  # Root package.json for pnpm
├── README.md                     # User-facing documentation
├── PLAN.md                       # Detailed implementation plan
└── CLAUDE.md                     # This file (development guide)
```

## Technology Stack

### WhatsApp Client (Node.js/TypeScript)

**Core Dependencies:**

- `@whiskeysockets/baileys` - WhatsApp Web API library
- `pino` + `pino-pretty` - Structured logging
- `@hapi/boom` - Error handling for Baileys
- `dotenv` - Environment variable management

**TypeScript Configuration:**

- Target: ES2022
- Module: NodeNext (native ES modules)
- Strict mode enabled
- Import extensions: `.js` (required for ES modules)

**Key Files:**

- `whatsapp.ts`: Manages WebSocket connection, QR code auth, message events
- `api-client.ts`: Implements SSE parsing for streaming AI responses
- `logger.ts`: Pino configuration with pretty printing for development

### AI API (Python)

**Core Dependencies:**

- `fastapi` - Web framework with auto-generated docs
- `pydantic-ai` - AI agent framework
- `litellm` - Unified LLM interface (used by Pydantic AI)
- `sqlalchemy` - PostgreSQL ORM
- `psycopg2-binary` - PostgreSQL driver
- `uvicorn` - ASGI server
- `python-dotenv` - Environment variable management

**Python Version:** 3.11+

**Key Files:**

- `main.py`: FastAPI app, endpoints, lifespan management
- `agent.py`: Pydantic AI agent configuration, history formatting
- `database.py`: SQLAlchemy models, session factory, DB operations
- `schemas.py`: Pydantic models for request/response validation

### Infrastructure

**PostgreSQL 16:**

- Runs in Docker container (`aiagent-postgres`)
- Health checks ensure ready state before API starts
- Persistent volume: `postgres-data`
- Table: `conversation_messages` (id, phone, role, content, timestamp)

**Monorepo Management:**

- pnpm Workspaces for Node.js packages
- uv for Python dependency management
- Separate `.env` files for each service

## Common Development Commands

### Initial Setup

```bash
# Clone and configure
git clone <your-repo>
cd ai-boilerplate
cp .env.example .env

# Edit .env and add your GEMINI_API_KEY
# Get API key: https://aistudio.google.com/apikey

# Start PostgreSQL
docker-compose up -d

# Verify database is healthy
docker-compose ps
```

### AI API (Python)

```bash
cd packages/ai-api

# First-time setup
cp .env.example .env
uv sync                    # Install all dependencies

# Development (hot reload)
uv run uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000

# Production
uv run uvicorn ai_api.main:app --host 0.0.0.0 --port 8000

# Health check
curl http://localhost:8000/health
```

### WhatsApp Client (Node.js)

```bash
cd packages/whatsapp-client

# First-time setup
cp .env.example .env
pnpm install               # Install dependencies

# Development (hot reload)
pnpm dev                   # Uses tsx watch

# Production
pnpm build                 # Compile TypeScript
pnpm start                 # Run compiled code

# When QR code appears:
# 1. Open WhatsApp on phone
# 2. Settings → Linked Devices → Link a Device
# 3. Scan QR code displayed in terminal
```

### Database Management

```bash
# Check PostgreSQL status
docker-compose ps

# View logs
docker-compose logs -f postgres

# Connect to database
docker exec -it aiagent-postgres psql -U aiagent -d aiagent

# Inside psql:
\dt                        # List tables
\d conversation_messages   # Describe table schema
SELECT * FROM conversation_messages ORDER BY timestamp DESC LIMIT 10;

# Reset database (DESTRUCTIVE)
docker-compose down -v     # Removes containers and volumes
docker-compose up -d       # Fresh start
```

### Testing and Verification

```bash
# Health check
curl http://localhost:8000/health

# Test non-streaming endpoint
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"phone":"test@s.whatsapp.net","message":"Hello, AI!"}'

# Test streaming endpoint
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"phone":"test@s.whatsapp.net","message":"Hello!"}'

# View API documentation
# Swagger UI: http://localhost:8000/docs
# ReDoc: http://localhost:8000/redoc
# OpenAPI schema: http://localhost:8000/openapi.json
```

### Debugging

```bash
# View real-time logs
cd packages/ai-api && uv run uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000
cd packages/whatsapp-client && pnpm dev    # WhatsApp client logs

# Reset WhatsApp session (re-authenticate)
rm -rf packages/whatsapp-client/auth_info_baileys/
cd packages/whatsapp-client && pnpm dev    # Scan QR again

# Reset database completely
docker-compose down -v
docker-compose up -d

# Check what's using a port
lsof -ti:8000              # Check port 8000
lsof -ti:8000 | xargs kill -9  # Kill process on port 8000
```

## Development Best Practices

**Note:** Don't add new libraries by editing `package.json` or `pyproject.toml` directly. Use `pnpm add <package>` or `uv add <package>` to ensure proper lockfile updates.

### TypeScript (WhatsApp Client)

**Async/Await:**

```typescript
// ✅ CORRECT: Properly handle Baileys events
sock.ev.on("messages.upsert", async ({ messages }) => {
  for (const msg of messages) {
    await handleIncomingMessage(sock, msg);
  }
});

// ❌ WRONG: Not awaiting async operations
sock.ev.on("messages.upsert", ({ messages }) => {
  messages.forEach((msg) => handleIncomingMessage(sock, msg)); // No await!
});
```

**Logging:**

```typescript
// ✅ CORRECT: Use Pino structured logging
logger.info({ from: phoneNumber, message: text }, "Received message");
logger.error({ error }, "Error processing message");

// ❌ WRONG: console.log
console.log("Received message from", phoneNumber);
```

**Error Handling:**

```typescript
// ✅ CORRECT: Send user-friendly error messages
try {
  const response = await sendMessageToAI(phone, message);
  await sock.sendMessage(phone, { text: response });
} catch (error) {
  logger.error({ error }, "Error processing message");
  await sock.sendMessage(phone, {
    text: "Sorry, I encountered an error. Please try again.",
  });
}
```

**Dependency Injection:**

```python
# ✅ CORRECT: Use FastAPI Depends for DB sessions
@app.post('/chat')
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    # db session automatically created and closed
    save_message(db, request.phone, 'user', request.message)

# ❌ WRONG: Manual session management
@app.post('/chat')
async def chat(request: ChatRequest):
    db = SessionLocal()  # Manual session - prone to leaks
    save_message(db, ...)
    db.close()  # Easy to forget!
```

**Error Handling:**

```python
# ✅ CORRECT: Use HTTPException with proper status codes
try:
    ai_response = await get_ai_response(message, history)
except Exception as e:
    logger.error(f'Error: {str(e)}', exc_info=True)
    raise HTTPException(status_code=500, detail='Internal server error')

# ❌ WRONG: Expose internal errors
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))  # Leaks internals!
```

**Docstrings for Auto-Generated Docs:**

```python
# ✅ CORRECT: Detailed docstrings feed Swagger UI
@app.post('/chat/stream', tags=['Chat'])
async def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Stream AI response for a chat message

    This endpoint accepts a message and streams the AI response using SSE.

    **Request Body:**
    - `phone`: User's phone number (e.g., "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - Server-Sent Events stream with AI response chunks
    """
```

### Database

**SQLAlchemy ORM:**

```python
# ✅ CORRECT: Use ORM methods
messages = db.query(ConversationMessage)\
    .filter(ConversationMessage.phone == phone)\
    .order_by(ConversationMessage.timestamp.desc())\
    .limit(10)\
    .all()

# ❌ WRONG: Raw SQL (avoid unless absolutely necessary)
db.execute(f"SELECT * FROM conversation_messages WHERE phone = '{phone}'")
```

**UTC Timestamps:**

```python
# ✅ CORRECT: Always use UTC
from datetime import datetime
timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

# ❌ WRONG: Local timezone (inconsistent)
timestamp = Column(DateTime, default=datetime.now)
```

**Indexes:**

```python
# ✅ CORRECT: Index frequently queried columns
phone = Column(String, index=True, nullable=False)

# ❌ WRONG: No index on phone (slow lookups)
phone = Column(String, nullable=False)
```

## Architecture Patterns

### Message Flow

```
1. User sends WhatsApp message
   ↓
2. Baileys client receives via WebSocket
   ↓
3. WhatsApp client → HTTP POST → AI API /chat/stream
   ↓
4. AI API → PostgreSQL (fetch last 10 messages)
   ↓
5. AI API → Format history as ModelRequest/ModelResponse
   ↓
6. AI API → Pydantic AI agent.run(message, history)
   ↓
7. Pydantic AI → Google Gemini API
   ↓
8. Gemini → Response text → Pydantic AI
   ↓
9. AI API → PostgreSQL (save user + assistant messages)
   ↓
10. AI API → Stream response via SSE
   ↓
11. WhatsApp client → Parse SSE chunks
   ↓
12. WhatsApp client → Baileys sendMessage()
   ↓
13. User receives AI response in WhatsApp
```

### Key Design Decisions

**SSE over WebSockets:**

- Simpler protocol for one-way streaming
- No need for bidirectional communication
- Easy to implement with fetch() API
- Future: Can upgrade to WebSockets if needed

**Complete Responses in MVP:**

- Current implementation sends full response in one SSE chunk
- Simplifies initial development
- Future: Implement true token-by-token streaming with `agent.run_stream()`

**Stateless API:**

- All conversation state stored in PostgreSQL
- API servers can be horizontally scaled
- No in-memory state (except database connections)

**Session Management:**

- WhatsApp session managed locally by Baileys
- Stored in `auth_info_baileys/` directory
- No session state in API or database

**10-Message History:**

- Configurable limit (`limit=10` parameter)
- Prevents token limit issues
- Good balance between context and cost
- Adjust in `main.py:79` and `main.py:125` if needed

**Message History Format:**

```python
# Pydantic AI expects ModelRequest/ModelResponse objects
history = [
    ModelRequest(parts=[UserPromptPart(content="What's 2+2?")]),
    ModelResponse(parts=[TextPart(content="4")]),
    ModelRequest(parts=[UserPromptPart(content="What was my question?")]),
]
```

## Troubleshooting Guide

### Keeping Docs Updated

The documentation is auto-generated from:

- Endpoint docstrings
- Pydantic model fields
- FastAPI `description` parameter

**Official Documentation:**

- [Baileys GitHub](https://github.com/WhiskeySockets/Baileys)
- [Pydantic AI Docs](https://ai.pydantic.dev/)
- [FastAPI Docs](https://fastapi.tiangolo.com/)
- [Gemini API](https://ai.google.dev/docs)

**Project Files:**

- `README.md` - User-facing setup guide
- `PLAN.md` - Detailed implementation plan with code snippets
- `.env.example` files - Environment variable templates

**Development Philosophy:**

- Keep it simple
- Ship working code
- Iterate based on real usage
- Avoid premature optimization
