import base64
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session

from .agent import AgentDeps, format_message_history, get_ai_response
from .commands import is_command, parse_and_execute
from .config import settings
from .database import (
    get_conversation_history,
    get_db,
    get_or_create_user,
    get_user_preferences,
    init_db,
    save_message,
)
from .embeddings import create_embedding_service
from .kb_models import KnowledgeBaseDocument
from .logger import logger
from .processing import process_pdf_document
from .queue.connection import close_arq_redis, get_arq_redis, get_redis_client
from .queue.schemas import ChunkData, EnqueueResponse, JobStatusResponse
from .queue.utils import get_job_chunks, get_job_metadata, save_job_image
from .schemas import (
    BatchUploadResponse,
    ChatRequest,
    ChatResponse,
    CommandResponse,
    FileUploadResult,
    PreferencesResponse,
    SaveMessageRequest,
    TranscribeResponse,
    TTSRequest,
    UpdatePreferencesRequest,
    UploadPDFResponse,
)
from .streams.manager import add_message_to_stream
from .transcription import create_groq_client, transcribe_audio, validate_audio_file
from .tts import (
    create_genai_client,
    get_audio_mimetype,
    get_voice_for_language,
    pcm_to_audio,
    synthesize_speech,
    validate_text_input,
)
from .whatsapp import create_whatsapp_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and Redis on startup"""
    logger.info("Starting AI API service...")

    # Initialize PostgreSQL
    init_db()

    # Initialize Redis connection pool
    try:
        await get_arq_redis()  # Initialize connection pool
        logger.info("✅ Redis connection pool initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Redis: {e}")
        raise

    logger.info("=" * 60)
    logger.info("AI API is ready!")
    logger.info("=" * 60)
    logger.info("📚 API Documentation:")
    logger.info("   Swagger UI: http://localhost:8000/docs")
    logger.info("   ReDoc:      http://localhost:8000/redoc")
    logger.info("   OpenAPI:    http://localhost:8000/openapi.json")
    logger.info("=" * 60)
    logger.info("🏥 Health Check: http://localhost:8000/health")
    logger.info("=" * 60)

    yield

    # Cleanup on shutdown
    logger.info("Shutting down AI API service...")
    await close_arq_redis()
    logger.info("✅ Redis connection pool closed")


app = FastAPI(
    title="AI WhatsApp Agent API",
    version="1.0.0",
    description="""
    ## AI WhatsApp Agent API

    A FastAPI service that powers an AI chatbot with conversation memory.

    ### Features
    - 🤖 **AI-powered responses** using Google Gemini via Pydantic AI
    - 💬 **Conversation memory** stored in PostgreSQL
    - 📡 **Streaming support** via Server-Sent Events (SSE)
    - 🔄 **Cross-platform** - works with WhatsApp, Telegram, and more

    ### Endpoints
    - `/health` - Health check endpoint
    - `/chat` - Non-streaming chat endpoint
    - `/chat/stream` - Streaming chat endpoint (SSE)

    ### Auto-Generated Documentation
    - **Swagger UI**: Available at `/docs`
    - **ReDoc**: Available at `/redoc`
    - **OpenAPI Schema**: Available at `/openapi.json`
    """,
    lifespan=lifespan,
)

# Configure upload directory for knowledge base PDFs
UPLOAD_DIR = Path(settings.kb_upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"Knowledge base upload directory: {UPLOAD_DIR}")


# Helper function for Redis Streams job status inference
async def get_stream_job_status(redis, job_id: str) -> str:
    """
    Infer job status from Redis chunks and metadata.

    Args:
        redis: Redis client instance
        job_id: Job identifier

    Returns:
        Status string: 'complete', 'in_progress', or 'queued'
    """
    # Check if metadata exists (job complete)
    metadata = await get_job_metadata(redis, job_id)
    if metadata:
        return "complete"

    # Check if chunks exist (job in progress)
    chunks = await get_job_chunks(redis, job_id)
    if chunks:
        return "in_progress"

    # No chunks or metadata (job queued or not found)
    return "queued"


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check endpoint

    Returns the service health status.
    """
    return {"status": "healthy"}


