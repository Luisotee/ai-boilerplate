"""Agent tools must not leak raw exception text to the LLM, and must keep the
shared DB session usable after a failure.

A raw ``str(e)`` from SQLAlchemy/httpx can carry DB hostnames, table names, SQL or
internal URLs, and the LLM may repeat it verbatim to the user. Details belong in
the logs (``exc_info=True``); the tool returns a generic message instead.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SECRET = "postgresql://admin:hunter2@db.internal:5432/prod"


def _ctx():
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = "123@s.whatsapp.net"
    ctx.deps.current_message_id = "wamid-1"
    ctx.deps.http_client = AsyncMock()
    ctx.deps.whatsapp_client = AsyncMock()
    ctx.deps.embedding_service = AsyncMock()
    return ctx


class TestSafeRollback:
    def test_swallows_rollback_failure(self):
        from ai_api.agent.tools._db import safe_rollback

        db = MagicMock()
        db.rollback.side_effect = RuntimeError("connection is closed")
        safe_rollback(db)  # must not raise
        db.rollback.assert_called_once()

    async def test_tool_still_returns_generic_message_when_rollback_fails(self):
        from ai_api.agent.tools.settings import update_tts_settings

        ctx = _ctx()
        ctx.deps.db.commit.side_effect = RuntimeError(SECRET)
        ctx.deps.db.rollback.side_effect = RuntimeError("connection is closed")
        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as mock_prefs:
            mock_prefs.return_value = MagicMock(tts_enabled=False, tts_language="en")
            result = await update_tts_settings(ctx, enabled=True)

        assert result == "Failed to update TTS settings. Please try again."


class TestMemoryTool:
    async def test_commit_failure_rolls_back_without_leaking(self):
        from ai_api.agent.tools.memory import update_core_memory

        ctx = _ctx()
        ctx.deps.db.commit.side_effect = RuntimeError(SECRET)
        with (
            patch("ai_api.agent.tools.memory.get_or_create_core_memory") as mock_mem,
            patch("ai_api.agent.tools.memory.runtime_config") as mock_rc,
        ):
            mock_mem.return_value = MagicMock(content="")
            mock_rc.get.return_value = 10_000
            result = await update_core_memory(ctx, "likes tea")

        ctx.deps.db.rollback.assert_called_once()
        assert SECRET not in result
        assert result == "Failed to update core memory. Please try again."


class TestSearchTools:
    async def test_conversation_search_failure(self):
        from ai_api.agent.tools.search import search_conversation_history

        ctx = _ctx()
        ctx.deps.embedding_service.generate.side_effect = RuntimeError(SECRET)
        result = await search_conversation_history(ctx, "topic")

        ctx.deps.db.rollback.assert_called_once()
        assert SECRET not in result
        assert result.startswith("Error searching conversation history.")

    async def test_knowledge_base_search_failure(self):
        from ai_api.agent.tools.search import search_knowledge_base

        ctx = _ctx()
        ctx.deps.embedding_service.generate.side_effect = RuntimeError(SECRET)
        result = await search_knowledge_base(ctx, "topic")

        ctx.deps.db.rollback.assert_called_once()
        assert SECRET not in result
        assert result.startswith("Error searching the knowledge base.")


class TestNetworkTools:
    async def test_fetch_website_failure(self):
        from ai_api.agent.tools.web import fetch_website

        ctx = _ctx()
        ctx.deps.http_client.get.side_effect = RuntimeError(SECRET)
        result = await fetch_website(ctx, "https://example.com")

        assert SECRET not in result
        assert result == "Failed to fetch that URL. Please try again."

    async def test_weather_failure(self):
        from ai_api.agent.tools.utility import get_weather

        ctx = _ctx()
        ctx.deps.http_client.get.side_effect = RuntimeError(SECRET)
        result = await get_weather(ctx, "Berlin")

        assert SECRET not in result
        assert result == "Could not get the weather right now. Please try again."

    @pytest.mark.parametrize(
        ("tool_name", "client_method", "kwargs"),
        [
            ("send_whatsapp_reaction", "send_reaction", {"emoji": "👍"}),
            (
                "send_whatsapp_location",
                "send_location",
                {"latitude": 1.0, "longitude": 2.0},
            ),
            ("send_whatsapp_message", "send_text", {"text": "hi"}),
        ],
    )
    async def test_whatsapp_tools_failure(self, tool_name, client_method, kwargs):
        from ai_api.agent.tools import whatsapp as wa_tools

        ctx = _ctx()
        getattr(ctx.deps.whatsapp_client, client_method).side_effect = RuntimeError(SECRET)
        result = await getattr(wa_tools, tool_name)(ctx, **kwargs)

        assert SECRET not in result
        assert result.startswith("Failed to send")
