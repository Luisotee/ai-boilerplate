"""
Late delivery of chat replies.

The clients poll ``GET /chat/job/{id}`` for at most ``POLL_MAX_DURATION_MS``
(120 s) after enqueueing. A reply that is ready later used to be written to
Redis and never read. Now whoever delivers a job's reply first claims
``job:claim:{job_id}``:

- the poll route claims ``poll`` when it sees a finished job, and returns the
  reply;
- the worker, when the job is old enough that its poller may have given up,
  claims ``push`` and sends the reply itself through the client's
  ``/whatsapp/send-text`` route; the poll route then answers ``silent``.

``SET NX`` makes the two exclusive, so the user gets the reply exactly once.
A pushed reply carries its images too (the job metadata's ``media``, see
``reply_media.py``): each is sent through ``/whatsapp/send-image``, or as its
link when that fails (the Telegram client has no such route).
"""

import re
import time

import httpx
from redis.asyncio import Redis

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..logger import logger
from ..queue.utils import get_job_media, get_job_metadata
from ..whatsapp import WhatsAppClient, create_whatsapp_client

#: Start pushing this long before the clients' budget runs out. The claim keeps
#: delivery exclusive, so pushing a little early never doubles a reply.
LATE_MARGIN_SECONDS = 10

#: Sent instead of running a job that waited longer than CHAT_JOB_MAX_AGE_SECONDS.
EXPIRED_JOB_NOTICE = "Sorry, I couldn't answer your message in time. Please send it again."

_DELIMITER_LINE_RE = re.compile(r"^---\s*$")


def claim_key(job_id: str) -> str:
    return f"job:claim:{job_id}"


async def claim_delivery(redis: Redis, job_id: str, by: str) -> str:
    """Claim the delivery of a job's reply for ``by`` ("poll" or "push").

    Returns the winner: ``by`` if this call claimed it (or already held it),
    otherwise whoever did.
    """
    if await redis.set(claim_key(job_id), by, nx=True, ex=settings.arq_keep_result):
        return by
    winner = await redis.get(claim_key(job_id))
    if isinstance(winner, bytes):
        winner = winner.decode()
    return winner or by


def job_age_seconds(enqueued_at_ms: int | None, now: float | None = None) -> float | None:
    """Seconds since the job was enqueued (its stream entry was added)."""
    if enqueued_at_ms is None:
        return None
    return (now if now is not None else time.time()) - enqueued_at_ms / 1000


def is_late(enqueued_at_ms: int | None, now: float | None = None) -> bool:
    """True when the job's poller may already have given up waiting."""
    age = job_age_seconds(enqueued_at_ms, now)
    budget = settings.poll_max_duration_ms / 1000 - LATE_MARGIN_SECONDS
    return age is not None and age >= budget


def is_expired(enqueued_at_ms: int | None, now: float | None = None) -> bool:
    """True when the job waited too long to be worth running at all."""
    age = job_age_seconds(enqueued_at_ms, now)
    return age is not None and age > settings.chat_job_max_age_seconds


def strip_burst_delimiters(text: str) -> str:
    """Drop the `---` burst lines the clients split on (outside code fences),
    mirroring the clients' stripSplitDelimiters: a pushed reply is one message."""
    lines = text.split("\n")
    in_fence = False
    kept = []
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        kept.append("" if not in_fence and _DELIMITER_LINE_RE.match(line) else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def media_fallback_text(item: dict) -> str:
    """The link sent instead of an image that could not be sent (as the clients do)."""
    caption, url = item.get("caption") or "", item.get("url") or ""
    return f"{caption}: {url}" if caption else url


async def job_media_items(redis: Redis, job_id: str) -> list[dict]:
    """The images listed in a finished job's metadata (empty when none)."""
    metadata = await get_job_metadata(redis, job_id) or {}
    media = metadata.get("media")
    return media if isinstance(media, list) else []


async def _push_media(
    redis: Redis, whatsapp_client: WhatsAppClient, job_id: str, whatsapp_jid: str, media: list
) -> None:
    """Send a pushed reply's images in order; a failed one goes as its link."""
    for item in media:
        try:
            data = await get_job_media(redis, job_id, int(item["index"]))
            if data is None:
                raise LookupError("image bytes expired")
            await whatsapp_client.send_image(
                whatsapp_jid,
                data,
                item.get("mimetype") or "image/jpeg",
                item.get("caption") or None,
            )
        except Exception:
            logger.warning(
                f"[Job {job_id}] Pushed reply image {item.get('index')} failed; sending its link",
                exc_info=True,
            )
            try:
                await whatsapp_client.send_text(whatsapp_jid, media_fallback_text(item))
            except Exception:
                logger.error(f"[Job {job_id}] Image fallback text failed", exc_info=True)


async def deliver_if_late(
    redis: Redis,
    whatsapp_client: WhatsAppClient | None,
    job_id: str,
    whatsapp_jid: str,
    text: str,
    enqueued_at_ms: int | None,
) -> bool:
    """Push a finished job's reply if its poller may have given up.

    Call it after the job's terminal metadata is written: the reply's images
    are read from it and sent after the text. Never raises: a failed push is
    logged, and the reply stays readable at /chat/job.

    Returns:
        True if this call delivered the reply.
    """
    if whatsapp_client is None or not is_late(enqueued_at_ms):
        return False
    try:
        media = await job_media_items(redis, job_id)
        body = strip_burst_delimiters(text) if text else ""
        if not body and not media:
            return False
        # Only a fresh claim pushes: an existing "push" claim means it was sent.
        if not await redis.set(claim_key(job_id), "push", nx=True, ex=settings.arq_keep_result):
            return False  # already delivered, by the poller or an earlier push
        if body:
            await whatsapp_client.send_text(whatsapp_jid, body)
        await _push_media(redis, whatsapp_client, job_id, whatsapp_jid, media)
        logger.info(
            f"[Job {job_id}] Reply pushed after the polling window "
            f"({job_age_seconds(enqueued_at_ms):.0f}s after enqueue)"
        )
        return True
    except Exception:
        logger.error(f"[Job {job_id}] Failed to push a late reply", exc_info=True)
        return False


async def push_text_once(
    redis: Redis,
    client_id: str | None,
    job_id: str,
    whatsapp_jid: str,
    text: str,
    enqueued_at_ms: int | None,
) -> bool:
    """deliver_if_late with its own short-lived client (for callers without one)."""
    async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http_client:
        client = create_whatsapp_client(
            http_client=http_client,
            base_url=get_whatsapp_client_url(client_id),
            api_key=get_whatsapp_api_key(client_id),
        )
        return await deliver_if_late(redis, client, job_id, whatsapp_jid, text, enqueued_at_ms)
