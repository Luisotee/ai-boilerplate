import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..config import settings
from ..database import get_db
from ..deps import UPLOAD_DIR, limiter
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger
from ..queue.connection import get_redis_client
from ..schemas import BatchUploadResponse, FileUploadResult, UploadPDFResponse
from ..streams.manager import enqueue_pdf_processing

router = APIRouter()


# Streaming read size for uploads (memory-efficient)
_CHUNK_SIZE = 8192


async def _save_upload(file: UploadFile, file_path: Path) -> str:
    """Stream an upload to disk and return its SHA-256 hex digest."""
    sha256 = hashlib.sha256()
    await file.seek(0)
    with open(file_path, "wb") as f:
        while chunk := await file.read(_CHUNK_SIZE):
            sha256.update(chunk)
            f.write(chunk)
    return sha256.hexdigest()


def _find_duplicate(db: Session, file_hash: str) -> KnowledgeBaseDocument | None:
    """
    Return a global knowledge-base document with identical content, if any.

    Conversation-scoped (chat) PDFs are ignored — they expire and belong to one
    chat. So are ``failed`` documents, so a file whose processing failed can be
    uploaded again. Rows from before the ``file_hash`` column have NULL there and
    never match.
    """
    return (
        db.query(KnowledgeBaseDocument)
        .filter(
            KnowledgeBaseDocument.file_hash == file_hash,
            KnowledgeBaseDocument.is_conversation_scoped.is_(False),
            KnowledgeBaseDocument.status != "failed",
        )
        .first()
    )


def _discard_upload(db: Session, document: KnowledgeBaseDocument, file_path: Path) -> None:
    """Remove a just-created document row and its file (enqueue failed)."""
    try:
        db.delete(document)
        db.commit()
    except Exception:
        logger.error(f"Failed to delete unqueued document {document.id}", exc_info=True)
        db.rollback()
        return
    # File only after the row is gone (an orphan file is harmless, a row
    # pointing at a missing file is not).
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        logger.warning(f"Failed to delete unqueued upload {file_path}", exc_info=True)


