"""Group-topics loop of the stream worker.

Every ``GROUP_TOPICS_INTERVAL_MINUTES``, while the hot ``group_topics_enabled``
setting is on, summarise each group's finished conversations into topics
(``rag/group_topics.py``). While it is off, the loop re-checks the setting
every :data:`DISABLED_POLL_SECONDS`, so turning it on applies within that.

A Redis lease (``group_topics:lock``, ``streams/lease.py``) lets one worker
process run at a time. It is renewed before every block, and a run that loses
it stops, so two workers never summarise the same block. A run also stops
starting new groups after :data:`RUN_BUDGET_SECONDS`; groups that waited
longest go first, so the rest are picked up by the next run.
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
from .lease import acquire_lease, extend_lease, release_lease

LOCK_KEY = "group_topics:lock"
#: Lease TTL. Renewed before each block, so it only has to outlive one block
#: (a summary is capped at 120 s, plus its embedding and store).
LOCK_TTL_SECONDS = 600
#: Stop starting new groups once a run is this old.
RUN_BUDGET_SECONDS = 900
#: How often a disabled loop re-reads ``group_topics_enabled``.
DISABLED_POLL_SECONDS = 30
#: Groups whose last try hit a provider failure. They go last in the next run,
#: so a group whose block makes a provider fail (say a 5xx on that content)
#: can't hold up every other group. Members are dropped once the group succeeds.
DEFERRED_KEY = "group_topics:deferred"
DEFERRED_TTL_SECONDS = 24 * 3600


def _utcnow() -> datetime:
    """Naive UTC, matching the DB's ``datetime.utcnow`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _deferred_groups(redis: Redis) -> set[str]:
    try:
        members = await redis.smembers(DEFERRED_KEY)
    except Exception:
        logger.warning("Group topics: could not read deferred groups", exc_info=True)
        return set()
    return {m.decode() if isinstance(m, bytes) else str(m) for m in members}


async def _set_deferred(redis: Redis, group_id: uuid.UUID, deferred: bool) -> None:
    try:
        if deferred:
            await redis.sadd(DEFERRED_KEY, str(group_id))
            await redis.expire(DEFERRED_KEY, DEFERRED_TTL_SECONDS)
        else:
            await redis.srem(DEFERRED_KEY, str(group_id))
    except Exception:
        logger.warning("Group topics: could not update deferred groups", exc_info=True)


async def run_group_topics_once(redis: Redis, token: str) -> None:
    """One run over every group with new messages, under the lease."""
    if not await acquire_lease(redis, LOCK_KEY, token, LOCK_TTL_SECONDS):
        logger.debug("Group topics: another worker holds the lease")
        return
    try:
        embedding_service = create_embedding_service(settings.gemini_api_key)
        if embedding_service is None:
            logger.warning("Group topics: no embedding service (GEMINI_API_KEY unset), skipping")
            return

        async def keep_lease() -> bool:
            return await extend_lease(redis, LOCK_KEY, token, LOCK_TTL_SECONDS)

        loop = asyncio.get_running_loop()
        started = loop.time()
        now = _utcnow()
        group_ids = await asyncio.to_thread(groups_with_new_messages, now)
        deferred = await _deferred_groups(redis)
        group_ids.sort(key=lambda g: str(g) in deferred)  # stable: deferred go last
        done = topics = skipped = 0
        for group_id in group_ids:
            if loop.time() - started > RUN_BUDGET_SECONDS:
                logger.info("Group topics: run budget used, the rest waits for the next run")
                break
            try:
                result = await ingest_group(
                    redis, group_id, now, embedding_service, keep_lease=keep_lease
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"Group topics: group {group_id} failed", exc_info=True)
                continue
            done += result.blocks_done
            topics += result.topics_stored
            skipped += result.blocks_skipped
            if result.lease_lost:
                logger.warning("Group topics: lease lost, stopping this run")
                break
            if result.unavailable:
                # Every group shares the providers: trying the rest now would
                # only add failed calls (and 429 pressure). This group goes
                # last next time, in case the failure is its own.
                await _set_deferred(redis, group_id, True)
                break
            if str(group_id) in deferred:
                await _set_deferred(redis, group_id, False)
        if group_ids:
            logger.info(
                f"Group topics: {len(group_ids)} groups checked, {done} blocks summarised, "
                f"{topics} topics stored, {skipped} blocks skipped after repeated failures"
            )
    finally:
        await release_lease(redis, LOCK_KEY, token)


async def run_group_topics_loop(redis: Redis) -> None:
    """Worker task: run the group-topics job every interval while it is enabled."""
    interval = settings.group_topics_interval_minutes * 60
    token = uuid.uuid4().hex
    logger.info(f"🗂️ Starting group-topics loop (every {interval // 60} min when enabled)")
    while True:
        enabled = False
        try:
            enabled = bool(runtime_config.get("group_topics_enabled"))
            if enabled:
                await run_group_topics_once(redis, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in group-topics loop", exc_info=True)
        await asyncio.sleep(interval if enabled else DISABLED_POLL_SECONDS)