@app.post("/knowledge-base/upload", response_model=UploadPDFResponse, tags=["Knowledge Base"])
async def upload_pdf(
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


@app.post(
    "/knowledge-base/upload/batch",
    response_model=BatchUploadResponse,
    tags=["Knowledge Base"],
)
async def upload_pdf_batch(
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


@app.get("/knowledge-base/status/{document_id}", tags=["Knowledge Base"])
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


@app.get("/knowledge-base/documents", tags=["Knowledge Base"])
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


@app.delete("/knowledge-base/documents/{document_id}", tags=["Knowledge Base"])
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


@app.post("/chat/save", tags=["Chat"])
async def save_message_only(request: SaveMessageRequest, db: Session = Depends(get_db)):
    """
    Save a message without generating AI response

    Used for group messages where bot shouldn't respond but needs to maintain context.

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (group or private)
    - `message`: Message text
    - `sender_jid`: Optional sender JID (for group messages)
    - `sender_name`: Optional sender name (for group messages)

    **Response:**
    - `success`: Boolean indicating if save was successful
    """
    logger.info(f"Saving message from {request.whatsapp_jid} (no response)")

    try:
        # Format message with sender name if group message
        content = (
            f"{request.sender_name}: {request.message}" if request.sender_name else request.message
        )

        # Generate embedding for message using embedding service
        user_embedding = None
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if embedding_service:
            try:
                user_embedding = await embedding_service.generate(content)
                if not user_embedding:
                    logger.warning("Failed to generate embedding (graceful degradation)")
            except Exception as e:
                logger.error(f"Embedding generation error (continuing anyway): {str(e)}")

        # Save user message only
        save_message(
            db,
            request.whatsapp_jid,
            "user",
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding,
        )

        return {"success": True}

    except Exception as e:
        logger.error(f"Error saving message: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post(
    "/chat/enqueue",
    response_model=EnqueueResponse | CommandResponse,
    tags=["Chat"],
)
async def enqueue_chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Enqueue a chat message for asynchronous processing

    This endpoint accepts a message, saves it immediately to the database,
    and adds it to a Redis Stream for processing. Returns a job ID
    that can be used to poll for status or stream results.

    **Commands:** Messages starting with "/" are treated as commands and return immediately:
    - `/settings` - Show current preferences
    - `/tts on|off` - Enable/disable TTS
    - `/tts lang [code]` - Set TTS language
    - `/stt lang [code|auto]` - Set STT language
    - `/help` - Show available commands

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text (or command like "/settings")
    - `conversation_type`: 'private' or 'group'
    - `sender_jid`: (Optional) Sender's JID for group messages
    - `sender_name`: (Optional) Sender's name for group messages

    **Response (regular message):**
    - `job_id`: Unique identifier for tracking this job
    - `status`: 'queued'
    - `message`: Success message

    **Response (command):**
    - `is_command`: true
    - `response`: Command result text

    **Next Steps:**
    - Poll `/chat/job/{job_id}` for status and accumulated chunks
    - Or stream from `/chat/stream/{job_id}` via SSE
    """
    has_image = request.image_data is not None and request.image_mimetype is not None
    logger.info(
        f"Received request from {request.whatsapp_jid}: {request.message[:50]}... (has_image={has_image})"
    )

    # Check for commands first (e.g., /settings, /tts on, /help)
    if is_command(request.message):
        user = get_or_create_user(db, request.whatsapp_jid, request.conversation_type)
        result = parse_and_execute(db, str(user.id), request.whatsapp_jid, request.message)
        if result.is_command:
            logger.info(f"Command executed for {request.whatsapp_jid}: {request.message}")
            return CommandResponse(is_command=True, response=result.response_text)

    try:
        # Check for document
        has_document = (
            request.document_data is not None
            and request.document_mimetype is not None
            and request.document_filename is not None
        )

        # For image messages, store with [Image] marker for history context
        if has_image:
            # Store as [Image: caption] or [Image] for database history
            content = f"[Image: {request.message}]" if request.message else "[Image]"
            if request.sender_name:
                content = f"{request.sender_name}: {content}"
        elif has_document:
            # Store as [Document: filename] for database history
            content = f"[Document: {request.document_filename}]"
            if request.message:
                content = f"{content} - {request.message}"
            if request.sender_name:
                content = f"{request.sender_name}: {content}"
        else:
            # Format message with sender name if provided (group message)
            content = (
                f"{request.sender_name}: {request.message}"
                if request.sender_name
                else request.message
            )

        # Get or create user
        user = get_or_create_user(db, request.whatsapp_jid, request.conversation_type)

        # Generate embedding for user message
        user_embedding = None
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if embedding_service:
            try:
                user_embedding = await embedding_service.generate(content)
                if user_embedding:
                    logger.info("Generated embedding for user message")
                else:
                    logger.warning("Failed to generate embedding (graceful degradation)")
            except Exception as e:
                logger.error(f"Embedding generation error (continuing anyway): {str(e)}")

        # Save user message immediately
        user_msg = save_message(
            db,
            request.whatsapp_jid,
            "user",
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding,
        )

        # Add message to user's Redis Stream for sequential processing
        redis_client = await get_redis_client()
        job_id = str(uuid.uuid4())

        # Build job data with optional whatsapp_message_id
        job_data = {
            "job_id": job_id,
            "user_id": str(user.id),
            "whatsapp_jid": request.whatsapp_jid,
            "message": request.message,  # Original message/caption for AI processing
            "conversation_type": request.conversation_type,
            "user_message_id": str(user_msg.id),
        }
        if request.whatsapp_message_id:
            job_data["whatsapp_message_id"] = request.whatsapp_message_id

        # Handle image data if present
        if has_image:
            # Store image in Redis separately (to avoid large stream messages)
            await save_job_image(redis_client, job_id, request.image_data)
            job_data["image_mimetype"] = request.image_mimetype
            job_data["has_image"] = "true"

        # Handle document (PDF) data if present
        has_document = (
            request.document_data is not None
            and request.document_mimetype is not None
            and request.document_filename is not None
        )

        if has_document:
            # Only support PDFs for now
            if request.document_mimetype != "application/pdf":
                raise HTTPException(
                    status_code=400,
                    detail="Only PDF documents are supported",
                )

            # Decode and save PDF file
            doc_id = uuid.uuid4()
            stored_filename = f"{doc_id}.pdf"
            file_path = UPLOAD_DIR / stored_filename

            try:
                pdf_content = base64.b64decode(request.document_data)
                file_size = len(pdf_content)

                # Check file size limit
                max_size_bytes = settings.kb_max_file_size_mb * 1024 * 1024
                if file_size > max_size_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Document too large ({file_size / 1024 / 1024:.1f} MB). Maximum: {settings.kb_max_file_size_mb} MB",
                    )

                with open(file_path, "wb") as f:
                    f.write(pdf_content)

                logger.info(f"Saved conversation PDF to {file_path} ({file_size / 1024:.1f} KB)")

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error saving document: {str(e)}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to save document")

            # Create database record with conversation scope
            expires_at = datetime.utcnow() + timedelta(hours=settings.conversation_pdf_ttl_hours)
            document = KnowledgeBaseDocument(
                id=doc_id,
                filename=stored_filename,
                original_filename=request.document_filename,
                file_size_bytes=file_size,
                mime_type=request.document_mimetype,
                status="pending",
                whatsapp_jid=request.whatsapp_jid,
                expires_at=expires_at,
                is_conversation_scoped=True,
                whatsapp_message_id=request.whatsapp_message_id,
            )
            db.add(document)
            db.commit()
            db.refresh(document)

            logger.info(f"Created conversation-scoped document {doc_id} (expires: {expires_at})")

            # Add document info to job data for processing
            job_data["has_document"] = "true"
            job_data["document_id"] = str(doc_id)
            job_data["document_path"] = str(file_path)
            job_data["document_filename"] = request.document_filename

        await add_message_to_stream(
            redis=redis_client,
            user_id=str(user.id),
            job_data=job_data,
        )

        logger.info(
            f"Job {job_id} added to stream for user {user.id} (has_image={has_image}, has_document={has_document})"
        )

        return EnqueueResponse(job_id=job_id, status="queued", message="Job queued successfully")

    except Exception as e:
        logger.error(f"Error enqueueing chat: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/chat/job/{job_id}", response_model=JobStatusResponse, tags=["Chat"])
async def get_job_status(job_id: str):
    """
    Get the status and accumulated chunks for a job

    Poll this endpoint to check job status and retrieve accumulated response chunks.
    Suitable for clients that prefer polling over streaming.

    **Parameters:**
    - `job_id`: Job identifier from `/chat/enqueue`

    **Response:**
    - `job_id`: Job identifier
    - `status`: 'queued', 'in_progress', or 'complete'
    - `chunks`: Array of response chunks (index, content, timestamp)
    - `total_chunks`: Total number of chunks available
    - `complete`: Boolean indicating if job is finished
    - `full_response`: (Only when complete) Complete assembled response
    """
    try:
        redis_client = await get_redis_client()

        # Infer status from Redis data (no arq)
        status = await get_stream_job_status(redis_client, job_id)

        # Get chunks
        chunks = await get_job_chunks(redis_client, job_id)
        total_chunks = len(chunks)

        # Build response
        response = JobStatusResponse(
            job_id=job_id,
            status=status,
            chunks=[ChunkData(**chunk) for chunk in chunks],
            total_chunks=total_chunks,
            complete=(status == "complete"),
        )

        # If complete, assemble full response
        if status == "complete" and chunks:
            response.full_response = "".join(chunk["content"] for chunk in chunks)

        await redis_client.close()
        return response

    except Exception as e:
        logger.error(f"Error getting job status for {job_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Non-streaming chat endpoint

    Alternative to `/chat/stream` that returns the complete response in a single JSON payload.

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - `response`: Complete AI-generated response text
    """
    logger.info(f"Received chat request from {request.whatsapp_jid}")

    try:
        # Get conversation history with type-specific limit
        history = get_conversation_history(db, request.whatsapp_jid, request.conversation_type)
        message_history = format_message_history(history) if history else None

        # Format message with sender name if provided (group message)
        content = (
            f"{request.sender_name}: {request.message}" if request.sender_name else request.message
        )

        # Generate embedding for user message using embedding service
        user_embedding = None
        embedding_service_for_save = create_embedding_service(settings.gemini_api_key)
        if embedding_service_for_save:
            try:
                user_embedding = await embedding_service_for_save.generate(content)
                if user_embedding:
                    logger.info("Generated embedding for user message")
                else:
                    logger.warning("Failed to generate embedding (graceful degradation)")
            except Exception as e:
                logger.error(f"Embedding generation error (continuing anyway): {str(e)}")

        # Save user message with group context and embedding
        save_message(
            db,
            request.whatsapp_jid,
            "user",
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding,
        )

        # Prepare agent dependencies for semantic search tool (dependency injection)
        user = get_or_create_user(db, request.whatsapp_jid, request.conversation_type)

        # Initialize embedding service following Pydantic AI best practices
        embedding_service = create_embedding_service(settings.gemini_api_key)

        # Initialize HTTP client and WhatsApp client for agent tools
        async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http_client:
            whatsapp_client = create_whatsapp_client(
                http_client=http_client,
                base_url=settings.whatsapp_client_url,
            )

            agent_deps = AgentDeps(
                db=db,
                user_id=str(user.id),
                whatsapp_jid=request.whatsapp_jid,
                recent_message_ids=[str(msg.id) for msg in history] if history else [],
                embedding_service=embedding_service,
                http_client=http_client,
                whatsapp_client=whatsapp_client,
                current_message_id=request.whatsapp_message_id,
            )

            # Get AI response (using formatted content) - consume stream into complete response
            ai_response = ""
            async for token in get_ai_response(content, message_history, agent_deps=agent_deps):
                ai_response += token

        # Generate embedding for assistant response using embedding service
        assistant_embedding = None
        if embedding_service_for_save:
            try:
                assistant_embedding = await embedding_service_for_save.generate(ai_response)
            except Exception as e:
                logger.error(f"Error generating assistant embedding: {str(e)}")

        # Save assistant response (no sender info for bot) with embedding
        save_message(
            db,
            request.whatsapp_jid,
            "assistant",
            ai_response,
            request.conversation_type,
            embedding=assistant_embedding,
        )

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f"Error processing chat: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/transcribe", response_model=TranscribeResponse, tags=["Speech-to-Text"])
async def transcribe_audio_endpoint(
    file: UploadFile = File(...),
    language: str | None = Form(None),
    whatsapp_jid: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """
    Transcribe audio to text using Groq Whisper API

    This endpoint ONLY does audio-to-text transcription. It does NOT:
    - Save messages to database
    - Call the AI agent
    - Generate embeddings
    - Process conversation history

    The client should call /chat/enqueue with the transcribed text for AI processing.

    **Request (multipart/form-data):**
    - `file`: Audio file (mp3, wav, ogg, m4a, webm, flac, etc.)
    - `language`: (Optional) ISO-639-1 language code (e.g., 'en', 'es') for better accuracy
    - `whatsapp_jid`: (Optional) JID to fetch user's language preferences

    **Response:**
    - `transcription`: Transcribed text
    - `message`: Status message

    **Supported Formats:** mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, flac
    **Maximum File Size:** 25 MB

    **Example:**
    ```bash
    curl -X POST http://localhost:8000/transcribe \\
      -F "file=@audio.mp3" \\
      -F "language=en"
    ```
    """
    logger.info("Received audio transcription request")

    try:
        # Step 1: Read and validate audio file
        audio_content = await file.read()
        file_size = len(audio_content)

        is_valid, error_msg, file_format = validate_audio_file(
            file.filename or "unknown", file.content_type, file_size
        )

        if not is_valid:
            logger.warning(f"Invalid audio file: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info(
            f"Audio validated: {file.filename} ({file_size / 1024:.1f} KB, format: {file_format})"
        )

        # Step 2: Determine language from preferences if not provided
        effective_language = language
        if whatsapp_jid and not language:
            prefs = get_user_preferences(db, whatsapp_jid)
            if prefs and prefs.stt_language:
                effective_language = prefs.stt_language
                logger.info(f"Using STT language from preferences: {effective_language}")

        # Step 3: Create Groq client
        groq_client = create_groq_client(settings.groq_api_key)
        if not groq_client:
            raise HTTPException(
                status_code=503,
                detail="Speech-to-text service not configured. Please set GROQ_API_KEY environment variable.",
            )

        # Step 4: Transcribe audio
        audio_file_obj = BytesIO(audio_content)
        transcription_text, transcription_error = await transcribe_audio(
            groq_client,
            audio_file_obj,
            file.filename or f"audio.{file_format}",
            language=effective_language,
        )

        if transcription_error:
            raise HTTPException(status_code=500, detail=transcription_error)

        logger.info(
            f'Transcription successful: "{transcription_text[:100]}..." ({len(transcription_text)} chars)'
        )

        # Step 5: Return transcription ONLY
        return TranscribeResponse(
            transcription=transcription_text, message="Audio transcribed successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing audio transcription: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/tts", tags=["Text-to-Speech"])
async def text_to_speech_endpoint(request: TTSRequest, db: Session = Depends(get_db)):
    """
    Convert text to speech using Gemini TTS API

    This endpoint converts text to audio using Google's Gemini TTS model.
    Returns audio in the requested format (default: OGG/Opus for WhatsApp compatibility).

    **Request Body:**
    - `text`: Text to convert to speech (max 5000 characters)
    - `whatsapp_jid`: (Optional) JID to fetch user's language preferences
    - `format`: Output format - 'ogg' (default), 'mp3', 'wav', or 'flac'

    **Response:**
    - Audio file in requested format

    **Configuration:**
    - Voice: Based on user's language preference (default: Kore/English)
    - Format: Configurable (OGG/Opus default for WhatsApp voice notes)

    **Example:**
    ```bash
    curl -X POST http://localhost:8000/tts \\
      -H "Content-Type: application/json" \\
      -d '{"text": "Hello!", "format": "mp3"}' \\
      --output speech.mp3
    ```
    """
    logger.info("Received TTS request")

    try:
        # Step 1: Validate text input
        is_valid, error_msg = validate_text_input(request.text)
        if not is_valid:
            logger.warning(f"Invalid TTS input: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info(f"Text validated: {len(request.text)} characters")

        # Step 2: Determine voice based on user preferences
        voice = settings.tts_default_voice
        if request.whatsapp_jid:
            prefs = get_user_preferences(db, request.whatsapp_jid)
            if prefs:
                voice = get_voice_for_language(prefs.tts_language)
                logger.info(f"Using voice '{voice}' for language '{prefs.tts_language}'")

        # Step 3: Create Gemini client
        genai_client = create_genai_client(settings.gemini_api_key)
        if not genai_client:
            raise HTTPException(
                status_code=503,
                detail="Text-to-speech service not configured. Please set GEMINI_API_KEY environment variable.",
            )

        # Step 4: Synthesize speech with selected voice
        pcm_data, synthesis_error = await synthesize_speech(genai_client, request.text, voice)

        if synthesis_error:
            raise HTTPException(status_code=500, detail=synthesis_error)

        # Step 5: Convert PCM to requested format
        output_format = request.format
        audio_data = pcm_to_audio(pcm_data, output_format)
        mimetype = get_audio_mimetype(output_format)

        logger.info(
            f"TTS successful: {len(audio_data)} bytes {output_format.upper()} audio generated"
        )

        # Step 6: Return audio file
        return Response(
            content=audio_data,
            media_type=mimetype,
            headers={"Content-Disposition": f"attachment; filename=speech.{output_format}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing TTS request: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/preferences/{whatsapp_jid}", response_model=PreferencesResponse, tags=["Preferences"])
async def get_preferences_endpoint(whatsapp_jid: str, db: Session = Depends(get_db)):
    """
    Get user preferences by WhatsApp JID

    Returns the current preferences for a user. Creates default preferences if none exist.

    **Path Parameters:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "1234567890@s.whatsapp.net")

    **Response:**
    - `tts_enabled`: Whether TTS is enabled
    - `tts_language`: TTS language code (e.g., 'en', 'es')
    - `stt_language`: STT language code, null for auto-detect
    """
    logger.info(f"Getting preferences for {whatsapp_jid}")

    try:
        prefs = get_user_preferences(db, whatsapp_jid)
        if not prefs:
            raise HTTPException(status_code=404, detail="User not found")

        return PreferencesResponse(
            tts_enabled=prefs.tts_enabled,
            tts_language=prefs.tts_language,
            stt_language=prefs.stt_language,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting preferences: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.patch("/preferences/{whatsapp_jid}", response_model=PreferencesResponse, tags=["Preferences"])
async def update_preferences_endpoint(
    whatsapp_jid: str, request: UpdatePreferencesRequest, db: Session = Depends(get_db)
):
    """
    Update user preferences

    Updates specific preference fields. Only provided fields are updated.

    **Path Parameters:**
    - `whatsapp_jid`: WhatsApp JID

    **Request Body (all fields optional):**
    - `tts_enabled`: Enable/disable TTS
    - `tts_language`: TTS language code (en, es, pt, fr, de)
    - `stt_language`: STT language code, or "auto" for auto-detect

    **Response:**
    - Updated preferences
    """
    logger.info(f"Updating preferences for {whatsapp_jid}")

    try:
        prefs = get_user_preferences(db, whatsapp_jid)
        if not prefs:
            raise HTTPException(status_code=404, detail="User not found")

        # Update only provided fields
        if request.tts_enabled is not None:
            prefs.tts_enabled = request.tts_enabled

        if request.tts_language is not None:
            # Validate language code
            supported = {"en", "es", "pt", "fr", "de"}
            if request.tts_language not in supported:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid TTS language. Supported: {', '.join(sorted(supported))}",
                )
            prefs.tts_language = request.tts_language

        if request.stt_language is not None:
            # Handle "auto" as null
            if request.stt_language.lower() == "auto":
                prefs.stt_language = None
            else:
                supported = {"en", "es", "pt", "fr", "de"}
                if request.stt_language not in supported:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid STT language. Supported: {', '.join(sorted(supported))}, auto",
                    )
                prefs.stt_language = request.stt_language

        db.commit()
        db.refresh(prefs)

        logger.info(f"Preferences updated for {whatsapp_jid}")

        return PreferencesResponse(
            tts_enabled=prefs.tts_enabled,
            tts_language=prefs.tts_language,
            stt_language=prefs.stt_language,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating preferences: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
