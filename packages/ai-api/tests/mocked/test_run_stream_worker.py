"""The stream worker runs the chat, PDF, broadcast, scheduler and scheduled-sender tasks,
and exits if any stops."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.scripts import run_stream_worker


async def _idle_consumer(_redis=None):
    await asyncio.sleep(3600)


@pytest.fixture(autouse=True)
def idle_broadcast_consumer():
    """Keep the real broadcast consumer, scheduler and sender (DB polling) out of these tests."""
    with (
        patch.object(run_stream_worker, "run_broadcast_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_scheduler", _idle_consumer),
        patch.object(run_stream_worker, "run_scheduled_sender", _idle_consumer),
    ):
        yield


@pytest.fixture(autouse=True)
def no_init_db():
    """The worker creates missing tables at start; there is no database here."""
    with patch.object(run_stream_worker, "init_db") as init_db:
        yield init_db


@pytest.fixture(autouse=True)
def idle_group_topics_loop():
    """Keep the real group-topics loop (runtime_config + Redis lease) out of these tests."""
    with patch.object(run_stream_worker, "run_group_topics_loop", _idle_consumer):
        yield


@pytest.mark.asyncio
async def test_a_crashed_scheduler_stops_the_worker():
    async def scheduler(_redis):
        raise RuntimeError("boom")

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_scheduler", scheduler),
    ):
        code = await asyncio.wait_for(run_stream_worker.main(), timeout=5)
    assert code == 1


@pytest.mark.asyncio
async def test_a_crashed_scheduled_sender_stops_the_worker():
    async def sender():
        raise RuntimeError("boom")

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_scheduled_sender", sender),
    ):
        code = await asyncio.wait_for(run_stream_worker.main(), timeout=5)
    assert code == 1


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


@pytest.mark.asyncio
async def test_broadcast_consumer_crash_stops_the_worker():
    async def broadcast_consumer(_redis):
        raise RuntimeError("boom")

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_broadcast_consumer", broadcast_consumer),
    ):
        code = await asyncio.wait_for(run_stream_worker.main(), timeout=5)

    assert code == 1


@pytest.mark.asyncio
async def test_group_topics_loop_runs_and_its_crash_stops_the_worker():
    seen = {}

    async def group_topics_loop(redis):
        seen["group_topics"] = redis
        raise RuntimeError("boom")

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", _idle_consumer),
        patch.object(run_stream_worker, "run_group_topics_loop", group_topics_loop),
    ):
        code = await asyncio.wait_for(run_stream_worker.main(), timeout=5)

    assert code == 1
    assert seen == {"group_topics": fake_redis}


@pytest.mark.asyncio
async def test_init_db_runs_first_and_retries_a_lost_race(no_init_db):
    no_init_db.side_effect = [RuntimeError("duplicate key value (pg_type)"), None]

    async def chat_consumer(_redis):
        return

    fake_redis = MagicMock()
    fake_redis.aclose = AsyncMock()
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "INIT_DB_RETRY_SECONDS", 0),
        patch.object(run_stream_worker, "Redis", return_value=fake_redis),
        patch.object(run_stream_worker, "run_stream_consumer", chat_consumer),
        patch.object(run_stream_worker, "run_pdf_consumer", _idle_consumer),
    ):
        assert await asyncio.wait_for(run_stream_worker.main(), timeout=5) == 1
    assert no_init_db.call_count == 2


@pytest.mark.asyncio
async def test_init_db_failing_every_time_stops_the_worker(no_init_db):
    no_init_db.side_effect = RuntimeError("database down")
    with (
        patch.object(run_stream_worker, "setup_instrumentation"),
        patch.object(run_stream_worker, "INIT_DB_RETRY_SECONDS", 0),
        patch.object(run_stream_worker, "Redis") as redis_cls,
        pytest.raises(RuntimeError),
    ):
        await asyncio.wait_for(run_stream_worker.main(), timeout=5)
    assert no_init_db.call_count == run_stream_worker.INIT_DB_ATTEMPTS
    redis_cls.assert_not_called()
