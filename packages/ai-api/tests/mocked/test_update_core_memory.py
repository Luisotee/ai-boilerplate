"""update_core_memory is also the agent's way to forget: the default prompt tells
it to pass an empty string to clear everything (the /memories command covers the
user-driven path, and the current document is always injected into the prompt,
so no separate show/clear tool is needed)."""

from unittest.mock import MagicMock, patch

from ai_api.agent.tools.memory import update_core_memory

MODULE = "ai_api.agent.tools.memory"


def _ctx():
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    return ctx


async def test_empty_content_clears_the_document():
    ctx = _ctx()
    mem = MagicMock(content="## Facts\n- likes tea")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await update_core_memory(ctx, "")

    assert mem.content == ""
    ctx.deps.db.commit.assert_called_once()
    assert result.startswith("Core memory updated (0 characters)")


async def test_prompt_documents_the_clear_path():
    from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT

    prompt = " ".join(DEFAULT_SYSTEM_PROMPT.split())
    assert "to forget everything, pass an empty string" in prompt
