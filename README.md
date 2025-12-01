# AI WhatsApp Agent System

A cross-platform AI agent system that enables conversational AI interactions via WhatsApp, with conversation memory maintained across sessions.

## Architecture

- **WhatsApp Client** (Node.js/TypeScript): Handles WhatsApp messaging using Baileys
- **AI API** (Python/FastAPI): Manages AI responses using Pydantic AI + Google Gemini
- **Database** (PostgreSQL): Stores conversation history for context continuity

## Prerequisites

- Node.js 18+ and pnpm
- Python 3.11+ and uv
- Docker and Docker Compose
- Google Gemini API key ([Get one here](https://aistudio.google.com/apikey))

## Quick Start

### 1. Clone and Setup

```bash
git clone <your-repo>
cd ai-boilerplate
cp .env.example .env
```

### 2. Configure Environment

Edit `.env` and add your Gemini API key:
```env
GEMINI_API_KEY=your_actual_api_key_here
```

### 3. Start Database

```bash
docker-compose up -d
```

Wait for PostgreSQL to be healthy:
```bash
docker-compose ps
```

### 4. Setup AI API

```bash
cd packages/ai-api
cp .env.example .env

# Edit .env if needed

uv sync
uv run uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000
```

The API will start on `http://localhost:8000`. Verify with:
```bash
curl http://localhost:8000/health
```

### 5. Setup WhatsApp Client

In a new terminal:
```bash
cd packages/whatsapp-client
cp .env.example .env
pnpm install
pnpm dev
```

### 6. Connect WhatsApp

1. A QR code will appear in the terminal
2. Open WhatsApp on your phone
3. Go to Settings → Linked Devices → Link a Device
4. Scan the QR code
5. Wait for "WhatsApp connection opened successfully"

### 7. Test the System

Send a message to your WhatsApp number from another phone. The AI agent should respond!

## How It Works

1. **User sends WhatsApp message** → Baileys client receives it
2. **WhatsApp client** → Sends message to AI API via HTTP POST
3. **AI API** → Fetches conversation history from PostgreSQL
4. **AI API** → Sends message + history to Gemini via Pydantic AI
5. **AI API** → Streams response back via SSE
6. **WhatsApp client** → Receives streamed response
7. **WhatsApp client** → Sends AI response to user on WhatsApp
8. **AI API** → Saves both messages to PostgreSQL

## Project Structure

```
ai-boilerplate/
├── packages/
│   ├── whatsapp-client/    # Node.js WhatsApp interface
│   └── ai-api/             # Python AI service
├── docker-compose.yml      # PostgreSQL setup
└── README.md
```

## Development

### View Logs

**AI API:**
```bash
cd packages/ai-api
uv run uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000
```

**WhatsApp Client:**
```bash
cd packages/whatsapp-client
pnpm dev
```

**Database:**
```bash
docker-compose logs -f postgres
```

### Database Access

Connect to PostgreSQL:
```bash
docker exec -it aiagent-postgres psql -U aiagent -d aiagent
```

View users:
```sql
SELECT * FROM users ORDER BY created_at DESC;
```

View messages with user info:
```sql
SELECT u.phone, u.name, m.role, m.content, m.timestamp
FROM conversation_messages m
JOIN users u ON m.user_id = u.id
ORDER BY m.timestamp DESC LIMIT 10;
```

### Database GUI (Adminer)

Adminer provides a web-based interface for managing your database, similar to Prisma Studio:

**Access:** http://localhost:8080

**Login credentials:**
- System: **PostgreSQL**
- Server: **postgres**
- Username: **aiagent**
- Password: **changeme**
- Database: **aiagent**

With Adminer you can:
- Browse tables and view data
- Run SQL queries
- Edit records directly
- Export data (CSV, SQL, etc.)

### API Documentation

The AI API includes auto-generated interactive documentation:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **OpenAPI Schema**: http://localhost:8000/openapi.json

### Restart Services

```bash
# Restart database
docker-compose restart

# Restart AI API (Ctrl+C then)
cd packages/ai-api && uv run uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000

# Restart WhatsApp Client (Ctrl+C then)
cd packages/whatsapp-client && pnpm dev
```

## Troubleshooting

### QR Code Not Showing

- Make sure WhatsApp client is running with `pnpm dev`
- Check that port 3000 isn't in use
- Delete `auth_info_baileys/` folder and restart

### AI Not Responding

- Verify AI API is running: `curl http://localhost:8000/health`
- Check `GEMINI_API_KEY` in `packages/ai-api/.env`
- Check AI API logs for errors

### Database Connection Failed

- Ensure PostgreSQL is running: `docker-compose ps`
- Check `DATABASE_URL` in both `.env` files
- Verify PostgreSQL is healthy: `docker-compose logs postgres`

### Connection Closed/Logged Out

- Baileys session expired
- Delete `auth_info_baileys/` folder
- Restart client and scan QR code again

## API Endpoints

### Health Check

`GET /health`

Returns service health status.

### Stream Chat

`POST /chat/stream`

Request body:
```json
{
  "phone": "1234567890@s.whatsapp.net",
  "message": "Hello, how are you?"
}
```

Returns: Server-Sent Events stream

### Non-Streaming Chat

`POST /chat`

Request body: Same as above

Response:
```json
{
  "response": "I'm doing well, thank you! How can I help you today?"
}
```

## Future Features (Post-MVP)

- [ ] Access control (allowlist/blocklist)
- [ ] Group support (@mention only)
- [ ] Reaction indicators (🔁 ⚙️ ✅ ⚠️)
- [ ] Image support (Gemini Vision)
- [ ] Message queue (prevent race conditions)
- [ ] Auto-reconnection with backoff
- [ ] Telegram integration

## License

MIT
