"""Tests for queue/connection.py after the arq removal (plain redis.asyncio)."""

import importlib.util
from unittest.mock import AsyncMock, patch

from redis.asyncio import Redis

from ai_api.queue import connection


class TestNoArq:
    def test_arq_is_not_installed(self):
        # The dependency was removed with the dead arq worker. If it comes back
        # (e.g. as a transitive dep), nothing here should start importing it.
        assert importlib.util.find_spec("arq") is None

    def test_arq_worker_modules_are_gone(self):
        assert importlib.util.find_spec("ai_api.queue.worker") is None
        assert importlib.util.find_spec("ai_api.scripts.run_worker") is None


class TestSharedClient:
    async def test_get_arq_redis_returns_plain_redis_singleton(self):
        connection._redis_pool = None
        try:
            first = await connection.get_arq_redis()
            second = await connection.get_arq_redis()
            assert isinstance(first, Redis)
            assert first is second
        finally:
            connection._redis_pool = None

    async def test_close_arq_redis_closes_and_resets(self):
        fake = AsyncMock(spec=Redis)
        connection._redis_pool = fake
        await connection.close_arq_redis()
        fake.aclose.assert_awaited_once()
        assert connection._redis_pool is None

    async def test_close_arq_redis_is_noop_when_unopened(self):
        connection._redis_pool = None
        await connection.close_arq_redis()
        assert connection._redis_pool is None

    async def test_client_uses_settings(self):
        with patch.object(connection, "Redis") as redis_cls:
            await connection.create_arq_pool()
        kwargs = redis_cls.call_args.kwargs
        assert kwargs["host"] == connection.settings.redis_host
        assert kwargs["port"] == connection.settings.redis_port
        assert kwargs["db"] == connection.settings.redis_db
        assert kwargs["decode_responses"] is False


class TestStandaloneClient:
    async def test_context_manager_closes_client(self):
        manager = connection.get_redis_client()
        manager.client = AsyncMock(spec=Redis)
        async with manager as client:
            assert client is manager.client
        manager.client.aclose.assert_awaited_once()