@router.post("/knowledge-base/upload", response_model=UploadPDFResponse, tags=["Knowledge Base"])
@limiter.exempt  # bulk loads (upload-kb.sh) — the route is still behind X-API-Key
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload a PDF document to the knowledge base

    The PDF is queued on the `stream:pdf_processing` Redis Stream and parsed by the
    stream worker (LlamaParse or Docling, per `PDF_PARSER`), chunked, and indexed
    for retrieval. Poll `/knowledge-base/status/{document_id}` for progress.

    **Request:**
    - `file`: PDF file (multipart/form-data)

    **Response:**
    - `document_id`: UUID for tracking processing status
    - `filename`: Original filename
    - `status`: Initial status ('queued')
    - `message`: Human-readable status message

    **Errors:**
    - `409`: identical content (SHA-256) is already in the knowledge base
    """
    logger.info(f"Received PDF upload: {file.filename}")

    # Validate file type
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    if not file.content_type or file.content_type not in [
        "application/pdf",
        "application/x-pdf",
    ]:
        logger.warning(f"Unexpected content type: {file.content_type}, but filename ends with .pdf")

    # Generate unique document ID and filename
    doc_id = uuid.uuid4()
    stored_filename = f"{doc_id}.pdf"
    file_path = UPLOAD_DIR / stored_filename

    # Validate and stream uploaded file to disk
    try:
        # Get file size without reading content (efficient for large files)
        file.file.seek(0, 2)  # Seek to end
        file_size = file.file.tell()  # Get position (size)
        file.file.seek(0)  # Reset to beginning

        # Check file size limit BEFORE reading
        max_size_bytes = settings.kb_max_file_size_mb * 1024 * 1024

        if file_size > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({file_size / 1024 / 1024:.1f} MB). Maximum size: {settings.kb_max_file_size_mb} MB",
            )

        file_hash = await _save_upload(file, file_path)

        logger.info(f"Saved PDF to {file_path} ({file_size / 1024:.1f} KB)")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving file: {str(e)}", exc_info=True)
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Reject content that is already in the knowledge base
    try:
        existing = _find_duplicate(db, file_hash)
    except Exception:
        logger.error("Duplicate check failed", exc_info=True)
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to check for duplicates")
    if existing:
        file_path.unlink(missing_ok=True)
        logger.info(f"Rejected duplicate upload {file.filename} (matches {existing.id})")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Duplicate document: '{existing.original_filename}' "
                f"(ID: {existing.id}, status: {existing.status})"
            ),
        )

    # Create database record
    try:
        document = KnowledgeBaseDocument(
            id=doc_id,
            filename=stored_filename,
            original_filename=file.filename,
            file_size_bytes=file_size,
            file_hash=file_hash,
            mime_type=file.content_type or "application/pdf",
            status="queued",
        )
        db.add(document)
        db.commit()
        db.refresh(document)

        logger.info(f"Created database record for document {doc_id}")

    except Exception as e:
        logger.error(f"Error creating database record: {str(e)}", exc_info=True)
        # Clean up uploaded file
        if file_path.exists():
            file_path.unlink()
        raise HTTPException(status_code=500, detail="Failed to create database record")

    # Enqueue for the stream worker's PDF consumer
    try:
        async with get_redis_client() as redis_client:
            await enqueue_pdf_processing(
                redis_client, document_id=str(doc_id), file_path=str(file_path)
            )
    except Exception:
        logger.error(f"Failed to enqueue PDF processing for {doc_id}", exc_info=True)
        # Nothing would ever pick the document up: undo the upload so the client
        # can simply retry (and a retry isn't blocked as a duplicate).
        _discard_upload(db, document, file_path)
        raise HTTPException(status_code=503, detail="Document queue unavailable. Please try again.")

    logger.info(f"Enqueued PDF processing for document {doc_id}")

    return UploadPDFResponse(
        document_id=str(doc_id),
        filename=file.filename,
        status="queued",
        message="PDF uploaded successfully. Queued for processing.",
    )


@router.post(
    "/knowledge-base/upload/batch",
    response_model=BatchUploadResponse,
    tags=["Knowledge Base"],
)
@limiter.exempt  # bulk loads (upload-kb.sh) — the route is still behind X-API-Key
async def upload_pdf_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload multiple PDF documents to the knowledge base in a single request

    Each file is validated independently. Valid files are saved and queued on the
    `stream:pdf_processing` Redis Stream for the stream worker, while invalid
    files are rejected with error details.

    **Request:**
    - `files`: Multiple PDF files (multipart/form-data)

    **Response:**
    - `total_files`: Total number of files submitted
    - `accepted`: Number of files queued for processing
    - `rejected`: Number of files rejected during validation
    - `results`: Per-file status with document_id (if accepted) or error (if rejected)
    - `message`: Overall batch status message

    Files whose content (SHA-256) is already in the knowledge base, or appears
    earlier in the same batch, are rejected as duplicates.

    **Configuration:**
    - `KB_MAX_FILE_SIZE_MB`: Maximum individual file size (default: 50 MB)
    - `KB_MAX_BATCH_SIZE_MB`: Maximum total batch size (default: 500 MB)
    """
    logger.info(f"Received batch PDF upload: {len(files)} files")

    # Read configuration
    max_file_size_bytes = settings.kb_max_file_size_mb * 1024 * 1024
    max_batch_size_bytes = settings.kb_max_batch_size_mb * 1024 * 1024

    # Check if any files provided
    if len(files) == 0:
        logger.warning("Batch upload with no files")
        return BatchUploadResponse(
            total_files=0,
            accepted=0,
            rejected=0,
            results=[],
            message="No files provided",
        )

    # Phase 1: Validate each file independently
    file_validations = []
    total_size = 0

    for file in files:
        error = None
        file_size = 0
        filename = file.filename or "unknown"

        # Validate filename
        if not file.filename:
            error = "Missing filename"
        elif not file.filename.endswith(".pdf"):
            error = "Only PDF files are supported"

        # Validate content type
        if not error and file.content_type:
            if file.content_type not in ["application/pdf", "application/x-pdf"]:
                logger.warning(f"Unexpected content type: {file.content_type} for {filename}")

        # Check file size without reading content (memory-efficient)
        if not error:
            try:
                file.file.seek(0, 2)  # Seek to end
                file_size = file.file.tell()  # Get size
                file.file.seek(0)  # Reset to beginning

                if file_size == 0:
                    error = "Empty file"
                elif file_size > max_file_size_bytes:
                    error = f"File too large ({file_size / 1024 / 1024:.1f} MB). Maximum: {settings.kb_max_file_size_mb} MB"

            except Exception as e:
                logger.error(f"Error checking file size {filename}: {str(e)}", exc_info=True)
                error = "Failed to read file metadata"

        file_validations.append(
            {
                "file": file,
                "filename": filename,
                "size": file_size,
                "error": error,
            }
        )

        total_size += file_size

    # Check total batch size
    if total_size > max_batch_size_bytes:
        logger.warning(
            f"Batch too large: {total_size / 1024 / 1024:.1f} MB > {settings.kb_max_batch_size_mb} MB"
        )
        # Reject all files that exceed the remaining batch size
        running_total = 0
        for validation in file_validations:
            if validation["error"] is None:
                running_total += validation["size"]
                if running_total > max_batch_size_bytes:
                    validation["error"] = (
                        f"Batch size limit exceeded. Total: {total_size / 1024 / 1024:.1f} MB, Maximum: {settings.kb_max_batch_size_mb} MB"
                    )

    # Phase 2: Process valid files and build results
    results = []
    accepted_count = 0
    rejected_count = 0

    batch_hashes: dict[str, str] = {}  # sha256 -> filename, for intra-batch dedup

    for validation in file_validations:
        filename = validation["filename"]
        error = validation["error"]

        # If file has validation error, add to rejected results
        if error:
            results.append(FileUploadResult(filename=filename, status="rejected", error=error))
            rejected_count += 1
            logger.info(f"Rejected file: {filename} - {error}")
            continue

        # File is valid, save it
        file_path: Path | None = None
        try:
            file = validation["file"]
            file_size = validation["size"]

            # Generate unique document ID and filename
            doc_id = uuid.uuid4()
            stored_filename = f"{doc_id}.pdf"
            file_path = UPLOAD_DIR / stored_filename

            file_hash = await _save_upload(file, file_path)

            logger.info(f"Saved PDF to {file_path} ({file_size / 1024:.1f} KB)")

            # Duplicates: within this batch first (no DB round trip), then the KB
            duplicate_error = None
            if file_hash in batch_hashes:
                duplicate_error = f"Duplicate of '{batch_hashes[file_hash]}' in this batch"
            elif existing := _find_duplicate(db, file_hash):
                duplicate_error = f"Duplicate of '{existing.original_filename}' (ID: {existing.id})"
            if duplicate_error:
                file_path.unlink(missing_ok=True)
                results.append(
                    FileUploadResult(filename=filename, status="rejected", error=duplicate_error)
                )
                rejected_count += 1
                logger.info(f"Rejected duplicate: {filename} - {duplicate_error}")
                continue
            batch_hashes[file_hash] = filename

            # Create database record
            document = KnowledgeBaseDocument(
                id=doc_id,
                filename=stored_filename,
                original_filename=filename,
                file_size_bytes=file_size,
                file_hash=file_hash,
                mime_type=file.content_type or "application/pdf",
                status="queued",
            )
            db.add(document)
            db.commit()
            db.refresh(document)

            logger.info(f"Created database record for document {doc_id}")

            # Enqueue for the stream worker's PDF consumer
            try:
                async with get_redis_client() as redis_client:
                    await enqueue_pdf_processing(
                        redis_client, document_id=str(doc_id), file_path=str(file_path)
                    )
            except Exception:
                logger.error(f"Failed to enqueue PDF processing for {doc_id}", exc_info=True)
                _discard_upload(db, document, file_path)
                results.append(
                    FileUploadResult(
                        filename=filename,
                        status="rejected",
                        error="Document queue unavailable. Please try again.",
                    )
                )
                rejected_count += 1
                continue

            logger.info(f"Enqueued processing for {filename} ({doc_id})")

            # Add to accepted results
            results.append(
                FileUploadResult(
                    filename=filename,
                    status="accepted",
                    document_id=str(doc_id),
                    message="Queued for processing",
                )
            )
            accepted_count += 1

        except Exception as e:
            logger.error(f"Error saving file {filename}: {str(e)}", exc_info=True)
            # A failed commit leaves the shared session unusable for the rest of
            # the batch until it is rolled back.
            try:
                db.rollback()
            except Exception:
                logger.error("Rollback failed during batch upload", exc_info=True)

            # Generic message only: str(e) can leak DB/SQL details to the caller
            results.append(
                FileUploadResult(
                    filename=filename,
                    status="rejected",
                    error="Failed to save file",
                )
            )
            rejected_count += 1

            # Clean up file if it was saved
            try:
                if file_path is not None:
                    file_path.unlink(missing_ok=True)
            except Exception as cleanup_error:
                logger.error(f"Cleanup error for {filename}: {str(cleanup_error)}")

    logger.info(f"Batch upload complete: {accepted_count} accepted, {rejected_count} rejected")

    # Build response message
    if accepted_count == 0:
        message = f"All {rejected_count} files were rejected"
    elif rejected_count == 0:
        message = f"Successfully queued {accepted_count} files for processing"
    else:
        message = (
            f"Processed {len(files)} files: {accepted_count} accepted, {rejected_count} rejected"
        )

    return BatchUploadResponse(
        total_files=len(files),
        accepted=accepted_count,
        rejected=rejected_count,
        results=results,
        message=message,
    )


