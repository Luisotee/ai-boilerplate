"""
Stream consumer functions for processing messages from Redis Streams.

Provides functions to discover active user streams, process messages
sequentially per user, and run the main consumer loop.
"""

import asyncio
import time

from redis.asyncio import Redis

from ..config import settings
from ..logger import logger
from ..queue.utils import (
    delete_job_image,
    get_job_chunks,
    get_job_metadata,
    save_job_chunk,
    set_job_metadata,
)
from .delivery import (
    EXPIRED_JOB_NOTICE,
    claim_key,
    is_expired,
    is_late,
    job_age_seconds,
    push_text_once,
)
from .manager import (
    GROUP_NAME,
    HEARTBEAT_INTERVAL_SECONDS,
    acknowledge_message,
    claim_orphaned_chat_entries,
    read_stream_messages,
    refresh_heartbeat,
    stream_entry_time_ms,
)
from .processor import process_chat_job_direct

#: Job metadata statuses that mean the job already ran to the end. A job with
#: one of these is acked without running again (a worker that died between
#: writing the result and acking). The success path writes no status at all.
TERMINAL_STATUSES = frozenset({"complete", "failed"})

#: Most jobs / images one merged burst run takes; more start the next run.
BURST_MAX_JOBS = 10
BURST_MAX_IMAGES = 10

#: process_user_stream gives up on a stream after this many errors in a row,
#: so one broken stream can't hold the consumer's gather (and every other
#: user's replies) forever. Discovery picks it up again next cycle.
MAX_CONSECUTIVE_STREAM_ERRORS = 3

#: An expired entry older than this is acked without a notice: a user who
#: wrote a day ago (e.g. a job left pending by a worker before crash recovery
#: existed) gains nothing from "please send it again".
SILENT_EXPIRY_AGE_SECONDS = 24 * 3600


def is_scheduled_entry(data: dict) -> bool:
    """Stream entries added by the scheduler (``trigger=scheduled``).

    The scheduler (PR #50) writes them to the same per-user stream, with their
    own ``run_id``/``job_id`` semantics and no platform message id, so the
    chat-specific steps here (the already-finished and expiry checks, burst
    merging) never touch them: each one goes to process_single_message alone.
    """
    return data.get(b"trigger") == b"scheduled"


async def discover_active_streams(redis: Redis) -> set[str]:
    """
    Discover user streams that have pending messages.

    Args:
        redis: Redis client instance

    Returns:
        Set of user IDs with pending or new messages
    """
    active_streams: set[str] = set()
    cursor = 0

    try:
        while True:
            try:
                cursor, keys = await redis.scan(cursor, match="stream:user:*", count=100)
                for key in keys:
                    stream_key = key.decode() if isinstance(key, bytes) else key
                    user_id = stream_key.split(":")[-1]

                    # Check if stream has pending or new messages
                    try:
                        info = await redis.xpending(stream_key, GROUP_NAME)
                        if info["pending"] > 0:
                            active_streams.add(user_id)
                            continue
                    except Exception:
                        # Consumer group may not exist yet
                        pass

                    # Check for new messages
                    messages = await redis.xread(streams={stream_key: "0-0"}, count=1)
                    if messages:
                        active_streams.add(user_id)

            except Exception as e:
                logger.error(f"Error scanning for streams at cursor {cursor}: {e}")
                # Continue with partial results
                if cursor == 0:
                    break

            if cursor == 0:
                break

    except Exception as e:
        logger.error(f"Fatal error discovering streams: {e}", exc_info=True)
        # Return partial results rather than crash

    return active_streams


async def process_user_stream(redis: Redis, user_id: str, running_flag: dict):
    """
    Process messages for one user sequentially, in stream order.

    Each round first takes over entries a dead worker left pending (they are
    older than anything new, so they run first), then reads new ones. When a
    live worker holds this user's pending entries, the stream is left to it.

    Args:
        redis: Redis client instance
        user_id: User ID to process messages for
        running_flag: Dict with 'running' key to control loop
    """
    errors = 0
    while running_flag.get("running", True):
        try:
            entries = await claim_orphaned_chat_entries(redis, user_id)
            if entries is None:
                break  # another live worker is processing this user
            if not entries:
                entries = await _read_new_entries(redis, user_id)
            if not entries:
                break

            entries = await _collect_burst(redis, user_id, entries)
            for run in plan_runs(entries):
                await _process_run(redis, user_id, run)
            errors = 0

        except Exception as e:
            errors += 1
            logger.error(f"Error in stream processing for user {user_id}: {e}")
            if errors >= MAX_CONSECUTIVE_STREAM_ERRORS:
                logger.error(f"Giving up on the stream of user {user_id} until the next cycle")
                break
            await asyncio.sleep(1)


