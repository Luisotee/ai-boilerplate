#!/usr/bin/env python3
"""
Redis Streams worker entry point.

Starts the consumer that processes chat messages from Redis Streams,
ensuring per-user sequential processing while allowing concurrent
processing across different users.
"""

# Load .env FIRST - before any imports that need it
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

# Now safe to import modules that need GEMINI_API_KEY
import asyncio
from redis.asyncio import Redis
from ..streams.consumer import run_stream_consumer
from ..logger import logger


async def main():
    """Main function to start the Redis Streams consumer."""
    redis = Redis(
        host=os.getenv('REDIS_HOST', 'localhost'),
        port=int(os.getenv('REDIS_PORT', '6379')),
        db=int(os.getenv('REDIS_DB', '0')),
        password=os.getenv('REDIS_PASSWORD') or None,
        decode_responses=False
    )

    try:
        await run_stream_consumer(redis)
    finally:
        await redis.close()
        logger.info("Redis connection closed")


if __name__ == '__main__':
    asyncio.run(main())
