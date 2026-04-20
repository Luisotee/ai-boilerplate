"""
PDF processing module with pluggable parser (LlamaParse primary, Docling fallback).

Handles background processing of uploaded PDFs: cloud-based parsing via LlamaParse
by default, local Docling fallback when installed, token-aware chunking with
tiktoken, embedding generation, and storage in the database.
"""

import asyncio
import importlib.util
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import tiktoken

from .config import settings
from .database import SessionLocal
from .embeddings import create_embedding_service
from .kb_models import KnowledgeBaseChunk, KnowledgeBaseDocument
from .logger import logger

# (page_number, markdown_text) — the intermediate representation both parsers emit.
PageContent = tuple[int, str]

# Regex for markdown headings (ATX style): `# Heading`, `## Subheading`, etc.
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)

# Regex for fenced code blocks (```lang ... ``` or ~~~ ... ~~~). Non-greedy, DOTALL
# so a single block on one page doesn't swallow the whole page.
_CODE_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~).*?\n.*?^[ \t]{0,3}\1", re.MULTILINE | re.DOTALL)


def _strip_code_fences(md: str) -> str:
    """Remove fenced code blocks so `# comments` inside code aren't treated as headings."""
    return _CODE_FENCE_RE.sub("", md)


# Errors the auto-mode dispatcher will recover from by falling back to Docling.
# Programming errors (TypeError, AttributeError, ImportError, KeyError) propagate
# so SDK signature drift surfaces as a real bug instead of a silent reroute.
_PARSER_RECOVERABLE_BASES: list[type[BaseException]] = [
    httpx.HTTPError,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
    ValueError,
]
try:
    # Public re-export in llama-cloud>=2.4. Base class for APIStatusError,
    # APIConnectionError, APITimeoutError — covers HTTP 4xx/5xx responses
    # plus transport errors the SDK wraps.
    from llama_cloud import APIError as _LlamaApiError

    _PARSER_RECOVERABLE_BASES.append(_LlamaApiError)
except ImportError:
    pass

_RECOVERABLE_PARSER_ERRORS: tuple[type[BaseException], ...] = tuple(_PARSER_RECOVERABLE_BASES)


@dataclass
class ParsedChunk:
    """Parser-agnostic chunk consumed by `_generate_and_store_embeddings`."""

    text: str
    token_count: int
    page_numbers: list[int] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    doc_item_count: int = 0


def _docling_available() -> bool:
    """Check whether the optional `docling` extra is installed."""
    return importlib.util.find_spec("docling") is not None


async def _parse_pdf_with_llamaparse(file_path: str) -> list[PageContent]:
    """Parse a PDF via the LlamaParse cloud API (llama-cloud SDK, Parse API v2)."""
    from llama_cloud import AsyncLlamaCloud

    if not settings.llama_cloud_api_key:
        raise ValueError(
            "LLAMA_CLOUD_API_KEY not configured — cannot parse via LlamaParse. "
            "Get a key at https://cloud.llamaindex.ai or set PDF_PARSER=docling."
        )

    async with AsyncLlamaCloud(api_key=settings.llama_cloud_api_key) as client:
        file_obj = await client.files.create(file=Path(file_path), purpose="parse")
        try:
            result = await client.parsing.parse(
                file_id=file_obj.id,
                tier=settings.llamaparse_tier,
                version="latest",
                expand=["markdown"],
            )
        finally:
            # LlamaCloud retains uploaded files indefinitely; always clean up so
            # a parsing loop doesn't eventually hit account storage limits.
            try:
                await client.files.delete(file_obj.id)
            except Exception as delete_err:
                logger.warning(
                    f"Failed to delete uploaded LlamaCloud file {file_obj.id}: {delete_err}"
                )

    if result.markdown is None or not result.markdown.pages:
        raise ValueError("LlamaParse returned an empty result (no markdown pages).")

    pages: list[PageContent] = []
    skipped_blank = 0
    skipped_failed = 0
    for i, page in enumerate(result.markdown.pages):
        # `MarkdownPage` is a union: success pages have `.markdown`, failed pages
        # have `.error` and no `.markdown` attribute. Handle both safely.
        page_no = getattr(page, "page_number", i + 1)
        if getattr(page, "success", True) is False:
            skipped_failed += 1
            logger.warning(
                f"LlamaParse failed page {page_no}: {getattr(page, 'error', 'unknown error')}"
            )
            continue
        md = getattr(page, "markdown", None)
        if md is None:
            skipped_blank += 1
            continue
        pages.append((page_no, md))
    if skipped_blank:
        logger.warning(f"LlamaParse returned None markdown for {skipped_blank} page(s); skipped.")
    if skipped_failed:
        logger.warning(f"LlamaParse reported {skipped_failed} failed page(s); skipped.")
    return pages


