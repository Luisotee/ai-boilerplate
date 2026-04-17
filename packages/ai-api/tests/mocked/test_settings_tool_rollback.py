"""Tests that settings tools roll back the shared session on failure.

Without the explicit rollback, a failed commit leaves SQLAlchemy's session in a dirty
state and poisons subsequent tool calls that share the session.
"""

from unittest.mock import MagicMock, patch

from ai_api.agent.tools.settings import (
    clean_user_data,
    get_user_settings,
    update_stt_settings,
    update_tts_settings,
)


def _make_ctx():
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = "123@s.whatsapp.net"
    return ctx


class TestUpdateTtsSettingsRollback:
    async def test_rollback_called_when_commit_raises(self):
        ctx = _make_ctx()
        ctx.deps.db.commit.side_effect = RuntimeError("connection lost")

        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.return_value = MagicMock(tts_enabled=False, tts_language="en")
            result = await update_tts_settings(ctx, enabled=True)

        ctx.deps.db.rollback.assert_called_once()
        assert result.startswith("Failed to update TTS settings:")
        assert "connection lost" in result

    async def test_rollback_not_called_on_success(self):
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.return_value = MagicMock(tts_enabled=False, tts_language="en")
            result = await update_tts_settings(ctx, enabled=True)

        ctx.deps.db.commit.assert_called_once()
        ctx.deps.db.rollback.assert_not_called()
        assert "TTS enabled" in result


class TestUpdateSttSettingsRollback:
    async def test_rollback_called_when_commit_raises(self):
        ctx = _make_ctx()
        ctx.deps.db.commit.side_effect = RuntimeError("db down")

        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.return_value = MagicMock(stt_language=None)
            result = await update_stt_settings(ctx, language="en")

        ctx.deps.db.rollback.assert_called_once()
        assert result.startswith("Failed to update STT settings:")
        assert "db down" in result

    async def test_rollback_called_on_auto_failure(self):
        """The 'auto' path also commits — it should roll back on failure too."""
        ctx = _make_ctx()
        ctx.deps.db.commit.side_effect = RuntimeError("boom")

        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.return_value = MagicMock(stt_language="en")
            result = await update_stt_settings(ctx, language="auto")

        ctx.deps.db.rollback.assert_called_once()
        assert result.startswith("Failed to update STT settings:")


class TestGetUserSettingsRollback:
    async def test_rollback_called_when_helper_raises(self):
        """get_or_create_preferences commits internally; if it raises mid-commit the
        shared session is left dirty. The tool must roll back so sibling tool calls
        don't inherit pending state."""
        ctx = _make_ctx()

        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.side_effect = RuntimeError("integrity error")
            result = await get_user_settings(ctx)

        ctx.deps.db.rollback.assert_called_once()
        assert result.startswith("Failed to retrieve settings:")

    async def test_rollback_not_called_on_success(self):
        ctx = _make_ctx()
        with (
            patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs,
            patch("ai_api.agent.tools.settings.format_settings") as mock_fmt,
        ):
            mock_prefs.return_value = MagicMock()
            mock_fmt.return_value = "settings-blob"
            result = await get_user_settings(ctx)

        ctx.deps.db.rollback.assert_not_called()
        assert result == "settings-blob"


class TestCleanUserDataRollback:
    async def test_rollback_called_when_handler_raises(self):
        """handle_clean_command commits internally after multi-table deletes; a failure
        mid-way leaves pending deletes on the shared session."""
        ctx = _make_ctx()

        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.side_effect = RuntimeError("delete failed")
            result = await clean_user_data(ctx, level="all")

        ctx.deps.db.rollback.assert_called_once()
        assert result.startswith("Failed to clean user data:")

    async def test_rollback_not_called_on_success(self):
        ctx = _make_ctx()
        with patch("ai_api.agent.tools.settings.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Cleared 42 messages."
            result = await clean_user_data(ctx, level="messages")

        ctx.deps.db.rollback.assert_not_called()
        assert result == "Cleared 42 messages."
