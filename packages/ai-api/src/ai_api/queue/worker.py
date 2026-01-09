"""
arq worker implementation for processing chat jobs.

This worker processes chat messages asynchronously by:
1. Fetching conversation history from PostgreSQL
2. Streaming tokens from the Pydantic AI agent
3. Saving each token chunk to Redis for real-time polling
4. Saving the complete response to PostgreSQL with embeddings
"""

import os
from typing import Any

import httpx
from arq.connections import RedisSettings
from redis.asyncio import Redis

from ..agent import AgentDeps, format_message_history, get_ai_response
from ..config import settings
from ..database import SessionLocal, get_conversation_history, save_message
from ..embeddings import create_embedding_service
from ..logger import logger
from ..whatsapp import WhatsAppClient, create_whatsapp_client
from .utils import save_job_chunk, set_job_metadata


async def process_chat_job(
    ctx: dict[str, Any],
    user_id: str,
    whatsapp_jid: str,
    message: str,
    conversation_type: str,
    user_message_id: str,
    whatsapp_message_id: str | None = None,
) -> dict[str, Any]:
    """
    Process a chat message asynchronously.

    This function:
    1. Retrieves conversation history from PostgreSQL
    2. Initializes embedding service and RAG instances
    3. Streams tokens from Pydantic AI agent
    4. Saves each token chunk to Redis for real-time client polling
    5. Generates embedding for complete response
    6. Saves final assistant message to PostgreSQL
    7. Stores job metadata in Redis

    Args:
        ctx: arq context containing:
            - job_id: Unique job identifier
            - redis: Redis connection instance
        user_id: User's UUID
        whatsapp_jid: WhatsApp JID (conversation identifier)
        message: User's message (already formatted with sender name if group)
        conversation_type: 'private' or 'group'
        user_message_id: UUID of saved user message in PostgreSQL
        whatsapp_message_id: WhatsApp message ID for reactions (optional)

    Returns:
        Dict with processing result including success status, job_id, chunk count

    Raises:
        Exception: Any error during processing (arq will mark job as failed)
    """
    job_id = ctx["job_id"]
    redis: Redis = ctx["redis"]

    logger.info(f"[Job {job_id}] Starting chat processing for user {user_id}")
    logger.info(f"[Job {job_id}] WhatsApp JID: {whatsapp_jid}")
    logger.info(f"[Job {job_id}] Conversation type: {conversation_type}")
    logger.info(f"[Job {job_id}] WhatsApp message ID: {whatsapp_message_id}")

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
        embedding_service = create_embedding_service(os.getenv("GEMINI_API_KEY"))

        # Step 2.5: Initialize HTTP client and WhatsApp client
        http_client = httpx.AsyncClient(timeout=settings.whatsapp_client_timeout)
        whatsapp_client = create_whatsapp_client(
            http_client=http_client,
            base_url=settings.whatsapp_client_url,
        )
        logger.info(f"[Job {job_id}] WhatsApp client initialized")

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

        # Step 4: Stream tokens from AI agent
        logger.info(f"[Job {job_id}] Starting AI streaming...")

        async for token in get_ai_response(message, message_history, agent_deps=agent_deps):
            full_response += token

            # Save chunk to Redis immediately
            await save_job_chunk(redis, job_id, chunk_index, token)
            chunk_index += 1

        logger.info(f"[Job {job_id}] AI streaming completed. Total chunks: {chunk_index}")
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

        # Re-raise so arq marks job as failed
        raise

    finally:
        # Close HTTP client
        if http_client:
            await http_client.aclose()
            logger.info(f"[Job {job_id}] HTTP client closed")
        db.close()
        logger.info(f"[Job {job_id}] Database session closed")


class WorkerSettings:
    """
    arq worker configuration.

    See: https://arq-docs.helpmanual.io/
    """

    # Redis connection settings
    redis_settings = RedisSettings(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        database=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
    )

    # Worker functions
    functions = [process_chat_job]

    # Job timeout (default 2 minutes, configurable)
    job_timeout = int(os.getenv("ARQ_JOB_TIMEOUT", "120"))

    # Max concurrent jobs across all users
    max_jobs = int(os.getenv("ARQ_MAX_JOBS", "50"))

    # Poll interval for new jobs (default 100ms)
    poll_delay = float(os.getenv("ARQ_POLL_DELAY", "0.1"))

    # Keep job results for 1 hour (3600 seconds)
    keep_result = int(os.getenv("ARQ_KEEP_RESULT", "3600"))

    # Health check interval (1 minute)
    health_check_interval = 60

    # Allow aborting jobs on worker shutdown (graceful shutdown)
    allow_abort_jobs = True

    # Queue name (default, can be overridden per job)
    queue_name = "arq:queue"
