"""
Knowledge base RAG implementation for PDF documents.

Provides semantic search over globally accessible PDF documents
with source attribution and citation.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..logger import logger


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
    Search for semantically similar document chunks.

    Args:
        db: Database session
        query_embedding: Pre-generated embedding vector for the query
        query_text: Optional query text (for logging only)
        limit: Maximum results to return (default from KB_SEARCH_LIMIT env)
        similarity_threshold: Minimum similarity score (default from KB_SIMILARITY_THRESHOLD env)
        whatsapp_jid: Optional WhatsApp JID to include conversation-scoped documents
        **kwargs: Additional parameters

    Returns:
        List of dicts, each containing:
        - 'chunk': Dict with chunk data (id, content, page_number, etc.)
        - 'document': Dict with document metadata (filename, upload_date, etc.)
        - 'similarity_score': Cosine similarity score
    """
    # Apply defaults from settings if not provided
    if similarity_threshold is None:
        similarity_threshold = settings.kb_similarity_threshold
    if limit is None:
        limit = settings.kb_search_limit

    if not query_embedding:
        logger.error("query_embedding is required for knowledge base search")
        return []

    query_preview = query_text[:50] if query_text else "embedding"
    logger.info(f"Knowledge base search: '{query_preview}...' (limit: {limit})")

    # Vector similarity query using cosine distance
    # JOIN with documents to get metadata and filter by status
    # pgvector uses <=> for cosine distance (lower = more similar)
    # We convert to similarity score: 1 - distance
    #
    # Conversation scope filtering:
    # - Global documents (whatsapp_jid IS NULL) are always included
    # - If whatsapp_jid is provided, also include documents scoped to that conversation
    # - Exclude expired documents (expires_at < NOW())
    query_sql = text("""
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
            d.doc_metadata as document_metadata,
            d.is_conversation_scoped,
            (1 - (c.embedding <=> CAST(:embedding AS vector))) AS similarity
        FROM knowledge_base_chunks c
        JOIN knowledge_base_documents d ON c.document_id = d.id
        WHERE d.status = 'completed'
          AND c.embedding IS NOT NULL
          AND (1 - (c.embedding <=> CAST(:embedding AS vector))) >= :threshold
          AND (d.whatsapp_jid IS NULL OR d.whatsapp_jid = :whatsapp_jid)
          AND (d.expires_at IS NULL OR d.expires_at > NOW())
        ORDER BY similarity DESC
        LIMIT :limit
    """)

    result = db.execute(
        query_sql,
        {
            "embedding": query_embedding,
            "threshold": similarity_threshold,
            "limit": limit,
            "whatsapp_jid": whatsapp_jid,
        },
    )
    rows = result.fetchall()

    logger.info(
        f"Knowledge base search found {len(rows)} results (threshold: {similarity_threshold})"
    )

    # Convert to structured results
    results = []
    for row in rows:
        results.append(
            {
                "chunk": {
                    "id": str(row.id),
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
                "similarity_score": float(row.similarity),
            }
        )

        logger.debug(
            f"  - [{row.original_filename}] (similarity: {row.similarity:.3f})\n{row.content}"
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
        snippet_lines = [
            f"=== Source {i} (relevance: {similarity:.2f}) ===",
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
