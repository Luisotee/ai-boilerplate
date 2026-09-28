"""Tests for the clean_user_data agent tool.

The tool wraps handle_clean_command and is exposed to the agent. These tests
mock the underlying handler and assert the tool forwards arguments correctly,
applies the right default level, and converts unexpected exceptions into a
user-visible failure string.
"""

from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.tools.settings import clean_user_data


def _make_ctx():
    """Build a mock RunContext whose .deps mimics AgentDeps."""
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = "123@s.whatsapp.net"
    return ctx


class TestCleanUserDataTool:
    async def test_default_level_is_messages(self):
        """The renamed tool defaults to 'messages' (was 'clear' before PR #18)."""
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Deleted 3 messages."
            result = await clean_user_data(ctx)

            mock_handle.assert_called_once_with(
                ctx.deps.db,
                "user-123",
                "123@s.whatsapp.net",
                level="messages",
            )
            assert result == "Deleted 3 messages."

    async def test_explicit_messages_level(self):
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            await clean_user_data(ctx, level="messages")
            mock_handle.assert_called_once_with(
                ctx.deps.db, "user-123", "123@s.whatsapp.net", level="messages"
            )

    async def test_data_level_passes_through(self):
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Deleted 5 messages and 2 documents."
            result = await clean_user_data(ctx, level="data")

            mock_handle.assert_called_once_with(
                ctx.deps.db, "user-123", "123@s.whatsapp.net", level="data"
            )
            assert "documents" in result

    async def test_all_level_passes_through(self):
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Full reset complete."
            result = await clean_user_data(ctx, level="all")

            mock_handle.assert_called_once_with(
                ctx.deps.db, "user-123", "123@s.whatsapp.net", level="all"
            )
            assert "Full reset" in result

    async def test_handler_exception_returns_failure_string(self):
        """An unexpected exception is caught and surfaced to the agent as text."""
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.side_effect = RuntimeError("db connection lost")
            result = await clean_user_data(ctx, level="all")

            assert result == "Failed to clean user data. Please try again."
            assert "db connection lost" not in result  # raw error never reaches the LLM

    async def test_invalid_level_propagates_handler_message(self):
        """Invalid levels are validated by handle_clean_command, not the tool."""
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = (
                "Invalid clean level 'bogus'. Use 'messages', 'data', or 'all'."
            )
            result = await clean_user_data(ctx, level="bogus")

            assert "Invalid clean level" in result
            mock_handle.assert_called_once_with(
                ctx.deps.db, "user-123", "123@s.whatsapp.net", level="bogus"
            )


class TestCleanUserDataGroupAdmin:
    """In a group only a confirmed admin may wipe data — like the /clean command.

    Regression: the tool had no admin check, so any group member could ask the
    bot in plain words to clean the whole group's history."""

    @pytest.mark.parametrize("jid", ["120363012345678@g.us", "tg:-1001234567890"])
    @pytest.mark.parametrize("is_admin", [False, None])
    async def test_non_admin_refused_in_group(self, jid, is_admin):
        ctx = _make_ctx()
        ctx.deps.whatsapp_jid = jid
        ctx.deps.is_group_admin = is_admin
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            result = await clean_user_data(ctx, level="all")
        mock_handle.assert_not_called()
        assert "Only a group admin" in result

    async def test_admin_allowed_in_group(self):
        ctx = _make_ctx()
        ctx.deps.whatsapp_jid = "120363012345678@g.us"
        ctx.deps.is_group_admin = True
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Deleted 3 messages."
            result = await clean_user_data(ctx)
        mock_handle.assert_called_once()
        assert result == "Deleted 3 messages."

    async def test_private_chat_needs_no_admin_flag(self):
        ctx = _make_ctx()
        ctx.deps.is_group_admin = None
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            await clean_user_data(ctx)
        mock_handle.assert_called_once()