async def _read_new_entries(
    redis: Redis, user_id: str, count: int = 1, block: int = 5000
) -> list[tuple[str, dict]]:
    """New (never delivered) entries of the user's stream, oldest first."""
    messages = await read_stream_messages(redis, user_id, count=count, block=block)
    return [
        (message_id.decode() if isinstance(message_id, bytes) else message_id, data)
        for _stream_key, message_list in messages or []
        for message_id, data in message_list
    ]


def _is_mergeable(data: dict) -> bool:
    """Chat jobs that may join a burst run: not scheduled, no PDF (a document
    job always runs alone, it has its own reply flow)."""
    return not is_scheduled_entry(data) and data.get(b"has_document") != b"true"


#: Fields that must match for two jobs to be answered in one run. In a group
#: that means the same sender (and the same admin flag, so a merged run never
#: gains rights one of its senders lacked).
_RUN_KEYS = (
    b"whatsapp_jid",
    b"conversation_type",
    b"sender_jid",
    b"sender_name",
    b"client_id",
    b"is_group_admin",
)


def _image_count(data: dict) -> int:
    if data.get(b"has_image") != b"true":
        return 0
    try:
        return int(data.get(b"image_count", b"1"))
    except ValueError:
        return 1


def plan_runs(entries: list[tuple[str, dict]]) -> list[list[tuple[str, dict]]]:
    """Split stream entries (in order) into runs: consecutive mergeable jobs of
    the same sender share a run; everything else runs alone. Order is kept."""
    runs: list[list[tuple[str, dict]]] = []
    current: list[tuple[str, dict]] = []
    images = 0
    for entry in entries:
        data = entry[1]
        if not _is_mergeable(data):
            if current:
                runs.append(current)
                current, images = [], 0
            runs.append([entry])
            continue
        fits = (
            current
            and all(current[-1][1].get(k) == data.get(k) for k in _RUN_KEYS)
            and len(current) < BURST_MAX_JOBS
            and images + _image_count(data) <= BURST_MAX_IMAGES
        )
        if current and not fits:
            runs.append(current)
            current, images = [], 0
        current.append(entry)
        images += _image_count(data)
    if current:
        runs.append(current)
    return runs


async def _collect_burst(
    redis: Redis, user_id: str, entries: list[tuple[str, dict]]
) -> list[tuple[str, dict]]:
    """Wait for the user to stop sending (CHAT_BURST_QUIET_MS without a new
    message, at most CHAT_BURST_MAX_WAIT_MS) and add what arrived meanwhile.

    Skipped when merging is off or the last entry can't be merged anyway (a
    scheduled entry or a PDF runs at once, alone).
    """
    quiet_ms = settings.chat_burst_quiet_ms
    if quiet_ms <= 0 or not entries or not _is_mergeable(entries[-1][1]):
        return entries
    deadline = time.monotonic() + settings.chat_burst_max_wait_ms / 1000
    while len(entries) < BURST_MAX_JOBS:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        # block=0 would mean "forever" to Redis
        block = max(1, min(quiet_ms, remaining_ms))
        new = await _read_new_entries(
            redis, user_id, count=BURST_MAX_JOBS - len(entries), block=block
        )
        if not new:
            break  # quiet: the burst is over
        entries.extend(new)
        if not _is_mergeable(new[-1][1]):
            break
    return entries


