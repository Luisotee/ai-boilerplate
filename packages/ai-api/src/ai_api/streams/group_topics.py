"""Group-topics loop of the stream worker.

Every ``GROUP_TOPICS_INTERVAL_MINUTES``, while the hot ``group_topics_enabled``
setting is on, summarise each group's finished conversations into topics
(``rag/group_topics.py``). Off, it only sleeps.

A Redis lease (``group_topics:lock``) lets one worker process run at a time;
a run stops starting new groups before the lease could expire, and groups that
waited longest go first, so the rest are picked up by the next run.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from ..config import settings
from ..embeddings import create_embedding_service
from ..logger import logger
from ..rag.group_topics import groups_with_new_messages, ingest_group
from ..runtime_config import runtime_config

LOCK_KEY = "group_topics:lock"
LOCK_TTL_SECONDS = 1800
#: Stop starting new groups once a run is this old (one group's worst case,
#: MAX_BLOCKS_PER_RUN summaries, must still fit before the lease expires).
RUN_BUDGET_SECONDS = 900

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _utcnow() -> datetime:
    """Naive UTC, matching the DB's ``datetime.utcnow`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


async def run_group_topics_once(redis: Redis, token: str) -> None:
    """One run over every group with new messages, under the lease."""
    if not await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS):
        logger.debug("Group topics: another worker holds the lease")
        return
    try:
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if embedding_service is None:
            logger.warning("Group topics: no embedding service (GEMINI_API_KEY unset), skipping")
            return

        loop = asyncio.get_running_loop()
        started = loop.time()
        now = _utcnow()
        group_ids = await asyncio.to_thread(groups_with_new_messages, now)
        done = topics = 0
        for group_id in group_ids:
            if loop.time() - started > RUN_BUDGET_SECONDS:
                logger.info("Group topics: run budget used, the rest waits for the next run")
                break
            try:
                result = await ingest_group(redis, group_id, now, embedding_service)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Group topics: group {group_id} failed", exc_info=True)
                continue
            done += result.blocks_done
            topics += result.topics_stored
            if result.unavailable:
                break
        if group_ids:
            logger.info(
                f"Group topics: {len(group_ids)} groups checked, {done} blocks summarised, "
                f"{topics} topics stored"
            )
    finally:
        try:
            await redis.eval(_RELEASE_LUA, 1, LOCK_KEY, token)
        except Exception:
            logger.warning("Group topics: failed to release the lease", exc_info=True)


async def run_group_topics_loop(redis: Redis) -> None:
    """Worker task: run the group-topics job every interval while it is enabled."""
    interval = settings.group_topics_interval_minutes * 60
    token = uuid.uuid4().hex
    logger.info(f"🗂️ Starting group-topics loop (every {interval // 60} min when enabled)")
    while True:
        try:
            if runtime_config.get("group_topics_enabled"):
                await run_group_topics_once(redis, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in group-topics loop", exc_info=True)
        await asyncio.sleep(interval)
