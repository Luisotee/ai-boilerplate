"""Chat stream crash recovery: orphaned entries, ownership, already-finished jobs."""

import json
from unittest.mock import AsyncMock, patch

import fakeredis
import pytest

from ai_api.streams import consumer as consumer_module
from ai_api.streams import manager
from ai_api.streams.manager import GROUP_NAME

USER = "user-1"
STREAM = f"stream:user:{USER}"


def _job(job_id: str, text: str = "hi") -> dict:
    return {
        "job_id": job_id,
        "user_id": USER,
        "whatsapp_jid": "5511999999999@s.whatsapp.net",
        "message": text,
        "conversation_type": "private",
        "user_message_id": f"umid-{job_id}",
    }


@pytest.fixture(autouse=True)
def _no_burst_merging(monkeypatch):
    """These tests are about recovery order; burst merging has its own tests."""
    from ai_api.config import settings

    monkeypatch.setattr(settings, "chat_burst_quiet_ms", 0)


@pytest.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis()
    await client.xgroup_create(STREAM, GROUP_NAME, id="0", mkstream=True)
    yield client
    await client.aclose()


async def _deliver_to(redis, consumer: str, count: int = 1):
    """Simulate another worker reading entries (they become its pending ones)."""
    await redis.xreadgroup(GROUP_NAME, consumer, {STREAM: ">"}, count=count)


async def _run_user_stream(redis):
    """One pass of process_user_stream with the processor mocked; returns the
    job ids it ran, in order."""
    ran: list[str] = []

    async def fake_process(user_id, message_id, data):
        ran.append(data[b"job_id"].decode())

    with (
        patch.object(consumer_module, "process_single_message", side_effect=fake_process),
        patch.object(consumer_module, "read_stream_messages", _nonblocking_read(redis)),
    ):
        await consumer_module.process_user_stream(redis, USER, {"running": True})
    return ran


def _nonblocking_read(redis):
    async def read(_redis, user_id, count=1, block=5000):
        return await redis.xreadgroup(
            GROUP_NAME, manager.CONSUMER_ID, {f"stream:user:{user_id}": ">"}, count=count
        )

    return read


async def test_orphan_of_a_dead_worker_runs_before_newer_messages(redis):
    await redis.xadd(STREAM, _job("old"))
    await _deliver_to(redis, "worker-dead")  # died mid-run, no heartbeat
    await redis.xadd(STREAM, _job("new"))

    with patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0):
        ran = await _run_user_stream(redis)

    assert ran == ["old", "new"]
    assert (await redis.xpending(STREAM, GROUP_NAME))["pending"] == 0


async def test_stream_held_by_a_live_worker_is_left_alone(redis):
    await redis.xadd(STREAM, _job("busy"))
    await _deliver_to(redis, "worker-live")
    await redis.set(f"{manager.HEARTBEAT_KEY_PREFIX}worker-live", "1", ex=30)
    await redis.xadd(STREAM, _job("next"))

    ran = await _run_user_stream(redis)

    assert ran == []  # neither the in-flight job nor the next one (order)
    pending = await redis.xpending_range(STREAM, GROUP_NAME, "-", "+", 10)
    assert [p["consumer"] for p in pending] == [b"worker-live"]


async def test_finished_job_is_acked_without_running_again(redis):
    await redis.xadd(STREAM, _job("done"))
    await _deliver_to(redis, "worker-dead")
    await redis.set("job:meta:done", json.dumps({"total_chunks": 1}))  # success: no status

    with patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0):
        ran = await _run_user_stream(redis)

    assert ran == []
    assert (await redis.xpending(STREAM, GROUP_NAME))["pending"] == 0


async def test_non_terminal_metadata_still_reruns(redis):
    await redis.xadd(STREAM, _job("half"))
    await _deliver_to(redis, "worker-dead")
    await redis.set("job:meta:half", json.dumps({"status": "in_progress"}))

    with patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0):
        ran = await _run_user_stream(redis)

    assert ran == ["half"]


async def test_scheduled_entry_skips_the_finished_check(redis):
    entry = {"trigger": "scheduled", "run_id": "r1", "job_id": "sched-1", "user_id": USER}
    await redis.xadd(STREAM, entry)
    await redis.set("job:meta:sched-1", json.dumps({"total_chunks": 1}))

    ran = await _run_user_stream(redis)

    assert ran == ["sched-1"]


async def test_failed_job_is_still_acked(redis):
    await redis.xadd(STREAM, _job("boom"))

    with (
        patch.object(
            consumer_module, "process_single_message", AsyncMock(side_effect=RuntimeError("x"))
        ),
        patch.object(consumer_module, "read_stream_messages", _nonblocking_read(redis)),
    ):
        await consumer_module.process_user_stream(redis, USER, {"running": True})

    assert (await redis.xpending(STREAM, GROUP_NAME))["pending"] == 0


def test_heartbeat_thread_keeps_this_worker_alive_until_stopped():
    """The key is refreshed from a thread (a blocked event loop can't stall it)
    and deleted on a clean stop, so the next worker takes over at once."""
    import threading

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    key = f"{manager.HEARTBEAT_KEY_PREFIX}{manager.CONSUMER_ID}"
    stop = manager.start_heartbeat_thread(client)
    try:
        for _ in range(100):
            if client.exists(key):
                break
            threading.Event().wait(0.01)
        assert client.exists(key)
        assert 0 < client.ttl(key) <= manager.HEARTBEAT_TTL_SECONDS
    finally:
        stop.set()
    for _ in range(100):
        if not client.exists(key):
            break
        threading.Event().wait(0.01)
    assert not client.exists(key)


