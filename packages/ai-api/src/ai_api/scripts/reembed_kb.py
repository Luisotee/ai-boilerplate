"""
Re-embed knowledge-base chunks stored before hybrid search.

Chunks embedded from their text alone have ``embedding_format IS NULL``. This
script re-embeds them from document title (Gemini's ``title``) + heading path +
chunk text, the format new uploads use, and sets ``embedding_format`` to
``KB_EMBEDDING_FORMAT``. Search keeps working on the old embeddings meanwhile
(same model and vector space), so it can run whenever convenient.

- Idempotent and resumable: each batch is committed on its own, and only rows
  still at ``embedding_format IS NULL`` are picked, so a stopped run continues
  where it left off.
- Rate-limited: at most ``--requests-per-minute`` embedding calls, one per
  batch of up to ``--batch-size`` chunks of one document. HTTP 429/5xx and
  timeouts back off exponentially; after the retries run out it exits 1 (run
  it again later).
- A chunk that still can't be embedded on its own is left as it is and
  skipped for the rest of the run.

Usage (from the checkout):

    docker compose exec api python -m ai_api.scripts.reembed_kb --dry-run
    docker compose exec api python -m ai_api.scripts.reembed_kb
    # locally: cd packages/ai-api && uv run python -m ai_api.scripts.reembed_kb
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime

from sqlalchemy import text

from ..config import settings
from ..database import SessionLocal
from ..embeddings import EMBED_BATCH_SIZE, create_embedding_service
from ..kb_models import KB_EMBEDDING_FORMAT, KnowledgeBaseChunk
from ..logger import logger
from ..processing import (
    document_title,
    embedding_batch_timeout,
    embedding_text,
    is_retriable_error,
)

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 5

# Chunks of documents search can return (completed, not expired) that still
# carry a legacy embedding.
_PENDING_SQL = """
    FROM knowledge_base_chunks c
    JOIN knowledge_base_documents d ON c.document_id = d.id
    WHERE c.embedding_format IS NULL
      AND c.embedding IS NOT NULL
      AND d.status = 'completed'
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
      AND NOT (c.id::text = ANY(CAST(:skip AS text[])))
