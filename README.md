# AI WhatsApp Agent

A production-ready AI agent system that brings conversational AI to WhatsApp with persistent memory, RAG-powered knowledge bases, and multi-language speech processing.

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Client** | Node.js, TypeScript, Fastify, Baileys (WhatsApp Web), Zod |
| **API** | Python 3.11+, FastAPI, Pydantic AI, SQLAlchemy 2.0 |
| **AI/ML** | Google Gemini (LLM, Embeddings, TTS), Groq Whisper (STT) |
| **Database** | PostgreSQL 16 + pgvector (vector similarity search) |
| **Infrastructure** | Docker Compose, Redis Streams, Background Workers |

## Features

### Conversational AI with Memory
- Persistent conversation history stored in PostgreSQL
- Context-aware responses using configurable message windows
- Separate tracking for private chats and group conversations
- Semantic search through past conversations using vector embeddings

### RAG Knowledge Base
- PDF document upload with background processing (Docling)
- Semantic chunking with token-aware splitting (512 tokens/chunk)
- Vector similarity search using pgvector (3072-dim embeddings)
- Auto-generated citations with document name, page number, and section

### Speech Processing
- **Speech-to-Text:** Groq Whisper v3 large with auto language detection
- **Text-to-Speech:** Gemini TTS with language-specific voices
- **5 Languages:** English, Spanish, Portuguese, French, German
- Per-user language preferences

### Real-time Streaming
- Server-Sent Events (SSE) for token-by-token streaming
- Async job queue with polling for background processing
- Redis Streams for per-user message queuing

### WhatsApp Integration
- Group chat support with sender attribution and @mention handling
- Message reactions (status indicators)
- Media handling: images, audio, video, documents
- Location sharing and contact cards (vCard)
- Voice messages with TTS responses

### Command System
| Command | Description |
|---------|-------------|
| `/settings` | Show current TTS/STT preferences |
| `/tts on\|off` | Enable/disable voice responses |
| `/tts lang [code]` | Set TTS language (en, es, pt, fr, de) |
| `/stt lang [code\|auto]` | Set transcription language |
| `/clean` | Delete all conversation history |
| `/clean [1h\|7d\|1m]` | Delete messages from time period |
| `/help` | Show available commands |

## Architecture

```
┌─────────────────┐     HTTP/SSE      ┌─────────────────┐
│   WhatsApp      │◄────────────────►│    AI API       │
│   Client        │                   │   (FastAPI)     │
│   (Fastify)     │                   │                 │
└────────┬────────┘                   └────────┬────────┘
         │                                     │
         │ WebSocket                           │ Async
         │                                     │
┌────────▼────────┐                   ┌────────▼────────┐
│   WhatsApp      │                   │   PostgreSQL    │
│   (Baileys)     │                   │   + pgvector    │
└─────────────────┘                   └────────┬────────┘
                                               │
                              ┌────────────────┼────────────────┐
                              │                │                │
                      ┌───────▼──────┐ ┌───────▼──────┐ ┌───────▼──────┐
                      │   Gemini     │ │    Groq      │ │    Redis     │
                      │ LLM/Embed/TTS│ │   Whisper    │ │   Streams    │
                      └──────────────┘ └──────────────┘ └──────────────┘
```

**Key Patterns:**
- Pydantic AI agent with tool system (6 tools: search, reactions, location, contacts, etc.)
- Dependency injection for testable, modular code
- Graceful degradation when optional APIs unavailable
- Background job processing with Redis Streams consumer groups

## Project Structure

```
packages/
├── whatsapp-client/           # TypeScript - WhatsApp interface
│   └── src/
│       ├── main.ts            # Fastify server (port 3001)
│       ├── whatsapp.ts        # Baileys connection + message events
│       ├── handlers/          # Text and audio message processors
│       ├── routes/            # REST API (messaging, media, operations)
│       ├── services/          # Baileys abstraction layer
│       └── utils/             # JID, reactions, vCard utilities
│
└── ai-api/                    # Python - AI service
    └── src/ai_api/
        ├── main.py            # FastAPI app (port 8000)
        ├── agent.py           # Pydantic AI agent + tools
        ├── commands.py        # Command parser
        ├── database.py        # SQLAlchemy models
        ├── embeddings.py      # Vector embedding generation
        ├── transcription.py   # Groq Whisper STT
        ├── tts.py             # Gemini TTS synthesis
        ├── processing.py      # PDF parsing (Docling)
        ├── rag/               # RAG implementations
        ├── queue/             # Redis job utilities
        └── streams/           # Background processor
```

## Quick Start

### Prerequisites
- Node.js 18+ and pnpm
- Python 3.11+ and uv
- Docker and Docker Compose
- API Keys: [Google Gemini](https://aistudio.google.com/apikey), [Groq](https://console.groq.com/keys) (optional)

### Setup

```bash
# Clone and configure
git clone <repo>
cd ai-boilerplate
cp .env.example .env  # Add your GEMINI_API_KEY

# Start infrastructure
docker-compose up -d

# Terminal 1: AI API
cd packages/ai-api
cp .env.example .env
uv sync
uv run uvicorn ai_api.main:app --reload --port 8000

# Terminal 2: WhatsApp Client
cd packages/whatsapp-client
cp .env.example .env
pnpm install
pnpm dev  # Scan QR code when prompted

# Terminal 3: Background Worker (optional, for async processing)
pnpm dev:queue
```

### Verify
1. Send a message to your WhatsApp number
2. The AI agent responds with context-aware replies
3. API docs available at http://localhost:8000/docs

## API Endpoints

### Chat
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/chat` | Synchronous chat response |
| POST | `/chat/stream` | SSE streaming response |
| POST | `/chat/enqueue` | Async job (returns job_id) |
| GET | `/chat/job/{job_id}` | Poll job status + chunks |

### Knowledge Base
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/knowledge-base/upload` | Upload single PDF |
| POST | `/knowledge-base/upload/batch` | Upload multiple PDFs |
| GET | `/knowledge-base/documents` | List documents (paginated) |
| GET | `/knowledge-base/status/{id}` | Processing status |
| DELETE | `/knowledge-base/documents/{id}` | Delete document |

### Speech
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/transcribe` | Speech-to-text (audio file) |
| POST | `/tts` | Text-to-speech (returns audio) |

### Preferences
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/preferences/{jid}` | Get user settings |
| PATCH | `/preferences/{jid}` | Update TTS/STT settings |

## Development

```bash
# From project root
pnpm dev:server      # Start AI API
pnpm dev:whatsapp    # Start WhatsApp client
pnpm dev:queue       # Start background worker
pnpm install:all     # Install all dependencies
pnpm lint            # Check TypeScript + Python
pnpm format          # Format all code
```

### Database Access
- **Adminer GUI:** http://localhost:8080 (postgres / aiagent / changeme)
- **Direct:** `docker exec -it aiagent-postgres psql -U aiagent -d aiagent`

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google Gemini API key (required) |
| `GROQ_API_KEY` | Groq API key (optional, for STT) |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `AI_API_URL` | AI API endpoint for WhatsApp client |

See `packages/*/.env.example` for full configuration options.

## License

MIT
