import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..agent import AgentDeps, format_message_history, get_ai_response
from ..commands import is_command, parse_and_execute
from ..config import settings
from ..database import (
    get_conversation_history,
    get_db,
    get_or_create_user,
    save_message,
)
from ..deps import UPLOAD_DIR, limiter
from ..embeddings import create_embedding_service
from ..kb_models import KnowledgeBaseDocument
from ..logger import logger
from ..queue.connection import get_redis_client
from ..queue.schemas import ChunkData, EnqueueResponse, JobStatusResponse
from ..queue.utils import get_job_chunks, get_job_metadata, save_job_image
from ..schemas import (
    ChatRequest,
    ChatResponse,
    CommandResponse,
    SaveMessageRequest,
)
from ..streams.manager import add_message_to_stream
from ..whatsapp import create_whatsapp_client

router = APIRouter()


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


@router.post("/chat/save", tags=["Chat"])
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
            phone=request.phone,
            whatsapp_lid=request.whatsapp_lid,
        )

        return {"success": True}

    except Exception as e:
        logger.error(f"Error saving message: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post(
    "/chat/enqueue",
    response_model=EnqueueResponse | CommandResponse,
    tags=["Chat"],
)
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def enqueue_chat(request: Request, chat_request: ChatRequest, db: Session = Depends(get_db)):
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
    has_image = chat_request.image_data is not None and chat_request.image_mimetype is not None
    logger.info(
        f"Received request from {chat_request.whatsapp_jid}: {chat_request.message[:50]}... (has_image={has_image})"
    )

    # Check for commands first (e.g., /settings, /tts on, /help)
    if is_command(chat_request.message):
        user = get_or_create_user(
            db,
            chat_request.whatsapp_jid,
            chat_request.conversation_type,
            phone=chat_request.phone,
            whatsapp_lid=chat_request.whatsapp_lid,
        )
        result = parse_and_execute(
            db,
            str(user.id),
            chat_request.whatsapp_jid,
            chat_request.message,
            conversation_type=chat_request.conversation_type,
            is_group_admin=chat_request.is_group_admin,
        )
        if result.is_command:
            logger.info(f"Command executed for {chat_request.whatsapp_jid}: {chat_request.message}")
            return CommandResponse(is_command=True, response=result.response_text)

    try:
        # Check for document
        has_document = (
            chat_request.document_data is not None
            and chat_request.document_mimetype is not None
            and chat_request.document_filename is not None
        )

        # For image messages, store with [Image] marker for history context
        if has_image:
            # Store as [Image: caption] or [Image] for database history
            content = f"[Image: {chat_request.message}]" if chat_request.message else "[Image]"
            if chat_request.sender_name:
                content = f"{chat_request.sender_name}: {content}"
        elif has_document:
            # Store as [Document: filename] for database history
            content = f"[Document: {chat_request.document_filename}]"
            if chat_request.message:
                content = f"{content} - {chat_request.message}"
            if chat_request.sender_name:
                content = f"{chat_request.sender_name}: {content}"
        else:
            # Format message with sender name if provided (group message)
            content = (
                f"{chat_request.sender_name}: {chat_request.message}"
                if chat_request.sender_name
                else chat_request.message
            )

        # Get or create user
        user = get_or_create_user(
            db,
            chat_request.whatsapp_jid,
            chat_request.conversation_type,
            phone=chat_request.phone,
            whatsapp_lid=chat_request.whatsapp_lid,
        )

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
            chat_request.whatsapp_jid,
            "user",
            content,
            chat_request.conversation_type,
            sender_jid=chat_request.sender_jid,
            sender_name=chat_request.sender_name,
            embedding=user_embedding,
            phone=chat_request.phone,
            whatsapp_lid=chat_request.whatsapp_lid,
        )

        # Add message to user's Redis Stream for sequential processing
        async with get_redis_client() as redis_client:
            job_id = str(uuid.uuid4())

            # Build job data with optional whatsapp_message_id
            job_data = {
                "job_id": job_id,
                "user_id": str(user.id),
                "whatsapp_jid": chat_request.whatsapp_jid,
                "message": chat_request.message,  # Original message/caption for AI processing
                "conversation_type": chat_request.conversation_type,
                "user_message_id": str(user_msg.id),
            }
            if chat_request.whatsapp_message_id:
                job_data["whatsapp_message_id"] = chat_request.whatsapp_message_id
            if chat_request.sender_name:
                job_data["sender_name"] = chat_request.sender_name

            # Handle image data if present
            if has_image:
                # Store image in Redis separately (to avoid large stream messages)
                await save_job_image(redis_client, job_id, chat_request.image_data)
                job_data["image_mimetype"] = chat_request.image_mimetype
                job_data["has_image"] = "true"

            if has_document:
                # Only support PDFs for now
                if chat_request.document_mimetype != "application/pdf":
                    raise HTTPException(
                        status_code=400,
                        detail="Only PDF documents are supported",
                    )

                # Decode and save PDF file
                doc_id = uuid.uuid4()
                stored_filename = f"{doc_id}.pdf"
                file_path = UPLOAD_DIR / stored_filename

                try:
                    pdf_content = base64.b64decode(chat_request.document_data)
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

                    logger.info(
                        f"Saved conversation PDF to {file_path} ({file_size / 1024:.1f} KB)"
                    )

                except HTTPException:
                    raise
                except Exception as e:
                    logger.error(f"Error saving document: {str(e)}", exc_info=True)
                    raise HTTPException(status_code=500, detail="Failed to save document")

                # Create database record with conversation scope
                expires_at = datetime.now(UTC) + timedelta(
                    hours=settings.conversation_pdf_ttl_hours
                )
                document = KnowledgeBaseDocument(
                    id=doc_id,
                    filename=stored_filename,
                    original_filename=chat_request.document_filename,
                    file_size_bytes=file_size,
                    mime_type=chat_request.document_mimetype,
                    status="pending",
                    whatsapp_jid=chat_request.whatsapp_jid,
                    expires_at=expires_at,
                    is_conversation_scoped=True,
                    whatsapp_message_id=chat_request.whatsapp_message_id,
                )
                db.add(document)
                db.commit()
                db.refresh(document)

                logger.info(
                    f"Created conversation-scoped document {doc_id} (expires: {expires_at})"
                )

                # Add document info to job data for processing
                job_data["has_document"] = "true"
                job_data["document_id"] = str(doc_id)
                job_data["document_path"] = str(file_path)
                job_data["document_filename"] = chat_request.document_filename

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