"""


def is_retriable(error: BaseException) -> bool:
    """Rate limits, server errors and timeouts are worth retrying (same rule as the PDF queue)."""
    return is_retriable_error(error)


def count_pending(db) -> tuple[int, int]:
    """(chunks, documents) still to re-embed."""
    row = db.execute(
        text(f"SELECT count(*), count(DISTINCT c.document_id) {_PENDING_SQL}"), {"skip": []}
    ).one()
    return int(row[0]), int(row[1])


def next_batch(db, batch_size: int, skip: list[str]) -> list:
    """Up to ``batch_size`` pending chunks, all of the first pending document."""
    return db.execute(
        text(f"""
            SELECT c.id, c.document_id, c.content, c.heading, c.chunk_metadata,
                   d.original_filename
            {_PENDING_SQL}
              AND c.document_id = (SELECT c.document_id {_PENDING_SQL} ORDER BY 1 LIMIT 1)
            ORDER BY c.chunk_index
            LIMIT :batch_size
        """),
        {"skip": skip, "batch_size": batch_size},
    ).fetchall()


def chunk_embedding_text(row) -> str:
    """Heading path (stored by newer uploads, else the chunk heading) + content."""
    metadata = row.chunk_metadata or {}
    path = metadata.get("heading_path") or ([row.heading] if row.heading else [])
    return embedding_text(row.content, path)


async def embed_batch(service, texts: list[str], title: str | None, pacer) -> list | None:
    """Embed one batch, backing off on retriable errors.

    Returns the vectors, or None when the batch call fails for a reason a retry
    won't fix (the caller then embeds chunk by chunk). Raises after
    MAX_RETRIES retriable failures.
    """
    for attempt in range(MAX_RETRIES + 1):
        await pacer()
        try:
            return await asyncio.wait_for(
                service.embed_documents(texts, title=title),
                timeout=embedding_batch_timeout(len(texts)),
            )
        except Exception as e:
            if not is_retriable(e):
                logger.warning(f"Batch embedding failed ({type(e).__name__}); trying one by one")
                return None
            if attempt == MAX_RETRIES:
                raise
            delay = BACKOFF_BASE_SECONDS * 2**attempt
            logger.warning(f"Embedding call failed ({type(e).__name__}); retrying in {delay}s")
            await asyncio.sleep(delay)
    return None  # pragma: no cover - the loop always returns or raises


def make_pacer(requests_per_minute: int):
    """An awaitable that spaces calls at least 60/rpm seconds apart."""
    interval = 60.0 / requests_per_minute
    last = [0.0]

    async def pace() -> None:
        wait = last[0] + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last[0] = time.monotonic()

    return pace


def store(db, chunk_id, vector) -> None:
    db.query(KnowledgeBaseChunk).filter(
        KnowledgeBaseChunk.id == chunk_id, KnowledgeBaseChunk.embedding_format.is_(None)
    ).update(
        {
            KnowledgeBaseChunk.embedding: vector,
            KnowledgeBaseChunk.embedding_generated_at: datetime.now(UTC),
            KnowledgeBaseChunk.embedding_format: KB_EMBEDDING_FORMAT,
        },
        synchronize_session=False,
    )


async def reembed(
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    requests_per_minute: int = 30,
    limit: int | None = None,
    dry_run: bool = False,
    db_factory=SessionLocal,
    service=None,
) -> dict:
    """Re-embed pending chunks. Returns counts: done, skipped, remaining."""
    db = db_factory()
    try:
        pending, documents = count_pending(db)
        logger.info(f"{pending} chunks in {documents} documents need re-embedding")
        if dry_run or pending == 0:
            return {"done": 0, "skipped": 0, "remaining": pending}

        service = service or create_embedding_service(settings.gemini_api_key)
        if service is None:
            raise RuntimeError("GEMINI_API_KEY is not set; cannot re-embed")

        pacer = make_pacer(requests_per_minute)
        skip: list[str] = []
        done = 0
        while limit is None or done + len(skip) < limit:
            size = batch_size if limit is None else min(batch_size, limit - done - len(skip))
            rows = next_batch(db, size, skip)
            if not rows:
                break
            title = document_title(rows[0].original_filename)
            texts = [chunk_embedding_text(r) for r in rows]
            vectors = await embed_batch(service, texts, title, pacer)
            if vectors is None:
                vectors = []
                for t in texts:
                    await pacer()
                    vectors.append(
                        await service.generate(t, task_type="RETRIEVAL_DOCUMENT", title=title)
                    )
            for row, vector in zip(rows, vectors, strict=True):
                if vector:
                    store(db, row.id, vector)
                    done += 1
                else:
                    logger.warning(f"Chunk {row.id} could not be re-embedded; skipping it")
                    skip.append(str(row.id))
            db.commit()
            logger.info(f"Re-embedded {done}/{pending} chunks")

        remaining, _ = count_pending(db)
        logger.info(f"Finished: {done} re-embedded, {len(skip)} skipped, {remaining} remaining")
        return {"done": done, "skipped": len(skip), "remaining": remaining}
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--batch-size", type=int, default=EMBED_BATCH_SIZE)
    parser.add_argument("--requests-per-minute", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None, help="stop after this many chunks")
    parser.add_argument("--dry-run", action="store_true", help="only count what is pending")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 100:
        parser.error("--batch-size must be between 1 and 100")
    if args.requests_per_minute < 1:
        parser.error("--requests-per-minute must be at least 1")

    try:
        asyncio.run(
            reembed(
                batch_size=args.batch_size,
                requests_per_minute=args.requests_per_minute,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    except Exception:
        logger.error("Re-embedding stopped; run the script again to resume", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
