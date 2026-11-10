# Knowledge base, PDF parsing and speech-to-text

Detail behind the database and media rules in the root `CLAUDE.md` / `AGENTS.md`. The PDF queue itself (concurrency, retry, dead letter, crash recovery) is in `packages/ai-api/src/ai_api/streams/CLAUDE.md`.

## Knowledge-base dedup

`knowledge_base_documents.file_hash` is the SHA-256 hex of the uploaded bytes (indexed, computed while streaming the upload to disk). `POST /knowledge-base/upload` answers **409** when identical content is already in the KB; the batch route rejects that file (and a repeat of an earlier file in the same batch) while accepting the rest. Only global documents with a status other than `failed` count, so conversation-scoped chat PDFs never block an upload and a failed document can be re-uploaded. Rows from before the migration have `file_hash = NULL` and never match.

Both upload routes are `@limiter.exempt` for bulk loads via `./upload-kb.sh <dir>`, which posts a folder of PDFs to the batch route with `AI_API_KEY` from the env or `.env`. They stay behind `X-API-Key`.

## Vector indexes

Both embedding columns (`knowledge_base_chunks.embedding`, `conversation_messages.embedding`) are `vector(3072)`. pgvector's IVFFlat and HNSW indexes stop at 2,000 dimensions for `vector`, so each column has an HNSW expression index on `embedding::halfvec(3072)` (`idx_kb_chunks_embedding_hnsw`, `idx_conversation_messages_embedding_hnsw`, `halfvec_cosine_ops`). No re-embedding was needed: half precision changes the ranking negligibly. HNSW, not IVFFlat, because `create_all()` builds the index on an empty table and IVFFlat computes its lists from the rows present at build time.

