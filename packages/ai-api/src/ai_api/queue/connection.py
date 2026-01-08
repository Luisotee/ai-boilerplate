"""
Redis connection management for arq queue.

Provides connection pooling and arq client initialization
shared between API endpoints and worker processes.
"""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import Redis

from ..config import settings
from ..logger import logger

# Global connection pool (reused across requests)
_arq_pool: ArqRedis | None = None


def get_redis_settings() -> RedisSettings:
    """
    Create Redis settings from config.

    Returns:
        RedisSettings for arq connection
    """
    return RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
        database=settings.redis_db,
        password=settings.redis_password,
    )


async def create_arq_pool() -> ArqRedis:
    """
    Create a new arq Redis connection pool.

    Returns:
        ArqRedis connection pool instance
    """
    redis_settings = get_redis_settings()

    logger.info(
        f"Creating arq Redis pool: {redis_settings.host}:{redis_settings.port}/{redis_settings.database}"
    )

    pool = await create_pool(redis_settings)

    return pool


async def get_arq_redis() -> ArqRedis:
    """
    Get or create the global arq Redis connection pool.

    This connection pool is reused across all API requests
    and should not be closed manually.

    Returns:
        ArqRedis connection pool instance
    """
    global _arq_pool

    if _arq_pool is None:
        _arq_pool = await create_arq_pool()

    return _arq_pool


async def close_arq_redis() -> None:
    """
    Close the global arq Redis connection pool.

    Should only be called during application shutdown.
    """
    global _arq_pool

    if _arq_pool is not None:
        logger.info("Closing arq Redis pool")
        await _arq_pool.close()
        _arq_pool = None


async def get_redis_client() -> Redis:
    """
    Get a standalone Redis client for custom operations.

    This is separate from the arq pool and should be used
    for direct Redis operations (chunk storage, metadata, etc.).

    Returns:
        Redis client instance
    """
    redis_settings = get_redis_settings()

    client = Redis(
        host=redis_settings.host,
        port=redis_settings.port,
        db=redis_settings.database,
        password=redis_settings.password,
        decode_responses=False,  # Return bytes for compatibility
    )

    return client
