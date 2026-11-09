"""Unit tests for ai_api.rag.group_topics: block splitting, ids, ingest and summary."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_api.config import settings
from ai_api.rag import group_topics as gt
from ai_api.rag.group_topics import (
    GAP,
    MAX_BLOCK,
    MIN_BLOCK,
    OVERLAP,
    POISON_LIMIT,
    STALE_TAIL,
    Block,
    Msg,
    Topic,
    format_topic_results,
    render_block,
    split_into_blocks,
    summarise_block,
    topic_id,
)

T0 = datetime(2026, 11, 1, 9, 0)
MODULE = "ai_api.rag.group_topics"


def _msgs(n: int, start: datetime = T0, step: timedelta = timedelta(minutes=1)) -> list[Msg]:
    return [
        Msg(id=uuid.uuid4(), timestamp=start + i * step, role="user", content=f"m{i}")
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _env_runtime_config():
    with patch(f"{MODULE}.runtime_config") as rc:
        rc.get.side_effect = lambda key: getattr(settings, key)
        yield rc


# ---------------------------------------------------------------------------
# split_into_blocks
# ---------------------------------------------------------------------------


class TestSplitIntoBlocks:
    def test_empty(self):
        assert split_into_blocks([], T0) == []

    def test_gap_ends_a_conversation(self):
        first = _msgs(30)
        second = _msgs(30, start=first[-1].timestamp + GAP + timedelta(minutes=1))
        now = second[-1].timestamp + GAP + timedelta(minutes=1)
        blocks = split_into_blocks(first + second, now)
        assert [len(b.messages) for b in blocks] == [30, 30]
        assert blocks[0].messages == tuple(first)

    def test_small_conversation_merges_into_next(self):
        small = _msgs(5)
        big = _msgs(30, start=small[-1].timestamp + GAP * 2)
        now = big[-1].timestamp + GAP * 2
        blocks = split_into_blocks(small + big, now)
        assert len(blocks) == 1
        assert len(blocks[0].messages) == 35

    def test_long_conversation_is_capped(self):
        msgs = _msgs(MAX_BLOCK * 2 + 30)
        now = msgs[-1].timestamp + GAP * 2
        blocks = split_into_blocks(msgs, now)
        assert [len(b.messages) for b in blocks] == [MAX_BLOCK, MAX_BLOCK, 30]

    def test_open_conversation_is_held(self):
        msgs = _msgs(40)
        now = msgs[-1].timestamp + timedelta(minutes=30)  # chat still going
        assert split_into_blocks(msgs, now) == []

    def test_full_block_of_an_open_conversation_is_taken(self):
        msgs = _msgs(MAX_BLOCK + 10)
        now = msgs[-1].timestamp + timedelta(minutes=5)
        blocks = split_into_blocks(msgs, now)
        assert [len(b.messages) for b in blocks] == [MAX_BLOCK]

    def test_small_finished_tail_waits_then_goes_when_stale(self):
        msgs = _msgs(MIN_BLOCK - 5)
        assert split_into_blocks(msgs, msgs[-1].timestamp + GAP * 2) == []
        stale = split_into_blocks(msgs, msgs[-1].timestamp + STALE_TAIL + timedelta(minutes=1))
        assert len(stale) == 1 and len(stale[0].messages) == MIN_BLOCK - 5

    def test_blocks_are_a_prefix_of_the_messages(self):
        done = _msgs(30)
        going = _msgs(30, start=done[-1].timestamp + GAP * 2)
        now = going[-1].timestamp + timedelta(minutes=10)
        blocks = split_into_blocks(done + going, now)
        assert [b.messages for b in blocks] == [tuple(done)]

    def test_context_is_the_overlap_before_each_block(self):
        prior = _msgs(8, start=T0 - timedelta(hours=10))
        first = _msgs(30)
        second = _msgs(30, start=first[-1].timestamp + GAP * 2)
        now = second[-1].timestamp + GAP * 2
        blocks = split_into_blocks(first + second, now, prior)
        assert blocks[0].context == tuple(prior[-OVERLAP:])
        assert blocks[1].context == tuple(first[-OVERLAP:])


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


class TestIds:
    def test_block_key_depends_only_on_message_ids(self):
        msgs = _msgs(3)
        changed = [Msg(m.id, m.timestamp, m.role, "edited", "x") for m in msgs]
        assert Block(tuple(msgs)).key == Block(tuple(changed), context=tuple(msgs)).key
        assert Block(tuple(msgs)).key != Block(tuple(msgs[:2])).key

    def test_topic_id_is_deterministic(self):
        assert topic_id("abc", 0) == topic_id("abc", 0)
        assert topic_id("abc", 0) != topic_id("abc", 1)
        assert topic_id("abc", 0) != topic_id("abd", 0)


# ---------------------------------------------------------------------------
# render / summarise
# ---------------------------------------------------------------------------


class TestRenderAndSummarise:
    def test_instructions_follow_the_chat_language(self):
        assert "language most of the messages are written in" in gt.TOPIC_INSTRUCTIONS
        assert "Portuguese" not in gt.TOPIC_INSTRUCTIONS

    def test_render_labels_context_and_clips_long_messages(self):
        ctx = _msgs(2, start=T0 - timedelta(hours=3))
        long = Msg(uuid.uuid4(), T0, "user", "x" * 5000, "Ana")
        bot = Msg(uuid.uuid4(), T0, "assistant", "hello")
        prompt = render_block(Block((long, bot), context=tuple(ctx)))
        assert prompt.index("Earlier messages") < prompt.index("Messages:\n")
        assert "Ana: " + "x" * gt.MAX_MESSAGE_CHARS + " […]" in prompt
        assert "x" * (gt.MAX_MESSAGE_CHARS + 1) not in prompt
        assert "Assistant: hello" in prompt

    async def test_summarise_uses_the_runtime_model_and_caps_topics(self):
        seen = {}

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen["tools"] = [t.name for t in info.function_tools]
            topics = [{"subject": f" S{i} ", "summary": f"sum {i}"} for i in range(7)]
            topics.append({"subject": "  ", "summary": "dropped"})
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, {"response": topics})]
            )

        with patch("ai_api.agent.core.build_runtime_model", return_value=FunctionModel(model_fn)):
            topics = await summarise_block(Block(tuple(_msgs(3))))
        assert seen["tools"] == []  # no tool schemas paid for
        assert [t.subject for t in topics] == ["S0", "S1", "S2", "S3", "S4"]


# ---------------------------------------------------------------------------
# ingest_group
# ---------------------------------------------------------------------------


def _blocks_of(n_blocks: int) -> tuple[list[Msg], datetime]:
    msgs: list[Msg] = []
    start = T0
    for _ in range(n_blocks):
        chunk = _msgs(30, start=start)
        msgs += chunk
        start = chunk[-1].timestamp + GAP * 2
    return msgs, start


def _redis(failures: int = 1) -> MagicMock:
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=failures)
    redis.expire = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _embedder() -> MagicMock:
    service = MagicMock()
    service.embed_documents = AsyncMock(side_effect=lambda texts: [[0.1] * 3 for _ in texts])
    return service


class TestIngestGroup:
    async def test_stores_each_block_and_cursor_is_its_last_message(self):
        msgs, now = _blocks_of(2)
        group = uuid.uuid4()
        store = MagicMock(return_value=True)
        topic = Topic(subject="Trip", summary="Planned the trip.")
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", AsyncMock(return_value=[topic])),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(_redis(), group, now, _embedder())

        assert result.blocks_done == 2 and result.topics_stored == 2
        stored_blocks = [c.args[1] for c in store.call_args_list]
        # The watermark is each block's last message, never "now".
        assert [b.last for b in stored_blocks] == [msgs[29], msgs[59]]
        assert store.call_args_list[0].args[2] == [topic]
        assert store.call_args_list[0].args[3] == [[0.1] * 3]

    async def test_block_without_topics_still_moves_the_cursor(self):
        msgs, now = _blocks_of(1)
        store = MagicMock(return_value=True)
        embedder = _embedder()
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", AsyncMock(return_value=[])),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(_redis(), uuid.uuid4(), now, embedder)
        assert result.blocks_done == 1
        assert store.call_args.args[2:] == ([], [])
        embedder.embed_documents.assert_not_called()

    async def test_provider_outage_stops_without_counting(self):
        msgs, now = _blocks_of(2)
        redis = _redis()
        store = MagicMock()
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", AsyncMock(side_effect=ModelAPIError("m", "down"))),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(redis, uuid.uuid4(), now, _embedder())
        assert result.unavailable is True
        redis.incr.assert_not_called()
        store.assert_not_called()

    async def test_failing_block_holds_the_group_until_the_limit(self):
        msgs, now = _blocks_of(2)
        redis = _redis(failures=POISON_LIMIT - 1)
        store = MagicMock()
        summarise = AsyncMock(side_effect=UnexpectedModelBehavior("bad output"))
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", summarise),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(redis, uuid.uuid4(), now, _embedder())
        assert result.blocks_done == 0 and result.blocks_skipped == 0
        assert summarise.await_count == 1  # later blocks wait, order kept
        store.assert_not_called()
        key = redis.incr.call_args.args[0]
        assert key.startswith("group_topics:fail:") and key.endswith(Block(tuple(msgs[:30])).key)
        redis.expire.assert_awaited()

    async def test_poison_block_is_skipped_on_the_third_failure(self):
        msgs, now = _blocks_of(2)
        topic = Topic(subject="B", summary="Second block.")
        summarise = AsyncMock(side_effect=[ValueError("rejected text"), [topic]])
        store = MagicMock(return_value=True)
        redis = _redis(failures=POISON_LIMIT)
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", summarise),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(redis, uuid.uuid4(), now, _embedder())
        assert result.blocks_skipped == 1 and result.blocks_done == 1
        skipped, second = store.call_args_list
        assert skipped.args[1].last == msgs[29] and skipped.args[2:] == ([], [])
        assert second.args[2] == [topic]
        redis.delete.assert_awaited()

    async def test_embedding_failure_counts_against_the_block(self):
        msgs, now = _blocks_of(1)
        embedder = _embedder()
        embedder.embed_documents.side_effect = ValueError("Expected 1 embeddings, got 0")
        redis = _redis()
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(
                f"{MODULE}.summarise_block",
                AsyncMock(return_value=[Topic(subject="a", summary="b")]),
            ),
            patch(f"{MODULE}.store_block", MagicMock()) as store,
        ):
            await gt.ingest_group(redis, uuid.uuid4(), now, embedder)
        redis.incr.assert_awaited_once()
        store.assert_not_called()

    async def test_cleaned_group_stops(self):
        msgs, now = _blocks_of(2)
        store = MagicMock(return_value=False)
        with (
            patch(f"{MODULE}.load_new_messages", return_value=(msgs, [])),
            patch(f"{MODULE}.summarise_block", AsyncMock(return_value=[])),
            patch(f"{MODULE}.store_block", store),
        ):
            result = await gt.ingest_group(_redis(), uuid.uuid4(), now, _embedder())
        assert result.blocks_done == 0
        assert store.call_count == 1


# ---------------------------------------------------------------------------
# search formatting
# ---------------------------------------------------------------------------


def test_format_topic_results():
    match = gt.TopicMatch(
        subject="Trip",
        summary="Planned the trip to Belém.",
        start_at=T0,
        end_at=T0 + timedelta(hours=1),
        message_count=42,
        similarity=0.83,
    )
    out = format_topic_results([match])
    assert "Subject: Trip" in out and "Belém" in out
    assert "2026-11-01 09:00 to 2026-11-01 10:00 UTC (42 messages)" in out
    assert "summaries, not the exact messages" in out
    assert "No relevant topics" in format_topic_results([])


def test_search_is_scoped_to_the_group_row():
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = []
    with patch(f"{MODULE}.prepare_hnsw_search"):
        gt.search_group_topics(db, [0.1, 0.2], "group-row-id", limit=5, similarity_threshold=0.7)
    sql, params = db.execute.call_args.args
    assert "FROM group_topics" in str(sql) and "WHERE user_id = :user_id" in str(sql)
    assert "::halfvec(3072)" in str(sql)
    assert params["user_id"] == "group-row-id"
