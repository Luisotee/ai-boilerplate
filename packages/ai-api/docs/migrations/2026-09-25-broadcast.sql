-- Broadcasts: per-chat opt-out flag and last-used chat client on `users`.
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
-- Safe to re-run (both statements are IF NOT EXISTS).
--
-- Apply it BEFORE deploying the new API image: the `User` model now selects
-- both columns, so every user lookup fails until they exist.
--
-- `broadcast_opt_out` defaults to false: every existing chat is opted IN, and
-- users opt out with `/broadcast off` or by asking the bot.
-- `last_client_id` stays NULL on existing rows until the user next writes; a
-- NULL row is treated as Telegram for a `tg:` JID and as Baileys otherwise.

BEGIN;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS broadcast_opt_out BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS last_client_id VARCHAR(16);

COMMIT;
