-- Broadcasts: per-chat opt-out flag and chat-client routing columns on `users`.
--
-- There is no Alembic in this project: `init_db()` runs
-- `Base.metadata.create_all()`, which creates missing *tables* but never adds
-- columns to an existing one. The two new tables (`broadcasts`,
-- `broadcast_recipients`) are created by `create_all()` on the next start;
-- only the columns below need this applied by hand (Docker Compose setup,
-- default credentials — substitute your POSTGRES_USER / POSTGRES_DB if you
-- changed them):
--
--   docker exec -i aiagent-postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-09-25-broadcast.sql
--
-- Safe to re-run (every statement is IF NOT EXISTS).
--
-- Already applied an earlier version of this file? Apply it again: the last
-- statement (`broadcast_recipients.attempt_started_at`) was added afterwards,
-- and `create_all()` won't add it to a table that already exists. The
-- `BroadcastRecipient` model selects it, so the broadcast worker and
-- `/admin/broadcasts` fail until it exists.
--
-- Apply it BEFORE deploying the new API image: the `User` model now selects
-- these columns, so every user lookup fails until they exist.
--
-- `broadcast_opt_out` defaults to false: every existing chat is opted IN, and
-- users opt out with `/broadcast off` or by asking the bot.
-- The routing columns stay NULL on existing rows until the user next writes:
-- `last_client_id` (platform last used), `whatsapp_client_id` (NULL = Baileys)
-- and `cloud_last_inbound_at` (NULL = outside the Cloud 24h window, so Cloud
-- chats are skipped until they write again).

BEGIN;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS broadcast_opt_out BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS last_client_id VARCHAR(16);

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS whatsapp_client_id VARCHAR(16);

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS cloud_last_inbound_at TIMESTAMP WITHOUT TIME ZONE;

-- Delivery claim (see streams/broadcast_consumer.py `_claim`). A no-op on a
-- fresh database, where create_all() already made the column.
ALTER TABLE IF EXISTS broadcast_recipients
  ADD COLUMN IF NOT EXISTS attempt_started_at TIMESTAMP WITHOUT TIME ZONE;

COMMIT;
