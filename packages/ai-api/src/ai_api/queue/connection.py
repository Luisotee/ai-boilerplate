"""
Redis connection management (plain ``redis.asyncio``).

Two clients, for two different jobs:

- ``get_arq_redis()`` — a process-wide client, opened in the API lifespan and
  used by the readiness probe. Long-lived; never close it per request.
- ``get_redis_client()`` — a standalone client as an async context manager, for
  direct key operations (stream jobs, response chunks, job metadata). This is
  the one to use in new code.

The ``*_arq_*`` function names are historical: they used to return an arq pool.
The arq worker and the ``arq`` dependency are gone — chat and PDF processing
live in ``streams/`` (Redis Streams) — but the names are kept so existing
callers and test patches (``ai_api.main.get_arq_redis``) keep working.
"""

from redis.asyncio import Redis

from ..config import settings
from ..logger import logger

# Global client (reused across requests)
_redis_pool: Redis | None = None


def _new_client() -> Redis:
    """Build a Redis client from config.

    ``decode_responses=False``: callers (stream jobs, chunk storage) expect bytes
    and decode explicitly.
    """
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=settings.redis_password,
        decode_responses=False,
    )


async def create_arq_pool() -> Redis:
    """Create a new Redis client (with its own connection pool)."""
    logger.info(
        f"Creating Redis pool: {settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
    )
    return _new_client()


async def get_arq_redis() -> Redis:
    """
    Get or create the global Redis client.

    Reused across all API requests; do not close it per request.
    """
    global _redis_pool

    if _redis_pool is None:
        _redis_pool = await create_arq_pool()

    return _redis_pool


async def close_arq_redis() -> None:
    """Close the global Redis client. Application shutdown only."""
    global _redis_pool

    if _redis_pool is not None:
        logger.info("Closing Redis pool")
        await _redis_pool.aclose()
        _redis_pool = None


class RedisClientManager:
    """
    Async context manager for a Redis client with automatic cleanup.

    Ensures Redis connections are properly closed when exiting context.
    """

    def __init__(self):
        self.client = _new_client()

    async def __aenter__(self) -> Redis:
        """Enter async context - returns Redis client."""
        return self.client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context - closes Redis client."""
        await self.client.aclose()
        return False


def get_redis_client() -> RedisClientManager:
    """
    Get a standalone Redis client for direct operations.

    Separate from the shared client above — use this for chunk storage, job
    metadata, stream jobs, and anything else that touches keys directly.

    Usage:
        async with get_redis_client() as redis:
            await redis.set("key", "value")

    Returns:
        RedisClientManager that must be used as an async context manager
    """
    return RedisClientManager()
