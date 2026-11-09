"""
Knowledge base RAG implementation for PDF documents.

Hybrid search (vector + keyword, fused with RRF) over knowledge-base
documents, with source attribution and citation.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..logger import logger
from ..runtime_config import runtime_config
from .vector import halfvec_distance, prepare_hnsw_search

# Reciprocal rank fusion constant (the usual 60): a hit's score from one ranked
# list is 1 / (RRF_K + rank), so the top few ranks of either list dominate.
RRF_K = 60

# Candidates each arm contributes before fusion: max(4 x limit, this).
MIN_CANDIDATE_POOL = 20

# Rows in scope for any KB search: completed documents that are global or scoped
# to this conversation, and not expired.
_SCOPE_SQL = """
    d.status = 'completed'
    AND (d.whatsapp_jid IS NULL OR d.whatsapp_jid = :whatsapp_jid)
    AND (d.expires_at IS NULL OR d.expires_at > NOW())
"""


def rrf_fuse(
    vector_ids: list[str],
    keyword_ids: list[str],
    *,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """Fuse two ranked id lists (best first) with reciprocal rank fusion.

    Each list contributes ``1 / (k + rank)`` (rank starts at 1) to every id it
    holds; ids in both lists add up. Returns ``(id, score)`` best first. Ties
    are broken by vector rank, then keyword rank, then id, so the order is
    deterministic.
    """
    vec_rank = {cid: i for i, cid in enumerate(vector_ids, 1)}
    kw_rank = {cid: i for i, cid in enumerate(keyword_ids, 1)}
    missing = float("inf")
    scores: dict[str, float] = {}
    for ranks in (vec_rank, kw_rank):
        for cid, rank in ranks.items():
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    fused = sorted(
        scores.items(),
        key=lambda item: (
            -item[1],
            vec_rank.get(item[0], missing),
            kw_rank.get(item[0], missing),
            item[0],
        ),
    )
    return fused[:limit] if limit is not None else fused


def _hybrid_search_sql() -> str:
    """Both arms in one statement; returns each candidate once with its ranks.

    - ``vec``: the nearest ``:pool`` chunks by the halfvec distance the HNSW
      index is built on, then only those at or above ``:threshold``.
    - ``kw``: chunks whose ``content_tsv`` matches ANY lexeme of the query
      (``plainto_tsquery`` with ``&`` turned into ``|``, found through the GIN
      index), kept only when they hold at least half of the query's distinct
      lexemes, and ranked by how many they hold, then ``ts_rank_cd``. OR keeps
      a chunk that lacks one word of the model's natural-language query; the
      half rule keeps a word common to the whole KB from filling the results
      on its own. The threshold doesn't apply here, so an exact term (a law
      number, a place, a product) survives when no vector hit passes it.
    """
    distance = halfvec_distance("c.embedding")
    return f"""
        WITH vec AS (
            SELECT id, row_number() OVER (ORDER BY dist, id) AS vec_rank
            FROM (
                SELECT c.id, {distance} AS dist
                FROM knowledge_base_chunks c
                JOIN knowledge_base_documents d ON c.document_id = d.id
                WHERE {_SCOPE_SQL}
                  AND c.embedding IS NOT NULL
                ORDER BY {distance}
                LIMIT :pool
            ) nearest
            WHERE 1 - dist >= :threshold
        ),
        q AS (
            SELECT
                replace(plainto_tsquery('kb_search', :query_text)::text, ' & ', ' | ')::tsquery
                    AS query,
                tsvector_to_array(to_tsvector('kb_search', :query_text)) AS lexemes
        ),
        kw AS (
            SELECT id, row_number() OVER (ORDER BY matched DESC, rank DESC, id) AS kw_rank
            FROM (
                SELECT
                    c.id,
                    cardinality(ARRAY(
                        SELECT unnest(q.lexemes)
                        INTERSECT
                        SELECT unnest(tsvector_to_array(c.content_tsv))
                    )) AS matched,
                    cardinality(q.lexemes) AS total,
                    ts_rank_cd(c.content_tsv, q.query) AS rank
                FROM knowledge_base_chunks c
                JOIN knowledge_base_documents d ON c.document_id = d.id
                CROSS JOIN q
                WHERE {_SCOPE_SQL}
                  AND c.content_tsv @@ q.query
            ) hits
            WHERE matched * 2 >= total
            ORDER BY matched DESC, rank DESC, id
            LIMIT :pool
        )
        SELECT
            c.id,
            c.document_id,
            c.chunk_index,
            c.content,
            c.content_type,
            c.page_number,
            c.heading,
            c.token_count,
            c.chunk_metadata,
            d.filename,
            d.original_filename,
            d.upload_date,
            d.doc_metadata AS document_metadata,
            d.is_conversation_scoped,
            CASE WHEN c.embedding IS NULL THEN NULL ELSE 1 - {distance} END AS similarity,
            vec.vec_rank,
            kw.kw_rank
        FROM (SELECT id FROM vec UNION SELECT id FROM kw) ids
        JOIN knowledge_base_chunks c ON c.id = ids.id
        JOIN knowledge_base_documents d ON c.document_id = d.id
        LEFT JOIN vec ON vec.id = c.id
        LEFT JOIN kw ON kw.id = c.id
    """


async def search_knowledge_base(
    db: Session,
    query_embedding: list[float],
    query_text: str = None,
    limit: int = None,
    similarity_threshold: float = None,
    whatsapp_jid: str = None,
    **kwargs,
) -> list[dict]:
    """
    Hybrid search over document chunks: vector similarity plus keywords.

    One SQL statement runs both arms (see ``_hybrid_search_sql``); the two
    rankings are fused with reciprocal rank fusion (``rrf_fuse``). The
    similarity threshold filters the vector arm only, so keyword hits survive
    when no vector hit passes it.

    Args:
        db: Database session
        query_embedding: Pre-generated embedding vector for the query
        query_text: Query text for the keyword arm (without it, vector only)
        limit: Maximum results to return (default from KB_SEARCH_LIMIT env)
        similarity_threshold: Minimum similarity for the vector arm (default from KB_SIMILARITY_THRESHOLD env)
        whatsapp_jid: Optional WhatsApp JID to include conversation-scoped documents
        **kwargs: Additional parameters

    Returns:
        List of dicts, best first, each containing:
        - 'chunk': Dict with chunk data (id, content, page_number, etc.)
        - 'document': Dict with document metadata (filename, upload_date, etc.)
        - 'similarity_score': Cosine similarity to the query (also for keyword-only hits)
        - 'rrf_score': Fused score the results are ordered by
        - 'match': 'vector', 'keyword' or 'both'
    """
    # Apply defaults from runtime config (env default, overridable via /admin)
    if similarity_threshold is None:
        similarity_threshold = runtime_config.get("kb_similarity_threshold")
    if limit is None:
        limit = runtime_config.get("kb_search_limit")

    if not query_embedding:
        logger.error("query_embedding is required for knowledge base search")
        return []

    query_preview = query_text[:50] if query_text else "embedding"
    logger.info(f"Knowledge base search: '{query_preview}...' (limit: {limit})")

    pool = max(4 * limit, MIN_CANDIDATE_POOL)
    prepare_hnsw_search(db, ef_search=pool)
    rows = db.execute(
        text(_hybrid_search_sql()),
        {
            "embedding": query_embedding,
            "query_text": query_text or "",
            "threshold": similarity_threshold,
            "pool": pool,
            "whatsapp_jid": whatsapp_jid,
        },
    ).fetchall()

    by_id = {str(row.id): row for row in rows}
    vector_ids = [
        str(r.id) for r in sorted((r for r in rows if r.vec_rank), key=lambda r: r.vec_rank)
    ]
    keyword_ids = [
        str(r.id) for r in sorted((r for r in rows if r.kw_rank), key=lambda r: r.kw_rank)
    ]
    fused = rrf_fuse(vector_ids, keyword_ids, limit=limit)

    logger.info(
        f"Knowledge base search found {len(fused)} results "
        f"({len(vector_ids)} vector >= {similarity_threshold}, {len(keyword_ids)} keyword)"
    )

    results = []
    for chunk_id, rrf_score in fused:
        row = by_id[chunk_id]
        if row.vec_rank and row.kw_rank:
            match = "both"
        elif row.vec_rank:
            match = "vector"
        else:
            match = "keyword"
        similarity = float(row.similarity) if row.similarity is not None else 0.0
        results.append(
            {
                "chunk": {
                    "id": chunk_id,
                    "content": row.content,
                    "content_type": row.content_type,
                    "page_number": row.page_number,
                    "heading": row.heading,
                    "chunk_index": row.chunk_index,
                    "token_count": row.token_count,
                    "metadata": row.chunk_metadata,
                },
                "document": {
                    "document_id": str(row.document_id),
                    "filename": row.filename,
                    "original_filename": row.original_filename,
                    "upload_date": row.upload_date,
                    "metadata": row.document_metadata,
                },
                "similarity_score": similarity,
                "rrf_score": rrf_score,
                "match": match,
            }
        )

        logger.debug(
            f"  - [{row.original_filename}] ({match}, similarity: {similarity:.3f}, "
            f"rrf: {rrf_score:.4f})\n{row.content}"
        )

    return results


def format_knowledge_base_results(results: list[dict]) -> str:
    """
    Format knowledge base chunks with source citations for agent consumption.

    Args:
        results: List of dicts with 'chunk', 'document', 'similarity_score'

    Returns:
        Formatted string with document snippets and citations
    """
    if not results:
        return "No relevant information found in the knowledge base."

    formatted_snippets = []

    for i, result in enumerate(results, 1):
        chunk = result["chunk"]
        doc = result["document"]
        similarity = result["similarity_score"]

        # Format source citation
        source_parts = [doc["original_filename"]]

        if chunk["page_number"]:
            source_parts.append(f"page {chunk['page_number']}")

        if chunk["heading"]:
            source_parts.append(f"section '{chunk['heading']}'")

        source = ", ".join(source_parts)

        # Clean the content: remove HTML comments and excessive whitespace
        import re

        content = chunk["content"]

        # Remove HTML comments
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

        # Remove excessive blank lines (keep max 1 blank line)
        content = re.sub(r"\n\s*\n\s*\n+", "\n\n", content)

        # Remove leading/trailing whitespace
        content = content.strip()

        # Log cleaning results
        original_len = len(chunk["content"])
        cleaned_len = len(content)
        if cleaned_len < original_len:
            logger.debug(
                f"Cleaned chunk {i}: {original_len} -> {cleaned_len} chars "
                f"({original_len - cleaned_len} chars removed)"
            )

        # Format chunk content
        # A keyword-only hit has a low cosine score by construction; say why it
        # is here so the model doesn't read the number as weak evidence.
        label = f"relevance: {similarity:.2f}"
        if result.get("match") == "keyword":
            label += ", keyword match"
        snippet_lines = [
            f"=== Source {i} ({label}) ===",
            f"📄 Document: {source}",
            "",
        ]

        # Add section heading if available
        if chunk["heading"]:
            snippet_lines.append(f"## {chunk['heading']}")
            snippet_lines.append("")

        # Add cleaned content
        snippet_lines.append(content)
        snippet_lines.append("")

        formatted_snippets.append("\n".join(snippet_lines))

    result_text = "\n".join(formatted_snippets)
    return f"Found {len(results)} relevant passages in knowledge base:\n\n{result_text}"