async def _process_run(redis: Redis, user_id: str, run: list[tuple[str, dict]]) -> None:
    """Run one planned run: a single entry as before, or a merged burst."""
    if len(run) == 1:
        await _process_entry(redis, user_id, *run[0])
        return
    try:
        pending = []
        for message_id, data in run:
            if await _already_finished(redis, data):
                logger.info(f"Chat job in entry {message_id} already finished; acking only")
                await _push_unconfirmed_late_reply(redis, message_id, data)
                continue
            try:
                _job_kwargs(message_id, data)
            except Exception as invalid:
                logger.error(f"Dropping malformed chat entry {message_id}: {invalid}")
                continue
            pending.append((message_id, data))
        if len(pending) == 1:
            await _run_entry(redis, user_id, *pending[0])
        elif pending:
            await _process_merged_run(redis, user_id, pending)
    except Exception as run_error:
        logger.error(
            f"Error processing a merged run of {len(run)} jobs for user {user_id}: {run_error}",
            exc_info=True,
        )
    finally:
        for message_id, _data in run:
            try:
                await acknowledge_message(redis, user_id, message_id)
            except Exception as ack_error:
                logger.error(f"Failed to acknowledge message {message_id}: {ack_error}")


async def _process_merged_run(redis: Redis, user_id: str, run: list[tuple[str, dict]]) -> None:
    """Answer several jobs of one sender with one agent run.

    The last job carries the reply (its message is the one reactions and tools
    refer to, and its poller started last); the earlier ones are closed as
    merged and their clients send nothing.
    """
    jobs = [_job_kwargs(message_id, data) for message_id, data in run]
    carrier = jobs[-1]
    absorbed = [job["job_id"] for job in jobs[:-1]]

    carrier_message_id = run[-1][0]
    if is_expired(stream_entry_time_ms(carrier_message_id)):
        await _expire_entry(redis, carrier_message_id, run[-1][1])
        for job_id in absorbed:
            await set_job_metadata(
                redis,
                job_id,
                {"status": "complete", "merged_into": carrier["job_id"], "total_chunks": 0},
            )
        return

    logger.info(
        f"Merging {len(jobs)} jobs of user {user_id} into one run "
        f"(carrier {carrier['job_id']}, absorbed {absorbed})"
    )
    await process_chat_job_direct(
        **{
            **carrier,
            "message": "\n".join(job["message"] for job in jobs if job["message"]),
            "has_image": any(job["has_image"] for job in jobs),
            "image_sources": [
                (job["job_id"], job["image_mimetype"], job.get("image_count", 1))
                for job in jobs
                if job["has_image"]
            ],
            "run_user_message_ids": [job["user_message_id"] for job in jobs],
            "absorbed_job_ids": absorbed,
        }
    )


async def _already_finished(redis: Redis, data: dict) -> bool:
    """True if this chat job already wrote terminal metadata (a worker died
    after finishing it but before acking). Only terminal statuses count, so a
    job that crashed mid-run still runs again."""
    raw_job_id = data.get(b"job_id")
    if not raw_job_id:
        return False
    metadata = await get_job_metadata(redis, raw_job_id.decode("utf-8", "replace"))
    if metadata is None or metadata.get("status", "complete") not in TERMINAL_STATUSES:
        return False
    carrier = metadata.get("merged_into")
    if carrier:
        # Absorbed jobs are closed when the merged run starts; the burst is
        # done only once its carrier is, else recovery re-runs all of it.
        carrier_meta = await get_job_metadata(redis, carrier)
        return (
            carrier_meta is not None and carrier_meta.get("status", "complete") in TERMINAL_STATUSES
        )
    return True


async def _push_unconfirmed_late_reply(redis: Redis, message_id: str, data: dict) -> None:
    """A finished, late job whose reply nobody delivered (the worker died
    between writing the result and pushing it): push it now. The client
    already promised the user the answer would come."""
    if is_scheduled_entry(data) or not is_late(stream_entry_time_ms(message_id)):
        return
    raw_job_id = data.get(b"job_id")
    raw_jid = data.get(b"whatsapp_jid")
    if not raw_job_id or not raw_jid:
        return
    job_id = raw_job_id.decode("utf-8", "replace")
    try:
        if await redis.exists(claim_key(job_id)):
            return  # the poller or an earlier push delivered it
        metadata = await get_job_metadata(redis, job_id) or {}
        if metadata.get("merged_into"):
            return  # nothing of its own to send
        text = "".join(c["content"] for c in await get_job_chunks(redis, job_id))
        if not text:
            return
        client_id = data.get(b"client_id")
        await push_text_once(
            redis,
            client_id.decode() if client_id else None,
            job_id,
            raw_jid.decode("utf-8", "replace"),
            text,
            stream_entry_time_ms(message_id),
        )
    except Exception:
        logger.error(f"[Job {job_id}] Failed to push an unconfirmed late reply", exc_info=True)