async def _parse_pdf_with_docling(file_path: str) -> list[PageContent]:
    """Parse a PDF with the local Docling pipeline and flatten to per-page markdown."""
    if not _docling_available():
        raise RuntimeError(
            "Docling is not installed. Install the optional extra with "
            "`uv sync --extra docling` (also requires poppler-utils + tesseract-ocr) "
            "or configure LLAMA_CLOUD_API_KEY to use LlamaParse."
        )

    from docling.document_converter import DocumentConverter

    def _convert() -> list[PageContent]:
        result = DocumentConverter().convert(file_path)
        doc = result.document
        page_count = getattr(result.input, "page_count", None) or 1

        # Probe per-page export support once. Older docling versions don't accept
        # the page_no kwarg — degrade to whole-doc dump and warn loudly so
        # operators know citations are now inaccurate.
        try:
            first = doc.export_to_markdown(page_no=1)
        except TypeError:
            logger.warning(
                "Docling lacks per-page export (page_no kwarg unsupported). "
                "Returning whole document as page 1 — citations will be inaccurate. "
                "Upgrade docling or set PDF_PARSER=llamaparse to fix."
            )
            return [(1, doc.export_to_markdown())]

        pages: list[PageContent] = [(1, first)]
        for page_no in range(2, page_count + 1):
            pages.append((page_no, doc.export_to_markdown(page_no=page_no)))
        return pages

    return await asyncio.wait_for(
        asyncio.to_thread(_convert),
        timeout=settings.kb_parse_timeout_seconds,
    )


async def _parse_pdf(file_path: str) -> tuple[list[PageContent], dict]:
    """
    Dispatch PDF parsing based on `settings.pdf_parser`.

    Returns:
        Tuple of (pages, metadata). Metadata includes `parser` (the one that
        actually succeeded) and `processing_date`.
    """
    choice = settings.pdf_parser
    has_key = bool(settings.llama_cloud_api_key)

    async def _run_llamaparse() -> tuple[list[PageContent], dict]:
        pages = await asyncio.wait_for(
            _parse_pdf_with_llamaparse(file_path),
            timeout=settings.llamaparse_timeout_seconds,
        )
        return pages, {"parser": "llamaparse"}

    async def _run_docling() -> tuple[list[PageContent], dict]:
        pages = await _parse_pdf_with_docling(file_path)
        return pages, {"parser": "docling"}

    if choice == "llamaparse":
        return await _run_llamaparse()

    if choice == "docling":
        return await _run_docling()

    # auto
    if has_key:
        try:
            return await _run_llamaparse()
        except _RECOVERABLE_PARSER_ERRORS as llama_err:
            if _docling_available():
                logger.warning("LlamaParse failed; falling back to Docling.", exc_info=True)
                try:
                    return await _run_docling()
                except Exception as docling_err:
                    raise docling_err from llama_err
            logger.error("LlamaParse failed and Docling extra is not installed.", exc_info=True)
            raise

    # auto + no key
    if _docling_available():
        logger.info("No LLAMA_CLOUD_API_KEY set — using Docling fallback.")
        return await _run_docling()

    raise ValueError(
        "No PDF parser is available. Set LLAMA_CLOUD_API_KEY or install the "
        "`docling` extra (`uv sync --extra docling`)."
    )


def _chunk_pages_tiktoken(
    pages: list[PageContent],
    max_tokens: int,
    encoder: "tiktoken.Encoding",
) -> list[ParsedChunk]:
    """
    Split per-page markdown into token-limited chunks via paragraph-greedy packing.

    Paragraphs (split on blank lines) are packed into chunks up to `max_tokens`.
    Pages are chunked independently — we don't merge across page boundaries, which
    keeps `page_number` authoritative for citations.

    Oversized single paragraphs fall back to byte-level token-window splitting,
    decoded with `errors="ignore"` so multi-byte UTF-8 characters that span a
    BPE token boundary don't produce U+FFFD garbage in the stored text.
    """
    chunks: list[ParsedChunk] = []

    for page_no, md in pages:
        if not md or not md.strip():
            continue

        page_headings = [m.group(2).strip() for m in _HEADING_RE.finditer(_strip_code_fences(md))]
        paragraphs = [p for p in md.split("\n\n") if p.strip()]
        buf: list[str] = []
        buf_tokens = 0

        def _flush() -> None:
            nonlocal buf, buf_tokens
            if not buf:
                return
            text = "\n\n".join(buf)
            inner_headings = [
                m.group(2).strip() for m in _HEADING_RE.finditer(_strip_code_fences(text))
            ]
            chunks.append(
                ParsedChunk(
                    text=text,
                    token_count=buf_tokens,
                    page_numbers=[page_no],
                    headings=inner_headings or page_headings[:1],
                    doc_item_count=1,
                )
            )
            buf = []
            buf_tokens = 0

        for para in paragraphs:
            tokens = encoder.encode(para)
            if len(tokens) > max_tokens:
                _flush()
                # Token-window split for a single oversized paragraph. Decode via
                # bytes so split-codepoint sequences don't surface as U+FFFD.
                for start in range(0, len(tokens), max_tokens):
                    window = tokens[start : start + max_tokens]
                    text = encoder.decode_bytes(window).decode("utf-8", errors="ignore")
                    chunks.append(
                        ParsedChunk(
                            text=text,
                            token_count=len(window),
                            page_numbers=[page_no],
                            headings=page_headings[:1],
                            doc_item_count=1,
                        )
                    )
                continue

            if buf_tokens + len(tokens) > max_tokens:
                _flush()
            buf.append(para)
            buf_tokens += len(tokens)

        _flush()

    return chunks


