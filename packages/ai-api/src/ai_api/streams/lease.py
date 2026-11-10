"""A Redis lease: one worker process at a time runs a fleet-wide job.

``SET key token NX EX ttl`` takes it; :func:`extend_lease` renews it only while
this token still holds it, and :func:`release_lease` deletes it only then, so a
worker whose lease expired can't extend or drop another worker's.
(``broadcast_consumer.py`` predates this module and keeps its own copy.)
"""

from redis.asyncio import Redis

from ..logger import logger

_EXTEND_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


async def acquire_lease(redis: Redis, key: str, token: str, ttl_seconds: int) -> bool:
    """Take the lease, or renew it if ``token`` already holds it."""
    if await redis.set(key, token, nx=True, ex=ttl_seconds):
        return True
    return await extend_lease(redis, key, token, ttl_seconds)


async def extend_lease(redis: Redis, key: str, token: str, ttl_seconds: int) -> bool:
    """Renew the lease for ``ttl_seconds``; False when ``token`` no longer holds it."""
    return bool(await redis.eval(_EXTEND_LUA, 1, key, token, ttl_seconds))


async def release_lease(redis: Redis, key: str, token: str) -> None:
    """Drop the lease if ``token`` still holds it. Never raises."""
    try:
        await redis.eval(_RELEASE_LUA, 1, key, token)
    except Exception:
        logger.warning(f"Failed to release the Redis lease {key}", exc_info=True)