async def _process_entry(redis: Redis, user_id: str, message_id: str, data: dict) -> None:
    """Run one stream entry and ack it, whatever happens (no infinite retries)."""
    try:
        if not is_scheduled_entry(data) and await _already_finished(redis, data):
            logger.info(f"Chat job in entry {message_id} already finished; acking only")
            await _push_unconfirmed_late_reply(redis, message_id, data)
        else:
            await _run_entry(redis, user_id, message_id, data)
    except Exception as msg_error:
        logger.error(
            f"Error processing message {message_id} for user {user_id}: {msg_error}",
            exc_info=True,
        )
    finally:
        try:
            await acknowledge_message(redis, user_id, message_id)
        except Exception as ack_error:
            logger.error(f"Failed to acknowledge message {message_id}: {ack_error}")


async def _run_entry(redis: Redis, user_id: str, message_id: str, data: dict) -> None:
    """Run one unfinished entry, or expire it when it waited too long (no ack)."""
    if not is_scheduled_entry(data) and is_expired(stream_entry_time_ms(message_id)):
        await _expire_entry(redis, message_id, data)
    else:
        await process_single_message(user_id, message_id, data)


async def _expire_entry(redis: Redis, message_id: str, data: dict) -> None:
    """Close a chat job that waited past CHAT_JOB_MAX_AGE_SECONDS without
    running it (an outage backlog): the user gets a short notice instead,
    unless it is over SILENT_EXPIRY_AGE_SECONDS old (then it is only acked)."""
    age = job_age_seconds(stream_entry_time_ms(message_id))
    if age is not None and age > SILENT_EXPIRY_AGE_SECONDS:
        logger.warning(f"Chat entry {message_id} is {age / 3600:.0f}h old; dropping it silently")
        if data.get(b"has_image") == b"true" and data.get(b"job_id"):
            await delete_job_image(redis, data[b"job_id"].decode("utf-8", "replace"))
        return

    def field(name: bytes) -> str | None:
        value = data.get(name)
        return value.decode("utf-8", "replace") if value is not None else None

    job_id = field(b"job_id") or message_id
    whatsapp_jid = field(b"whatsapp_jid")
    logger.warning(f"[Job {job_id}] Expired before it could run; not running it")
    await save_job_chunk(redis, job_id, 0, EXPIRED_JOB_NOTICE)
    await set_job_metadata(
        redis,
        job_id,
        {
            "user_id": field(b"user_id"),
            "whatsapp_jid": whatsapp_jid,
            "conversation_type": field(b"conversation_type"),
            "total_chunks": 1,
            "user_message_id": field(b"user_message_id"),
            "expired": True,
        },
    )
    if field(b"has_image") == "true":
        await delete_job_image(redis, job_id)
    if whatsapp_jid:
        await push_text_once(
            redis,
            field(b"client_id"),
            job_id,
            whatsapp_jid,
            EXPIRED_JOB_NOTICE,
            stream_entry_time_ms(message_id),
        )


def _safe_decode(value: bytes | None, default: str | None = None) -> str | None:
    """Safely decode bytes to string with error handling."""
    if value is None:
        return default
    try:
        return value.decode("utf-8")
    except (UnicodeDecodeError, AttributeError) as e:
        logger.warning(f"Failed to decode value: {e}")
        return default