@router.get("/knowledge-base/status/{document_id}", tags=["Knowledge Base"])
async def get_document_status(document_id: str, db: Session = Depends(get_db)):
    """
    Check processing status of an uploaded document

    Returns the current processing status and metadata for a document.

    **Path Parameters:**
    - `document_id`: UUID of the document

    **Response:**
    - `id`: Document UUID
    - `original_filename`: Original filename
    - `status`: Current status (queued, processing, completed, partial, failed)
    - `chunk_count`: Number of chunks created (0 if not completed)
    - `error_message`: Error details if status is 'failed'
    - `upload_date`: When document was uploaded
    - `processed_date`: When processing completed (null if not completed)
    """
    try:
        document = (
            db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == document_id).first()
        )

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return {
            "id": str(document.id),
            "original_filename": document.original_filename,
            "status": document.status,
            "chunk_count": document.chunk_count,
            "error_message": document.error_message,
            "upload_date": document.upload_date,
            "processed_date": document.processed_date,
            "file_size_bytes": document.file_size_bytes,
            "doc_metadata": document.doc_metadata,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving document status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/knowledge-base/documents", tags=["Knowledge Base"])
async def list_documents(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """
    List all documents in the knowledge base

    Returns a paginated list of documents with optional status filtering.

    **Query Parameters:**
    - `status`: Optional filter by status (queued, pending, processing, completed, partial, failed)
    - `limit`: Maximum number of documents to return (default: 50, max: 100)
    - `offset`: Number of documents to skip for pagination (default: 0)

    **Response:**
    - `documents`: List of document metadata
    - `total`: Total count of documents (filtered)
    - `limit`: Applied limit
    - `offset`: Applied offset
    """
    try:
        # Validate limit
        if limit > 100:
            limit = 100
        if limit < 1:
            limit = 1

        # Build query
        query = db.query(KnowledgeBaseDocument)

        # Apply status filter if provided
        if status:
            valid_statuses = ["queued", "pending", "processing", "completed", "partial", "failed"]
            if status not in valid_statuses:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
                )
            query = query.filter(KnowledgeBaseDocument.status == status)

        # Get total count
        total = query.count()

        # Apply pagination and ordering
        documents = (
            query.order_by(KnowledgeBaseDocument.upload_date.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        # Format response
        return {
            "documents": [
                {
                    "id": str(doc.id),
                    "original_filename": doc.original_filename,
                    "status": doc.status,
                    "chunk_count": doc.chunk_count,
                    "file_size_bytes": doc.file_size_bytes,
                    "upload_date": doc.upload_date,
                    "processed_date": doc.processed_date,
                    "error_message": doc.error_message,
                    "doc_metadata": doc.doc_metadata,
                }
                for doc in documents
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing documents: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/knowledge-base/documents/{document_id}", tags=["Knowledge Base"])
async def delete_document(document_id: str, db: Session = Depends(get_db)):
    """
    Delete a document and all its chunks

    Removes the document from the database (cascades to delete all chunks)
    and deletes the PDF file from disk.

    **Path Parameters:**
    - `document_id`: UUID of the document to delete

    **Response:**
    - `success`: Boolean indicating successful deletion
    - `message`: Confirmation message
    - `deleted_chunks`: Number of chunks deleted
    """
    try:
        # Find document
        document = (
            db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.id == document_id).first()
        )

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Get chunk count before deletion
        chunk_count = document.chunk_count

        # Delete file from disk
        file_path = UPLOAD_DIR / document.filename
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Deleted file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete file {file_path}: {str(e)}")
                # Continue with database deletion even if file deletion fails

        # Delete from database (cascades to chunks)
        db.delete(document)
        db.commit()

        logger.info(f"Deleted document {document_id} with {chunk_count} chunks")

        return {
            "success": True,
            "message": f'Document "{document.original_filename}" deleted successfully',
            "deleted_chunks": chunk_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
