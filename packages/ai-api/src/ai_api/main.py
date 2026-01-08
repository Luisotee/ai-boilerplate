import os
import json
import uuid
import asyncio
from pathlib import Path
from io import BytesIO
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, UploadFile, File, Form
from typing import List, Optional
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from dotenv import load_dotenv

# Load environment variables BEFORE importing local modules
load_dotenv()

from .logger import logger
from .database import init_db, get_db, get_conversation_history, save_message, get_or_create_user
from .schemas import ChatRequest, ChatResponse, SaveMessageRequest, UploadPDFResponse, BatchUploadResponse, FileUploadResult, TranscribeResponse
from .agent import get_ai_response, format_message_history, AgentDeps
from .embeddings import create_embedding_service
from .transcription import create_groq_client, transcribe_audio, validate_audio_file
from .kb_models import KnowledgeBaseDocument
from .processing import process_pdf_document
from .queue.connection import get_arq_redis, close_arq_redis, get_redis_client
from .queue.schemas import EnqueueResponse, JobStatusResponse, ChunkData
from .queue.utils import get_job_chunks, get_job_metadata
from arq.jobs import Job
from .streams.manager import add_message_to_stream

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and Redis on startup"""
    logger.info('Starting AI API service...')

    # Initialize PostgreSQL
    init_db()

    # Initialize Redis connection pool
    try:
        arq_redis = await get_arq_redis()
        logger.info('✅ Redis connection pool initialized')
    except Exception as e:
        logger.error(f'❌ Failed to initialize Redis: {e}')
        raise

    logger.info('=' * 60)
    logger.info('AI API is ready!')
    logger.info('=' * 60)
    logger.info('📚 API Documentation:')
    logger.info('   Swagger UI: http://localhost:8000/docs')
    logger.info('   ReDoc:      http://localhost:8000/redoc')
    logger.info('   OpenAPI:    http://localhost:8000/openapi.json')
    logger.info('=' * 60)
    logger.info('🏥 Health Check: http://localhost:8000/health')
    logger.info('=' * 60)

    yield

    # Cleanup on shutdown
    logger.info('Shutting down AI API service...')
    await close_arq_redis()
    logger.info('✅ Redis connection pool closed')

app = FastAPI(
    title='AI WhatsApp Agent API',
    version='1.0.0',
    description='''
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
    ''',
    lifespan=lifespan
)

# Configure upload directory for knowledge base PDFs
UPLOAD_DIR = Path(os.getenv('KB_UPLOAD_DIR', '/tmp/knowledge_base'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f'Knowledge base upload directory: {UPLOAD_DIR}')


# Helper function for Redis Streams job status inference
async def get_stream_job_status(redis: 'Redis', job_id: str) -> str:
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
        return 'complete'

    # Check if chunks exist (job in progress)
    chunks = await get_job_chunks(redis, job_id)
    if chunks:
        return 'in_progress'

    # No chunks or metadata (job queued or not found)
    return 'queued'


@app.get('/health', tags=['Health'])
async def health_check():
    """
    Health check endpoint

    Returns the service health status.
    """
    return {'status': 'healthy'}

@app.post('/knowledge-base/upload', response_model=UploadPDFResponse, tags=['Knowledge Base'])
async def upload_pdf(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db)
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
    logger.info(f'Received PDF upload: {file.filename}')

    # Validate file type
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail='Only PDF files are supported')

    if not file.content_type or file.content_type not in ['application/pdf', 'application/x-pdf']:
        logger.warning(f'Unexpected content type: {file.content_type}, but filename ends with .pdf')

    # Generate unique document ID and filename
    doc_id = uuid.uuid4()
    stored_filename = f"{doc_id}.pdf"
    file_path = UPLOAD_DIR / stored_filename

    # Read and save uploaded file
    try:
        content = await file.read()
        file_size = len(content)

        # Check file size limit
        max_size_mb = int(os.getenv('KB_MAX_FILE_SIZE_MB', '50'))
        max_size_bytes = max_size_mb * 1024 * 1024

        if file_size > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f'File too large ({file_size / 1024 / 1024:.1f} MB). Maximum size: {max_size_mb} MB'
            )

        with open(file_path, 'wb') as f:
            f.write(content)

        logger.info(f'Saved PDF to {file_path} ({file_size / 1024:.1f} KB)')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error saving file: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Failed to save file')

    # Create database record
    try:
        document = KnowledgeBaseDocument(
            id=doc_id,
            filename=stored_filename,
            original_filename=file.filename,
            file_size_bytes=file_size,
            mime_type=file.content_type or 'application/pdf',
            status='pending'
        )
        db.add(document)
        db.commit()
        db.refresh(document)

        logger.info(f'Created database record for document {doc_id}')

    except Exception as e:
        logger.error(f'Error creating database record: {str(e)}', exc_info=True)
        # Clean up uploaded file
        if file_path.exists():
            file_path.unlink()
        raise HTTPException(status_code=500, detail='Failed to create database record')

    # Schedule background processing
    background_tasks.add_task(
        process_pdf_document,
        document_id=str(doc_id),
        file_path=str(file_path)
    )

    logger.info(f'Scheduled background processing for document {doc_id}')

    return UploadPDFResponse(
        document_id=str(doc_id),
        filename=file.filename,
        status='pending',
        message='PDF uploaded successfully. Processing in background.'
    )

@app.post('/knowledge-base/upload/batch', response_model=BatchUploadResponse, tags=['Knowledge Base'])
async def upload_pdf_batch(
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db)
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
    logger.info(f'Received batch PDF upload: {len(files)} files')

    # Read configuration
    max_file_size_mb = int(os.getenv('KB_MAX_FILE_SIZE_MB', '50'))
    max_batch_size_mb = int(os.getenv('KB_MAX_BATCH_SIZE_MB', '500'))
    max_file_size_bytes = max_file_size_mb * 1024 * 1024
    max_batch_size_bytes = max_batch_size_mb * 1024 * 1024

    # Check if any files provided
    if len(files) == 0:
        logger.warning('Batch upload with no files')
        return BatchUploadResponse(
            total_files=0,
            accepted=0,
            rejected=0,
            results=[],
            message='No files provided'
        )

    # Phase 1: Validate each file independently
    file_validations = []
    total_size = 0

    for file in files:
        error = None
        file_content = None
        file_size = 0
        filename = file.filename or 'unknown'

        # Validate filename
        if not file.filename:
            error = 'Missing filename'
        elif not file.filename.endswith('.pdf'):
            error = 'Only PDF files are supported'

        # Validate content type
        if not error and file.content_type:
            if file.content_type not in ['application/pdf', 'application/x-pdf']:
                logger.warning(f'Unexpected content type: {file.content_type} for {filename}')

        # Read file and check size
        if not error:
            try:
                file_content = await file.read()
                file_size = len(file_content)

                if file_size == 0:
                    error = 'Empty file'
                elif file_size > max_file_size_bytes:
                    error = f'File too large ({file_size / 1024 / 1024:.1f} MB). Maximum: {max_file_size_mb} MB'

            except Exception as e:
                logger.error(f'Error reading file {filename}: {str(e)}', exc_info=True)
                error = 'Failed to read file'

        file_validations.append({
            'file': file,
            'filename': filename,
            'size': file_size,
            'error': error,
            'content': file_content
        })

        total_size += file_size

    # Check total batch size
    if total_size > max_batch_size_bytes:
        logger.warning(f'Batch too large: {total_size / 1024 / 1024:.1f} MB > {max_batch_size_mb} MB')
        # Reject all files that exceed the remaining batch size
        running_total = 0
        for validation in file_validations:
            if validation['error'] is None:
                running_total += validation['size']
                if running_total > max_batch_size_bytes:
                    validation['error'] = f'Batch size limit exceeded. Total: {total_size / 1024 / 1024:.1f} MB, Maximum: {max_batch_size_mb} MB'

    # Phase 2: Process valid files and build results
    results = []
    accepted_count = 0
    rejected_count = 0

    for validation in file_validations:
        filename = validation['filename']
        error = validation['error']

        # If file has validation error, add to rejected results
        if error:
            results.append(FileUploadResult(
                filename=filename,
                status='rejected',
                error=error
            ))
            rejected_count += 1
            logger.info(f'Rejected file: {filename} - {error}')
            continue

        # File is valid, save it
        try:
            file = validation['file']
            content = validation['content']
            file_size = validation['size']

            # Generate unique document ID and filename
            doc_id = uuid.uuid4()
            stored_filename = f"{doc_id}.pdf"
            file_path = UPLOAD_DIR / stored_filename

            # Save file to disk
            with open(file_path, 'wb') as f:
                f.write(content)

            logger.info(f'Saved PDF to {file_path} ({file_size / 1024:.1f} KB)')

            # Create database record
            document = KnowledgeBaseDocument(
                id=doc_id,
                filename=stored_filename,
                original_filename=filename,
                file_size_bytes=file_size,
                mime_type=file.content_type or 'application/pdf',
                status='pending'
            )
            db.add(document)
            db.commit()
            db.refresh(document)

            logger.info(f'Created database record for document {doc_id}')

            # Schedule background processing
            background_tasks.add_task(
                process_pdf_document,
                document_id=str(doc_id),
                file_path=str(file_path)
            )

            logger.info(f'Scheduled processing for {filename} ({doc_id})')

            # Add to accepted results
            results.append(FileUploadResult(
                filename=filename,
                status='accepted',
                document_id=str(doc_id),
                message='Queued for processing'
            ))
            accepted_count += 1

        except Exception as e:
            logger.error(f'Error saving file {filename}: {str(e)}', exc_info=True)

            # Add to rejected results
            results.append(FileUploadResult(
                filename=filename,
                status='rejected',
                error=f'Failed to save file: {str(e)}'
            ))
            rejected_count += 1

            # Clean up file if it was saved
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception as cleanup_error:
                logger.error(f'Cleanup error for {filename}: {str(cleanup_error)}')

    logger.info(f'Batch upload complete: {accepted_count} accepted, {rejected_count} rejected')

    # Build response message
    if accepted_count == 0:
        message = f'All {rejected_count} files were rejected'
    elif rejected_count == 0:
        message = f'Successfully queued {accepted_count} files for processing'
    else:
        message = f'Processed {len(files)} files: {accepted_count} accepted, {rejected_count} rejected'

    return BatchUploadResponse(
        total_files=len(files),
        accepted=accepted_count,
        rejected=rejected_count,
        results=results,
        message=message
    )

@app.get('/knowledge-base/status/{document_id}', tags=['Knowledge Base'])
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
        document = db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.id == document_id
        ).first()

        if not document:
            raise HTTPException(status_code=404, detail='Document not found')

        return {
            'id': str(document.id),
            'original_filename': document.original_filename,
            'status': document.status,
            'chunk_count': document.chunk_count,
            'error_message': document.error_message,
            'upload_date': document.upload_date,
            'processed_date': document.processed_date,
            'file_size_bytes': document.file_size_bytes,
            'doc_metadata': document.doc_metadata
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error retrieving document status: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.get('/knowledge-base/documents', tags=['Knowledge Base'])
async def list_documents(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
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
            valid_statuses = ['pending', 'processing', 'completed', 'failed']
            if status not in valid_statuses:
                raise HTTPException(
                    status_code=400,
                    detail=f'Invalid status. Must be one of: {", ".join(valid_statuses)}'
                )
            query = query.filter(KnowledgeBaseDocument.status == status)

        # Get total count
        total = query.count()

        # Apply pagination and ordering
        documents = query.order_by(
            KnowledgeBaseDocument.upload_date.desc()
        ).limit(limit).offset(offset).all()

        # Format response
        return {
            'documents': [
                {
                    'id': str(doc.id),
                    'original_filename': doc.original_filename,
                    'status': doc.status,
                    'chunk_count': doc.chunk_count,
                    'file_size_bytes': doc.file_size_bytes,
                    'upload_date': doc.upload_date,
                    'processed_date': doc.processed_date,
                    'error_message': doc.error_message,
                    'doc_metadata': doc.doc_metadata
                }
                for doc in documents
            ],
            'total': total,
            'limit': limit,
            'offset': offset
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error listing documents: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.delete('/knowledge-base/documents/{document_id}', tags=['Knowledge Base'])
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
        document = db.query(KnowledgeBaseDocument).filter(
            KnowledgeBaseDocument.id == document_id
        ).first()

        if not document:
            raise HTTPException(status_code=404, detail='Document not found')

        # Get chunk count before deletion
        chunk_count = document.chunk_count

        # Delete file from disk
        file_path = UPLOAD_DIR / document.filename
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f'Deleted file: {file_path}')
            except Exception as e:
                logger.warning(f'Failed to delete file {file_path}: {str(e)}')
                # Continue with database deletion even if file deletion fails

        # Delete from database (cascades to chunks)
        db.delete(document)
        db.commit()

        logger.info(f'Deleted document {document_id} with {chunk_count} chunks')

        return {
            'success': True,
            'message': f'Document "{document.original_filename}" deleted successfully',
            'deleted_chunks': chunk_count
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error deleting document: {str(e)}', exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/chat/save', tags=['Chat'])
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
    logger.info(f'Saving message from {request.whatsapp_jid} (no response)')

    try:
        # Format message with sender name if group message
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Generate embedding for message using embedding service
        user_embedding = None
        embedding_service = create_embedding_service(os.getenv("GEMINI_API_KEY"))
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
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding
        )

        return {'success': True}

    except Exception as e:
        logger.error(f'Error saving message: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/chat/enqueue', response_model=EnqueueResponse, tags=['Chat'])
async def enqueue_chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Enqueue a chat message for asynchronous processing

    This endpoint accepts a message, saves it immediately to the database,
    and adds it to a Redis Stream for processing. Returns a job ID
    that can be used to poll for status or stream results.

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text
    - `conversation_type`: 'private' or 'group'
    - `sender_jid`: (Optional) Sender's JID for group messages
    - `sender_name`: (Optional) Sender's name for group messages

    **Response:**
    - `job_id`: Unique identifier for tracking this job
    - `status`: 'queued'
    - `message`: Success message

    **Next Steps:**
    - Poll `/chat/job/{job_id}` for status and accumulated chunks
    - Or stream from `/chat/stream/{job_id}` via SSE
    """
    logger.info(f'Enqueueing chat request from {request.whatsapp_jid}')

    try:
        # Format message with sender name if provided (group message)
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Get or create user
        user = get_or_create_user(db, request.whatsapp_jid, request.conversation_type)

        # Generate embedding for user message
        user_embedding = None
        embedding_service = create_embedding_service(os.getenv("GEMINI_API_KEY"))
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
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding
        )

        # Add message to user's Redis Stream for sequential processing
        redis_client = await get_redis_client()
        job_id = str(uuid.uuid4())

        message_id = await add_message_to_stream(
            redis=redis_client,
            user_id=str(user.id),
            job_data={
                'job_id': job_id,
                'user_id': str(user.id),
                'whatsapp_jid': request.whatsapp_jid,
                'message': content,
                'conversation_type': request.conversation_type,
                'user_message_id': str(user_msg.id),
            }
        )

        logger.info(f'Job {job_id} added to stream for user {user.id}')

        return EnqueueResponse(
            job_id=job_id,
            status='queued',
            message='Job queued successfully'
        )

    except Exception as e:
        logger.error(f'Error enqueueing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')