def _job_kwargs(message_id: str, data: dict) -> dict:
    """Validate a chat stream entry and turn it into process_chat_job_direct kwargs."""
    required_fields = [
        b"job_id",
        b"user_id",
        b"whatsapp_jid",
        b"message",
        b"conversation_type",
        b"user_message_id",
    ]
    missing = [f.decode() for f in required_fields if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    job_id = _safe_decode(data[b"job_id"])
    if not job_id:
        raise ValueError("job_id is required but could not be decoded")

    # Optional image fields (image_count > 1 for an album)
    has_image = _safe_decode(data.get(b"has_image", b""), "false") == "true"
    image_mimetype = _safe_decode(data.get(b"image_mimetype")) if has_image else None
    try:
        image_count = int(_safe_decode(data.get(b"image_count"), "1") or 1)
    except ValueError:
        image_count = 1

    # Optional document fields
    has_document = _safe_decode(data.get(b"has_document", b""), "false") == "true"

    # Optional group-admin flag (absent = unknown, treated as not admin)
    is_group_admin_raw = _safe_decode(data.get(b"is_group_admin"))
    is_group_admin = None if is_group_admin_raw is None else is_group_admin_raw == "true"

    return {
        "user_id": _safe_decode(data[b"user_id"]),
        "whatsapp_jid": _safe_decode(data[b"whatsapp_jid"]),
        "message": _safe_decode(data[b"message"]),
        "conversation_type": _safe_decode(data[b"conversation_type"]),
        "user_message_id": _safe_decode(data[b"user_message_id"]),
        "job_id": job_id,
        "whatsapp_message_id": _safe_decode(data.get(b"whatsapp_message_id")),
        "image_mimetype": image_mimetype,
        "has_image": has_image,
        "has_document": has_document,
        "document_id": _safe_decode(data.get(b"document_id")) if has_document else None,
        "document_path": _safe_decode(data.get(b"document_path")) if has_document else None,
        "document_filename": (
            _safe_decode(data.get(b"document_filename")) if has_document else None
        ),
        # Sender name (group attribution) and client id (multi-client routing)
        "sender_name": _safe_decode(data.get(b"sender_name")),
        "client_id": _safe_decode(data.get(b"client_id")),
        "is_group_admin": is_group_admin,
        "enqueued_at_ms": stream_entry_time_ms(message_id),
        **({"image_count": image_count} if image_count > 1 else {}),
    }


async def process_single_message(user_id: str, message_id: str, data: dict):
    """
    Process a single message from stream with robust validation.

    Args:
        user_id: User ID the message belongs to
        message_id: Stream message ID
        data: Message data dictionary with job information
    """
    try:
        kwargs = _job_kwargs(message_id, data)
        logger.info(f"Processing job {kwargs['job_id']} for user {user_id}")

        # Call core processor function
        await process_chat_job_direct(**kwargs)

    except Exception as e:
        logger.error(
            f"Failed to process message {message_id} for user {user_id}: {e}",
            exc_info=True,
        )
        # Re-raise to be caught by caller
        raise


async def run_stream_consumer(redis: Redis):
    """
    Main consumer loop - processes messages from all user streams.

    This function:
    1. Discovers user streams with pending messages
    2. Processes each user's stream concurrently (but sequentially within each stream)
    3. Repeats every second

    Args:
        redis: Redis client instance
    """
    running_flag = {"running": True}
    logger.info("🚀 Starting Redis Streams consumer")
    heartbeat = asyncio.create_task(_heartbeat_loop(redis), name="chat-consumer-heartbeat")

    try:
        while running_flag["running"]:
            try:
                # Discover streams with pending messages
                active_streams = await discover_active_streams(redis)

                # Process each user stream concurrently
                # But each user's messages are processed sequentially
                if active_streams:
                    # Convert set to list ONCE for deterministic ordering
                    users_list = list(active_streams)

                    tasks = [
                        process_user_stream(redis, user_id, running_flag) for user_id in users_list
                    ]
                    # Use return_exceptions=True to prevent one failure from canceling all
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Log any exceptions with guaranteed user-result correspondence
                    for user_id, result in zip(users_list, results):
                        if isinstance(result, Exception):
                            logger.error(
                                f"Stream processing failed for user {user_id}: {result}",
                                exc_info=True,
                            )

            except Exception as e:
                logger.error(f"Error in consumer loop: {e}", exc_info=True)
                await asyncio.sleep(5)  # Back off on errors
                continue

            # Sleep before next discovery cycle
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down consumer...")
        running_flag["running"] = False
    finally:
        heartbeat.cancel()


async def _heartbeat_loop(redis: Redis) -> None:
    """Keep this worker's liveness key fresh, so other workers never take over
    the jobs it is running (and do take them over once it is gone)."""
    while True:
        try:
            await refresh_heartbeat(redis)
        except Exception as e:
            logger.warning(f"Failed to refresh the chat consumer heartbeat: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