async def process_pdf_document(
    document_id: str,
    file_path: str,
    whatsapp_jid: str | None = None,
):
    """
    Background task to process an uploaded PDF document with timeout constraints.

    Applies multi-level timeouts:
    - Overall processing timeout (300s default)
    - Parser timeout (300s for LlamaParse, 180s for Docling)
    - Per-embedding timeout (10s default)
    - Batch embedding timeout (240s default)
    """
    try:
        await asyncio.wait_for(
            _process_pdf_document_impl(document_id, file_path, whatsapp_jid),
            timeout=settings.kb_processing_timeout_seconds,
        )
    except TimeoutError:
        logger.error(
            f"❌ Document processing timeout after {settings.kb_processing_timeout_seconds}s: {document_id}"
        )
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
    """Internal implementation of PDF processing with individual timeouts."""
    db = SessionLocal()
    encoder = tiktoken.get_encoding("cl100k_base")

    try:
        logger.info(f"Starting processing for document {document_id}")

        document = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
        if not document:
            logger.error(f"Document {document_id} not found in database")
            return

        document.status = "processing"
        db.commit()
        logger.info(f"Document status updated to 'processing': {document.original_filename}")

        if not Path(file_path).exists():
            raise FileNotFoundError(f"PDF file not found: {file_path}")

        # Step 1: Parse PDF (LlamaParse primary, Docling fallback)
        logger.info(f"Parsing PDF (mode: {settings.pdf_parser}): {file_path}")
        try:
            pages, parser_metadata = await _parse_pdf(file_path)
        except TimeoutError:
            raise ValueError(
                "PDF parsing timeout. PDF may be corrupt, too large, or the parser is unreachable."
            )

        if not pages:
            raise ValueError("Parser returned no pages")

        logger.info(f"Parsed {len(pages)} pages via {parser_metadata['parser']}")

        # Step 2: Record metadata
        metadata = {
            "page_count": len(pages),
            "parser": parser_metadata["parser"],
            "processing_date": datetime.now(UTC).isoformat(),
        }
        document.doc_metadata = metadata
        db.commit()
        logger.info(f"Updated document metadata: {metadata}")

        # Step 3: Embedding service
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if not embedding_service:
            raise ValueError("GEMINI_API_KEY not configured - cannot generate embeddings")

        # Step 4: Chunk pages with tiktoken
        logger.info(f"Chunking document by page (max_tokens: {settings.kb_max_chunk_tokens})")
        chunks = _chunk_pages_tiktoken(pages, settings.kb_max_chunk_tokens, encoder)
        logger.info(f"Generated {len(chunks)} chunks from document")

        if not chunks:
            raise ValueError("Chunker produced no valid chunks")

        # Step 5: Generate embeddings with batch timeout
        logger.info("Generating embeddings and storing chunks...")
        try:
            stored_count, failure_metadata = await asyncio.wait_for(
                _generate_and_store_embeddings(
                    chunks=chunks,
                    embedding_service=embedding_service,
                    document_id=document_id,
                    db=db,
                ),
                timeout=settings.kb_embedding_batch_timeout_seconds,
            )
        except TimeoutError:
            actual_count = db.query(KnowledgeBaseChunk).filter_by(document_id=document_id).count()
            logger.warning(
                f"Embedding timeout after {settings.kb_embedding_batch_timeout_seconds}s. "
                f"Committed {actual_count}/{len(chunks)} chunks."
            )
            raise ValueError(
                f"Embedding generation timeout after {settings.kb_embedding_batch_timeout_seconds} seconds. "
                f"Successfully committed {actual_count} of {len(chunks)} chunks."
            )

        logger.info(
            f"Stored {stored_count}/{failure_metadata['total_chunks_parsed']} chunks "
            f"(skipped: {failure_metadata['chunks_skipped']})"
        )

        # Step 6: Update document status based on completeness
        if stored_count == 0:
            document.status = "failed"
        elif failure_metadata["chunks_skipped"] > 0:
            document.status = "partial"
            logger.warning(
                f"Document {document_id} partially processed: "
                f"{stored_count}/{failure_metadata['total_chunks_parsed']} chunks. "
                f"Skipped {failure_metadata['chunks_skipped']} chunks due to errors."
            )
        else:
            document.status = "completed"

        document.processed_date = datetime.now(UTC)
        document.chunk_count = stored_count

        if failure_metadata["chunks_skipped"] > 0:
            if document.doc_metadata is None:
                document.doc_metadata = {}
            document.doc_metadata["processing_errors"] = {
                "total_chunks_parsed": failure_metadata["total_chunks_parsed"],
                "chunks_stored": stored_count,
                "chunks_skipped": failure_metadata["chunks_skipped"],
                "skipped_chunk_indices": failure_metadata["skipped_chunk_indices"],
                "failure_summary": {
                    "embedding_timeout": sum(
                        1
                        for r in failure_metadata["failure_reasons"].values()
                        if r == "embedding_timeout"
                    ),
                    "embedding_generation_failed": sum(
                        1
                        for r in failure_metadata["failure_reasons"].values()
                        if r == "embedding_generation_failed"
                    ),
                },
            }

        db.commit()

        logger.info(
            f"✅ Processed document {document_id}: {document.original_filename} "
            f"({stored_count} chunks, status: {document.status})"
        )

    except Exception as e:
        logger.error(f"❌ Error processing document {document_id}: {str(e)}", exc_info=True)

        try:
            document = db.query(KnowledgeBaseDocument).filter_by(id=document_id).first()
            if document:
                actual_chunk_count = (
                    db.query(KnowledgeBaseChunk).filter_by(document_id=document_id).count()
                )
                if actual_chunk_count > 0:
                    document.status = "partial"
                    logger.info(
                        f"Document {document_id} partially processed: "
                        f"{actual_chunk_count} chunks committed before failure"
                    )
                else:
                    document.status = "failed"

                document.chunk_count = actual_chunk_count
                document.error_message = str(e)
                document.processed_date = datetime.now(UTC)
                db.commit()

                logger.info(
                    f"Document {document_id} marked as {document.status} "
                    f"with {actual_chunk_count} chunks"
                )
        except Exception as update_error:
            logger.error(f"Failed to update document status: {str(update_error)}")
            db.rollback()

    finally:
        db.close()
        logger.info(f"Processing completed for document {document_id}")


