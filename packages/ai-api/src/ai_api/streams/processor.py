"""
Core chat processing logic extracted from arq worker.

This processor can be called directly without arq job context,
making it compatible with Redis Streams.
"""

import httpx
from redis.asyncio import Redis

from ..agent import AgentDeps, format_message_history, get_ai_response
from ..config import settings
from ..database import SessionLocal, get_conversation_history, save_message
from ..embeddings import create_embedding_service
from ..logger import logger
from ..processing import process_pdf_document
from ..queue.connection import get_redis_client
from ..queue.utils import delete_job_image, get_job_image, save_job_chunk, set_job_metadata
from ..whatsapp import WhatsAppClient, create_whatsapp_client


async def process_chat_job_direct(
    user_id: str,
    whatsapp_jid: str,
    message: str,
    conversation_type: str,
    user_message_id: str,
    job_id: str,
    whatsapp_message_id: str | None = None,
    image_mimetype: str | None = None,
    has_image: bool = False,
    has_document: bool = False,
    document_id: str | None = None,
    document_path: str | None = None,
    document_filename: str | None = None,
) -> dict:
    """
    Process a chat message asynchronously without arq context.

    This function:
    1. Retrieves conversation history from PostgreSQL
    2. Initializes embedding service and RAG instances
    3. Streams tokens from Pydantic AI agent (with optional image for vision)
    4. Saves each token chunk to Redis for real-time client polling
    5. Generates embedding for complete response
    6. Saves final assistant message to PostgreSQL
    7. Stores job metadata in Redis

    Args:
        user_id: User's UUID
        whatsapp_jid: WhatsApp JID (conversation identifier)
        message: User's message (already formatted with sender name if group)
        conversation_type: 'private' or 'group'
        user_message_id: UUID of saved user message in PostgreSQL
        job_id: Unique job identifier
        whatsapp_message_id: WhatsApp message ID for reactions (optional)
        image_mimetype: Image MIME type if image is attached (optional)
        has_image: Whether this message has an attached image
        has_document: Whether this message has an attached document (PDF)
        document_id: UUID of the document in knowledge base (optional)
        document_path: Path to the document file (optional)
        document_filename: Original filename of the document (optional)

    Returns:
        Dict with processing result including success status, job_id, chunk count

    Raises:
        Exception: Any error during processing
    """
    logger.info(f"[Job {job_id}] Starting chat processing for user {user_id}")
    logger.info(f"[Job {job_id}] WhatsApp JID: {whatsapp_jid}")
    logger.info(f"[Job {job_id}] Conversation type: {conversation_type}")
    logger.info(f"[Job {job_id}] WhatsApp message ID: {whatsapp_message_id}")
    logger.info(f"[Job {job_id}] Has image: {has_image}")
    logger.info(f"[Job {job_id}] Has document: {has_document}")

    # Get Redis client
    redis: Redis = await get_redis_client()

    db = SessionLocal()
    chunk_index = 0
    full_response = ""
    http_client: httpx.AsyncClient | None = None
    whatsapp_client: WhatsAppClient | None = None

    try:
        # Step 1: Get conversation history from PostgreSQL
        logger.info(f"[Job {job_id}] Fetching conversation history...")
        history = get_conversation_history(db, whatsapp_jid, conversation_type)
        message_history = format_message_history(history) if history else None
        logger.info(
            f"[Job {job_id}] Retrieved {len(history) if history else 0} messages from history"
        )

        # Step 2: Initialize embedding service
        logger.info(f"[Job {job_id}] Initializing embedding service...")
        embedding_service = create_embedding_service(settings.gemini_api_key)

        # Step 2.5: Initialize HTTP client and WhatsApp client
        http_client = httpx.AsyncClient(timeout=settings.whatsapp_client_timeout)
        whatsapp_client = create_whatsapp_client(
            http_client=http_client,
            base_url=settings.whatsapp_client_url,
        )
        logger.info(f"[Job {job_id}] WhatsApp client initialized")

        # Step 2.6: Process document if present
        if has_document and document_id and document_path:
            logger.info(f"[Job {job_id}] Processing document {document_id}")

            # Send processing reaction
            if whatsapp_message_id:
                try:
                    await whatsapp_client.send_reaction(whatsapp_jid, whatsapp_message_id, "⏳")
                    logger.info(f"[Job {job_id}] Sent processing reaction ⏳")
                except Exception as e:
                    logger.warning(f"[Job {job_id}] Failed to send processing reaction: {e}")

            # Process the PDF document
            try:
                await process_pdf_document(
                    document_id=document_id,
                    file_path=document_path,
                    whatsapp_jid=whatsapp_jid,
                )
                logger.info(f"[Job {job_id}] Document processing completed")

                # Send success reaction
                if whatsapp_message_id:
                    try:
                        await whatsapp_client.send_reaction(whatsapp_jid, whatsapp_message_id, "✅")
                        logger.info(f"[Job {job_id}] Sent success reaction ✅")
                    except Exception as e:
                        logger.warning(f"[Job {job_id}] Failed to send success reaction: {e}")

            except Exception as e:
                logger.error(f"[Job {job_id}] Document processing failed: {e}")

                # Send failure reaction
                if whatsapp_message_id:
                    try:
                        await whatsapp_client.send_reaction(whatsapp_jid, whatsapp_message_id, "❌")
                        logger.info(f"[Job {job_id}] Sent failure reaction ❌")
                    except Exception as reaction_error:
                        logger.warning(
                            f"[Job {job_id}] Failed to send failure reaction: {reaction_error}"
                        )

                # Continue to send error response to user
                full_response = f"Sorry, I couldn't process your document '{document_filename}'. Please try uploading it again."

                # Save response and return early
                await save_job_chunk(redis, job_id, 0, full_response)
                save_message(db, whatsapp_jid, "assistant", full_response, conversation_type)
                await set_job_metadata(
                    redis,
                    job_id,
                    {
                        "user_id": user_id,
                        "whatsapp_jid": whatsapp_jid,
                        "message": message,
                        "conversation_type": conversation_type,
                        "total_chunks": 1,
                        "user_message_id": user_message_id,
                        "error": str(e),
                    },
                )

                return {
                    "success": False,
                    "job_id": job_id,
                    "error": str(e),
                }

            # Update message to indicate document was processed
            message = f"I have uploaded a document called '{document_filename}'. Please analyze it and let me know what it contains."

        # Step 3: Prepare agent dependencies
        agent_deps = AgentDeps(
            db=db,
            user_id=user_id,
            whatsapp_jid=whatsapp_jid,
            recent_message_ids=[str(msg.id) for msg in history] if history else [],
            embedding_service=embedding_service,
            http_client=http_client,
            whatsapp_client=whatsapp_client,
            current_message_id=whatsapp_message_id,
        )

        # Step 4: Get AI response (with optional image for vision)
        logger.info(f"[Job {job_id}] Getting AI response...")

        # Retrieve image data if present
        image_data = None
        if has_image:
            image_data = await get_job_image(redis, job_id)
            if image_data:
                logger.info(f"[Job {job_id}] Retrieved image data for vision processing")
            else:
                logger.warning(f"[Job {job_id}] Image flag set but no image data found in Redis")

        async for token in get_ai_response(
            message,
            message_history,
            agent_deps=agent_deps,
            image_data=image_data,
            image_mimetype=image_mimetype,
        ):
            full_response += token

        # Clean up image data from Redis after processing
        if has_image:
            await delete_job_image(redis, job_id)

        # Save complete response as single chunk
        await save_job_chunk(redis, job_id, 0, full_response)
        chunk_index = 1

        logger.info(f"[Job {job_id}] AI response completed.")
        logger.info(f"[Job {job_id}] Full response length: {len(full_response)} characters")

        # Step 5: Generate embedding for complete assistant response
        assistant_embedding = None
        if embedding_service:
            try:
                logger.info(f"[Job {job_id}] Generating embedding for assistant response...")
                assistant_embedding = await embedding_service.generate(full_response)
                logger.info(f"[Job {job_id}] Embedding generated successfully")
            except Exception as e:
                logger.error(f"[Job {job_id}] Error generating assistant embedding: {e}")
                # Continue without embedding - not critical

        # Step 6: Save complete assistant response to PostgreSQL
        logger.info(f"[Job {job_id}] Saving assistant message to database...")
        assistant_msg = save_message(
            db,
            whatsapp_jid,
            "assistant",
            full_response,
            conversation_type,
            embedding=assistant_embedding,
        )
        logger.info(f"[Job {job_id}] Assistant message saved with ID: {assistant_msg.id}")

        # Step 7: Save job metadata to Redis
        await set_job_metadata(
            redis,
            job_id,
            {
                "user_id": user_id,
                "whatsapp_jid": whatsapp_jid,
                "message": message,
                "conversation_type": conversation_type,
                "total_chunks": chunk_index,
                "db_message_id": str(assistant_msg.id),
                "user_message_id": user_message_id,
            },
        )

        logger.info(f"[Job {job_id}] ✅ Completed successfully")

        return {
            "success": True,
            "job_id": job_id,
            "total_chunks": chunk_index,
            "response_length": len(full_response),
            "db_message_id": str(assistant_msg.id),
        }

    except Exception as e:
        logger.error(f"[Job {job_id}] ❌ Error processing chat: {e}", exc_info=True)

        # Save partial response if any
        if full_response:
            logger.info(f"[Job {job_id}] Saving partial response ({len(full_response)} chars)")
            try:
                save_message(
                    db,
                    whatsapp_jid,
                    "assistant",
                    f"[Partial - Error] {full_response}",
                    conversation_type,
                    embedding=None,
                )
            except Exception as save_error:
                logger.error(f"[Job {job_id}] Failed to save partial response: {save_error}")

        # Re-raise exception
        raise

    finally:
        # Close HTTP client
        if http_client:
            await http_client.aclose()
            logger.info(f"[Job {job_id}] HTTP client closed")
        db.close()
        logger.info(f"[Job {job_id}] Database session closed")
