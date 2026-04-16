"""Tests that update_tts_settings and update_stt_settings roll back the session on failure.

Without the explicit rollback, a failed commit leaves SQLAlchemy's session in a dirty
state and poisons subsequent tool calls that share the session.
"""

from unittest.mock import MagicMock, patch

from ai_api.agent.tools.settings import update_stt_settings, update_tts_settings


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
