"""
PDF processing module with Docling integration and semantic chunking.

Handles background processing of uploaded PDFs: parsing, semantic chunking,
embedding generation, and storage in the database.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import tiktoken
from docling.chunking import HybridChunker
from docling.document_converter import DocumentConverter
from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
from docling_core.types.doc import DoclingDocument

from .config import settings
from .database import SessionLocal
from .embeddings import create_embedding_service
from .kb_models import KnowledgeBaseChunk, KnowledgeBaseDocument
from .logger import logger


async def process_pdf_document(
    document_id: str,
    file_path: str,
    whatsapp_jid: str | None = None,
):
    """
    Background task to process an uploaded PDF document with timeout constraints.

    Applies multi-level timeouts:
    - Overall processing timeout (300s default)
    - Docling parsing timeout (180s default)
    - Per-embedding timeout (10s default)
    - Batch embedding timeout (240s default)

    Args:
        document_id: UUID of the document record
        file_path: Absolute path to the PDF file on disk
        whatsapp_jid: Optional WhatsApp JID for conversation-scoped documents
    """
    try:
        # Wrap processing in overall timeout
        await asyncio.wait_for(
            _process_pdf_document_impl(document_id, file_path, whatsapp_jid),
            timeout=settings.kb_processing_timeout_seconds,
        )
    except TimeoutError:
        logger.error(
            f"❌ Document processing timeout after {settings.kb_processing_timeout_seconds}s: {document_id}"
        )
        # Update document status to failed with timeout error
        db = SessionLocal()
        try:
            document = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
            if document:
                document.status = "failed"
                document.error_message = (
                    f"Processing timeout after {settings.kb_processing_timeout_seconds} seconds"
                )
                document.processed_date = datetime.now(UTC)
                db.commit()
        except Exception as update_error:
            logger.error(f"Failed to update document status after timeout: {update_error}")
        finally:
            db.close()


async def _process_pdf_document_impl(
    document_id: str,
    file_path: str,
    whatsapp_jid: str | None = None,
):
    """
    Internal implementation of PDF processing with individual timeouts.

    This is separated from the public function to allow overall timeout wrapping.
    """
    db = SessionLocal()
    encoder = tiktoken.get_encoding("cl100k_base")

    try:
        logger.info(f"Starting processing for document {document_id}")

        # Update status to processing
        document = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
        if not document:
            logger.error(f"Document {document_id} not found in database")
            return

        document.status = "processing"
        db.commit()
        logger.info(f"Document status updated to 'processing': {document.original_filename}")

        # Verify file exists
        if not Path(file_path).exists():
            raise FileNotFoundError(f"PDF file not found: {file_path}")

        # Step 1: Parse PDF with Docling (with timeout)
        logger.info(f"Parsing PDF with Docling: {file_path}")
        try:
            # Run CPU-bound Docling in thread pool with timeout
            def _convert_pdf():
                converter = DocumentConverter()
                return converter.convert(file_path)

            result = await asyncio.wait_for(
                asyncio.to_thread(_convert_pdf), timeout=settings.kb_docling_timeout_seconds
            )
        except TimeoutError:
            raise ValueError(
                f"Docling parsing timeout after {settings.kb_docling_timeout_seconds} seconds. "
                f"PDF may be corrupt or too complex."
            )

        # Step 2: Keep DoclingDocument for metadata access
        doc: DoclingDocument = result.document
        logger.info("Docling parsed document successfully")

        # Step 3: Extract metadata
        metadata = {}
        if hasattr(result.input, "page_count"):
            metadata["page_count"] = result.input.page_count
        metadata["docling_version"] = "latest"
        metadata["processing_date"] = datetime.now(UTC).isoformat()

        document.doc_metadata = metadata
        db.commit()
        logger.info(f"Updated document metadata: {metadata}")

        # Step 4: Get embedding service
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if not embedding_service:
            raise ValueError("GEMINI_API_KEY not configured - cannot generate embeddings")

        # Step 5: Perform hybrid chunking
        logger.info(
            f"Chunking document with HybridChunker (max_tokens: {settings.kb_max_chunk_tokens})"
        )

        tokenizer_wrapper = OpenAITokenizer(
            tokenizer=encoder,
            max_tokens=settings.kb_max_chunk_tokens,
        )

        chunker = HybridChunker(
            tokenizer=tokenizer_wrapper,
            merge_peers=True,
        )

        doc_chunks = list(chunker.chunk(doc))
        logger.info(f"Generated {len(doc_chunks)} chunks from document")

        if not doc_chunks:
            raise ValueError("HybridChunker produced no valid chunks")

        # Step 6: Generate embeddings with timeout protection
        logger.info("Generating embeddings and storing chunks...")
        stored_count = 0

        # Wrap embedding generation in batch timeout
        try:
            stored_count = await asyncio.wait_for(
                _generate_and_store_embeddings(
                    doc_chunks=doc_chunks,
                    encoder=encoder,
                    embedding_service=embedding_service,
                    document_id=document_id,
                    db=db,
                ),
                timeout=settings.kb_embedding_batch_timeout_seconds,
            )
        except TimeoutError:
            raise ValueError(
                f"Embedding generation timeout after {settings.kb_embedding_batch_timeout_seconds} seconds. "
                f"Generated {stored_count}/{len(doc_chunks)} chunks before timeout."
            )

        logger.info(f"Stored {stored_count} chunks with embeddings")

        # Step 7: Update document status to completed
        document.status = "completed"
        document.processed_date = datetime.now(UTC)
        document.chunk_count = stored_count
        db.commit()

        logger.info(
            f"✅ Successfully processed document {document_id}: "
            f"{document.original_filename} ({stored_count} chunks)"
        )

    except Exception as e:
        logger.error(f"❌ Error processing document {document_id}: {str(e)}", exc_info=True)

        # Update document status to failed
        try:
            document = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
            if document:
                document.status = "failed"
                document.error_message = str(e)
                document.processed_date = datetime.now(UTC)
                db.commit()
                logger.info(f"Document {document_id} marked as failed")
        except Exception as update_error:
            logger.error(f"Failed to update document status: {str(update_error)}")

    finally:
        db.close()
        logger.info(f"Processing completed for document {document_id}")


async def _generate_and_store_embeddings(
    doc_chunks: list,
    encoder,
    embedding_service,
    document_id: str,
    db,
) -> int:
    """
    Generate embeddings for chunks with per-chunk timeout.

    Returns:
        Number of chunks successfully stored
    """
    stored_count = 0

    for i, chunk in enumerate(doc_chunks):
        # Extract text content
        chunk_text = chunk.text

        # Calculate token count
        token_count = len(encoder.encode(chunk_text))

        # Extract page numbers and headings from provenance metadata
        page_numbers = set()
        headings = []

        for doc_item in chunk.meta.doc_items:
            if hasattr(doc_item, "prov") and doc_item.prov:
                for prov in doc_item.prov:
                    if hasattr(prov, "page_no"):
                        page_numbers.add(prov.page_no)

            if hasattr(doc_item, "label") and "SECTION_HEADER" in str(doc_item.label):
                if hasattr(doc_item, "text"):
                    headings.append(doc_item.text)

        primary_page = min(page_numbers) if page_numbers else None
        primary_heading = headings[0] if headings else None

        chunk_metadata = {
            "all_page_numbers": sorted(list(page_numbers)),
            "all_headings": headings,
            "doc_item_count": len(chunk.meta.doc_items),
        }

        logger.debug(
            f"Chunk {i}: page={primary_page}, heading={primary_heading}, tokens={token_count}"
        )

        # Generate embedding with timeout
        try:
            chunk_embedding = await asyncio.wait_for(
                embedding_service.generate(chunk_text, task_type="RETRIEVAL_DOCUMENT"),
                timeout=settings.kb_embedding_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                f"Embedding timeout for chunk {i} after {settings.kb_embedding_timeout_seconds}s - skipping"
            )
            continue

        if not chunk_embedding:
            logger.warning(f"Failed to generate embedding for chunk {i} - skipping")
            continue

        # Create chunk record
        chunk_obj = KnowledgeBaseChunk(
            document_id=document_id,
            chunk_index=i,
            content=chunk_text,
            content_type="text",
            page_number=primary_page,
            heading=primary_heading,
            embedding=chunk_embedding,
            embedding_generated_at=datetime.now(UTC),
            token_count=token_count,
            chunk_metadata=chunk_metadata,
        )

        db.add(chunk_obj)
        stored_count += 1

        # Commit in batches for efficiency
        if (i + 1) % 10 == 0:
            db.commit()
            logger.debug(f"Committed batch of 10 chunks (up to {i + 1})")

    # Final commit
    db.commit()

    return stored_count