- Queries use the index only when they order by the same expression, so the SQL in `rag/` builds its distance with `rag/vector.halfvec_distance()`. Ordering by a derived `similarity` column scans every row.
- `prepare_hnsw_search()` runs right before each search, in the same transaction: `hnsw.ef_search` = max(100, the search's LIMIT or candidate pool), capped at pgvector's 1000, and `hnsw.iterative_scan = relaxed_order`, so filters (user, status, scope) don't leave a search short of its LIMIT. Both are `SET LOCAL`.
- Conversation search filters by `user_id`: the planner uses the HNSW index only for a chat holding a large share of the table and the `user_id` btree plus an exact sort otherwise, which is what we want.
- **Needs pgvector >= 0.8** (the `pgvector/pgvector:pg16` image ships 0.8.x). `halfvec` came in 0.7, but without 0.8's iterative scan a filtered search can return fewer rows than its LIMIT; the API logs a warning at the first search on an older version.
- Existing deployments apply `docs/migrations/2026-11-09-vector-hnsw-indexes.sql`; on pgvector < 0.8 it stops without changing anything and its header says how to upgrade. Check with `\di *hnsw*` and `EXPLAIN` the query: the plan should show `Index Scan using idx_..._hnsw`.

## Hybrid search

`rag/knowledge_base.py` `search_knowledge_base` runs a vector arm and a keyword arm in one SQL statement and fuses them with reciprocal rank fusion (`rrf_fuse`, k = 60). The tool's signature didn't change.

- **Vector arm**: the nearest `max(4 x limit, 20)` chunks (at most 1000) by the halfvec distance (see Vector indexes), then only those with similarity >= `KB_SIMILARITY_THRESHOLD`.
- **Keyword arm**: `knowledge_base_chunks.content_tsv`, a stored generated `tsvector` over heading + content with a GIN index (`idx_kb_chunks_content_tsv`). The query's words are OR-ed, so the model's query needn't appear whole, and a chunk must hold at least half of them, so a word common to the whole KB can't fill the results alone. Ranked by words matched, then `ts_rank_cd`; only the best `10 x pool` by `ts_rank_cd` are counted, which bounds the cost when a word is everywhere. **The threshold doesn't apply here**: an exact law number, place or product name survives when no vector hit passes it.
- **Fusion**: RRF lets a chunk found by both arms, even far down each list, outscore the top hit of one arm, so the top hit of each arm always keeps a slot in the results.
- **Results** keep every earlier key. `similarity_score` is the real cosine similarity, even for a keyword-only hit, and new keys are `rrf_score` and `match` (`vector`, `keyword`, `both`). A keyword-only hit is shown to the model as `relevance: 0.12, keyword match`.

**Text-search language (`KB_TEXT_SEARCH_LANGUAGE`, default `portuguese`)**. `content_tsv` holds the lexemes of two copies of that built-in config: `kb_stem`, unchanged, and `kb_fold`, whose words go through the language's stop words (`kb_stop`), then `unaccent`, then the stemmer. Both are needed: Snowball stems Portuguese by its accents (`licitação` and `licitações` both become `licit`, but unaccented they become `licitaca` and `licitaco`), while `kb_fold` lets a query typed without accents (`licitacao`) match. The query counts a word as matched when either config's lexeme is in the chunk. A generated column can't call `unaccent()` (not immutable), but `to_tsvector('kb_fold', ...)` can. `init_db()` creates the configs before `create_all()`; existing ones are never changed, and a different setting only logs a warning. To switch an existing DB, for example to English:

```sql
ALTER TABLE knowledge_base_chunks DROP COLUMN content_tsv;  -- and its index
DROP TEXT SEARCH CONFIGURATION kb_stem;
DROP TEXT SEARCH CONFIGURATION kb_fold;
DROP TEXT SEARCH DICTIONARY IF EXISTS kb_stop;
```

then apply `docs/migrations/2026-11-09-kb-hybrid-search.sql` with `-v kb_text_search_language=english` and set `KB_TEXT_SEARCH_LANGUAGE=english`. `simple` (no stemming, no stopwords) works for any language but matches far more loosely.

Manual check (the test suite has no Postgres; run on `pgvector/pgvector:pg16` when either config changes):

```sql
SELECT to_tsvector('kb_stem', 'licitação licitações não') || to_tsvector('kb_fold', 'licitação licitações não');
-- 'licit':1,2 'licitaca':3 'licitaco':4   (singular and plural share 'licit'; the stop word 'não' is gone)
```

**Embedding input**. New chunks are embedded from heading path + chunk text, with the document title (the filename without extension) as Gemini's `title` for `RETRIEVAL_DOCUMENT`, and stored with `embedding_format = 1`. The heading path comes from the chunker's section stack, carried across pages. Chunks are embedded 32 per call (`EmbeddingService.embed_documents`), each call under 4x `KB_EMBEDDING_TIMEOUT_SECONDS`. A timeout, 429 or 5xx fails the document so the PDF queue retries it later; any other batch error falls back to one call per chunk, so one bad chunk skips only itself.

**Moving a deployment over**

1. Apply `docs/migrations/2026-11-09-vector-hnsw-indexes.sql` and `docs/migrations/2026-11-09-kb-hybrid-search.sql` (the second rewrites `knowledge_base_chunks` once; pass `-v kb_text_search_language=<lang>` when not portuguese). Old code keeps working on the new schema.
2. Roll out the code. Keyword search covers every existing chunk at once, and the vector arm keeps using the old embeddings (same model and space).
3. Optionally upgrade the old embeddings (`embedding_format IS NULL`) to the new input format. The script commits per batch, so it can be stopped and re-run; it rate-limits itself and backs off on 429/5xx:

   ```bash
   docker compose exec api python -m ai_api.scripts.reembed_kb --dry-run   # count
   docker compose exec api python -m ai_api.scripts.reembed_kb             # --requests-per-minute 30 --batch-size 32 --limit N
   ```

   Old rows don't store the cross-page heading path, so they get their own heading; re-uploading a document gives the full path.

## PDF parsing

LlamaParse (cloud, primary) via the `llama-cloud` SDK, with an **optional** Docling fallback behind the `[docling]` extra. `PDF_PARSER` is `auto` | `llamaparse` | `docling`. In `auto` mode LlamaParse runs when `LLAMA_CLOUD_API_KEY` is set and falls back to Docling on any parser error *if* the extra is installed.

## Speech-to-text

Groq Whisper (cloud, primary) plus an **optional** self-hosted Whisper server (speaches by default; any OpenAI-compatible endpoint works). `STT_PROVIDER` is `auto` | `groq` | `whisper`. In `auto` mode: with `GROQ_API_KEY` set, Groq runs and falls back to self-hosted on recoverable errors when `WHISPER_BASE_URL` is also set; with only `WHISPER_BASE_URL`, self-hosted runs alone; with neither, `/transcribe` returns 503. Start the self-hosted container with `docker compose --profile whisper up -d`.
