"""
Core chat processing logic extracted from arq worker.

This processor can be called directly without arq job context,
making it compatible with Redis Streams.
"""

import os
from redis.asyncio import Redis

from ..logger import logger
from ..database import SessionLocal, get_conversation_history, save_message
from ..agent import get_ai_response, format_message_history, AgentDeps
from ..embeddings import create_embedding_service
from ..rag.conversation import ConversationRAG
from ..rag.knowledge_base import KnowledgeBaseRAG
from ..queue.utils import save_job_chunk, set_job_metadata
from ..queue.connection import get_redis_client


async def process_chat_job_direct(
    user_id: str,
    whatsapp_jid: str,
    message: str,
    conversation_type: str,
    user_message_id: str,
    job_id: str
) -> dict:
    """
    Process a chat message asynchronously without arq context.

    This function:
    1. Retrieves conversation history from PostgreSQL
    2. Initializes embedding service and RAG instances
    3. Streams tokens from Pydantic AI agent
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

    Returns:
        Dict with processing result including success status, job_id, chunk count

    Raises:
        Exception: Any error during processing
    """
    logger.info(f"[Job {job_id}] Starting chat processing for user {user_id}")
    logger.info(f"[Job {job_id}] WhatsApp JID: {whatsapp_jid}")
    logger.info(f"[Job {job_id}] Conversation type: {conversation_type}")

    # Get Redis client
    redis: Redis = await get_redis_client()

    db = SessionLocal()
    chunk_index = 0
    full_response = ""

    try:
        # Step 1: Get conversation history from PostgreSQL
        logger.info(f"[Job {job_id}] Fetching conversation history...")
        history = get_conversation_history(db, whatsapp_jid, conversation_type)
        message_history = format_message_history(history) if history else None
        logger.info(f"[Job {job_id}] Retrieved {len(history) if history else 0} messages from history")

        # Step 2: Initialize embedding service and RAG instances
        logger.info(f"[Job {job_id}] Initializing embedding service and RAG...")
        embedding_service = create_embedding_service(os.getenv("GEMINI_API_KEY"))
        conversation_rag = ConversationRAG() if embedding_service else None
        knowledge_base_rag = KnowledgeBaseRAG() if embedding_service else None

        # Step 3: Prepare agent dependencies
        agent_deps = AgentDeps(
            db=db,
            user_id=user_id,
            whatsapp_jid=whatsapp_jid,
            recent_message_ids=[str(msg.id) for msg in history] if history else [],
            embedding_service=embedding_service,
            conversation_rag=conversation_rag,
            knowledge_base_rag=knowledge_base_rag
        )

        # Step 4: Stream tokens from AI agent
        logger.info(f"[Job {job_id}] Starting AI streaming...")

        async for token in get_ai_response(message, message_history, agent_deps=agent_deps):
            full_response += token

            # Save chunk to Redis immediately
            await save_job_chunk(
                redis,
                job_id,
                chunk_index,
                token
            )
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
            'assistant',
            full_response,
            conversation_type,
            embedding=assistant_embedding
        )
        logger.info(f"[Job {job_id}] Assistant message saved with ID: {assistant_msg.id}")

        # Step 7: Save job metadata to Redis
        await set_job_metadata(
            redis,
            job_id,
            {
                'user_id': user_id,
                'whatsapp_jid': whatsapp_jid,
                'message': message,
                'conversation_type': conversation_type,
                'total_chunks': chunk_index,
                'db_message_id': str(assistant_msg.id),
                'user_message_id': user_message_id
            }
        )

        logger.info(f"[Job {job_id}] ✅ Completed successfully")

        return {
            'success': True,
            'job_id': job_id,
            'total_chunks': chunk_index,
            'response_length': len(full_response),
            'db_message_id': str(assistant_msg.id)
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
                    'assistant',
                    f"[Partial - Error] {full_response}",
                    conversation_type,
                    embedding=None
                )
            except Exception as save_error:
                logger.error(f"[Job {job_id}] Failed to save partial response: {save_error}")

        # Re-raise exception
        raise

    finally:
        db.close()
        logger.info(f"[Job {job_id}] Database session closed")
