"""search_conversation_history searches group topics first, in groups only."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.agent.tools.search import search_conversation_history
from ai_api.config import settings
from ai_api.rag.group_topics import TopicMatch

MODULE = "ai_api.agent.tools.search"
SECRET = "postgresql://admin:hunter2@db.internal:5432/prod"
GROUP_JID = "120363000000000000@g.us"
TOPIC = TopicMatch(
    subject="Trip",
    summary="Planned the trip.",
    start_at=datetime(2026, 11, 1, 9),
    end_at=datetime(2026, 11, 1, 10),
    message_count=30,
    similarity=0.8,
)


def _ctx(jid: str) -> MagicMock:
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "row-of-this-chat"
    ctx.deps.whatsapp_jid = jid
    ctx.deps.recent_message_ids = []
    ctx.deps.embedding_service.generate = AsyncMock(return_value=[0.1, 0.2])
    return ctx


@pytest.fixture
def enabled():
    values = {"group_topics_enabled": True}
    with patch(f"{MODULE}.runtime_config") as rc:
        rc.get.side_effect = lambda key: values.get(key, getattr(settings, key))
        yield values


@pytest.fixture
def message_search():
    with (
        patch(f"{MODULE}.search_conversation_fn", AsyncMock(return_value=[])) as fn,
        patch(f"{MODULE}.format_conversation_results", return_value="MESSAGES"),
    ):
        yield fn


async def test_group_with_topics_returns_topics(enabled, message_search):
    ctx = _ctx(GROUP_JID)
    with patch(f"{MODULE}.search_group_topics", return_value=[TOPIC]) as topics:
        result = await search_conversation_history(ctx, "the trip")
    assert "Subject: Trip" in result
    assert "Matching messages" not in result  # none matched
    # Scoped to the run's own chat row, never a tool argument.
    assert topics.call_args.args == (ctx.deps.db, [0.1, 0.2], "row-of-this-chat")
    # The messages are still searched, over the whole history.
    kwargs = message_search.await_args.kwargs
    assert kwargs["user_id"] == "row-of-this-chat"
    assert "newer_than" not in kwargs


async def test_message_older_than_any_topic_is_still_found(enabled):
    """A message from before the job's backfill window stays findable next to a topic."""
    old = SimpleNamespace(
        timestamp=datetime(2026, 6, 1, 12),
        role="user",
        sender_name="Ana",
        content="The address is Rua das Flores 12",
    )
    hit = {
        "messages_before": [],
        "matched_message": old,
        "messages_after": [],
        "similarity_score": 0.7,
    }
    with (
        patch(f"{MODULE}.search_group_topics", return_value=[TOPIC]),
        patch(f"{MODULE}.search_conversation_fn", AsyncMock(return_value=[hit])),
    ):
        result = await search_conversation_history(_ctx(GROUP_JID), "the address")
    assert result.index("Subject: Trip") < result.index("Matching messages:")
    assert "Rua das Flores 12" in result
    assert "2026-06-01 12:00" in result


async def test_group_without_topics_falls_back_to_messages(enabled, message_search):
    with patch(f"{MODULE}.search_group_topics", return_value=[]):
        await search_conversation_history(_ctx(GROUP_JID), "the trip")
    message_search.assert_awaited_once()


@pytest.mark.parametrize(
    "jid", ["5511999999999@s.whatsapp.net", "tg:12345", "web:3f2a", "cli:laptop"]
)
async def test_private_chat_never_reads_topics(enabled, message_search, jid):
    with patch(f"{MODULE}.search_group_topics") as topics:
        await search_conversation_history(_ctx(jid), "the trip")
    topics.assert_not_called()
    message_search.assert_awaited_once()


async def test_telegram_group_reads_topics(enabled, message_search):
    with (
        patch(f"{MODULE}.search_group_topics", return_value=[TOPIC]) as topics,
    ):
        await search_conversation_history(_ctx("tg:-100123"), "the trip")
    topics.assert_called_once()


async def test_disabled_skips_topics(enabled, message_search):
    enabled["group_topics_enabled"] = False
    with patch(f"{MODULE}.search_group_topics") as topics:
        await search_conversation_history(_ctx(GROUP_JID), "the trip")
    topics.assert_not_called()
    message_search.assert_awaited_once()


async def test_topic_search_error_is_generic_and_rolls_back(enabled, message_search):
    ctx = _ctx(GROUP_JID)
    with patch(f"{MODULE}.search_group_topics", side_effect=RuntimeError(SECRET)):
        result = await search_conversation_history(ctx, "the trip")
    ctx.deps.db.rollback.assert_called_once()
    assert SECRET not in result
    assert result.startswith("Error searching conversation history.")
