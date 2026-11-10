"""
Inbound message deduplication.

Every platform redelivers: Baileys replays offline messages (``type: append``),
and the Cloud API and Telegram retry a webhook that didn't answer in time. Each
copy used to be saved, embedded and run through the agent again. The first
copy of a (conversation, platform message id) pair claims a Redis key; later
copies within ``DEDUP_TTL_SECONDS`` are dropped by the chat routes.

Scheduled stream entries (``trigger=scheduled``) never come through the chat
routes, so they are never deduplicated here.
"""

from redis.asyncio import Redis

from ..config import settings
from ..logger import logger

DEDUP_TTL_SECONDS = 24 * 60 * 60


def dedup_key(whatsapp_jid: str, message_id: str) -> str:
    """Key for one inbound message. The jid is part of it because a Telegram
    message id is only unique within its chat."""
    return f"dedup:{settings.service_name}:{whatsapp_jid}:{message_id}"


async def claim_inbound(redis: Redis, whatsapp_jid: str, message_id: str | None) -> bool:
    """Claim an inbound message. True if it is new (or can't be checked).

    Without a platform message id there is nothing to dedup on. A Redis error
    fails open: a rare duplicate reply beats dropping a message.
    """
    if not message_id:
        return True
    try:
        claimed = await redis.set(
            dedup_key(whatsapp_jid, message_id), "1", nx=True, ex=DEDUP_TTL_SECONDS
        )
    except Exception:
        logger.warning("Dedup check failed; processing the message anyway", exc_info=True)
        return True
    if not claimed:
        logger.info(f"Dropping duplicate inbound message {message_id} for {whatsapp_jid}")
    return bool(claimed)


async def release_inbound(redis: Redis, whatsapp_jid: str, message_id: str | None) -> None:
    """Forget a claim, so the platform's retry of a message we failed on still runs."""
    if not message_id:
        return
    try:
        await redis.delete(dedup_key(whatsapp_jid, message_id))
    except Exception:
        logger.warning("Failed to release a dedup claim", exc_info=True)
