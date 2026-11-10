"""A stored /admin prompt that names renamed tools gets a warning (agent/core.py)."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.core import REMOVED_TOOLS, agent, removed_tools_named, warn_on_removed_tools


def test_every_removed_tool_is_gone_and_its_replacement_exists():
    tools = agent._function_toolset.tools
    for old, new in REMOVED_TOOLS.items():
        assert old not in tools
        assert new in tools


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (None, []),
        ("Use update_core_memory to save facts.", ["update_core_memory"]),
        (
            "get_user_settings, then update_tts_settings",
            ["get_user_settings", "update_tts_settings"],
        ),
        ("Use the memory tool and settings.", []),
        ("my_update_core_memory_v2", []),  # whole words only
    ],
)
def test_removed_tools_named(prompt, expected):
    assert removed_tools_named(prompt) == expected


def test_warning_names_the_replacement(caplog):
    with caplog.at_level(logging.WARNING, logger="ai-api"):
        warn_on_removed_tools("call set_broadcast_subscription when asked")
    assert "set_broadcast_subscription -> settings" in caplog.text


def test_no_warning_for_a_current_prompt(caplog):
    with caplog.at_level(logging.WARNING, logger="ai-api"):
        warn_on_removed_tools("Be brief.")
    assert caplog.text == ""


def test_startup_check_reads_the_stored_prompt(caplog):
    from ai_api import main

    with (
        patch.object(main, "SessionLocal", MagicMock()),
        patch.object(main, "get_active_prompt", return_value="use update_stt_settings"),
        caplog.at_level(logging.WARNING, logger="ai-api"),
    ):
        main._warn_on_stale_prompt()
    assert "update_stt_settings -> settings" in caplog.text


def test_startup_check_never_raises(caplog):
    from ai_api import main

    with (
        patch.object(main, "SessionLocal", side_effect=RuntimeError("db down")),
        caplog.at_level(logging.WARNING, logger="ai-api"),
    ):
        main._warn_on_stale_prompt()
    assert "Could not check the stored system prompt" in caplog.text
