"""The group-topics worker loop: opt-in, one worker at a time, one group can't stop the rest."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.rag.group_topics import IngestResult
from ai_api.streams import group_topics as loop_mod

MODULE = "ai_api.streams.group_topics"


def _redis(lease: bool = True, deferred: set[str] | None = None) -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=lease)
    # extend (when the lease is already taken) / release; 0 = not ours
    redis.eval = AsyncMock(return_value=1 if lease else 0)
    redis.smembers = AsyncMock(return_value={g.encode() for g in deferred or set()})
    redis.sadd = AsyncMock()
    redis.srem = AsyncMock()
    redis.expire = AsyncMock()
    return redis


def _patched(groups, ingest):
    return (
        patch(f"{MODULE}.create_embedding_service", return_value=MagicMock()),
        patch(f"{MODULE}.groups_with_new_messages", return_value=list(groups)),
        patch(f"{MODULE}.ingest_group", ingest),
    )


async def test_disabled_polls_the_setting_often():
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch(f"{MODULE}.runtime_config") as rc,
        patch(f"{MODULE}.run_group_topics_once", AsyncMock()) as once,
        patch(f"{MODULE}.asyncio.sleep", sleep),
    ):
        rc.get.return_value = False
        with pytest.raises(asyncio.CancelledError):
            await loop_mod.run_group_topics_loop(_redis())
    once.assert_not_called()
    rc.get.assert_called_with("group_topics_enabled")
    sleep.assert_awaited_once_with(loop_mod.DISABLED_POLL_SECONDS)


async def test_enabled_runs_then_sleeps_the_interval():
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with (
        patch(f"{MODULE}.runtime_config") as rc,
        patch(f"{MODULE}.run_group_topics_once", AsyncMock()) as once,
        patch(f"{MODULE}.asyncio.sleep", sleep),
        patch.object(loop_mod.settings, "group_topics_interval_minutes", 15),
    ):
        rc.get.return_value = True
        with pytest.raises(asyncio.CancelledError):
            await loop_mod.run_group_topics_loop(_redis())
    once.assert_awaited_once()
    sleep.assert_awaited_once_with(15 * 60)


async def test_lease_held_elsewhere_skips_the_run():
    redis = _redis(lease=False)
    with patch(f"{MODULE}.groups_with_new_messages") as groups:
        await loop_mod.run_group_topics_once(redis, "token")
    groups.assert_not_called()
    assert redis.eval.await_count == 1  # the extend attempt; no release


async def test_lease_is_renewed_before_each_block():
    redis = _redis()

    async def ingest(_redis, _group, _now, _emb, keep_lease):
        assert await keep_lease() is True
        assert await keep_lease() is True
        return IngestResult(blocks_done=2)

    with (
        patch(f"{MODULE}.create_embedding_service", return_value=MagicMock()),
        patch(f"{MODULE}.groups_with_new_messages", return_value=[uuid.uuid4()]),
        patch(f"{MODULE}.ingest_group", ingest),
    ):
        await loop_mod.run_group_topics_once(redis, "token")
    extends = [c for c in redis.eval.await_args_list if "expire" in c.args[0]]
    assert len(extends) == 2
    assert extends[0].args[2:] == (loop_mod.LOCK_KEY, "token", loop_mod.LOCK_TTL_SECONDS)


async def test_one_failing_group_does_not_stop_the_others():
    redis = _redis()
    a, b = uuid.uuid4(), uuid.uuid4()
    ingest = AsyncMock(side_effect=[RuntimeError("boom"), IngestResult(blocks_done=1)])
    p1, p2, p3 = _patched([a, b], ingest)
    with p1, p2, p3:
        await loop_mod.run_group_topics_once(redis, "token")
    assert [c.args[1] for c in ingest.await_args_list] == [a, b]
    redis.eval.assert_awaited_once()  # lease released


async def test_provider_outage_ends_the_run_and_defers_that_group():
    redis = _redis()
    a, b = uuid.uuid4(), uuid.uuid4()
    ingest = AsyncMock(return_value=IngestResult(unavailable=True))
    p1, p2, p3 = _patched([a, b], ingest)
    with p1, p2, p3:
        await loop_mod.run_group_topics_once(redis, "token")
    assert ingest.await_count == 1
    redis.sadd.assert_awaited_once_with(loop_mod.DEFERRED_KEY, str(a))


async def test_deferred_group_goes_last_and_is_cleared_on_success():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    redis = _redis(deferred={str(a)})
    ingest = AsyncMock(return_value=IngestResult(blocks_done=1))
    p1, p2, p3 = _patched([a, b, c], ingest)
    with p1, p2, p3:
        await loop_mod.run_group_topics_once(redis, "token")
    assert [call.args[1] for call in ingest.await_args_list] == [b, c, a]
    redis.srem.assert_awaited_once_with(loop_mod.DEFERRED_KEY, str(a))


async def test_lost_lease_ends_the_run():
    redis = _redis()
    ingest = AsyncMock(return_value=IngestResult(lease_lost=True))
    p1, p2, p3 = _patched([uuid.uuid4(), uuid.uuid4()], ingest)
    with p1, p2, p3:
        await loop_mod.run_group_topics_once(redis, "token")
    assert ingest.await_count == 1


async def test_run_summary_logs_skipped_blocks():
    redis = _redis()
    ingest = AsyncMock(return_value=IngestResult(blocks_done=1, blocks_skipped=2))
    p1, p2, p3 = _patched([uuid.uuid4()], ingest)
    with p1, p2, p3, patch(f"{MODULE}.logger") as log:
        await loop_mod.run_group_topics_once(redis, "token")
    summary = log.info.call_args.args[0]
    assert "2 blocks skipped" in summary and "1 blocks summarised" in summary


async def test_no_embedding_service_skips_and_releases():
    redis = _redis()
    with (
        patch(f"{MODULE}.create_embedding_service", return_value=None),
        patch(f"{MODULE}.groups_with_new_messages") as groups,
    ):
        await loop_mod.run_group_topics_once(redis, "token")
    groups.assert_not_called()
    redis.eval.assert_awaited_once()
