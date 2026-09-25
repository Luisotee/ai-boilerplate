"""
Mocked tests for the get_chat_history agent tool.

Tests the tool function directly by mocking the DB session and the
get_conversation_messages helper it delegates to.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.tools.history import get_chat_history
from ai_api.config import settings
from tests.helpers.factories import make_conversation_message

MODULE = "ai_api.agent.tools.history"
SECRET = "postgresql://admin:hunter2@db.internal:5432/prod"


def _make_ctx(*, whatsapp_jid: str = "5511999999999@s.whatsapp.net") -> MagicMock:
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = whatsapp_jid
    return ctx


@pytest.fixture(autouse=True)
def _env_runtime_config():
    """Serve settings from the env defaults, without the DB-override lookup."""
    with patch(f"{MODULE}.runtime_config") as mock_rc:
        mock_rc.get.side_effect = lambda key: getattr(settings, key)
        yield mock_rc


class TestGetChatHistory:
    @patch(f"{MODULE}.get_conversation_messages")
    async def test_private_chat_labels_speakers(self, mock_msgs):
        mock_msgs.return_value = [
            make_conversation_message("user", "good morning"),  # private: no sender_name
            make_conversation_message("assistant", "Hi!"),
        ]
        ctx = _make_ctx()
        result = await get_chat_history(ctx, limit=10)

        # Private user lines get a generic label; the bot gets BOT_NAME's default.
        assert "User: good morning" in result
        assert "Assistant: Hi!" in result
        # Scoped to the run's own conversation row, never a tool argument.
        assert mock_msgs.call_args.args == (ctx.deps.db, "user-123")
        assert mock_msgs.call_args.kwargs["limit"] == 10
        assert mock_msgs.call_args.kwargs["since"] is None

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_assistant_label_follows_bot_name_setting(self, mock_msgs, _env_runtime_config):
        _env_runtime_config.get.side_effect = lambda key: {"bot_name": "Jarvis"}[key]
        mock_msgs.return_value = [make_conversation_message("assistant", "At your service.")]
        result = await get_chat_history(_make_ctx())

        assert result == "Jarvis: At your service."

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_since_hours_sets_naive_utc_window_and_timestamps(self, mock_msgs):
        mock_msgs.return_value = [
            make_conversation_message("user", "hi", timestamp=datetime(2026, 6, 5, 14, 30))
        ]
        result = await get_chat_history(_make_ctx(), since_hours=24)

        since = mock_msgs.call_args.kwargs["since"]
        assert since.tzinfo is None  # the timestamp column is naive UTC
        expected = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
        assert abs((since - expected).total_seconds()) < 60
        # Windowed output carries timestamps.
        assert result.startswith("[2026-06-05 14:30] ")

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_non_positive_since_hours_means_no_window(self, mock_msgs):
        mock_msgs.return_value = [make_conversation_message("user", "hi")]
        await get_chat_history(_make_ctx(), since_hours=0)
        assert mock_msgs.call_args.kwargs["since"] is None

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_group_chat_does_not_double_the_sender_prefix(self, mock_msgs):
        mock_msgs.return_value = [
            make_conversation_message("user", "Bob: what's up", sender_name="Bob")
        ]
        result = await get_chat_history(_make_ctx(whatsapp_jid="120363000000001@g.us"))
        assert result == "Bob: what's up"

    @patch(f"{MODULE}.get_conversation_messages", return_value=[])
    async def test_empty_window_message(self, _mock_msgs):
        result = await get_chat_history(_make_ctx(), since_hours=2)
        assert "last 2h" in result

    @patch(f"{MODULE}.get_conversation_messages", return_value=[])
    async def test_empty_no_window_message(self, _mock_msgs):
        result = await get_chat_history(_make_ctx())
        assert "don't have any stored messages" in result

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_exception_rolls_back_and_hides_details(self, mock_msgs):
        mock_msgs.side_effect = RuntimeError(SECRET)
        ctx = _make_ctx()
        result = await get_chat_history(ctx)

        assert result.startswith("I couldn't read the conversation history")
        assert SECRET not in result
        ctx.deps.db.rollback.assert_called_once()

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_failing_rollback_still_returns_generic_message(self, mock_msgs):
        mock_msgs.side_effect = RuntimeError(SECRET)
        ctx = _make_ctx()
        ctx.deps.db.rollback.side_effect = RuntimeError("connection is closed")
        result = await get_chat_history(ctx)
        assert result.startswith("I couldn't read the conversation history")


def test_tool_is_registered_with_the_agent():
    from ai_api.agent.core import agent

    assert "get_chat_history" in agent._function_toolset.tools
