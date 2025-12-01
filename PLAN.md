# AI WhatsApp Agent System - Implementation Plan

## Project Overview

A cross-platform AI agent system that allows users to interact with an AI assistant via WhatsApp while maintaining conversational memory. Built as a monorepo with a Node.js WhatsApp client and Python AI API.

**Key Decisions:**

- **LLM**: Google Gemini (via LiteLLM)
- **Monorepo**: pnpm Workspaces
- **Database**: PostgreSQL (Docker Compose)
- **Communication**: HTTP + Server-Sent Events (SSE)
- **Approach**: Minimal MVP - no premature abstractions

**Note:** Remember to use your context7 tool to get the latest libraries and best practices for all implementations.
**Note:** The code snippets below are illustrative. Adjust as needed during actual implementation.

## Project Structure

```
ai-boilerplate/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── pnpm-workspace.yaml
├── packages/
│   ├── whatsapp-client/
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── .env.example
│   │   ├── src/
│   │   │   ├── index.ts          # Main entry point
│   │   │   ├── whatsapp.ts       # Baileys connection & message handling
│   │   │   ├── api-client.ts     # HTTP client for AI API + SSE handling
│   │   │   ├── logger.ts         # Simple logging utility
│   │   │   └── types.ts          # TypeScript type definitions
│   │   └── auth_info_baileys/    # Created by Baileys for session
│   │
│   └── ai-api/
│       ├── pyproject.toml
│       ├── .env.example
│       ├── src/
│       │   └── ai_api/
│       │       ├── __init__.py
│       │       ├── main.py           # FastAPI app
│       │       ├── agent.py          # Pydantic AI agent setup
│       │       ├── database.py       # PostgreSQL connection & models
│       │       ├── schemas.py        # Pydantic models for API
│       │       └── logger.py         # Structured logging
│       └── README.md
```

---

## Implementation Plan

### Phase 1: Project Initialization (30 minutes)

#### 1.1 Initialize Git Repository (Done)

```bash
cd /home/luis/projects/ai-boilerplate
git init
```

#### 1.2 Create Root Configuration Files

**pnpm-workspace.yaml**:

```yaml
packages:
  - "packages/*"
```

**.gitignore**:

```
# Node
node_modules/
dist/
*.log
.env
.env.local

# Python
__pycache__/
*.py[cod]
*$py.class
.venv/
.uv/
*.egg-info/

# WhatsApp session
packages/whatsapp-client/auth_info_baileys/

# Database
postgres-data/

# IDE
.vscode/
.idea/
*.swp
*.swo
```

**.env.example**:

```env
# PostgreSQL
POSTGRES_USER=aiagent
POSTGRES_PASSWORD=changeme
POSTGRES_DB=aiagent
DATABASE_URL=postgresql://aiagent:changeme@localhost:5432/aiagent

# Google Gemini
GEMINI_API_KEY=your_gemini_api_key_here

# Services
AI_API_URL=http://localhost:8000
WHATSAPP_CLIENT_PORT=3000
```

**docker-compose.yml**:

```yaml
version: "3.8"

services:
  postgres:
    image: postgres:16-alpine
    container_name: aiagent-postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-aiagent}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}
      POSTGRES_DB: ${POSTGRES_DB:-aiagent}
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aiagent"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  postgres-data:
```

#### 1.3 Initialize pnpm Workspace

```bash
pnpm init
```

### Phase 2: WhatsApp Client Setup (1-2 hours)

#### 2.1 Create WhatsApp Client Package

```bash
mkdir -p packages/whatsapp-client/src
cd packages/whatsapp-client
pnpm init
```

#### 2.2 Install Dependencies

```bash
pnpm add @whiskeysockets/baileys pino @hapi/boom
pnpm add -D typescript @types/node tsx
```

#### 2.3 Configure TypeScript

**tsconfig.json**:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "./dist",
    "rootDir": "./src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true
  },
  "include": ["src/**/*"],
  "exclude": ["node_modules", "dist"]
}
```

**package.json** (update scripts):

```json
{
  "name": "@ai-boilerplate/whatsapp-client",
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "tsx watch src/index.ts",
    "start": "node dist/index.js",
    "build": "tsc"
  }
}
```

#### 2.4 Create Source Files

**src/types.ts**:

```typescript
export interface ChatMessage {
  phone: string; // User's phone number (e.g., "1234567890@s.whatsapp.net")
  message: string; // User's message text
  timestamp: Date;
}

export interface AIResponse {
  response: string;
}
```

**src/logger.ts**:

```typescript
import pino from "pino";

