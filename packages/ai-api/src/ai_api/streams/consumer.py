"""
Stream consumer functions for processing messages from Redis Streams.

Provides functions to discover active user streams, process messages
sequentially per user, and run the main consumer loop.
"""

import asyncio

from redis.asyncio import Redis

from ..logger import logger
from ..queue.utils import get_job_metadata
from .manager import (
    GROUP_NAME,
    HEARTBEAT_INTERVAL_SECONDS,
    acknowledge_message,
    claim_orphaned_chat_entries,
    read_stream_messages,
    refresh_heartbeat,
)
from .processor import process_chat_job_direct

#: Job metadata statuses that mean the job already ran to the end. A job with
#: one of these is acked without running again (a worker that died between
#: writing the result and acking). The success path writes no status at all.
TERMINAL_STATUSES = frozenset({"complete", "failed"})


def is_scheduled_entry(data: dict) -> bool:
    """Stream entries added by the scheduler (``trigger=scheduled``).

    They carry their own ``run_id``/``job_id`` semantics and no platform message
    id, so the chat-specific steps here (the already-finished check, burst
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
    while running_flag.get("running", True):
        try:
            entries = await claim_orphaned_chat_entries(redis, user_id)
            if entries is None:
                break  # another live worker is processing this user
            if not entries:
                entries = await _read_new_entries(redis, user_id)
            if not entries:
                break

            for message_id, data in entries:
                await _process_entry(redis, user_id, message_id, data)

        except Exception as e:
            logger.error(f"Error in stream processing for user {user_id}: {e}")
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


async def _already_finished(redis: Redis, data: dict) -> bool:
    """True if this chat job already wrote terminal metadata (a worker died
    after finishing it but before acking). Only terminal statuses count, so a
    job that crashed mid-run still runs again."""
    raw_job_id = data.get(b"job_id")
    if not raw_job_id:
        return False
    metadata = await get_job_metadata(redis, raw_job_id.decode("utf-8", "replace"))
    return metadata is not None and metadata.get("status", "complete") in TERMINAL_STATUSES


async def _process_entry(redis: Redis, user_id: str, message_id: str, data: dict) -> None:
    """Run one stream entry and ack it, whatever happens (no infinite retries)."""
    try:
        if not is_scheduled_entry(data) and await _already_finished(redis, data):
            logger.info(f"Chat job in entry {message_id} already finished; acking only")
        else:
            await process_single_message(user_id, message_id, data)
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


async def process_single_message(user_id: str, message_id: str, data: dict):
    """
    Process a single message from stream with robust validation.

    Args:
        user_id: User ID the message belongs to
        message_id: Stream message ID
        data: Message data dictionary with job information
    """

    def safe_decode(value: bytes | None, default: str | None = None) -> str | None:
        """Safely decode bytes to string with error handling."""
        if value is None:
            return default
        try:
            return value.decode("utf-8")
        except (UnicodeDecodeError, AttributeError) as e:
            logger.warning(f"Failed to decode value: {e}")
            return default

    try:
        # Validate required fields
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

        job_id = safe_decode(data[b"job_id"])
        if not job_id:
            raise ValueError("job_id is required but could not be decoded")

        logger.info(f"Processing job {job_id} for user {user_id}")

        # Extract optional whatsapp_message_id
        whatsapp_message_id = safe_decode(data.get(b"whatsapp_message_id"))

        # Extract optional image fields
        has_image = safe_decode(data.get(b"has_image", b""), "false") == "true"
        image_mimetype = safe_decode(data.get(b"image_mimetype")) if has_image else None

        # Extract optional document fields
        has_document = safe_decode(data.get(b"has_document", b""), "false") == "true"
        document_id = safe_decode(data.get(b"document_id")) if has_document else None
        document_path = safe_decode(data.get(b"document_path")) if has_document else None
        document_filename = safe_decode(data.get(b"document_filename")) if has_document else None

        # Extract optional sender name (for group message attribution)
        sender_name = safe_decode(data.get(b"sender_name"))

        # Extract optional client ID (for multi-client routing)
        client_id = safe_decode(data.get(b"client_id"))

        # Extract optional group-admin flag (absent = unknown, treated as not admin)
        is_group_admin_raw = safe_decode(data.get(b"is_group_admin"))
        is_group_admin = None if is_group_admin_raw is None else is_group_admin_raw == "true"

        # Call core processor function
        await process_chat_job_direct(
            user_id=safe_decode(data[b"user_id"]),
            whatsapp_jid=safe_decode(data[b"whatsapp_jid"]),
            message=safe_decode(data[b"message"]),
            conversation_type=safe_decode(data[b"conversation_type"]),
            user_message_id=safe_decode(data[b"user_message_id"]),
            job_id=job_id,
            whatsapp_message_id=whatsapp_message_id,
            image_mimetype=image_mimetype,
            has_image=has_image,
            has_document=has_document,
            document_id=document_id,
            document_path=document_path,
            document_filename=document_filename,
            sender_name=sender_name,
            client_id=client_id,
            is_group_admin=is_group_admin,
        )

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
