---
name: rag-debugger
description: >-
  Diagnoses knowledge base pipeline issues (upload, PDF parsing, chunking,
  embedding, pgvector retrieval). Use when documents aren't being processed,
  search returns no results, embeddings fail, or KB status is stuck.
---

# Debug Knowledge Base & RAG Pipeline (`rag-debugger`)

Read-only runbook for investigating the 6-stage RAG / Knowledge Base subsystem.

```
Stage 1: Upload (routes/knowledge_base.py) → DB record 'queued' + Redis stream:pdf_processing
Stage 2: Parsing (streams/pdf_consumer.py, processing.py) → Docling conversion
Stage 3: Chunking (processing.py) → HybridChunker (512 tokens max)
Stage 4: Embedding (embeddings.py) → gemini-embedding-001 (3072 dims, RETRIEVAL_DOCUMENT)
Stage 5: Storage (kb_models.py) → knowledge_base_chunks + IVFFlat index
Stage 6: Retrieval (rag/knowledge_base.py, search.py) → RETRIEVAL_QUERY + cosine distance (<=>)
```

## Diagnostic Checklist by Stage

### Stage 1: Upload & Enqueue
- **File Types**: Only `.pdf` accepted.
- **Size Limits**: `KB_MAX_FILE_SIZE_MB` (50 MB), `KB_MAX_BATCH_SIZE_MB` (500 MB).
- **Dedup**: SHA-256 hash match against existing document returns **409 Conflict**.
- **Enqueue**: Job placed on `stream:pdf_processing`. Check queue length:
  ```bash
  docker compose exec redis redis-cli XLEN stream:pdf_processing
  ```

### Stage 2: PDF Parsing (Docling)
- Worker runs `streams/pdf_consumer.py` (not the API process). Check worker logs:
  ```bash
  docker compose logs worker --tail=100
  ```
- **Retries**: Transient failures park in `pdf_processing:retry` sorted set. Check pending retries:
  ```bash
  docker compose exec redis redis-cli ZRANGE pdf_processing:retry 0 -1 WITHSCORES
  ```
- **Dead Letter**: Permanent failures land on `stream:pdf_processing:dead`. Check reason:
  ```bash
  docker compose exec redis redis-cli XRANGE stream:pdf_processing:dead - +
  ```

### Stage 3 & 4: Chunking & Embeddings
- **Model**: `gemini-embedding-001`.
- **Dimensions**: Strictly `3072` (constant `EMBEDDING_DIMENSIONS`).
- **Task Type for Ingestion**: `RETRIEVAL_DOCUMENT` (CRITICAL: never `RETRIEVAL_QUERY`).
- Check document processing errors in database:
  ```sql
  SELECT id, filename, status, error_message, doc_metadata->'processing_errors'
  FROM knowledge_base_documents
  ORDER BY created_at DESC LIMIT 5;
  ```

### Stage 5: pgvector Storage
- Chunks stored in `knowledge_base_chunks` with nullable `Vector(3072)` embeddings.
- **IVFFlat Index**: Must be created manually once data is loaded (to avoid full table scan):
  ```sql
  CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding
  ON knowledge_base_chunks
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
  ```

### Stage 6: Retrieval
- **Task Type for Query**: `RETRIEVAL_QUERY` (CRITICAL: must match search query type).
- **Similarity Calculation**: Cosine distance `<=>`, converted to `1 - distance`.
- **Threshold**: Default `KB_SIMILARITY_THRESHOLD=0.7`. If search returns nothing, test lowering the threshold.
- Check expiration: For conversation-scoped documents, verify `expires_at > NOW()`.
