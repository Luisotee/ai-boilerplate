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
"""

import re
import time

import httpx
from redis.asyncio import Redis

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..logger import logger
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


async def deliver_if_late(
    redis: Redis,
    whatsapp_client: WhatsAppClient | None,
    job_id: str,
    whatsapp_jid: str,
    text: str,
    enqueued_at_ms: int | None,
) -> bool:
    """Push a finished job's reply if its poller may have given up.

    Call it after the job's terminal metadata is written. Never raises: a
    failed push is logged, and the reply stays readable at /chat/job.

    Returns:
        True if this call delivered the reply.
    """
    if not text or whatsapp_client is None or not is_late(enqueued_at_ms):
        return False
    try:
        # Only a fresh claim pushes: an existing "push" claim means it was sent.
        if not await redis.set(claim_key(job_id), "push", nx=True, ex=settings.arq_keep_result):
            return False  # already delivered, by the poller or an earlier push
        await whatsapp_client.send_text(whatsapp_jid, strip_burst_delimiters(text))
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
