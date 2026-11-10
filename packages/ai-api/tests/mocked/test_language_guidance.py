"""
Tests for the reply-language rule appended to every run (agent/core.py).

Like the formatting rule, it lives in its own `@agent.instructions` hook so an
/admin prompt override (which replaces DEFAULT_SYSTEM_PROMPT wholesale) keeps it.
"""

from unittest.mock import MagicMock, patch

from pydantic_ai.models.test import TestModel

from ai_api.agent.core import (
    AgentDeps,
    agent,
    inject_core_memory,
    language_guidance,
)


class TestLanguageGuidance:
    async def test_answers_in_the_language_of_the_current_message(self):
        text = await language_guidance(MagicMock())
        assert "language of the message you are answering" in text

    async def test_never_infers_it_from_history_people_or_timezone(self):
        text = await language_guidance(MagicMock())
        for source in ("earlier messages", "other people's messages", "timezone"):
            assert source in text

    async def test_a_standing_instruction_counts_only_when_it_names_a_language(self):
        text = await language_guidance(MagicMock())
        assert "only when it names one" in text

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
            result = await agent.run("oi", deps=deps)

        instructions = result.all_messages()[0].instructions or ""
        assert "CUSTOM ADMIN PROMPT" in instructions
        assert "== LANGUAGE ==" in instructions


def test_core_memory_is_the_last_instructions_hook():
    # Everything before the per-chat memory is the same for every chat, so it
    # stays a cacheable prefix; a new hook must go above inject_core_memory.
    assert agent._instructions[-1] is inject_core_memory
