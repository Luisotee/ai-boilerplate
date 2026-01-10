"""
Knowledge base database models.

Defines SQLAlchemy models for storing PDF documents and their chunked content
with vector embeddings for semantic search.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .database import Base


class KnowledgeBaseDocument(Base):
    """
    Represents an uploaded PDF document in the knowledge base.

    Tracks document metadata, processing status, and provides relationship
    to document chunks.
    """

    __tablename__ = "knowledge_base_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    filename = Column(String, nullable=False)  # Stored filename (UUID.pdf)
    original_filename = Column(String, nullable=False)  # User's original filename
    file_size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String, default="application/pdf")
    upload_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_date = Column(DateTime, nullable=True)
    status = Column(
        String, nullable=False, default="pending"
    )  # 'pending', 'processing', 'completed', 'failed'
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
    token_count = Column(Integer, nullable=True)  # Approximate token count
    chunk_metadata = Column(JSON, nullable=True)  # Chunk-level metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationship
    document = relationship("KnowledgeBaseDocument", back_populates="chunks")

    # Indexes
    __table_args__ = (
        Index("idx_kb_chunks_document", "document_id"),
        Index("idx_kb_chunks_page", "page_number"),
        # IVFFlat index for vector similarity search - created manually after table creation
        # CREATE INDEX idx_kb_chunks_embedding ON knowledge_base_chunks USING ivfflat (embedding vector_cosine_ops);
    )

    def __repr__(self):
        return f"<KnowledgeBaseChunk(id={self.id}, document_id={self.document_id}, chunk_index={self.chunk_index})>"
