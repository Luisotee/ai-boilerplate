"""Platform revoke and edit events for saved history (`services/message_events.py`).

The chat clients call these when a user deletes a message for everyone
(Baileys) or edits one (Baileys, Telegram; the Cloud API delivers neither).
Both act only on rows that already exist, never create a conversation, and
never call the agent.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..embeddings import create_embedding_service
from ..logger import logger
from ..schemas import EditMessageRequest, MessageEventResponse, RevokeMessageRequest
from ..services.message_events import edit_message, revoke_message
from .chat import _is_whitelisted

router = APIRouter()


@router.post("/chat/revoke", response_model=MessageEventResponse, tags=["Chat"])
async def revoke(request: RevokeMessageRequest, db: Session = Depends(get_db)):
    """Delete the saved copy of a message its sender deleted for everyone.

    No whitelist check on purpose: it only ever removes rows that were saved
    while the chat was in scope, and honouring a retraction must not depend on
    the chat still being whitelisted.
    """
    try:
        changed = revoke_message(
            db,
            request.whatsapp_jid,
            request.whatsapp_message_id,
            sender_jid=request.sender_jid,
            is_group_admin=request.is_group_admin,
            phone=request.phone,
        )
    except Exception:
        db.rollback()
        logger.error("Failed to apply a message revoke", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    return MessageEventResponse(changed=changed)


@router.post("/chat/edit", response_model=MessageEventResponse, tags=["Chat"])
async def edit(request: EditMessageRequest, db: Session = Depends(get_db)):
    """Replace the saved copy of an edited message and re-embed it.

    If the new embedding can't be generated the old one is dropped, so the
    edited-away text can no longer surface through semantic search.
    """
    if not _is_whitelisted(request.whatsapp_jid, request.phone):
        logger.warning(f"Blocked edit for non-whitelisted JID: {request.whatsapp_jid}")
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        rows = edit_message(
            db,
            request.whatsapp_jid,
            request.whatsapp_message_id,
            request.message,
            sender_jid=request.sender_jid,
            phone=request.phone,
        )
        if not rows:
            return MessageEventResponse(changed=0)

        embedding_service = create_embedding_service(settings.gemini_api_key)
        for row in rows:
            embedding = None
            if embedding_service:
                try:
                    embedding = await embedding_service.generate(row.content)
                except Exception:
                    logger.error("Embedding generation failed for an edited message", exc_info=True)
            row.embedding = embedding or None
            row.embedding_generated_at = datetime.now(UTC) if embedding else None
        db.commit()
    except Exception:
        db.rollback()
        logger.error("Failed to apply a message edit", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    logger.info(
        f"Updated {len(rows)} edited message(s) {request.whatsapp_message_id} "
        f"in {request.whatsapp_jid}"
    )
    return MessageEventResponse(changed=len(rows))
