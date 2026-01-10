"""
PDF processing module with Docling integration and semantic chunking.

Handles background processing of uploaded PDFs: parsing, semantic chunking,
embedding generation, and storage in the database.
"""

from datetime import datetime
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
    Background task to process an uploaded PDF document.

    Steps:
    1. Update document status to 'processing'
    2. Parse PDF with Docling
    3. Extract Markdown content
    4. Perform semantic chunking
    5. Generate embeddings for each chunk
    6. Store chunks in database
    7. Update document status to 'completed' or 'failed'

    Args:
        document_id: UUID of the document record
        file_path: Absolute path to the PDF file on disk
        whatsapp_jid: Optional WhatsApp JID for conversation-scoped documents
    """
    db = SessionLocal()
    encoder = tiktoken.get_encoding("cl100k_base")  # Token counter

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

        # Step 1: Parse PDF with Docling
        logger.info(f"Parsing PDF with Docling: {file_path}")
        converter = DocumentConverter()
        result = converter.convert(file_path)

        # Step 2: Keep DoclingDocument for metadata access (don't export to Markdown!)
        doc: DoclingDocument = result.document
        logger.info("Docling parsed document successfully")

        # Step 3: Extract metadata
        metadata = {}
        if hasattr(result.input, "page_count"):
            metadata["page_count"] = result.input.page_count
        metadata["docling_version"] = "latest"
        metadata["processing_date"] = datetime.utcnow().isoformat()

        document.doc_metadata = metadata
        db.commit()
        logger.info(f"Updated document metadata: {metadata}")

        # Step 4: Get embedding service
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if not embedding_service:
            raise ValueError("GEMINI_API_KEY not configured - cannot generate embeddings")

        # Step 5: Perform hybrid chunking with token-aware chunker
        # HybridChunker uses document structure + token limits for optimal RAG chunks
        logger.info(
            f"Chunking document with HybridChunker (max_tokens: {settings.kb_max_chunk_tokens})"
        )

        # Wrap tiktoken encoder in OpenAITokenizer for HybridChunker compatibility
        tokenizer_wrapper = OpenAITokenizer(
            tokenizer=encoder,
            max_tokens=settings.kb_max_chunk_tokens,
        )

        chunker = HybridChunker(
            tokenizer=tokenizer_wrapper,  # Use wrapped tokenizer
            merge_peers=True,  # Merge consecutive chunks with same heading
        )

        doc_chunks = list(chunker.chunk(doc))
        logger.info(f"Generated {len(doc_chunks)} chunks from document")

        if not doc_chunks:
            raise ValueError("HybridChunker produced no valid chunks")

        # Step 6: Generate embeddings and store chunks with metadata
        logger.info("Generating embeddings and storing chunks...")
        stored_count = 0

        for i, chunk in enumerate(doc_chunks):
            # Extract text content
            chunk_text = chunk.text

            # Calculate token count
            token_count = len(encoder.encode(chunk_text))

            # Extract page numbers from provenance metadata
            page_numbers = set()
            headings = []

            # Iterate through doc items in the chunk to collect metadata
            for doc_item in chunk.meta.doc_items:
                if hasattr(doc_item, "prov") and doc_item.prov:
                    for prov in doc_item.prov:
                        if hasattr(prov, "page_no"):
                            page_numbers.add(prov.page_no)

                # Extract headings (if item is a section header)
                if hasattr(doc_item, "label") and "SECTION_HEADER" in str(doc_item.label):
                    if hasattr(doc_item, "text"):
                        headings.append(doc_item.text)

            # Determine primary page number (first page in sorted set)
            primary_page = min(page_numbers) if page_numbers else None

            # Determine primary heading (first heading)
            primary_heading = headings[0] if headings else None

            # Prepare chunk metadata
            chunk_metadata = {
                "all_page_numbers": sorted(list(page_numbers)),  # All pages this chunk spans
                "all_headings": headings,  # All headings in this chunk
                "doc_item_count": len(chunk.meta.doc_items),
            }

            logger.debug(
                f"Chunk {i}: page={primary_page}, heading={primary_heading}, tokens={token_count}"
            )

            # Generate embedding for chunk
            chunk_embedding = await embedding_service.generate(
                chunk_text, task_type="RETRIEVAL_DOCUMENT"
            )

            if not chunk_embedding:
                logger.warning(f"Failed to generate embedding for chunk {i} - skipping")
                continue

            # Create chunk record with extracted metadata
            chunk_obj = KnowledgeBaseChunk(
                document_id=document_id,
                chunk_index=i,
                content=chunk_text,
                content_type="text",
                page_number=primary_page,  # ✅ FIXED: Extract from provenance
                heading=primary_heading,  # ✅ FIXED: Extract from doc structure
                embedding=chunk_embedding,
                embedding_generated_at=datetime.utcnow(),
                token_count=token_count,
                chunk_metadata=chunk_metadata,  # Store additional metadata
            )

            db.add(chunk_obj)
            stored_count += 1

            # Commit in batches for efficiency
            if (i + 1) % 10 == 0:
                db.commit()
                logger.debug(f"Committed batch of 10 chunks (up to {i + 1})")

        # Final commit
        db.commit()
        logger.info(f"Stored {stored_count} chunks with embeddings")

        # Step 8: Update document status to completed
        document.status = "completed"
        document.processed_date = datetime.utcnow()
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
                document.processed_date = datetime.utcnow()
                db.commit()
                logger.info(f"Document {document_id} marked as failed")
        except Exception as update_error:
            logger.error(f"Failed to update document status: {str(update_error)}")

    finally:
        db.close()
        logger.info(f"Processing completed for document {document_id}")
