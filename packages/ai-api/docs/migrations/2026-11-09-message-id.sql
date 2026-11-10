-- Deleted and edited messages: `conversation_messages.whatsapp_message_id` keeps
-- the platform's message id (WhatsApp key.id / wamid, Telegram message_id) so
-- /chat/revoke and /chat/edit can find the saved row.
--
-- There is no Alembic in this project: `create_all()` never adds columns to an
-- existing table. Apply by hand (substitute your POSTGRES_USER / POSTGRES_DB if
-- you changed them):
--
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-11-09-message-id.sql
--
-- Safe to re-run (IF NOT EXISTS). Apply it BEFORE deploying the new API image:
-- the model now selects `whatsapp_message_id`, so every history query fails
-- until the column exists. Rows saved before it stay NULL, so a revoke or edit
-- of an older message is a no-op.
--
-- The index is named `ix_conversation_messages_whatsapp_message_id` because that
-- is what SQLAlchemy's `Column(String, index=True)` emits via `create_all()`.

ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS whatsapp_message_id VARCHAR;

CREATE INDEX IF NOT EXISTS ix_conversation_messages_whatsapp_message_id
    ON conversation_messages (whatsapp_message_id);
