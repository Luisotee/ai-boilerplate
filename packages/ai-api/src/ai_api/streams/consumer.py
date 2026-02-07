"""
Stream consumer functions for processing messages from Redis Streams.

Provides functions to discover active user streams, process messages
sequentially per user, and run the main consumer loop.
"""

import asyncio

from redis.asyncio import Redis

from ..logger import logger
from .manager import GROUP_NAME, acknowledge_message, read_stream_messages
from .processor import process_chat_job_direct


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
    Process messages for one user sequentially.

    Args:
        redis: Redis client instance
        user_id: User ID to process messages for
        running_flag: Dict with 'running' key to control loop
    """
    while running_flag.get("running", True):
        try:
            messages = await read_stream_messages(redis, user_id, count=1)

            if not messages:
                # No new messages, break
                break

            for stream_key, message_list in messages:
                for message_id, data in message_list:
                    try:
                        await process_single_message(user_id, message_id.decode(), data)
                        await acknowledge_message(redis, user_id, message_id.decode())
                    except Exception as msg_error:
                        logger.error(
                            f"Error processing message {message_id} for user {user_id}: {msg_error}",
                            exc_info=True,
                        )
                        # Still acknowledge to prevent infinite retries
                        try:
                            await acknowledge_message(redis, user_id, message_id.decode())
                        except Exception as ack_error:
                            logger.error(f"Failed to acknowledge failed message: {ack_error}")

        except Exception as e:
            logger.error(f"Error in stream processing for user {user_id}: {e}")
            await asyncio.sleep(1)


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
