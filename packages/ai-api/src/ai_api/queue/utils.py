"""
Queue utility functions for job tracking and chunk storage.

Provides helpers for storing streaming chunks in Redis
and managing job metadata.
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional
from redis.asyncio import Redis

from ..logger import logger


async def save_job_chunk(
    redis: Redis,
    job_id: str,
    index: int,
    content: str
) -> None:
    """
    Save a streaming chunk to Redis with TTL.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier
        index: Chunk index (0-based, sequential)
        content: Chunk content (token or text fragment)
    """
    chunk_key = f"job:chunks:{job_id}"

    chunk_data = json.dumps({
        'index': index,
        'content': content,
        'timestamp': datetime.utcnow().isoformat()
    })

    # Append chunk to list
    await redis.rpush(chunk_key, chunk_data)

    # Set TTL (default 1 hour, configurable)
    ttl = int(os.getenv('QUEUE_CHUNK_TTL', '3600'))
    await redis.expire(chunk_key, ttl)


async def get_job_chunks(
    redis: Redis,
    job_id: str,
    start_index: int = 0
) -> List[Dict[str, Any]]:
    """
    Retrieve chunks for a job from Redis.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier
        start_index: Start index for range retrieval (default 0 = all chunks)

    Returns:
        List of chunk dictionaries with index, content, timestamp
    """
    chunk_key = f"job:chunks:{job_id}"

    # Get chunks from start_index to end
    raw_chunks = await redis.lrange(chunk_key, start_index, -1)

    # Parse JSON chunks
    chunks = []
    for raw_chunk in raw_chunks:
        try:
            chunk = json.loads(raw_chunk)
            chunks.append(chunk)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse chunk for job {job_id}: {e}")
            continue

    return chunks


async def get_chunk_count(
    redis: Redis,
    job_id: str
) -> int:
    """
    Get the total number of chunks for a job.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier

    Returns:
        Total number of chunks
    """
    chunk_key = f"job:chunks:{job_id}"
    count = await redis.llen(chunk_key)
    return count


async def set_job_metadata(
    redis: Redis,
    job_id: str,
    metadata: Dict[str, Any]
) -> None:
    """
    Store job metadata in Redis.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier
        metadata: Metadata dictionary (user_id, whatsapp_jid, etc.)
    """
    meta_key = f"job:meta:{job_id}"

    # Add timestamp
    metadata['created_at'] = datetime.utcnow().isoformat()

    await redis.set(
        meta_key,
        json.dumps(metadata),
        ex=int(os.getenv('ARQ_KEEP_RESULT', '3600'))  # Match job result TTL
    )


async def get_job_metadata(
    redis: Redis,
    job_id: str
) -> Optional[Dict[str, Any]]:
    """
    Retrieve job metadata from Redis.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier

    Returns:
        Metadata dictionary or None if not found
    """
    meta_key = f"job:meta:{job_id}"

    data = await redis.get(meta_key)
    if not data:
        return None

    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse metadata for job {job_id}: {e}")
        return None


async def delete_job_data(
    redis: Redis,
    job_id: str
) -> None:
    """
    Delete all job data from Redis (chunks + metadata).

    Useful for manual cleanup or testing.

    Args:
        redis: Redis client instance
        job_id: Unique job identifier
    """
    chunk_key = f"job:chunks:{job_id}"
    meta_key = f"job:meta:{job_id}"
    result_key = f"job:result:{job_id}"

    await redis.delete(chunk_key, meta_key, result_key)
    logger.info(f"Deleted all data for job {job_id}")
