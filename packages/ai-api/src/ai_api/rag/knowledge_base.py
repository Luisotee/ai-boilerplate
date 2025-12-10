"""
Knowledge base RAG implementation for PDF documents.

Provides semantic search over globally accessible PDF documents
with source attribution and citation.
"""

import os
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

from .base import BaseRAG
from ..logger import logger


class KnowledgeBaseRAG(BaseRAG):
    """
    RAG implementation for searching PDF knowledge base.

    Uses vector similarity search with pgvector to find relevant
    document chunks based on semantic meaning.
    """

    def __init__(self):
        """Initialize KnowledgeBaseRAG with configuration from environment."""
        self.similarity_threshold = float(os.getenv('KB_SIMILARITY_THRESHOLD', '0.7'))
        self.default_limit = int(os.getenv('KB_SEARCH_LIMIT', '5'))
        logger.info(f"KnowledgeBaseRAG initialized "
                    f"(threshold: {self.similarity_threshold}, limit: {self.default_limit})")

    async def search(
        self,
        db: Session,
        query_embedding: List[float],
        query_text: str = None,
        limit: int = None,
        **kwargs
    ) -> List[dict]:
        """
        Search for semantically similar document chunks.

        Args:
            db: Database session
            query_embedding: Pre-generated embedding vector for the query
            query_text: Optional query text (for logging only)
            limit: Maximum results to return (default from env)
            **kwargs: Additional parameters

        Returns:
            List of dicts, each containing:
            - 'chunk': Dict with chunk data (id, content, page_number, etc.)
            - 'document': Dict with document metadata (filename, upload_date, etc.)
            - 'similarity_score': Cosine similarity score
        """
        if not query_embedding:
            logger.error("query_embedding is required for knowledge base search")
            return []

        if limit is None:
            limit = self.default_limit

        query_preview = query_text[:50] if query_text else "embedding"
        logger.info(f"Knowledge base search: '{query_preview}...' (limit: {limit})")

        # Vector similarity query using cosine distance
        # JOIN with documents to get metadata and filter by status
        # pgvector uses <=> for cosine distance (lower = more similar)
        # We convert to similarity score: 1 - distance
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
                (1 - (c.embedding <=> CAST(:embedding AS vector))) AS similarity
            FROM knowledge_base_chunks c
            JOIN knowledge_base_documents d ON c.document_id = d.id
            WHERE d.status = 'completed'
              AND c.embedding IS NOT NULL
              AND (1 - (c.embedding <=> CAST(:embedding AS vector))) >= :threshold
            ORDER BY similarity DESC
            LIMIT :limit
        """)

        result = db.execute(query_sql, {
            'embedding': query_embedding,
            'threshold': self.similarity_threshold,
            'limit': limit
        })
        rows = result.fetchall()

        logger.info(f"Knowledge base search found {len(rows)} results "
                    f"(threshold: {self.similarity_threshold})")

        # Convert to structured results
        results = []
        for row in rows:
            results.append({
                'chunk': {
                    'id': str(row.id),
                    'content': row.content,
                    'content_type': row.content_type,
                    'page_number': row.page_number,
                    'heading': row.heading,
                    'chunk_index': row.chunk_index,
                    'token_count': row.token_count,
                    'metadata': row.chunk_metadata
                },
                'document': {
                    'document_id': str(row.document_id),
                    'filename': row.filename,
                    'original_filename': row.original_filename,
                    'upload_date': row.upload_date,
                    'metadata': row.document_metadata
                },
                'similarity_score': float(row.similarity)
            })

            logger.debug(f"  - [{row.original_filename}] "
                        f"(similarity: {row.similarity:.3f})\n{row.content}")

        return results

    def format_results(self, results: List[dict]) -> str:
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
            chunk = result['chunk']
            doc = result['document']
            similarity = result['similarity_score']

            # Format source citation
            source_parts = [doc['original_filename']]

            if chunk['page_number']:
                source_parts.append(f"page {chunk['page_number']}")

            if chunk['heading']:
                source_parts.append(f"section '{chunk['heading']}'")

            source = ", ".join(source_parts)

            # Clean the content: remove HTML comments and excessive whitespace
            import re
            content = chunk['content']

            # Remove HTML comments
            content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

            # Remove excessive blank lines (keep max 1 blank line)
            content = re.sub(r'\n\s*\n\s*\n+', '\n\n', content)

            # Remove leading/trailing whitespace
            content = content.strip()

            # Log cleaning results
            original_len = len(chunk['content'])
            cleaned_len = len(content)
            if cleaned_len < original_len:
                logger.debug(f"Cleaned chunk {i}: {original_len} -> {cleaned_len} chars "
                           f"({original_len - cleaned_len} chars removed)")

            # Format chunk content
            snippet_lines = [
                f"=== Source {i} (relevance: {similarity:.2f}) ===",
                f"📄 Document: {source}",
                ""
            ]

            # Add section heading if available
            if chunk['heading']:
                snippet_lines.append(f"## {chunk['heading']}")
                snippet_lines.append("")

            # Add cleaned content
            snippet_lines.append(content)
            snippet_lines.append("")

            formatted_snippets.append("\n".join(snippet_lines))

        result_text = "\n".join(formatted_snippets)
        return f"Found {len(results)} relevant passages in knowledge base:\n\n{result_text}"
