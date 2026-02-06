import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..config import settings
from ..database import get_db
from ..deps import UPLOAD_DIR, limiter
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger
from ..processing import process_pdf_document
from ..schemas import BatchUploadResponse, FileUploadResult, UploadPDFResponse

router = APIRouter()


@router.post("/knowledge-base/upload", response_model=UploadPDFResponse, tags=["Knowledge Base"])
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    """
    Upload a PDF document to the knowledge base

    The PDF will be parsed with Docling, chunked semantically, and indexed for retrieval.
    Processing happens in the background.

    **Request:**
    - `file`: PDF file (multipart/form-data)

    **Response:**
    - `document_id`: UUID for tracking processing status
    - `filename`: Original filename
    - `status`: Initial status ('pending')
    - `message`: Human-readable status message
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

    # Read and save uploaded file
    try:
        content = await file.read()
        file_size = len(content)

        # Check file size limit
        max_size_bytes = settings.kb_max_file_size_mb * 1024 * 1024

        if file_size > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large ({file_size / 1024 / 1024:.1f} MB). Maximum size: {settings.kb_max_file_size_mb} MB",
            )

        with open(file_path, "wb") as f:
            f.write(content)

        logger.info(f"Saved PDF to {file_path} ({file_size / 1024:.1f} KB)")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving file: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save file")

    # Create database record
    try:
        document = KnowledgeBaseDocument(
            id=doc_id,
            filename=stored_filename,
            original_filename=file.filename,
            file_size_bytes=file_size,
            mime_type=file.content_type or "application/pdf",
            status="pending",
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

    # Schedule background processing
    background_tasks.add_task(
        process_pdf_document, document_id=str(doc_id), file_path=str(file_path)
    )

    logger.info(f"Scheduled background processing for document {doc_id}")

    return UploadPDFResponse(
        document_id=str(doc_id),
        filename=file.filename,
        status="pending",
        message="PDF uploaded successfully. Processing in background.",
    )


@router.post(
    "/knowledge-base/upload/batch",
    response_model=BatchUploadResponse,
    tags=["Knowledge Base"],
)
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def upload_pdf_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    """
    Upload multiple PDF documents to the knowledge base in a single request

    Each file is validated independently. Valid files are saved and processed,
    while invalid files are rejected with error details. Processing happens in
    the background for all accepted files.

    **Request:**
    - `files`: Multiple PDF files (multipart/form-data)

    **Response:**
    - `total_files`: Total number of files submitted
    - `accepted`: Number of files queued for processing
    - `rejected`: Number of files rejected during validation
    - `results`: Per-file status with document_id (if accepted) or error (if rejected)
    - `message`: Overall batch status message

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
        file_content = None
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

        # Read file and check size
        if not error:
            try:
                file_content = await file.read()
                file_size = len(file_content)

                if file_size == 0:
                    error = "Empty file"
                elif file_size > max_file_size_bytes:
                    error = f"File too large ({file_size / 1024 / 1024:.1f} MB). Maximum: {settings.kb_max_file_size_mb} MB"

            except Exception as e:
                logger.error(f"Error reading file {filename}: {str(e)}", exc_info=True)
                error = "Failed to read file"

        file_validations.append(
            {
                "file": file,
                "filename": filename,
                "size": file_size,
                "error": error,
                "content": file_content,
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
        try:
            file = validation["file"]
            content = validation["content"]
            file_size = validation["size"]

            # Generate unique document ID and filename
            doc_id = uuid.uuid4()
            stored_filename = f"{doc_id}.pdf"
            file_path = UPLOAD_DIR / stored_filename

            # Save file to disk
            with open(file_path, "wb") as f:
                f.write(content)

            logger.info(f"Saved PDF to {file_path} ({file_size / 1024:.1f} KB)")

            # Create database record
            document = KnowledgeBaseDocument(
                id=doc_id,
                filename=stored_filename,
                original_filename=filename,
                file_size_bytes=file_size,
                mime_type=file.content_type or "application/pdf",
                status="pending",
            )
            db.add(document)
            db.commit()
            db.refresh(document)

            logger.info(f"Created database record for document {doc_id}")

            # Schedule background processing
            background_tasks.add_task(
                process_pdf_document, document_id=str(doc_id), file_path=str(file_path)
            )

            logger.info(f"Scheduled processing for {filename} ({doc_id})")

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

            # Add to rejected results
            results.append(
                FileUploadResult(
                    filename=filename,
                    status="rejected",
                    error=f"Failed to save file: {str(e)}",
                )
            )
            rejected_count += 1

            # Clean up file if it was saved
            try:
                if file_path.exists():
                    file_path.unlink()
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
    - `status`: Current status (pending, processing, completed, failed)
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
    - `status`: Optional filter by status (pending, processing, completed, failed)
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
            valid_statuses = ["pending", "processing", "completed", "failed"]
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
