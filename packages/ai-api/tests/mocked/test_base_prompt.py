"""Tests for the base prompt hook in agent/core.py (sections in agent/prompt.py)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT, section_hash


def _row(content, base):
    return SimpleNamespace(content=content, base_hash=section_hash(base), updated_at=None)


async def test_base_system_prompt_is_the_code_default_without_overrides():
    from ai_api.agent.core import base_system_prompt

    with patch("ai_api.agent.prompt.get_prompt_overrides", return_value={}):
        assert await base_system_prompt(MagicMock()) == DEFAULT_SYSTEM_PROMPT


async def test_base_system_prompt_applies_a_fresh_whole_override():
    from ai_api.agent.core import base_system_prompt

    rows = {"all": _row("CUSTOM PROMPT", DEFAULT_SYSTEM_PROMPT)}
    with patch("ai_api.agent.prompt.get_prompt_overrides", return_value=rows):
        assert await base_system_prompt(MagicMock()) == "CUSTOM PROMPT"


async def test_base_system_prompt_ignores_a_stale_whole_override():
    """A saved copy of an old default never hides the current one."""
    from ai_api.agent.core import base_system_prompt

    rows = {"all": _row("OLD COPY", "an older default")}
    with patch("ai_api.agent.prompt.get_prompt_overrides", return_value=rows):
        assert await base_system_prompt(MagicMock()) == DEFAULT_SYSTEM_PROMPT


async def test_an_empty_whole_override_is_never_applied():
    from ai_api.agent.core import base_system_prompt

    rows = {"all": _row("", DEFAULT_SYSTEM_PROMPT)}
    with patch("ai_api.agent.prompt.get_prompt_overrides", return_value=rows):
        assert await base_system_prompt(MagicMock()) == DEFAULT_SYSTEM_PROMPT
