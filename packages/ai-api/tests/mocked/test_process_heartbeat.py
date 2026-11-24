"""Per-process heartbeats in Redis (process_heartbeat.py)."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import fakeredis

from ai_api import process_heartbeat as hb
from ai_api.runtime_config import REGISTRY, SECRET_MASK

STARTED = datetime(2026, 11, 20, 5, 0, tzinfo=UTC)


def _values(**overrides):
    def get(key):
        return overrides.get(key, f"value-of-{key}")

    return get


def test_payload_masks_secrets_and_covers_every_setting():
    with patch.object(hb.runtime_config, "get", side_effect=_values()):
        payload = hb.heartbeat_payload("worker", STARTED)
    assert payload["process"] == "worker"
    assert set(payload["settings"]) == {s.key for s in REGISTRY}
    for spec in REGISTRY:
        if spec.secret:
            assert payload["settings"][spec.key] == SECRET_MASK
    assert payload["prompt_code_hash"]
    json.dumps(payload, default=str)


async def test_publish_then_read_back():
    redis = fakeredis.FakeAsyncRedis()
    with patch.object(hb.runtime_config, "get", side_effect=_values()):
        await hb.publish_heartbeat(redis, "api", STARTED)
        await hb.publish_heartbeat(redis, "worker", STARTED)
    processes = await hb.read_processes(redis)
    assert [p["process"] for p in processes] == ["api", "worker"]
    assert processes[0]["started_at"] == STARTED
    keys = [k.decode() async for k in redis.scan_iter(match="admin:process:*")]
    for key in keys:
        assert 0 < await redis.ttl(key) <= hb.HEARTBEAT_TTL_SECONDS


async def test_malformed_heartbeats_are_skipped():
    redis = fakeredis.FakeAsyncRedis()
    await redis.set("admin:process:api:x:1", "not json")
    await redis.set("admin:process:api:y:1", json.dumps({"process": "api"}))
    assert await hb.read_processes(redis) == []


async def test_a_failing_redis_never_stops_the_loop():
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError("redis down")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    with (
        patch.object(hb.runtime_config, "get", side_effect=_values()),
        patch.object(hb.asyncio, "sleep", fake_sleep),
    ):
        try:
            await hb.run_heartbeat(redis, "api")
        except asyncio.CancelledError:
            pass
    assert redis.set.await_count == 2
    assert sleeps == [hb.HEARTBEAT_INTERVAL_SECONDS] * 2


def _beat(process, instance, started_hour, **extra):
    started = datetime(2026, 11, 20, started_hour, tzinfo=UTC).isoformat()
    payload = {
        "process": process,
        "instance": instance,
        "pid": 1,
        "started_at": started,
        "last_seen_at": started,
        "prompt_code_hash": "h",
        "settings": {},
    }
    payload.update(extra)
    return json.dumps(payload)


async def test_a_heartbeat_missing_a_field_or_mistyped_is_skipped():
    redis = fakeredis.FakeAsyncRedis()
    good = _beat("api", "a", 5)
    no_instance = json.loads(_beat("worker", "w", 5))
    del no_instance["instance"]
    await redis.set("admin:process:api:a:1", good)
    await redis.set("admin:process:worker:w:1", json.dumps(no_instance))
    await redis.set("admin:process:worker:x:1", _beat("worker", "x", 5, pid="one"))
    await redis.set("admin:process:worker:y:1", "[1, 2]")
    assert [p["instance"] for p in await hb.read_processes(redis)] == ["a"]


async def test_a_replaced_containers_heartbeat_is_hidden_by_the_newer_one():
    redis = fakeredis.FakeAsyncRedis()
    await redis.set("admin:process:worker:old:1", _beat("worker", "old", 5))
    await redis.set("admin:process:worker:new:1", _beat("worker", "new", 6))
    await redis.set("admin:process:api:a:1", _beat("api", "a", 4))
    processes = await hb.read_processes(redis)
    assert [(p["process"], p["instance"]) for p in processes] == [("api", "a"), ("worker", "new")]


async def test_shutdown_deletes_its_own_key():
    redis = fakeredis.FakeAsyncRedis()
    with patch.object(hb.runtime_config, "get", side_effect=_values()):
        task = asyncio.create_task(hb.run_heartbeat(redis, "api"))
        for _ in range(50):
            if [k async for k in redis.scan_iter(match="admin:process:*")]:
                break
            await asyncio.sleep(0.01)
        assert await hb.read_processes(redis)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert await hb.read_processes(redis) == []


def test_a_concurrent_replica_that_still_refreshes_stays_listed():
    now = datetime(2026, 11, 20, 6, 0, 30, tzinfo=UTC)

    def proc(instance, started_hour, last_seen):
        started = datetime(2026, 11, 20, started_hour, tzinfo=UTC)
        return {
            "process": "worker",
            "instance": instance,
            "started_at": started,
            "last_seen_at": last_seen,
        }

    live_old = proc("a", 5, now)
    dead_old = proc("b", 4, datetime(2026, 11, 20, 5, 58, tzinfo=UTC))
    newest = proc("c", 6, now)
    kept = hb._drop_replaced_instances([live_old, dead_old, newest], now=now)
    assert [p["instance"] for p in kept] == ["a", "c"]