@app.get('/chat/job/{job_id}', response_model=JobStatusResponse, tags=['Chat'])
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
            complete=(status == 'complete')
        )

        # If complete, assemble full response
        if status == 'complete' and chunks:
            response.full_response = ''.join(chunk['content'] for chunk in chunks)

        await redis_client.close()
        return response

    except Exception as e:
        logger.error(f'Error getting job status for {job_id}: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')


@app.get('/chat/stream/{job_id}', tags=['Chat'])
async def stream_job_chunks(job_id: str):
    """
    Stream job chunks via Server-Sent Events as they arrive

    Connect to this endpoint to receive real-time streaming of response chunks
    as the worker processes them. Uses SSE (Server-Sent Events) protocol.

    **Parameters:**
    - `job_id`: Job identifier from `/chat/enqueue`

    **Response:**
    - Server-Sent Events stream
    - Format: `data: {"index": N, "content": "..."}\n\n`
    - End signal: `data: [DONE]\n\n`
    - Error signal: `data: [ERROR]\n\n`

    **Example:**
    ```javascript
    const eventSource = new EventSource('/chat/stream/' + jobId);
    eventSource.onmessage = (event) => {
      if (event.data === '[DONE]') {
        eventSource.close();
      } else if (event.data === '[ERROR]') {
        eventSource.close();
      } else {
        const chunk = JSON.parse(event.data);
        console.log(chunk.content);
      }
    };
    ```
    """
    async def generate():
        last_index = -1

        try:
            redis_client = await get_redis_client()

            while True:
                # Get new chunks since last poll
                chunks = await get_job_chunks(redis_client, job_id, start_index=last_index + 1)

                # Yield new chunks
                for chunk in chunks:
                    if chunk['index'] > last_index:
                        yield f'data: {json.dumps(chunk)}\n\n'
                        last_index = chunk['index']

                # Check if job is complete (metadata exists)
                metadata = await get_job_metadata(redis_client, job_id)
                if metadata:
                    yield 'data: [DONE]\n\n'
                    break

                # Poll interval (100ms)
                await asyncio.sleep(0.1)

            await redis_client.close()

        except Exception as e:
            logger.error(f'Error streaming job {job_id}: {str(e)}', exc_info=True)
            yield 'data: [ERROR]\n\n'

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )

