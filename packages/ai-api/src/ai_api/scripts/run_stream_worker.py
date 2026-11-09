#!/usr/bin/env python3
"""
Redis Streams worker entry point.

Runs four tasks side by side in one process:

- the chat consumer (``streams/consumer.py``) — per-user sequential processing
  of chat messages, concurrent across users
- the PDF consumer (``streams/pdf_consumer.py``) — knowledge-base uploads and
  chat PDF attachments, at most ``KB_MAX_CONCURRENT_PROCESSING`` at a time
- the broadcast consumer (``streams/broadcast_consumer.py``) — operator
  broadcasts from ``POST /admin/broadcasts``, paced per platform; only one
  worker process sends at a time (Redis lease)
- the group-topics loop (``streams/group_topics.py``) — summarises finished
  group conversations into searchable topics when ``group_topics_enabled``

If any task stops, the worker exits non-zero so the process supervisor
(Docker ``restart: unless-stopped``) restarts it with all of them healthy.

It runs ``init_db()`` first, like the API, so tables a worker task needs (the
group-topics ones) exist even when the worker starts before the API.
"""

import asyncio
import sys

from redis.asyncio import Redis

from ..config import settings
from ..database import init_db
from ..instrument import setup_instrumentation
from ..logger import logger
from ..streams.broadcast_consumer import run_broadcast_consumer
from ..streams.consumer import run_stream_consumer
from ..streams.group_topics import run_group_topics_loop
from ..streams.pdf_consumer import run_pdf_consumer

#: init_db() tries before giving up, and the pause between them. The API runs
#: init_db() at the same moment on a fresh deploy, and two concurrent
#: CREATE EXTENSION / CREATE TABLE can make one of them fail once; Postgres may
#: also still be starting.
INIT_DB_ATTEMPTS = 3
INIT_DB_RETRY_SECONDS = 3.0


async def _init_db() -> None:
    """Run init_db() (idempotent), retrying a lost race with the API."""
    for attempt in range(1, INIT_DB_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(init_db)
            return
        except Exception:
            if attempt == INIT_DB_ATTEMPTS:
                raise
            logger.warning(
                f"init_db failed (attempt {attempt}/{INIT_DB_ATTEMPTS}), retrying",
                exc_info=True,
            )
            await asyncio.sleep(INIT_DB_RETRY_SECONDS)


async def main() -> int:
    """Start every consumer; return an exit code once any one stops."""
    # This is the process that actually runs the Pydantic AI agent, so it is the
    # one that emits the token-usage and cost metrics.
    setup_instrumentation("ai-api-worker")

    await _init_db()

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
        asyncio.create_task(run_broadcast_consumer(redis), name="broadcast-consumer"),
        asyncio.create_task(run_group_topics_loop(redis), name="group-topics"),
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
