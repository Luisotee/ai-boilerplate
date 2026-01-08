#!/usr/bin/env python3
"""
Redis Streams worker entry point.

Starts the consumer that processes chat messages from Redis Streams,
ensuring per-user sequential processing while allowing concurrent
processing across different users.
"""

import asyncio

from redis.asyncio import Redis

from ..config import settings
from ..logger import logger
from ..streams.consumer import run_stream_consumer


async def main():
    """Main function to start the Redis Streams consumer."""
    redis = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password,
        decode_responses=False,
    )

    try:
        await run_stream_consumer(redis)
    finally:
        await redis.close()
        logger.info("Redis connection closed")


if __name__ == "__main__":
    asyncio.run(main())