@app.post('/chat', response_model=ChatResponse, tags=['Chat'])
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
    logger.info(f'Received chat request from {request.whatsapp_jid}')

    try:
        # Get conversation history with type-specific limit
        history = get_conversation_history(
            db,
            request.whatsapp_jid,
            request.conversation_type
        )
        message_history = format_message_history(history) if history else None

        # Format message with sender name if provided (group message)
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Generate embedding for user message using embedding service
        user_embedding = None
        embedding_service_for_save = create_embedding_service(os.getenv("GEMINI_API_KEY"))
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
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name,
            embedding=user_embedding
        )

        # Prepare agent dependencies for semantic search tool (dependency injection)
        user = get_or_create_user(db, request.whatsapp_jid, request.conversation_type)

        # Initialize embedding service following Pydantic AI best practices
        embedding_service = create_embedding_service(os.getenv("GEMINI_API_KEY"))

        agent_deps = AgentDeps(
            db=db,
            user_id=str(user.id),
            whatsapp_jid=request.whatsapp_jid,
            recent_message_ids=[str(msg.id) for msg in history] if history else [],
            embedding_service=embedding_service
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
            'assistant',
            ai_response,
            request.conversation_type,
            embedding=assistant_embedding
        )

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/transcribe', response_model=TranscribeResponse, tags=['Speech-to-Text'])
async def transcribe_audio_endpoint(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None)
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
    logger.info('Received audio transcription request')

    try:
        # Step 1: Read and validate audio file
        audio_content = await file.read()
        file_size = len(audio_content)

        is_valid, error_msg, file_format = validate_audio_file(
            file.filename or 'unknown',
            file.content_type,
            file_size
        )

        if not is_valid:
            logger.warning(f'Invalid audio file: {error_msg}')
            raise HTTPException(status_code=400, detail=error_msg)

        logger.info(f'Audio validated: {file.filename} ({file_size / 1024:.1f} KB, format: {file_format})')

        # Step 2: Create Groq client
        groq_client = create_groq_client(os.getenv('GROQ_API_KEY'))
        if not groq_client:
            raise HTTPException(
                status_code=503,
                detail='Speech-to-text service not configured. Please set GROQ_API_KEY environment variable.'
            )

        # Step 3: Transcribe audio
        audio_file_obj = BytesIO(audio_content)
        transcription_text, transcription_error = await transcribe_audio(
            groq_client,
            audio_file_obj,
            file.filename or f'audio.{file_format}',
            language=language
        )

        if transcription_error:
            raise HTTPException(status_code=500, detail=transcription_error)

        logger.info(f'Transcription successful: "{transcription_text[:100]}..." ({len(transcription_text)} chars)')

        # Step 4: Return transcription ONLY
        return TranscribeResponse(
            transcription=transcription_text,
            message='Audio transcribed successfully'
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error processing audio transcription: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')
