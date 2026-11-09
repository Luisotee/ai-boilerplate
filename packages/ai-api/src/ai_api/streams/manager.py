"""
Stream manager functions for Redis Streams operations.

Two kinds of stream:

- ``stream:user:{user_id}`` — chat jobs, one stream per user so each user's
  messages are processed in order (consumer: ``streams/consumer.py``)
- ``stream:pdf_processing`` — PDF parsing/embedding jobs, one shared stream,
  concurrency-limited by the consumer (``streams/pdf_consumer.py``)
"""

import json
import os
import socket
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import WatchError

from ..logger import logger

# Constants
GROUP_NAME = "workers"
# Unique per process start. The pid alone repeats (it is 1 in every Docker
# container), and a restarted worker must never look like the one that died
# holding a job: see claim_orphaned_chat_entries.
CONSUMER_ID = f"worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Liveness of chat consumers. Each worker refreshes its key while it runs; a
# pending chat entry whose consumer has no key is an orphan of a dead worker.
HEARTBEAT_KEY_PREFIX = "chat_worker:alive:"
HEARTBEAT_TTL_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10
# XCLAIM only entries idle at least this long, so when two live workers race
# for the same orphan, the first claim resets its idle time and the second
# claims nothing.
ORPHAN_CLAIM_MIN_IDLE_MS = 1000


async def add_message_to_stream(redis: Redis, user_id: str, job_data: dict) -> str:
    """
    Add message to user's stream.

    Args:
        redis: Redis client instance
        user_id: User ID to create stream for
        job_data: Dictionary containing job information

    Returns:
        Message ID from Redis (decoded as string)
    """
    stream_key = f"stream:user:{user_id}"
    message_id = await redis.xadd(
        stream_key,
        job_data,
        maxlen=1000,  # Keep last 1000 messages
    )
    logger.info(f"Added message {message_id} to {stream_key}")
    return message_id.decode()


