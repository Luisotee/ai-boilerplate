"""Tests for the DB-backed system prompt resolution in agent/core.py."""

from unittest.mock import MagicMock, patch


async def test_base_system_prompt_uses_db_override():
    from ai_api.agent.core import base_system_prompt

    ctx = MagicMock()
    with patch("ai_api.agent.core.get_active_prompt", return_value="CUSTOM PROMPT"):
        assert await base_system_prompt(ctx) == "CUSTOM PROMPT"


async def test_base_system_prompt_falls_back_to_default():
    from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT, base_system_prompt

    ctx = MagicMock()
    with patch("ai_api.agent.core.get_active_prompt", return_value=None):
        assert await base_system_prompt(ctx) == DEFAULT_SYSTEM_PROMPT


async def test_base_system_prompt_falls_back_on_empty_string():
    from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT, base_system_prompt

    ctx = MagicMock()
    # get_active_prompt returns None for an empty row, but guard against "" too.
    with patch("ai_api.agent.core.get_active_prompt", return_value=""):
        assert await base_system_prompt(ctx) == DEFAULT_SYSTEM_PROMPT