async def _generate_and_store_embeddings(
    chunks: list[ParsedChunk],
    embedding_service,
    document_id: str,
    db,
) -> tuple[int, dict]:
    """Generate embeddings for parsed chunks with per-chunk timeout."""
    stored_count = 0
    skipped_chunks: list[int] = []
    failure_reasons: dict[int, str] = {}

    for i, chunk in enumerate(chunks):
        primary_page = chunk.page_numbers[0] if chunk.page_numbers else None
        primary_heading = chunk.headings[0] if chunk.headings else None
        chunk_metadata = {
            "all_page_numbers": chunk.page_numbers,
            "all_headings": chunk.headings,
            "doc_item_count": chunk.doc_item_count,
        }

        logger.debug(
            f"Chunk {i}: page={primary_page}, heading={primary_heading}, tokens={chunk.token_count}"
        )

        try:
            chunk_embedding = await asyncio.wait_for(
                embedding_service.generate(chunk.text, task_type="RETRIEVAL_DOCUMENT"),
                timeout=settings.kb_embedding_timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                f"Embedding timeout for chunk {i} after {settings.kb_embedding_timeout_seconds}s - skipping"
            )
            skipped_chunks.append(i)
            failure_reasons[i] = "embedding_timeout"
            continue

        if not chunk_embedding:
            logger.warning(f"Failed to generate embedding for chunk {i} - skipping")
            skipped_chunks.append(i)
            failure_reasons[i] = "embedding_generation_failed"
            continue

        chunk_obj = KnowledgeBaseChunk(
            document_id=document_id,
            chunk_index=i,
            content=chunk.text,
            content_type="text",
            page_number=primary_page,
            heading=primary_heading,
            embedding=chunk_embedding,
            embedding_generated_at=datetime.now(UTC),
            token_count=chunk.token_count,
            chunk_metadata=chunk_metadata,
        )
        db.add(chunk_obj)
        stored_count += 1

        if (i + 1) % 10 == 0:
            db.commit()
            logger.debug(f"Committed batch of 10 chunks (up to {i + 1})")

    db.commit()

    failure_metadata = {
        "total_chunks_parsed": len(chunks),
        "chunks_stored": stored_count,
        "chunks_skipped": len(skipped_chunks),
        "skipped_chunk_indices": skipped_chunks,
        "failure_reasons": failure_reasons,
    }

    return stored_count, failure_metadata