async def test_heartbeat_key_means_alive(redis):
    await redis.set(f"{manager.HEARTBEAT_KEY_PREFIX}{manager.CONSUMER_ID}", "1", ex=30)
    assert await manager.is_consumer_alive(redis, manager.CONSUMER_ID)
    assert not await manager.is_consumer_alive(redis, "worker-gone")


async def test_own_recent_leftover_keeps_its_place_next_to_a_dead_workers_entry(redis):
    """Own leftovers are claimed without the idle floor, a dead worker's with it."""
    import asyncio

    await redis.xadd(STREAM, _job("old"))
    await _deliver_to(redis, "worker-dead")
    await asyncio.sleep(0.1)
    await redis.xadd(STREAM, _job("mine"))
    await _deliver_to(redis, manager.CONSUMER_ID)  # e.g. a failed ack, moments ago
    await redis.xadd(STREAM, _job("new"))

    merged: list[str] = []

    async def merged_run(**kwargs):
        merged.extend([*kwargs["absorbed_job_ids"], kwargs["job_id"]])

    with (
        patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 50),
        patch.object(consumer_module, "process_chat_job_direct", side_effect=merged_run),
    ):
        ran = await _run_user_stream(redis)

    # The two recovered entries run first (merged: same sender), then the new one
    assert merged + ran == ["old", "mine", "new"]


async def test_consumer_group_is_created_once_per_stream():
    client = fakeredis.FakeAsyncRedis()
    real_create = client.xgroup_create
    calls = []

    async def create(*args, **kwargs):
        calls.append(args)
        return await real_create(*args, **kwargs)

    try:
        with patch.object(client, "xgroup_create", create):
            await manager.ensure_consumer_group(client, "u9")
            await manager.ensure_consumer_group(client, "u9")
            assert len(calls) == 1
            manager.forget_consumer_group(client, "u9")
            await manager.ensure_consumer_group(client, "u9")
            assert len(calls) == 2
    finally:
        await client.aclose()


def test_consumer_id_is_unique_per_process_start():
    # pid 1 in every container: the id must still differ between starts.
    assert manager.CONSUMER_ID.count("-") >= 3


def test_stream_entry_time():
    assert manager.stream_entry_time_ms("1794236767849-0") == 1794236767849
    assert manager.stream_entry_time_ms("garbage") is None


async def test_new_stream_without_a_group_is_processed():
    """XADD creates the stream but not the group (a new user's first message)."""
    client = fakeredis.FakeAsyncRedis()
    try:
        await client.xadd(STREAM, _job("first"))
        ran = await _run_user_stream(client)
        assert ran == ["first"]
        assert (await client.xpending(STREAM, GROUP_NAME))["pending"] == 0
    finally:
        await client.aclose()


async def test_a_failing_stream_gives_up_after_repeated_errors(redis):
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch.object(consumer_module, "claim_orphaned_chat_entries", failing),
        patch.object(consumer_module.asyncio, "sleep", AsyncMock()),
    ):
        await consumer_module.process_user_stream(redis, USER, {"running": True})
    assert failing.await_count == consumer_module.MAX_CONSECUTIVE_STREAM_ERRORS


async def test_recently_delivered_entry_of_a_silent_worker_is_not_taken(redis):
    """No heartbeat but delivered moments ago: maybe a live worker that missed
    its heartbeat (blocked loop, pre-heartbeat worker), so wait."""
    await redis.xadd(STREAM, _job("inflight"))
    await _deliver_to(redis, "worker-quiet")
    await redis.xadd(STREAM, _job("next"))

    ran = await _run_user_stream(redis)

    assert ran == []
    pending = await redis.xpending_range(STREAM, GROUP_NAME, "-", "+", 10)
    assert [p["consumer"] for p in pending] == [b"worker-quiet"]


async def test_day_old_orphan_is_dropped_without_a_notice(redis):
    await redis.xadd(STREAM, _job("ancient"), id="1000-0")
    await _deliver_to(redis, "worker-1")

    with (
        patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0),
        patch.object(consumer_module, "push_text_once", AsyncMock()) as push,
    ):
        ran = await _run_user_stream(redis)

    assert ran == []
    push.assert_not_awaited()
    assert await redis.get("job:meta:ancient") is None
    assert (await redis.xpending(STREAM, GROUP_NAME))["pending"] == 0


class TestUnconfirmedLateReply:
    async def _finished_late_orphan(self, redis):
        import time

        late_ms = int((time.time() - 300) * 1000)
        await redis.xadd(STREAM, _job("slow"), id=f"{late_ms}-0")
        await _deliver_to(redis, "worker-dead")
        await redis.set("job:meta:slow", json.dumps({"total_chunks": 1}))
        await redis.rpush("job:chunks:slow", json.dumps({"index": 0, "content": "the answer"}))

    async def test_is_pushed_on_recovery(self, redis):
        await self._finished_late_orphan(redis)
        with (
            patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0),
            patch.object(consumer_module, "push_text_once", AsyncMock()) as push,
        ):
            ran = await _run_user_stream(redis)

        assert ran == []
        assert push.await_args.args[4] == "the answer"

    async def test_is_not_pushed_when_already_delivered(self, redis):
        await self._finished_late_orphan(redis)
        await redis.set("job:claim:slow", "poll")
        with (
            patch.object(manager, "ORPHAN_CLAIM_MIN_IDLE_MS", 0),
            patch.object(consumer_module, "push_text_once", AsyncMock()) as push,
        ):
            await _run_user_stream(redis)

        push.assert_not_awaited()
