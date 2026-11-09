-- HNSW vector indexes for knowledge-base and conversation-history search.
--
-- pgvector indexes `vector` only up to 2,000 dimensions, so the IVFFlat index
-- the docs used to suggest for our vector(3072) columns could never be created
-- ("column cannot have more than 2000 dimensions for ivfflat index") and every
-- search was a full scan. These are expression indexes on the halfvec cast
-- (halfvec indexes up to 4,000 dims); the queries in rag/ order by the same
-- expression. Names and definitions match what create_all() emits on a fresh DB.
--
-- Needs pgvector >= 0.8: halfvec arrived in 0.7, but before 0.8 an HNSW scan
-- has no iterative scan, so a filtered search (user, scope, status) can come
-- back short of its LIMIT, worse than the full scan it replaces. On an older
-- version this file stops before creating anything. Check with:
--   SELECT extversion FROM pg_extension WHERE extname = 'vector';
-- If older: `docker compose pull postgres && docker compose up -d postgres`
-- (pgvector/pgvector:pg16 ships 0.8.x), then `ALTER EXTENSION vector UPDATE;`
-- and run this file again.
--
-- CONCURRENTLY keeps the bot saving messages while the index builds, which can
-- take minutes on a long history. It can't run inside a transaction, so apply
-- this file with plain `psql < file` (no -1 / --single-transaction, and no
-- migration runner that wraps files in a transaction). Safe to
-- re-run: an index left INVALID by an interrupted build is dropped and rebuilt.

SELECT coalesce(
    (SELECT string_to_array(split_part(extversion, '-', 1), '.')::int[] >= '{0,8}'
     FROM pg_extension WHERE extname = 'vector'),
    false
) AS pgvector_ok
\gset
\if :pgvector_ok
\else
\echo 'pgvector >= 0.8 is required; see the header of this file. Nothing was changed.'
\quit
\endif

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'idx_kb_chunks_embedding_hnsw' AND NOT i.indisvalid
    ) THEN
        DROP INDEX idx_kb_chunks_embedding_hnsw;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'idx_conversation_messages_embedding_hnsw' AND NOT i.indisvalid
    ) THEN
        DROP INDEX idx_conversation_messages_embedding_hnsw;
    END IF;
END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_chunks_embedding_hnsw
    ON knowledge_base_chunks
    USING hnsw (CAST(embedding AS HALFVEC(3072)) halfvec_cosine_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_messages_embedding_hnsw
    ON conversation_messages
    USING hnsw (CAST(embedding AS HALFVEC(3072)) halfvec_cosine_ops);