export const logger = pino({
  level: process.env.LOG_LEVEL || "info",
  transport: {
    target: "pino-pretty",
    options: {
      colorize: true,
      translateTime: "SYS:standard",
      ignore: "pid,hostname",
    },
  },
});
```

**src/api-client.ts**:

```typescript
import { logger } from "./logger.js";

const AI_API_URL = process.env.AI_API_URL || "http://localhost:8000";

export async function sendMessageToAI(
  phone: string,
  message: string
): Promise<AsyncIterable<string>> {
  const response = await fetch(`${AI_API_URL}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ phone, message }),
  });

  if (!response.ok) {
    throw new Error(`AI API error: ${response.status} ${response.statusText}`);
  }

  if (!response.body) {
    throw new Error("No response body from AI API");
  }

  return parseSSE(response.body);
}

async function* parseSSE(body: ReadableStream<Uint8Array>): AsyncIterable<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const data = line.slice(6);
          if (data === "[DONE]") return;
          yield data;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
```

**src/whatsapp.ts**:

```typescript
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import { logger } from "./logger.js";
import { sendMessageToAI } from "./api-client.js";

export async function startWhatsAppClient() {
  const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: true,
    browser: ["AI Agent", "Chrome", "1.0.0"],
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect } = update;

    if (connection === "close") {
      const shouldReconnect =
        (lastDisconnect?.error as Boom)?.output?.statusCode !==
        DisconnectReason.loggedOut;

      logger.info({ error: lastDisconnect?.error }, "Connection closed");

      if (shouldReconnect) {
        logger.info("Reconnecting...");
        startWhatsAppClient();
      } else {
        logger.info("Logged out. Please scan QR code again.");
      }
    } else if (connection === "open") {
      logger.info("WhatsApp connection opened successfully");
    }
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", async ({ messages }) => {
    for (const msg of messages) {
      await handleIncomingMessage(sock, msg);
    }
  });

  return sock;
}

async function handleIncomingMessage(sock: any, msg: WAMessage) {
  // Ignore messages from self or broadcast
  if (msg.key.fromMe || msg.key.remoteJid === "status@broadcast") return;

  const messageText = msg.message?.conversation || msg.message?.extendedTextMessage?.text;

  if (!messageText || !msg.key.remoteJid) return;

  logger.info(
    {
      from: msg.key.remoteJid,
      message: messageText,
    },
    "Received message"
  );

  try {
    // Stream response from AI API
    const stream = await sendMessageToAI(msg.key.remoteJid, messageText);
    let fullResponse = "";

    for await (const chunk of stream) {
      fullResponse += chunk;
    }

    // Send complete response to WhatsApp
    await sock.sendMessage(msg.key.remoteJid, { text: fullResponse });

    logger.info({ to: msg.key.remoteJid }, "Sent AI response");
  } catch (error) {
    logger.error({ error }, "Error processing message");
    await sock.sendMessage(msg.key.remoteJid, {
      text: "Sorry, I encountered an error. Please try again.",
    });
  }
}
```

**src/index.ts**:

```typescript
import dotenv from "dotenv";
import { logger } from "./logger.js";
import { startWhatsAppClient } from "./whatsapp.js";

dotenv.config();

async function main() {
  logger.info("Starting WhatsApp AI Agent Client...");

  // Validate environment variables
  if (!process.env.AI_API_URL) {
    logger.warn("AI_API_URL not set, using default: http://localhost:8000");
  }

  await startWhatsAppClient();
}

main().catch((error) => {
  logger.error({ error }, "Fatal error in main");
  process.exit(1);
});
```

**Install additional dependency**:

```bash
pnpm add dotenv pino-pretty
```

**.env.example**:

```env
AI_API_URL=http://localhost:8000
LOG_LEVEL=info
```

### Phase 3: AI API Setup (1-2 hours) Done

#### 3.1 Create AI API Package

```bash
cd ../..
mkdir -p packages/ai-api/src/ai_api
cd packages/ai-api
```

#### 3.2 Initialize Python Project with uv

```bash
uv init --lib
```

#### 3.3 Add Dependencies

```bash
uv add fastapi uvicorn pydantic-ai litellm psycopg2-binary sqlalchemy python-dotenv
```

#### 3.4 Configure pyproject.toml

Update the `[project.scripts]` section:

```toml
[project.scripts]
dev = "uvicorn ai_api.main:app --reload --host 0.0.0.0 --port 8000"
start = "uvicorn ai_api.main:app --host 0.0.0.0 --port 8000"
```

#### 3.5 Create Source Files

**src/ai_api/logger.py**:

```python
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger('ai-api')
```

**src/ai_api/database.py**:

```python
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .logger import logger

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://aiagent:changeme@localhost:5432/aiagent')

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ConversationMessage(Base):
    __tablename__ = 'conversation_messages'

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

def init_db():
    """Initialize database tables"""
    logger.info('Initializing database...')
    Base.metadata.create_all(bind=engine)
    logger.info('Database initialized successfully')

def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_conversation_history(db, phone: str, limit: int = 10):
    """Retrieve recent conversation history for a user"""
    messages = db.query(ConversationMessage)\
        .filter(ConversationMessage.phone == phone)\
        .order_by(ConversationMessage.timestamp.desc())\
        .limit(limit)\
        .all()

    return list(reversed(messages))

def save_message(db, phone: str, role: str, content: str):
    """Save a message to the database"""
    message = ConversationMessage(phone=phone, role=role, content=content)
    db.add(message)
    db.commit()
```

**src/ai_api/schemas.py**:

```python
from pydantic import BaseModel

class ChatRequest(BaseModel):
    phone: str
    message: str

class ChatResponse(BaseModel):
    response: str
```

**src/ai_api/agent.py**:

```python
import os
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from .logger import logger

# Initialize Gemini via LiteLLM
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    raise ValueError('GEMINI_API_KEY environment variable is required')

# Create the AI agent
agent = Agent(
    model='gemini-1.5-flash',
    system_prompt='''You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.'''
)

async def get_ai_response(user_message: str, message_history=None):
    """
    Get AI response for a user message with optional history

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages

    Returns:
        AI response text
    """
    logger.info(f'Getting AI response for message: {user_message[:50]}...')

    result = await agent.run(user_message, message_history=message_history)

    logger.info(f'AI response generated: {result.data[:50]}...')
    return result.data

def format_message_history(db_messages):
    """
    Convert database messages to Pydantic AI message format

    Args:
        db_messages: List of ConversationMessage objects

    Returns:
        List of messages in Pydantic AI format
    """
    from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, UserPromptPart, TextPart

    formatted = []
    for msg in db_messages:
        if msg.role == 'user':
            formatted.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        else:
            formatted.append(ModelResponse(parts=[TextPart(content=msg.content)]))

    return formatted
```

**src/ai_api/main.py**:

```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from .logger import logger
from .database import init_db, get_db, get_conversation_history, save_message
from .schemas import ChatRequest, ChatResponse
from .agent import get_ai_response, format_message_history

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup"""
    logger.info('Starting AI API service...')
    init_db()
    yield
    logger.info('Shutting down AI API service...')

app = FastAPI(
    title='AI WhatsApp Agent API',
    version='1.0.0',
    lifespan=lifespan
)

@app.get('/health')
async def health_check():
    """Health check endpoint"""
    return {'status': 'healthy'}

@app.post('/chat/stream')
async def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Stream AI response for a chat message

    This endpoint accepts a message and streams the AI response using SSE
    """
    logger.info(f'Received chat request from {request.phone}')

    try:
        # Get conversation history
        history = get_conversation_history(db, request.phone, limit=10)
        message_history = format_message_history(history) if history else None

        # Save user message
        save_message(db, request.phone, 'user', request.message)

        # Get AI response
        ai_response = await get_ai_response(request.message, message_history)

        # Save assistant response
        save_message(db, request.phone, 'assistant', ai_response)

        # Stream response
        async def generate():
            # For MVP, send complete response in one chunk
            # Future: implement actual streaming with agent.run_stream()
            yield f'data: {ai_response}\n\n'
            yield 'data: [DONE]\n\n'

        return StreamingResponse(
            generate(),
            media_type='text/event-stream'
        )

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/chat', response_model=ChatResponse)
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Non-streaming chat endpoint (alternative to /chat/stream)
    """
    logger.info(f'Received chat request from {request.phone}')

    try:
        # Get conversation history
        history = get_conversation_history(db, request.phone, limit=10)
        message_history = format_message_history(history) if history else None

        # Save user message
        save_message(db, request.phone, 'user', request.message)

        # Get AI response
        ai_response = await get_ai_response(request.message, message_history)

        # Save assistant response
        save_message(db, request.phone, 'assistant', ai_response)

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')
```

**.env.example**:

```env
DATABASE_URL=postgresql://aiagent:changeme@localhost:5432/aiagent
GEMINI_API_KEY=your_gemini_api_key_here
LOG_LEVEL=INFO
```

### Phase 4: Root Documentation (30 minutes) Done

**README.md**:

```markdown
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

\`\`\`bash
git clone <your-repo>
cd ai-boilerplate
cp .env.example .env
\`\`\`

### 2. Configure Environment

Edit `.env` and add your Gemini API key:
\`\`\`env
GEMINI_API_KEY=your_actual_api_key_here
\`\`\`

### 3. Start Database

\`\`\`bash
docker-compose up -d
\`\`\`

Wait for PostgreSQL to be healthy:
\`\`\`bash
docker-compose ps
\`\`\`

### 4. Setup AI API

\`\`\`bash
cd packages/ai-api
cp .env.example .env

# Edit .env if needed

uv sync
uv run dev
\`\`\`

The API will start on `http://localhost:8000`. Verify with:
\`\`\`bash
curl http://localhost:8000/health
\`\`\`

### 5. Setup WhatsApp Client

In a new terminal:
\`\`\`bash
cd packages/whatsapp-client
cp .env.example .env
pnpm install
pnpm dev
\`\`\`

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

\`\`\`
ai-boilerplate/
├── packages/
│ ├── whatsapp-client/ # Node.js WhatsApp interface
│ └── ai-api/ # Python AI service
├── docker-compose.yml # PostgreSQL setup
└── README.md
\`\`\`

## Development

### View Logs

**AI API:**
\`\`\`bash
cd packages/ai-api
uv run dev
\`\`\`

**WhatsApp Client:**
\`\`\`bash
cd packages/whatsapp-client
pnpm dev
\`\`\`

**Database:**
\`\`\`bash
docker-compose logs -f postgres
\`\`\`

### Database Access

Connect to PostgreSQL:
\`\`\`bash
docker exec -it aiagent-postgres psql -U aiagent -d aiagent
\`\`\`

View messages:
\`\`\`sql
SELECT \* FROM conversation_messages ORDER BY timestamp DESC LIMIT 10;
\`\`\`

### Restart Services

\`\`\`bash

# Restart database

docker-compose restart

# Restart AI API (Ctrl+C then)

cd packages/ai-api && uv run dev

# Restart WhatsApp Client (Ctrl+C then)

cd packages/whatsapp-client && pnpm dev
\`\`\`

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
\`\`\`

---

## API Endpoints

### Health Check

\`GET /health\`

Returns service health status.

### Stream Chat

\`POST /chat/stream\`

Request body:
\`\`\`json
{
"phone": "1234567890@s.whatsapp.net",
"message": "Hello, how are you?"
}
\`\`\`

Returns: Server-Sent Events stream

### Non-Streaming Chat

\`POST /chat\`

Request body: Same as above

Response:
\`\`\`json
{
"response": "I'm doing well, thank you! How can I help you today?"
}
\`\`\`
```

---

## Implementation Order

### Step 1: Database (15 min)

1. Create `docker-compose.yml`
2. Create root `.env` from `.env.example`
3. Run `docker-compose up -d`
4. Verify PostgreSQL: `docker-compose ps`

### Step 2: AI API (45 min)

1. Create `packages/ai-api/` structure
2. Run `uv init --lib`
3. Add dependencies with `uv add`
4. Create all Python files
5. Create `.env` from `.env.example`
6. Start with `uv run dev`
7. Test health: `curl http://localhost:8000/health`

### Step 3: WhatsApp Client (45 min)

1. Create `packages/whatsapp-client/` structure
2. Run `pnpm init`
3. Install dependencies
4. Create TypeScript files
5. Create `.env` from `.env.example`
6. Start with `pnpm dev`
7. Scan QR code

### Step 4: Integration Testing (30 min)

1. Send test WhatsApp message
2. Verify AI response received
3. Check database for saved messages
4. Test conversation continuity (send follow-up)

### Step 5: Documentation (15 min)

1. Create comprehensive README.md
2. Document troubleshooting steps
3. Add setup instructions

---

## Environment Variables Reference

### Root `.env`

- `POSTGRES_USER`: PostgreSQL username
- `POSTGRES_PASSWORD`: PostgreSQL password
- `POSTGRES_DB`: Database name
- `DATABASE_URL`: Full database connection string
- `GEMINI_API_KEY`: Google Gemini API key
- `AI_API_URL`: URL for AI API service
- `WHATSAPP_CLIENT_PORT`: Port for WhatsApp client

### `packages/ai-api/.env`

- `DATABASE_URL`: PostgreSQL connection
- `GEMINI_API_KEY`: Gemini API key
- `LOG_LEVEL`: Logging level (INFO/DEBUG/ERROR)

### `packages/whatsapp-client/.env`

- `AI_API_URL`: AI API endpoint
- `LOG_LEVEL`: Logging level

---

## Database Schema

### `conversation_messages` Table

| Column      | Type      | Description                                |
| ----------- | --------- | ------------------------------------------ |
| `id`        | INTEGER   | Primary key, auto-increment                |
| `phone`     | VARCHAR   | User's phone number (with @s.whatsapp.net) |
| `role`      | VARCHAR   | 'user' or 'assistant'                      |
| `content`   | TEXT      | Message content                            |
| `timestamp` | TIMESTAMP | Message timestamp (UTC)                    |

**Indexes:**

- Primary key on `id`
- Index on `phone` for fast user lookups
- Natural ordering by `timestamp`

---

## Testing Strategy

### Manual Testing Checklist

#### Initial Setup

- [ ] Database starts successfully
- [ ] AI API health check returns 200
- [ ] WhatsApp QR code appears
- [ ] WhatsApp connects successfully

#### Basic Functionality

- [ ] Send simple message → receive AI response
- [ ] AI response is relevant to question
- [ ] Both messages saved to database
- [ ] Logs show proper flow

#### Conversation Memory

- [ ] Ask "What's 2+2?" → AI responds "4"
- [ ] Ask "What was my last question?" → AI recalls it
- [ ] Verify both messages in database
- [ ] History properly formatted

#### Error Handling

- [ ] Stop AI API → WhatsApp sends error message
- [ ] Invalid Gemini key → proper error logged
- [ ] Database down → error logged (API won't start)

#### Reconnection

- [ ] Close WhatsApp client (Ctrl+C)
- [ ] Restart → reconnects without QR
- [ ] Session persists in `auth_info_baileys/`

### SQL Queries for Verification

```sql
-- View all conversations
SELECT phone, role, content, timestamp
FROM conversation_messages
ORDER BY timestamp DESC;

-- View conversation for specific user
SELECT role, content, timestamp
FROM conversation_messages
WHERE phone = 'YOUR_PHONE@s.whatsapp.net'
ORDER BY timestamp;

-- Count messages by user
SELECT phone, COUNT(*) as message_count
FROM conversation_messages
GROUP BY phone;
```

---

## Key Technical Notes

### Baileys Session Management

- Session stored in `auth_info_baileys/` directory
- Add to `.gitignore` (contains credentials)
- Delete folder to force re-authentication
- Automatic reconnection on disconnect (unless logged out)

### Pydantic AI Message History

- Store last 10 messages per user (configurable)
- Format: `ModelRequest` (user) and `ModelResponse` (assistant)
- Serializable to JSON for database storage
- Pass to `agent.run()` via `message_history` parameter

### LiteLLM + Gemini

- Model name: `gemini-1.5-flash` or `gemini-1.5-pro`
- API key via `GEMINI_API_KEY` env var
- Streaming supported but simplified in MVP
- Rate limits: Check Gemini console

### Server-Sent Events (SSE)

- Content-Type: `text/event-stream`
- Format: `data: <content>\n\n`
- End signal: `data: [DONE]\n\n`
- Keep-alive recommended for production

### Database Considerations

- SQLAlchemy ORM for type safety
- UTC timestamps for consistency
- Index on `phone` for performance
- Future: Add pagination for long histories

---

## Success Criteria

The MVP is considered complete when:

1. ✅ WhatsApp client connects and maintains session
2. ✅ User can send message via WhatsApp
3. ✅ AI receives message with conversation history
4. ✅ AI responds with relevant answer
5. ✅ Response appears in WhatsApp
6. ✅ Both messages saved to PostgreSQL
7. ✅ Follow-up questions maintain context
8. ✅ Basic error handling works
9. ✅ Services log properly
10. ✅ Documentation covers setup and troubleshooting

---

## Next Steps After MVP

Once MVP is validated:

1. **Real Streaming**: Implement `agent.run_stream()` for true token-by-token streaming
2. **Access Control**: Add allowlist/blocklist functionality
3. **Group Support**: Detect groups and respond only when mentioned
4. **Reactions**: Add status indicators (🔁 ⚙️ ✅ ⚠️)
5. **Images**: Support image messages with Gemini Vision
6. **Message Queue**: Implement per-user queuing to prevent race conditions
7. **Monitoring**: Add Prometheus metrics and health checks
8. **Tests**: Unit tests and integration tests
9. **CI/CD**: GitHub Actions for automated testing
10. **Deployment**: Production deployment guide (Docker, systemd, etc.)
