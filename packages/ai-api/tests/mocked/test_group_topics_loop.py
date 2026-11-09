"""The group-topics worker loop: opt-in, one worker at a time, one group can't stop the rest."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.rag.group_topics import IngestResult
from ai_api.streams import group_topics as loop_mod

MODULE = "ai_api.streams.group_topics"


def _redis(lease: bool = True) -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=lease)
    redis.eval = AsyncMock(return_value=1)
    return redis


async def test_disabled_does_nothing():
    with (
        patch(f"{MODULE}.runtime_config") as rc,
        patch(f"{MODULE}.run_group_topics_once", AsyncMock()) as once,
        patch(f"{MODULE}.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
    ):
        rc.get.return_value = False
        with pytest.raises(asyncio.CancelledError):
            await loop_mod.run_group_topics_loop(_redis())
    once.assert_not_called()
    rc.get.assert_called_with("group_topics_enabled")


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
    redis.eval.assert_not_called()


async def test_one_failing_group_does_not_stop_the_others():
    redis = _redis()
    a, b = uuid.uuid4(), uuid.uuid4()
    ingest = AsyncMock(side_effect=[RuntimeError("boom"), IngestResult(blocks_done=1)])
    with (
        patch(f"{MODULE}.create_embedding_service", return_value=MagicMock()),
        patch(f"{MODULE}.groups_with_new_messages", return_value=[a, b]),
        patch(f"{MODULE}.ingest_group", ingest),
    ):
        await loop_mod.run_group_topics_once(redis, "token")
    assert [c.args[1] for c in ingest.await_args_list] == [a, b]
    redis.eval.assert_awaited_once()  # lease released


async def test_provider_outage_ends_the_run():
    redis = _redis()
    ingest = AsyncMock(return_value=IngestResult(unavailable=True))
    with (
        patch(f"{MODULE}.create_embedding_service", return_value=MagicMock()),
        patch(f"{MODULE}.groups_with_new_messages", return_value=[uuid.uuid4(), uuid.uuid4()]),
        patch(f"{MODULE}.ingest_group", ingest),
    ):
        await loop_mod.run_group_topics_once(redis, "token")
    assert ingest.await_count == 1


async def test_no_embedding_service_skips_and_releases():
    redis = _redis()
    with (
        patch(f"{MODULE}.create_embedding_service", return_value=None),
        patch(f"{MODULE}.groups_with_new_messages") as groups,
    ):
        await loop_mod.run_group_topics_once(redis, "token")
    groups.assert_not_called()
    redis.eval.assert_awaited_once()
