# Message flow

The step-by-step path summarised in the Message Flow section of the root `CLAUDE.md` / `AGENTS.md`.

How a WhatsApp message traverses the system:

1. Baileys WebSocket → `whatsapp.ts` `messages.upsert` event
2. `normalizeMessageContent()` unwraps viewOnce/ephemeral message wrappers
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts` (transcribe first), image → `handlers/image.ts`, document → `handlers/document.ts`
4. All handlers funnel into `handleTextMessage()` with optional base64 image/document
5. `api-client.ts` sends POST `/chat/enqueue` to AI API → returns `job_id`
6. `routes/chat.py` intercepts slash commands (`/settings`, `/tts`, `/stt`, `/clean`, `/memories`, `/broadcast`, `/help`) before queuing; they never reach the AI agent
7. Non-command messages: saved to PostgreSQL, enqueued to Redis Stream (`stream:user:{user_id}`)
8. `streams/processor.py`: fetches conversation history → runs Pydantic AI agent with tools → streams response chunks to Redis
9. `api-client.ts` polls GET `/chat/job/{id}` (500ms interval, max 120s) until complete
10. WhatsApp client sends text reply; optionally generates TTS audio if user preference enabled

## Deleted and edited messages

Every saved user message keeps the platform's own id in `conversation_messages.whatsapp_message_id` (all three clients send it as `whatsapp_message_id`). When a sender deletes a message for everyone or edits it, the client calls `POST /chat/revoke` or `POST /chat/edit` (`routes/message_events.py`, logic in `services/message_events.py`), which deletes the row or rewrites its content and re-embeds it, so the old text leaves history, `get_chat_history`, `search_conversation_history` and `get_group_context`.

- **Baileys**: `messages.update` (`handlers/message-events.ts`). Revoke and text edits; caption edits are skipped.
- **Telegram**: `edited_message:text` in `updates.ts`. The Bot API delivers no deletions.
- **Cloud API**: neither; Meta's webhook has no inbound edit or revoke.
- **Who may change a row**: in a private chat, the contact. In a group, the row's own sender; a revoke of someone else's message also needs `is_group_admin: true` (fails closed). `/chat/edit` checks the whitelist; `/chat/revoke` doesn't, because it only removes rows saved while the chat was in scope.
- Rows saved before the column existed have no id, so their revoke or edit is a no-op. Migration: `packages/ai-api/docs/migrations/2026-11-09-message-id.sql`.

## Database tables

11 tables: models in `database.py` (users, conversation_messages, conversation_preferences, core_memories, bot_prompt, runtime_settings, broadcasts, broadcast_recipients, bug_reports) and `kb_models.py` (knowledge_base_documents, knowledge_base_chunks).

## Job pipeline history

Redis Streams (`streams/`) is the only job pipeline. The dead arq worker (`queue/worker.py`, `scripts/run_worker.py`) and the `arq` dependency were removed; `queue/` now holds only the Redis plumbing that outlived it: `queue/connection.py` (plain `redis.asyncio`), job metadata and response-chunk storage. `get_arq_redis` / `close_arq_redis` / `create_arq_pool` and the `ARQ_KEEP_RESULT` env var (still the job-metadata TTL) keep their legacy names on purpose, because renaming them would break callers, test patches and existing overrides.
