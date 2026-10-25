---
name: message-tracer
description: >-
  Traces messages through the full 10-step pipeline (clients -> handlers -> AI
  API -> Redis Streams -> Pydantic AI -> response) for debugging. Use when
  messages aren't being processed, responses are missing, the bot doesn't reply,
  or cross-service communication fails.
---

# Trace Message Flow Pipeline (`message-tracer`)

Read-only diagnostic runbook for tracing messages across the 10-step monorepo pipeline.

```
Step 1:  Inbound Socket/Webhook (whatsapp.ts / webhook.ts / updates.ts)
Step 2:  normalizeMessageContent() unwraps viewOnce/ephemeral wrappers
Step 3:  Type dispatch: text, audio (transcribe), image, document (PDF)
Step 4:  Funnel into handleTextMessage()
Step 5:  api-client.ts sends POST /chat/enqueue to AI API (receives job_id)
Step 6:  routes/chat.py intercepts slash commands before queuing
Step 7:  Non-command: saved to PostgreSQL, enqueued to Redis Stream (stream:user:{user_id})
Step 8:  streams/processor.py: fetches history -> runs Pydantic AI agent -> streams response
Step 9:  api-client.ts polls GET /chat/job/{id} (500ms interval, max 120s)
Step 10: WhatsApp/Telegram client delivers response text and optional TTS audio
```

## Diagnostic Checklist by Pipeline Stage

### 1. Inbound & Dispatch (Steps 1–3)
- **`fromMe` Filter**: Check if the client is ignoring its own echoed messages (`msg.key.fromMe`).
- **Normalization**: Verify `normalizeMessageContent()` was called on the raw message.
- **Group Mentions**:
  - In groups, messages without bot mentions are marked `saveOnly=true` (saved to history, never sent to LLM).
  - Both JID (`@s.whatsapp.net`) and LID (`@lid`) formats must be checked for bot mentions (`utils/message.ts`).
- **Group Gating (`GROUP_GATING=jid|membership`)**:
  - In `jid` mode, only whitelisted group JIDs are admitted.
  - In `membership` mode, groups with at least one whitelisted participant are admitted.

### 2. Handlers & API Communication (Steps 4–5)
- **Media Download**: If media extraction fails, check failure reaction (`sendFailureReaction` / emoji ❌).
- **Audio Transcription**: Verify Groq API key is valid when audio transcription fails.
- **API Key Authentication**: Verify `X-API-Key` matches `AI_API_KEY` in root `.env`.
- **Polling Timeout**: `api-client.ts` polls for 120s (`POLL_MAX_DURATION_MS`). Look for `RequestTimeoutError`.

### 3. Command Interception & Queue (Steps 6–7)
- **Slash Commands**: Handled synchronously in `routes/chat.py`. If a command was sent, verify it didn't queue as a normal chat message.
- **Group Admin Enforcement**: Group admin commands (`/clean`, `/tts`, `/stt`, `/settings`, `/memories`, `/broadcast`) fail closed if `is_group_admin` is not true.
- **Stream Worker Running**: Chat messages are consumed by `run_stream_worker.py`:
  ```bash
  docker compose ps worker
  docker compose logs worker --tail=50
  ```
- **Redis Stream Key**: User messages land on `stream:user:{user_id}`. Inspect stream in redis:
  ```bash
  docker compose exec redis redis-cli XLEN stream:user:<user_id>
  ```

### 4. AI Processing & Agent Execution (Step 8)
- **Model Fallback Chain**: Check whether primary model (DeepSeek) encountered an error and fell back to Gemini (`FallbackModel`).
- **Stream Events**: Streaming uses `run_stream_events` (never `agent.run_stream`).
- **Markdown Formatting**: Output passes through `formatting.markdown_to_whatsapp`.

### 5. Delivery (Step 10)
- **WhatsApp Client**: Check connection status (`/health` on port 3001) or QR code status (`/admin/whatsapp/qr`).
- **Telegram Client**: Check 4096-character splitting and HTML entity formatting (`waMarkupToHtml`).
- **TTS**: If user enabled TTS, failure in audio synthesis logs a warning but must not prevent text reply delivery.