async def ensure_consumer_group(redis: Redis, user_id: str):
    """
    Create consumer group if it doesn't exist.

    Args:
        redis: Redis client instance
        user_id: User ID to create consumer group for
    """
    stream_key = f"stream:user:{user_id}"
    try:
        await redis.xgroup_create(stream_key, GROUP_NAME, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.error(f"Error creating group: {e}")


async def read_stream_messages(
    redis: Redis, user_id: str, count: int = 1, block: int = 5000
) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
    """
    Read messages from user stream - returns in order.

    Args:
        redis: Redis client instance
        user_id: User ID to read messages for
        count: Number of messages to read (default 1)
        block: Milliseconds to block waiting for messages (default 5000)

    Returns:
        List of (stream_key, [(message_id, data)]) tuples
    """
    stream_key = f"stream:user:{user_id}"

    await ensure_consumer_group(redis, user_id)

    messages = await redis.xreadgroup(
        groupname=GROUP_NAME,
        consumername=CONSUMER_ID,
        streams={stream_key: ">"},
        count=count,
        block=block,
    )
    return messages


async def acknowledge_message(redis: Redis, user_id: str, message_id: str):
    """
    Acknowledge message processed.

    Args:
        redis: Redis client instance
        user_id: User ID the message belongs to
        message_id: Message ID to acknowledge
    """
    stream_key = f"stream:user:{user_id}"
    await redis.xack(stream_key, GROUP_NAME, message_id)


def stream_entry_time_ms(message_id: str) -> int | None:
    """Unix ms at which Redis added a stream entry (the first part of its id)."""
    try:
        return int(str(message_id).split("-", 1)[0])
    except ValueError:
        return None


async def refresh_heartbeat(redis: Redis) -> None:
    """Mark this worker's chat consumer as alive for HEARTBEAT_TTL_SECONDS."""
    await redis.set(f"{HEARTBEAT_KEY_PREFIX}{CONSUMER_ID}", "1", ex=HEARTBEAT_TTL_SECONDS)


async def is_consumer_alive(redis: Redis, consumer: str) -> bool:
    """True if ``consumer`` refreshed its heartbeat within the TTL."""
    return bool(await redis.exists(f"{HEARTBEAT_KEY_PREFIX}{consumer}"))


async def claim_orphaned_chat_entries(
    redis: Redis, user_id: str, count: int = 100
) -> list[tuple[str, dict[bytes, bytes]]] | None:
    """
    Take over a user's chat entries left pending by a dead worker.

    An entry is pending from the moment a worker reads it until it is acked, so
    a worker killed mid-run (deploy, OOM, crash) leaves it pending forever: it
    is never delivered again as a new (``">"``) entry.

    Returns:
        None when a live worker other than this one holds pending entries of
        this stream: that worker is processing this user, so leave the stream
        alone (one worker per user keeps the order). Otherwise the entries
        claimed for this worker, oldest first (empty when there were none).
        Entries this worker itself left pending (a failed ack) count as
        orphans too: per-user processing in one worker is sequential, so none
        of them is in flight while this runs.
    """
    stream_key = f"stream:user:{user_id}"
    pending = await redis.xpending_range(stream_key, GROUP_NAME, "-", "+", count)
    if not pending:
        return []

    owners = {_decode(p["consumer"]) for p in pending}
    for owner in owners - {CONSUMER_ID}:
        if await is_consumer_alive(redis, owner):
            return None

    ids = [_decode(p["message_id"]) for p in pending]
    claimed = await redis.xclaim(stream_key, GROUP_NAME, CONSUMER_ID, ORPHAN_CLAIM_MIN_IDLE_MS, ids)
    out: list[tuple[str, dict[bytes, bytes]]] = []
    for message_id, data in claimed or []:
        mid = _decode(message_id)
        if not data:  # trimmed from the stream (maxlen); nothing left to run
            await redis.xack(stream_key, GROUP_NAME, mid)
            continue
        out.append((mid, data))
    out.sort(key=lambda entry: tuple(int(part) for part in entry[0].split("-")))
    if out:
        logger.warning(
            f"Recovered {len(out)} chat job(s) of user {user_id} left pending by "
            f"{', '.join(sorted(owners))}"
        )
    return out


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


# ── PDF processing stream ────────────────────────────────────────────────────
#
# One shared stream (not per user) consumed by `streams/pdf_consumer.py` in the
# worker. Delayed retries live in a sorted set scored by due time, so a pending
# retry survives a worker restart; jobs that exhaust their retries (or are
# malformed) are copied to a dead-letter stream for inspection.

PDF_STREAM_KEY = "stream:pdf_processing"
PDF_GROUP_NAME = "pdf_workers"
PDF_RETRY_KEY = "pdf_processing:retry"
PDF_DEAD_LETTER_KEY = "stream:pdf_processing:dead"

# Safety cap only: processed entries are deleted right after XACK, so the stream
# holds just outstanding jobs. Generous so a big batch upload is never trimmed.
PDF_STREAM_MAXLEN = 10_000
PDF_DEAD_LETTER_MAXLEN = 1_000


def _str_fields(fields: dict) -> dict[str, str]:
    """Normalize stream fields to ``str -> str`` (decoding bytes, dropping None)."""
    out: dict[str, str] = {}
    for key, value in fields.items():
        if value is None:
            continue
        k = key.decode() if isinstance(key, bytes) else str(key)
        v = value.decode() if isinstance(value, bytes) else str(value)
        out[k] = v
    return out


async def enqueue_pdf_processing(
    redis: Redis,
    document_id: str,
    file_path: str,
    whatsapp_jid: str | None = None,
    retry_count: int = 0,
    job_id: str | None = None,
    whatsapp_message_id: str | None = None,
    client_id: str | None = None,
) -> str:
    """
    Add a PDF processing job to the shared PDF stream.

    Args:
        redis: Redis client instance
        document_id: UUID of the knowledge_base_documents row
        file_path: Path to the PDF on disk (must be readable by the worker —
            the API and worker share the upload volume)
        whatsapp_jid: Conversation JID for conversation-scoped (chat) PDFs
        retry_count: Attempts already made (0 = first try)
        job_id: Chat job ID the PDF arrived with (logging only)
        whatsapp_message_id: Message to react to with ✅/❌ when done (chat PDFs)
        client_id: Chat client that owns the message (reaction routing)

    Returns:
        Stream message ID
    """
    job_data = _str_fields(
        {
            "document_id": document_id,
            "file_path": file_path,
            "retry_count": str(retry_count),
            "enqueued_at": datetime.now(UTC).isoformat(),
            "whatsapp_jid": whatsapp_jid or None,
            "job_id": job_id or None,
            "whatsapp_message_id": whatsapp_message_id or None,
            "client_id": client_id or None,
        }
    )
    message_id = await redis.xadd(
        PDF_STREAM_KEY, job_data, maxlen=PDF_STREAM_MAXLEN, approximate=True
    )
    mid = message_id.decode() if isinstance(message_id, bytes) else message_id
    logger.info(
        f"Enqueued PDF processing for document {document_id} "
        f"(retry={retry_count}, message_id={mid})"
    )
    return mid


async def ensure_pdf_consumer_group(redis: Redis) -> None:
    """Create the PDF consumer group (and stream) if it doesn't exist."""
    try:
        await redis.xgroup_create(PDF_STREAM_KEY, PDF_GROUP_NAME, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            logger.error(f"Error creating PDF consumer group: {e}")


async def read_pdf_stream_messages(
    redis: Redis, count: int = 1, block: int = 5000
) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
    """Read NEW messages from the PDF stream for this consumer."""
    return await redis.xreadgroup(
        groupname=PDF_GROUP_NAME,
        consumername=CONSUMER_ID,
        streams={PDF_STREAM_KEY: ">"},
        count=count,
        block=block,
    )


async def acknowledge_pdf_message(redis: Redis, message_id: str) -> None:
    """Acknowledge a PDF job and delete it from the stream (it's done either way)."""
    await redis.xack(PDF_STREAM_KEY, PDF_GROUP_NAME, message_id)
    await redis.xdel(PDF_STREAM_KEY, message_id)


async def claim_stale_pdf_messages(
    redis: Redis, min_idle_ms: int, count: int = 10
) -> list[tuple[str, dict[bytes, bytes]]]:
    """
    Claim PDF jobs delivered to some consumer but never acknowledged.

    That happens when a worker dies mid-parse (restart, deploy, OOM kill —
    Docling is memory hungry). ``min_idle_ms`` must exceed the longest a live
    worker can legitimately hold a job, or a slow parse would be stolen.
    """
    result = await redis.xautoclaim(
        PDF_STREAM_KEY,
        PDF_GROUP_NAME,
        CONSUMER_ID,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=count,
    )
    claimed = result[1] if result and len(result) > 1 else []
    out: list[tuple[str, dict[bytes, bytes]]] = []
    for message_id, data in claimed:
        if not data:  # entry was deleted from the stream; nothing to redo
            mid = message_id.decode() if isinstance(message_id, bytes) else message_id
            await redis.xack(PDF_STREAM_KEY, PDF_GROUP_NAME, mid)
            continue
        out.append((message_id.decode() if isinstance(message_id, bytes) else message_id, data))
    return out


async def schedule_pdf_retry(redis: Redis, fields: dict, due_at: float) -> None:
    """Park a PDF job in the retry sorted set until ``due_at`` (unix seconds)."""
    member = json.dumps(_str_fields(fields), sort_keys=True)
    await redis.zadd(PDF_RETRY_KEY, {member: due_at})


async def promote_due_pdf_retries(redis: Redis, now: float, limit: int = 50) -> int:
    """
    Move retries whose due time has passed back onto the PDF stream.

    Each retry is moved in one MULTI/EXEC (ZREM + XADD) under WATCH, so it is
    always either still parked or already on the stream: a connection lost
    mid-promotion can never leave it in neither place. If another worker
    touched the retry set in between, EXEC aborts (WatchError) and the retry is
    left for the next sweep, so each retry is still promoted exactly once.

    Returns:
        Number of jobs re-enqueued
    """
    due = await redis.zrangebyscore(PDF_RETRY_KEY, "-inf", now, start=0, num=limit)
    promoted = 0
    for member in due:
        try:
            fields = json.loads(member)
        except (TypeError, ValueError):
            if await redis.zrem(PDF_RETRY_KEY, member):
                logger.error(f"Dropping malformed PDF retry entry: {member!r}")
            continue
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(PDF_RETRY_KEY)
                if await pipe.zscore(PDF_RETRY_KEY, member) is None:
                    continue  # another worker got it
                pipe.multi()
                pipe.zrem(PDF_RETRY_KEY, member)
                pipe.xadd(PDF_STREAM_KEY, fields, maxlen=PDF_STREAM_MAXLEN, approximate=True)
                await pipe.execute()
            except WatchError:
                continue  # the retry set changed underneath us; next sweep retries
        promoted += 1
        logger.info(
            f"Re-enqueued PDF document {fields.get('document_id')} "
            f"(retry {fields.get('retry_count')})"
        )
    return promoted


async def dead_letter_pdf_job(redis: Redis, fields: dict, reason: str) -> None:
    """Record a permanently failed PDF job on the dead-letter stream."""
    entry = _str_fields(fields)
    entry["failed_at"] = datetime.now(UTC).isoformat()
    entry["reason"] = reason[:500]
    await redis.xadd(PDF_DEAD_LETTER_KEY, entry, maxlen=PDF_DEAD_LETTER_MAXLEN, approximate=True)
