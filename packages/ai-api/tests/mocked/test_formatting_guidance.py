"""
Tests for the chat-markup rule appended to every run (agent/core.py).

The active prompt can be an /admin override that replaces DEFAULT_SYSTEM_PROMPT
wholesale, so the formatting rule lives in its own `@agent.instructions` hook
and must reach the model whatever the base prompt is.
"""

from unittest.mock import MagicMock, patch

from pydantic_ai.models.test import TestModel

from ai_api.agent.core import (
    DEFAULT_SYSTEM_PROMPT,
    AgentDeps,
    agent,
    formatting_guidance,
)


class TestFormattingGuidance:
    async def test_shows_the_wrong_and_right_bold_syntax(self):
        text = await formatting_guidance(MagicMock())
        assert "**text**" in text
        assert "*bold*" in text

    async def test_forbids_markdown_links(self):
        text = await formatting_guidance(MagicMock())
        assert "[text](" in text

    async def test_keeps_the_burst_delimiter_convention(self):
        # The rule must not read as "no special lines at all" — `---` bursts
        # are how multi-message replies are produced.
        text = await formatting_guidance(MagicMock())
        assert "---" in text

    async def test_reaches_the_model_even_with_an_admin_prompt_override(self):
        deps = AgentDeps(
            db=MagicMock(),
            user_id="u1",
            whatsapp_jid="123@s.whatsapp.net",
            recent_message_ids=[],
        )
        with (
            patch("ai_api.agent.core.get_active_prompt", return_value="CUSTOM ADMIN PROMPT"),
            patch(
                "ai_api.agent.core.get_or_create_core_memory",
                return_value=MagicMock(content=""),
            ),
            agent.override(model=TestModel(call_tools=[])),
        ):
            result = await agent.run("hi", deps=deps)

        instructions = result.all_messages()[0].instructions or ""
        assert "CUSTOM ADMIN PROMPT" in instructions
        assert "== FORMATTING ==" in instructions


class TestDefaultPrompt:
    def test_default_prompt_does_not_demonstrate_markdown_bold(self):
        # A prompt full of **x** teaches the very syntax the rule forbids.
        assert "**" not in DEFAULT_SYSTEM_PROMPT

    def test_default_prompt_describes_get_chat_history(self):
        assert "get_chat_history" in DEFAULT_SYSTEM_PROMPT
