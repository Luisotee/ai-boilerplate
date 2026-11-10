"""Queue a text message for the stream worker: embed, save, add to the user's stream.

The text-only core of ``POST /chat/enqueue`` (``routes/chat.py``), used by the
``/v1`` web API. ``enqueue_chat`` still has its own copy, which also handles
images and PDFs; converge the two when the parallel pipeline changes to that
route (dedup, burst merging) have merged.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, save_message
from ..embeddings import create_embedding_service
from ..logger import logger
from ..queue.connection import get_redis_client
from ..streams.manager import add_message_to_stream


async def enqueue_text_job(
    db: Session,
    user: User,
    message: str,
    *,
    client_id: str,
    conversation_type: str = "private",
) -> str:
    """Save ``message`` as the user's turn and queue it on their stream. Returns the job id.

    The job runs on ``user.whatsapp_jid`` (a ``web:`` row, or the linked
    WhatsApp/Telegram row, so history is shared) with ``client_id`` marking the
    surface (``surfaces.py``). No ``whatsapp_message_id``: there's nothing to react to.
    """
    whatsapp_jid = user.whatsapp_jid

    embedding = None
    embedding_service = create_embedding_service(settings.gemini_api_key)
    if embedding_service:
        try:
            embedding = await embedding_service.generate(message)
        except Exception as e:
            logger.error(f"Embedding generation error (continuing anyway): {e}")

    user_msg = save_message(
        db, whatsapp_jid, "user", message, conversation_type, embedding=embedding
    )

    job_id = str(uuid.uuid4())
    async with get_redis_client() as redis:
        await add_message_to_stream(
            redis=redis,
            user_id=str(user.id),
            job_data={
                "job_id": job_id,
                "user_id": str(user.id),
                "whatsapp_jid": whatsapp_jid,
                "message": message,
                "conversation_type": conversation_type,
                "user_message_id": str(user_msg.id),
                "client_id": client_id,
            },
        )
    logger.info(f"Job {job_id} added to stream for user {user.id} ({client_id})")
    return job_id
