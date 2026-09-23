"""The stream worker runs the chat AND PDF consumers, and exits if either stops."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.scripts import run_stream_worker


@pytest.mark.asyncio
async def test_starts_both_consumers_and_exits_nonzero_when_one_stops():
    chat_cancelled = asyncio.Event()

    async def chat_consumer(_redis):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            chat_cancelled.set()
            raise

    async def pdf_consumer(_redis):
        raise RuntimeError("boom")

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()

    with (
        patch.object(run_stream_worker, "setup_instrumentation") as setup,
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", chat_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", pdf_consumer),
    ):
        code = await asyncio.wait_for(run_stream_worker.main(), timeout=5)

    assert code == 1
    setup.assert_called_once_with("ai-api-worker")
    assert chat_cancelled.is_set()
    fake_redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_pdf_consumer_receives_the_shared_redis_client():
    seen = {}

    async def chat_consumer(redis):
        seen["chat"] = redis
        return  # exits → worker shuts down

    async def pdf_consumer(redis):
        seen["pdf"] = redis
        await asyncio.sleep(3600)

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", chat_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", pdf_consumer),
    ):
        assert await asyncio.wait_for(run_stream_worker.main(), timeout=5) == 1

    assert seen == {"chat": fake_redis, "pdf": fake_redis}