@router.get("/chat/job/{job_id}", response_model=JobStatusResponse, tags=["Chat"])
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
        async with get_redis_client() as redis_client:
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

            return response

    except Exception as e:
        logger.error(f"Error getting job status for {job_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat", response_model=ChatResponse, tags=["Chat"])
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def chat(request: Request, chat_request: ChatRequest, db: Session = Depends(get_db)):
    """
    Non-streaming chat endpoint

    Alternative to `/chat/stream` that returns the complete response in a single JSON payload.

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - `response`: Complete AI-generated response text
    """
    logger.info(f"Received chat request from {chat_request.whatsapp_jid}")

    try:
        # Get conversation history with type-specific limit
        history = get_conversation_history(
            db, chat_request.whatsapp_jid, chat_request.conversation_type
        )
        message_history = format_message_history(history) if history else None

        # Format message with sender name if provided (group message)
        content = (
            f"{chat_request.sender_name}: {chat_request.message}"
            if chat_request.sender_name
            else chat_request.message
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
            chat_request.whatsapp_jid,
            "user",
            content,
            chat_request.conversation_type,
            sender_jid=chat_request.sender_jid,
            sender_name=chat_request.sender_name,
            embedding=user_embedding,
            phone=chat_request.phone,
            whatsapp_lid=chat_request.whatsapp_lid,
        )

        # Prepare agent dependencies for semantic search tool (dependency injection)
        user = get_or_create_user(
            db,
            chat_request.whatsapp_jid,
            chat_request.conversation_type,
            phone=chat_request.phone,
            whatsapp_lid=chat_request.whatsapp_lid,
        )

        # Initialize embedding service following Pydantic AI best practices
        embedding_service = create_embedding_service(settings.gemini_api_key)

        # Initialize HTTP client and WhatsApp client for agent tools
        async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http_client:
            whatsapp_client = create_whatsapp_client(
                http_client=http_client,
                base_url=settings.whatsapp_client_url,
                api_key=settings.whatsapp_api_key,
            )

            agent_deps = AgentDeps(
                db=db,
                user_id=str(user.id),
                whatsapp_jid=chat_request.whatsapp_jid,
                recent_message_ids=[str(msg.id) for msg in history] if history else [],
                embedding_service=embedding_service,
                http_client=http_client,
                whatsapp_client=whatsapp_client,
                current_message_id=chat_request.whatsapp_message_id,
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
            chat_request.whatsapp_jid,
            "assistant",
            ai_response,
            chat_request.conversation_type,
            embedding=assistant_embedding,
        )

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f"Error processing chat: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
