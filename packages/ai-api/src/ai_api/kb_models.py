"""
Knowledge base database models.

Defines SQLAlchemy models for storing PDF documents and their chunked content
with vector embeddings for semantic search.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import deferred, relationship
from sqlalchemy.sql import func

from .database import Base

# See KnowledgeBaseChunk.embedding_format.
KB_EMBEDDING_FORMAT = 1


class KnowledgeBaseDocument(Base):
    """
    Represents an uploaded PDF document in the knowledge base.

    Tracks document metadata, processing status, and provides relationship
    to document chunks.

    Status values:
    - 'queued': Upload received, waiting on the PDF stream (also while a retry is
      scheduled). Rows created before the PDF stream existed may say 'pending'
    - 'pending': Legacy name for 'queued'
    - 'processing': Currently being parsed and chunked
    - 'completed': Successfully processed, all chunks generated
    - 'partial': Partially processed, some chunks created before failure
    - 'failed': Processing failed, no chunks created
    """

    __tablename__ = "knowledge_base_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    filename = Column(String, nullable=False)  # Stored filename (UUID.pdf)
    original_filename = Column(String, nullable=False)  # User's original filename
    file_size_bytes = Column(Integer, nullable=False)
    # SHA-256 hex digest of the uploaded bytes, for duplicate detection. NULL on
    # rows created before the column existed (docs/migrations/*-kb-file-hash.sql).
    file_hash = Column(String(64), nullable=True, index=True)
    mime_type = Column(String, default="application/pdf")
    upload_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_date = Column(DateTime, nullable=True)
    status = Column(
        String, nullable=False, default="queued"
    )  # 'queued', 'processing', 'completed', 'partial', 'failed' (legacy: 'pending')
    error_message = Column(Text, nullable=True)
    doc_metadata = Column(JSON, nullable=True)  # Document-level metadata (author, title, etc.)
    chunk_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Conversation-scoped document fields
    whatsapp_jid = Column(String, nullable=True)  # Conversation scope (null = global)
    expires_at = Column(DateTime, nullable=True)  # TTL expiration for conversation docs
    is_conversation_scoped = Column(Boolean, default=False, nullable=False)
    whatsapp_message_id = Column(String, nullable=True)  # For sending reactions

    # Relationship
    chunks = relationship(
        "KnowledgeBaseChunk", back_populates="document", cascade="all, delete-orphan"
    )

    # Indexes
    __table_args__ = (
        Index("idx_kb_docs_status", "status"),
        Index("idx_kb_docs_upload_date", "upload_date"),
        Index("idx_kb_docs_filename", "filename"),
        Index("idx_kb_docs_whatsapp_jid", "whatsapp_jid"),
        Index("idx_kb_docs_expires_at", "expires_at"),
        Index("idx_kb_docs_conversation_scoped", "is_conversation_scoped"),
    )

    def __repr__(self):
        return f"<KnowledgeBaseDocument(id={self.id}, filename='{self.original_filename}', status='{self.status}')>"


class KnowledgeBaseChunk(Base):
    """
    Represents a semantically chunked piece of a PDF document.

    Stores chunk content, embeddings for vector search, and metadata about
    the chunk's position within the document.
    """

    __tablename__ = "knowledge_base_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_base_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)  # Order within document
    content = Column(Text, nullable=False)
    content_type = Column(String, default="text")  # 'text', 'table', 'list', 'code'
    page_number = Column(Integer, nullable=True)  # Source page in PDF
    heading = Column(String, nullable=True)  # Section heading if available
    embedding = Column(Vector(3072), nullable=True)  # Google gemini-embedding-001 (3072 dimensions)
    embedding_generated_at = Column(DateTime, nullable=True)
    # What the embedding was computed from. NULL: the chunk text alone (rows from
    # before hybrid search). 1 (KB_EMBEDDING_FORMAT): document title (Gemini's
    # `title`) + heading path + chunk text. scripts/reembed_kb.py upgrades NULL rows.
    embedding_format = Column(SmallInteger, nullable=True)
    # Keyword-search vector, computed by Postgres from heading + content through
    # the `kb_search` text-search config (unaccent + the configured language's
    # stemmer; created by init_db / the migration before this table). Deferred so
    # ordinary chunk loads never select it.
    content_tsv = deferred(
        Column(
            TSVECTOR,
            Computed(
                "to_tsvector('kb_search'::regconfig, coalesce(heading, '') || ' ' || content)",
                persisted=True,
            ),
        )
    )
    token_count = Column(Integer, nullable=True)  # Approximate token count
    chunk_metadata = Column(JSON, nullable=True)  # Chunk-level metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship
    document = relationship("KnowledgeBaseDocument", back_populates="chunks")

    # Indexes
    __table_args__ = (
        Index("idx_kb_chunks_document", "document_id"),
        Index("idx_kb_chunks_page", "page_number"),
        # HNSW index for vector search. pgvector can't index vector(3072) (its
        # limit is 2,000 dims), so the index is on the halfvec cast (limit 4,000)
        # and queries must order by the same expression (rag/vector.py).
        # Existing DBs: docs/migrations/2026-11-09-vector-hnsw-indexes.sql.
        Index(
            "idx_kb_chunks_embedding_hnsw",
            func.cast(embedding, HALFVEC(3072)).label("embedding"),
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        # GIN index for the keyword half of the hybrid search.
        # Existing DBs: docs/migrations/2026-11-09-kb-hybrid-search.sql.
        Index("idx_kb_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    def __repr__(self):
        return f"<KnowledgeBaseChunk(id={self.id}, document_id={self.document_id}, chunk_index={self.chunk_index})>"
