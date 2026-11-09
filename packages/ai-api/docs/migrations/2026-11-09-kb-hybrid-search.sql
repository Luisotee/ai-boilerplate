-- Hybrid (keyword + vector) knowledge-base search.
--
-- Adds to knowledge_base_chunks:
--   content_tsv       generated tsvector over heading + content, via the
--                     `kb_search` text-search config, plus its GIN index
--   embedding_format  NULL = embedding of the chunk text alone (older rows),
--                     1 = title + heading path + chunk (scripts/reembed_kb.py
--                     upgrades the NULL rows)
-- Names and definitions match what create_all() / init_db() emit on a fresh DB.
--
-- `kb_search` copies a built-in text-search config and puts `unaccent` in front
-- of its stemmer. The language defaults to portuguese; for another one, set it
-- to the same value as KB_TEXT_SEARCH_LANGUAGE:
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     -v kb_text_search_language=english < this-file.sql
-- An existing kb_search config is left as it is.
--
-- Adding the generated column rewrites knowledge_base_chunks once (it fills
-- content_tsv for every existing chunk, so keyword search covers old documents
-- immediately). Safe to re-run.

\if :{?kb_text_search_language}
\else
\set kb_text_search_language portuguese
\endif

CREATE EXTENSION IF NOT EXISTS unaccent;

SELECT format('CREATE TEXT SEARCH CONFIGURATION kb_search (COPY = %I)', :'kb_text_search_language')
WHERE NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'kb_search')
\gexec

-- Map words through unaccent first only when the config has no unaccent yet
-- (i.e. it was just created above).
SELECT format(
    'ALTER TEXT SEARCH CONFIGURATION kb_search ALTER MAPPING FOR hword, hword_part, word WITH unaccent, %I',
    CASE WHEN :'kb_text_search_language' = 'simple' THEN 'simple'
         ELSE :'kb_text_search_language' || '_stem' END
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_ts_config_map m
    JOIN pg_ts_config c ON c.oid = m.mapcfg
    JOIN pg_ts_dict d ON d.oid = m.mapdict
    WHERE c.cfgname = 'kb_search' AND d.dictname = 'unaccent'
)
\gexec

ALTER TABLE knowledge_base_chunks
    ADD COLUMN IF NOT EXISTS embedding_format SMALLINT;

ALTER TABLE knowledge_base_chunks
    ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR
    GENERATED ALWAYS AS (to_tsvector('kb_search'::regconfig, coalesce(heading, '') || ' ' || content)) STORED;

CREATE INDEX IF NOT EXISTS idx_kb_chunks_content_tsv
    ON knowledge_base_chunks USING gin (content_tsv);
