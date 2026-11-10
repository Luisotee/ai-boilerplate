-- Hybrid (keyword + vector) knowledge-base search.
--
-- Adds to knowledge_base_chunks:
--   content_tsv       generated tsvector over heading + content, through two
--                     text-search configs (below), plus its GIN index
--   embedding_format  NULL = embedding of the chunk text alone (older rows),
--                     1 = title + heading path + chunk (scripts/reembed_kb.py
--                     upgrades the NULL rows)
-- Names and definitions match what create_all() / init_db() emit on a fresh DB.
--
-- Two configs, both copies of the language's built-in config:
--   kb_stem  unchanged: full stemming (licitação and licitações -> 'licit').
--   kb_fold  words go through kb_stop (the language's stop words, still
--            accented), unaccent, then the stemmer, so a query typed without
--            accents ("licitacao") still matches. Stemming unaccented words is
--            weaker (Snowball needs the accents), which is why kb_stem exists.
-- content_tsv holds the lexemes of both. The language defaults to portuguese;
-- for another one, set it to the same value as KB_TEXT_SEARCH_LANGUAGE:
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     -v kb_text_search_language=english < this-file.sql
-- Existing kb_stem / kb_fold configs are left as they are.
--
-- Adding the generated column rewrites knowledge_base_chunks once (it fills
-- content_tsv for every existing chunk, so keyword search covers old documents
-- immediately). Safe to re-run. A content_tsv from an earlier draft of this
-- file (one `kb_search` config) is dropped and rebuilt.

\if :{?kb_text_search_language}
\else
\set kb_text_search_language portuguese
\endif

CREATE EXTENSION IF NOT EXISTS unaccent;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_attrdef ad
        JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
        WHERE ad.adrelid = 'knowledge_base_chunks'::regclass
          AND a.attname = 'content_tsv'
          AND pg_get_expr(ad.adbin, ad.adrelid) NOT LIKE '%kb_fold%'
    ) THEN
        ALTER TABLE knowledge_base_chunks DROP COLUMN content_tsv;  -- drops its index too
    END IF;
END $$;
DROP TEXT SEARCH CONFIGURATION IF EXISTS kb_search;

SELECT format('CREATE TEXT SEARCH CONFIGURATION kb_stem (COPY = %I)', :'kb_text_search_language')
WHERE NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'kb_stem')
\gexec

-- kb_stop: the stemmer's stop-word list, checked before unaccent folds the
-- accents away. Languages whose stemmer has no stop words (and simple) get none.
SELECT format(
    'CREATE TEXT SEARCH DICTIONARY kb_stop (TEMPLATE = simple, STOPWORDS = %I, ACCEPT = false)',
    substring(dictinitoption FROM 'stopwords = ''([a-z_]+)''')
)
FROM pg_ts_dict
WHERE dictname = :'kb_text_search_language' || '_stem'
  AND dictinitoption LIKE '%stopwords%'
  AND NOT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictname = 'kb_stop')
  AND NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'kb_fold')
\gexec

SELECT format('CREATE TEXT SEARCH CONFIGURATION kb_fold (COPY = %I)', :'kb_text_search_language')
WHERE NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'kb_fold')
\gexec

-- Map words through [kb_stop,] unaccent, stemmer only when kb_fold has no
-- unaccent yet (i.e. it was just created above).
SELECT format(
    'ALTER TEXT SEARCH CONFIGURATION kb_fold ALTER MAPPING FOR hword, hword_part, word WITH %s',
    concat_ws(
        ', ',
        (SELECT 'kb_stop' FROM pg_ts_dict WHERE dictname = 'kb_stop'),
        'unaccent',
        CASE WHEN :'kb_text_search_language' = 'simple' THEN 'simple'
             ELSE quote_ident(:'kb_text_search_language' || '_stem') END
    )
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_ts_config_map m
    JOIN pg_ts_config c ON c.oid = m.mapcfg
    JOIN pg_ts_dict d ON d.oid = m.mapdict
    WHERE c.cfgname = 'kb_fold' AND d.dictname = 'unaccent'
)
\gexec

ALTER TABLE knowledge_base_chunks
    ADD COLUMN IF NOT EXISTS embedding_format SMALLINT;

ALTER TABLE knowledge_base_chunks
    ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector('kb_stem'::regconfig, coalesce(heading, '') || ' ' || content)
        || to_tsvector('kb_fold'::regconfig, coalesce(heading, '') || ' ' || content)
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_kb_chunks_content_tsv
    ON knowledge_base_chunks USING gin (content_tsv);
