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

    while True:
        cursor, keys = await redis.scan(cursor, match="stream:user:*", count=100)
        for key in keys:
            stream_key = key.decode()
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

        if cursor == 0:
            break

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
                    await process_single_message(user_id, message_id.decode(), data)
                    await acknowledge_message(redis, user_id, message_id.decode())

        except Exception as e:
            logger.error(f"Error processing stream for user {user_id}: {e}")
            await asyncio.sleep(1)


async def process_single_message(user_id: str, message_id: str, data: dict):
    """
    Process a single message from stream.

    Args:
        user_id: User ID the message belongs to
        message_id: Stream message ID
        data: Message data dictionary with job information
    """
    job_id = data[b"job_id"].decode()
    logger.info(f"Processing job {job_id} for user {user_id}")

    # Extract optional whatsapp_message_id
    whatsapp_message_id = None
    if b"whatsapp_message_id" in data:
        whatsapp_message_id = data[b"whatsapp_message_id"].decode()

    # Extract optional image fields
    has_image = data.get(b"has_image", b"").decode() == "true"
    image_mimetype = None
    if b"image_mimetype" in data:
        image_mimetype = data[b"image_mimetype"].decode()

    # Extract optional document fields
    has_document = data.get(b"has_document", b"").decode() == "true"
    document_id = None
    document_path = None
    document_filename = None
    if b"document_id" in data:
        document_id = data[b"document_id"].decode()
    if b"document_path" in data:
        document_path = data[b"document_path"].decode()
    if b"document_filename" in data:
        document_filename = data[b"document_filename"].decode()

    # Call core processor function
    await process_chat_job_direct(
        user_id=data[b"user_id"].decode(),
        whatsapp_jid=data[b"whatsapp_jid"].decode(),
        message=data[b"message"].decode(),
        conversation_type=data[b"conversation_type"].decode(),
        user_message_id=data[b"user_message_id"].decode(),
        job_id=job_id,
        whatsapp_message_id=whatsapp_message_id,
        image_mimetype=image_mimetype,
        has_image=has_image,
        has_document=has_document,
        document_id=document_id,
        document_path=document_path,
        document_filename=document_filename,
    )


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
            # Discover streams with pending messages
            active_streams = await discover_active_streams(redis)

            # Process each user stream concurrently
            # But each user's messages are processed sequentially
            if active_streams:
                tasks = [
                    process_user_stream(redis, user_id, running_flag) for user_id in active_streams
                ]
                await asyncio.gather(*tasks)

            # Sleep before next discovery cycle
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down consumer...")
        running_flag["running"] = False
