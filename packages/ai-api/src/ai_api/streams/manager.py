"""
Stream manager functions for Redis Streams operations.

Provides functions to add messages to user streams, manage consumer groups,
read messages in order, and acknowledge processed messages.
"""

import os

from redis.asyncio import Redis

from ..logger import logger

# Constants
GROUP_NAME = "workers"
CONSUMER_ID = f"worker-{os.getpid()}"


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
