-- `/memories undo`: `core_memories.previous_content` is the backup it restores,
-- and `content_trusted` records whether the current document was written by a
-- trusted source (private chat, confirmed group admin, /memories clear or undo),
-- so a non-admin group save only ever backs up a trusted state.
--
-- There is no Alembic in this project: `create_all()` never adds columns to an
-- existing table. Apply by hand (substitute your POSTGRES_USER / POSTGRES_DB if
-- you changed them):
--
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-11-09-core-memory-previous.sql
--
-- Safe to re-run (IF NOT EXISTS). Apply it BEFORE deploying the new API image:
-- the model now selects both columns, so every core-memory read fails
-- until the column exists.

ALTER TABLE core_memories ADD COLUMN IF NOT EXISTS previous_content TEXT;
ALTER TABLE core_memories ADD COLUMN IF NOT EXISTS content_trusted BOOLEAN NOT NULL DEFAULT true;
