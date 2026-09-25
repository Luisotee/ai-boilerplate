#!/usr/bin/env python3
"""
Redis Streams worker entry point.

Runs two consumers side by side in one process:

- the chat consumer (``streams/consumer.py``) — per-user sequential processing
  of chat messages, concurrent across users
- the PDF consumer (``streams/pdf_consumer.py``) — knowledge-base uploads and
  chat PDF attachments, at most ``KB_MAX_CONCURRENT_PROCESSING`` at a time

If either consumer stops, the worker exits non-zero so the process supervisor
(Docker ``restart: unless-stopped``) restarts it with both consumers healthy.
"""

import asyncio
import sys

from redis.asyncio import Redis

from ..config import settings
from ..instrument import setup_instrumentation
from ..logger import logger
from ..streams.consumer import run_stream_consumer
from ..streams.pdf_consumer import run_pdf_consumer


async def main() -> int:
    """Start both consumers; return an exit code once either one stops."""
    # This is the process that actually runs the Pydantic AI agent, so it is the
    # one that emits the token-usage and cost metrics.
    setup_instrumentation("ai-api-worker")

    redis = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password,
        decode_responses=False,
    )

    tasks = {
        asyncio.create_task(run_stream_consumer(redis), name="chat-consumer"),
        asyncio.create_task(run_pdf_consumer(redis), name="pdf-consumer"),
    }
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc:
                logger.error(f"Worker task {task.get_name()} crashed, shutting down", exc_info=exc)
            else:
                logger.error(f"Worker task {task.get_name()} exited unexpectedly, shutting down")
        return 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        logger.info("Redis connection closed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
