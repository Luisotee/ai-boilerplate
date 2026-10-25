---
name: codebase-context
description: >-
  Gathers and explores codebase architecture, package structure, key entry points,
  database models, and conventions. Use when exploring or getting oriented in the
  repository.
---

# Codebase Architecture Context (`codebase-context`)

Exploration and architectural guide for navigating the `ai-boilerplate` monorepo.

## 1. Package Map & Entry Points

```
packages/
├── whatsapp-client/   # TypeScript — Fastify (port 3001) + Baileys WhatsApp WebSocket
│   └── src/           # whatsapp.ts (message router), handlers/, routes/, services/
├── whatsapp-cloud/    # TypeScript — Fastify (port 3002) + Meta WhatsApp Cloud API
│   └── src/           # routes/webhook.ts (event dispatch), handlers/, services/
├── telegram-client/   # TypeScript — Fastify (port 3003) + grammY Telegram Bot API
│   └── src/           # updates.ts (dispatch table), bot.ts, handlers/, services/
└── ai-api/            # Python — FastAPI (port 8000) + Pydantic AI agent
    └── src/ai_api/    # main.py, agent/core.py, streams/processor.py, routes/, database.py
scripts/
└── instance_check.py  # Multi-bot host validation (ports, service names, volumes)
```

## 2. Core Pipelines & Control Flow

### Inbound Message Pipeline (10 Steps)
1. **Transport Inbound**: Baileys WebSocket (`whatsapp.ts`) / Meta Webhook (`webhook.ts`) / grammY (`updates.ts`).
2. **Wrapper Normalization**: `normalizeMessageContent()` strips viewOnce / ephemeral wrappers.
3. **Dispatch & Extraction**: Media download (audio transcribed, image/PDF extracted) vs text.
4. **Processing Funnel**: All inbound messages funnel into `handleTextMessage()`.
5. **Enqueue**: `api-client.ts` sends `POST /chat/enqueue` to Python AI API.
6. **Slash Command Interception**: Synchronous commands (`/settings`, `/tts`, `/clean`, etc.) intercept in `routes/chat.py` before queueing. Group admin commands fail closed.
7. **Stream Enqueue**: Chat messages persist to PostgreSQL `conversation_messages` and enqueue to Redis Stream `stream:user:{user_id}`.
8. **Worker Agent Execution**: `run_stream_worker.py` consumes stream, loads history, and invokes the Pydantic AI agent (`streams/processor.py`).
9. **Result Polling**: `api-client.ts` polls `GET /chat/job/{id}` (500ms interval, 120s timeout).
10. **Delivery**: Text response sent via platform client; optional TTS generated if enabled.

## 3. Database Schema (PostgreSQL + pgvector)

11 tables managed across `database.py` and `kb_models.py`:
- `users`: Core identity, WhatsApp JID, phone, linked IDs.
- `conversation_messages`: Message history, role (`user`/`assistant`), token counts.
- `conversation_preferences`: Per-user settings (TTS, STT, prompt overrides).
- `core_memories`: Long-term user memory markdown injected into system prompt.
- `bot_prompt`: Active dynamic system prompt (`PUT /admin/prompt`).
- `runtime_settings`: Hot dynamic configuration (`REGISTRY`).
- `broadcasts` & `broadcast_recipients`: Audience-targeted broadcasts.
- `bug_reports`: User-submitted and automated bug reports.
- `knowledge_base_documents` & `knowledge_base_chunks`: RAG documents and 3072-dim embeddings.

> [!IMPORTANT]
> No Alembic migrations. Schema changes use idempotent SQL files in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql` (use `/db-migration` skill).

## 4. Key Monorepo Invariants
- **API Key Auth**: `X-API-Key` required on all non-health routes.
- **Whitelist Enforcement**: Exact matching, no fuzzy matching. Normalized digits only checked for `@s.whatsapp.net`.
- **Model Fallback Chain**: DeepSeek (primary) falling back to Gemini (`FallbackModel`). Streaming uses `run_stream_events`.
- **Tool Error Hygiene**: Agent tools return generic English strings on error; never leak raw `str(e)`.
- **Rule File Limit**: All `AGENTS.md` files must stay `<= 24,000` bytes. Check via `pnpm check:rules`.
