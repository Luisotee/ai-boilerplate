-- One level of `/memories undo`: `core_memories.previous_content` keeps the
-- document as it was before the last change (agent rewrite or /memories clear).
--
-- There is no Alembic in this project: `create_all()` never adds columns to an
-- existing table. Apply by hand (substitute your POSTGRES_USER / POSTGRES_DB if
-- you changed them):
--
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-11-09-core-memory-previous.sql
--
-- Safe to re-run (IF NOT EXISTS). Apply it BEFORE deploying the new API image:
-- the model now selects `previous_content`, so every core-memory read fails
-- until the column exists.

ALTER TABLE core_memories ADD COLUMN IF NOT EXISTS previous_content TEXT;
