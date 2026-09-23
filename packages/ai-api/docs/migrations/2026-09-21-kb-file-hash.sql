-- Knowledge-base duplicate detection: SHA-256 content hash on
-- `knowledge_base_documents`.
--
-- There is no Alembic in this project: `init_db()` runs
-- `Base.metadata.create_all()`, which creates missing *tables* but never adds
-- columns to an existing one. Fresh databases get the column from the model;
-- existing ones need this applied by hand (Docker Compose setup, default
-- credentials — substitute your POSTGRES_USER / POSTGRES_DB if you changed them):
--
--   docker exec -i aiagent-postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-09-21-kb-file-hash.sql
--
-- Safe to re-run (both statements are IF NOT EXISTS).
--
-- Apply it BEFORE deploying the new API image: the model now selects
-- `file_hash`, so every knowledge-base query fails until the column exists.
--
-- The index is named `ix_knowledge_base_documents_file_hash` because that is
-- exactly what SQLAlchemy's `Column(String(64), index=True)` emits via
-- `create_all()`, so an upgraded database matches a freshly created one.
--
-- Existing rows keep `file_hash = NULL` and are never treated as duplicates;
-- only uploads made after this migration are deduplicated.

BEGIN;

ALTER TABLE knowledge_base_documents
  ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_knowledge_base_documents_file_hash
  ON knowledge_base_documents (file_hash);

COMMIT;
