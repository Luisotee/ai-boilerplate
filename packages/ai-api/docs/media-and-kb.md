# Knowledge base, PDF parsing and speech-to-text

Detail behind the database and media rules in the root `CLAUDE.md` / `AGENTS.md`. The PDF queue itself (concurrency, retry, dead letter, crash recovery) is in `packages/ai-api/src/ai_api/streams/CLAUDE.md`.

## Knowledge-base dedup

`knowledge_base_documents.file_hash` is the SHA-256 hex of the uploaded bytes (indexed, computed while streaming the upload to disk). `POST /knowledge-base/upload` answers **409** when identical content is already in the KB; the batch route rejects that file (and a repeat of an earlier file in the same batch) while accepting the rest. Only global documents with a status other than `failed` count, so conversation-scoped chat PDFs never block an upload and a failed document can be re-uploaded. Rows from before the migration have `file_hash = NULL` and never match.

Both upload routes are `@limiter.exempt` for bulk loads via `./upload-kb.sh <dir>`, which posts a folder of PDFs to the batch route with `AI_API_KEY` from the env or `.env`. They stay behind `X-API-Key`.

## Vector indexes

Both embedding columns (`knowledge_base_chunks.embedding`, `conversation_messages.embedding`) are `vector(3072)`. pgvector's IVFFlat and HNSW indexes stop at 2,000 dimensions for `vector`, so each column has an HNSW expression index on `embedding::halfvec(3072)` (`idx_kb_chunks_embedding_hnsw`, `idx_conversation_messages_embedding_hnsw`, `halfvec_cosine_ops`). No re-embedding was needed: half precision changes the ranking negligibly. HNSW, not IVFFlat, because `create_all()` builds the index on an empty table and IVFFlat computes its lists from the rows present at build time.

- Queries use the index only when they order by the same expression, so the SQL in `rag/` builds its distance with `rag/vector.halfvec_distance()`. Ordering by a derived `similarity` column scans every row.
- `prepare_hnsw_search()` runs right before each search, in the same transaction: `hnsw.ef_search = 100` and, on pgvector >= 0.8, `hnsw.iterative_scan = relaxed_order`, so filters (user, status, scope) don't leave a search short of its LIMIT. Both are `SET LOCAL`.
- Conversation search filters by `user_id`: the planner uses the HNSW index only for a chat holding a large share of the table and the `user_id` btree plus an exact sort otherwise, which is what we want.
- Existing deployments apply `docs/migrations/2026-11-09-vector-hnsw-indexes.sql` (needs pgvector >= 0.7 for `halfvec`; the file says how to upgrade). Check with `\di *hnsw*` and `EXPLAIN` the query: the plan should show `Index Scan using idx_..._hnsw`.

## PDF parsing

LlamaParse (cloud, primary) via the `llama-cloud` SDK, with an **optional** Docling fallback behind the `[docling]` extra. `PDF_PARSER` is `auto` | `llamaparse` | `docling`. In `auto` mode LlamaParse runs when `LLAMA_CLOUD_API_KEY` is set and falls back to Docling on any parser error *if* the extra is installed.

## Speech-to-text

Groq Whisper (cloud, primary) plus an **optional** self-hosted Whisper server (speaches by default; any OpenAI-compatible endpoint works). `STT_PROVIDER` is `auto` | `groq` | `whisper`. In `auto` mode: with `GROQ_API_KEY` set, Groq runs and falls back to self-hosted on recoverable errors when `WHISPER_BASE_URL` is also set; with only `WHISPER_BASE_URL`, self-hosted runs alone; with neither, `/transcribe` returns 503. Start the self-hosted container with `docker compose --profile whisper up -d`.
