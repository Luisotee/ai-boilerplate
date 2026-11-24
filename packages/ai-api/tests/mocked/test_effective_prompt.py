"""GET /admin/prompt/effective must show exactly what the model gets.

``build_effective_prompt`` calls the registered hooks itself; these tests run
the real agent with a ``FunctionModel`` for the same deps and compare its
captured instructions, so a wrong rebuild, a missing hook or a hook registered
around ``instruction_hook`` fails here.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_api.agent.core import INSTRUCTION_HOOKS, agent
from ai_api.agent.effective_prompt import (
    PREVIEW_USER_ID,
    build_effective_prompt,
    preview_deps,
    prompt_code_hash,
    surface_of_jid,
)
from ai_api.agent.prompt import SECTIONS_BY_KEY, section_hash


def _row(content, base):
    return SimpleNamespace(content=content, base_hash=section_hash(base), updated_at=None)


async def _model_instructions(deps) -> str:
    captured = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("ok")])

    with agent.override(model=FunctionModel(respond)):
        await agent.run("hi", deps=deps)
    return captured["instructions"]


# (chat, surface, shared groups on, core memory, overrides)
CASES = {
    "whatsapp_private": ("private", "whatsapp", False, "", {}),
    "whatsapp_group": ("group", "whatsapp", False, "", {}),
    "telegram_private_shared_groups": ("private", "telegram", True, "", {}),
    "telegram_group": ("group", "telegram", False, "", {}),
    "web_with_memory": ("private", "web", False, "- Name: Ana", {}),
    "section_override": (
        "private",
        "whatsapp",
        True,
        "- Lives in Lisbon",
        {"tone": _row("Tone: brief.", SECTIONS_BY_KEY["tone"].default)},
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
async def test_content_is_what_the_model_receives(case):
    chat, surface, shared, memory, overrides = CASES[case]
    deps = preview_deps(MagicMock(), chat, surface)
    mem = SimpleNamespace(content=memory) if memory else None
    with (
        patch("ai_api.agent.prompt.get_prompt_overrides", return_value=overrides),
        patch("ai_api.agent.core.get_core_memory", return_value=mem),
        patch("ai_api.agent.core.shared_group_tools_available", return_value=shared),
    ):
        expected = await _model_instructions(deps)
        effective = await build_effective_prompt(deps)

    assert effective.content == expected
    joined = "\n\n".join(b.content for b in effective.blocks if b.content).strip()
    assert joined == expected
    assert effective.chat == chat and effective.surface == surface
    if memory:
        assert memory in effective.content


def test_every_registered_instructions_function_is_a_listed_hook():
    """A bare agent.instructions decorator would reach the model but not FleetView."""
    registered = [f for f in agent._instructions if callable(f)]
    assert registered == [h.func for h in INSTRUCTION_HOOKS]
    assert INSTRUCTION_HOOKS[0].func.__name__ == "base_system_prompt"
    assert INSTRUCTION_HOOKS[-1].func.__name__ == "inject_core_memory"


async def test_section_blocks_carry_source_and_staleness():
    rows = {
        "tone": _row("Tone: brief.", SECTIONS_BY_KEY["tone"].default),
        "bursts": _row("Bursts: old.", "an older bursts rule"),
    }
    deps = preview_deps(MagicMock(), "private", "whatsapp")
    with (
        patch("ai_api.agent.prompt.get_prompt_overrides", return_value=rows),
        patch("ai_api.agent.core.get_core_memory", return_value=None),
    ):
        effective = await build_effective_prompt(deps)
    blocks = {b.key: b for b in effective.blocks}
    assert blocks["section:tone"].source == "override"
    assert blocks["section:bursts"].source == "code"
    assert blocks["section:bursts"].stale_override is True
    assert blocks["section:bursts"].content == SECTIONS_BY_KEY["bursts"].default
    assert blocks["hook:language_guidance"].editable is False


async def test_a_fresh_whole_override_is_one_block():
    from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

    deps = preview_deps(MagicMock(), "private", "whatsapp")
    with (
        patch(
            "ai_api.agent.prompt.get_prompt_overrides",
            return_value={"all": _row("WHOLE", DEFAULT_SYSTEM_PROMPT)},
        ),
        patch("ai_api.agent.core.get_core_memory", return_value=None),
    ):
        effective = await build_effective_prompt(deps)
    sections = [b for b in effective.blocks if b.kind == "section"]
    assert [(b.key, b.content) for b in sections] == [("whole_override", "WHOLE")]
    assert effective.content.startswith("WHOLE\n\n")


async def test_the_preview_writes_nothing():
    """inject_core_memory reads only: no core-memory row for the preview user."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None  # no memory row
    deps = preview_deps(db, "private", "whatsapp")
    with patch("ai_api.agent.prompt.get_prompt_overrides", return_value={}):
        await build_effective_prompt(deps)
    db.add.assert_not_called()
    db.commit.assert_not_called()
    assert deps.user_id == PREVIEW_USER_ID


def test_a_real_users_preview_uses_their_chat():
    user = SimpleNamespace(id="u-1", whatsapp_jid="tg:-100", whatsapp_client_id=None)
    deps = preview_deps(MagicMock(), "private", "whatsapp", user)
    assert (deps.user_id, deps.whatsapp_jid, deps.client_id) == ("u-1", "tg:-100", "telegram")


def test_a_cloud_users_preview_runs_as_cloud_on_the_whatsapp_surface():
    """The Cloud API hides the shared-group section: the preview must use client 'cloud'."""
    user = SimpleNamespace(id="u-2", whatsapp_jid="5511@s.whatsapp.net", whatsapp_client_id="cloud")
    deps = preview_deps(MagicMock(), "private", "telegram", user)
    assert deps.client_id == "cloud"
    assert surface_of_jid(deps.whatsapp_jid, "cloud") == ("whatsapp", "cloud")
    # A group is never Cloud (the Cloud API has no groups).
    assert surface_of_jid("120363@g.us", "cloud") == ("whatsapp", "baileys")


def test_web_has_no_group_chat():
    with pytest.raises(ValueError):
        preview_deps(MagicMock(), "group", "web")


def test_prompt_code_hash_is_stable_in_a_process():
    assert prompt_code_hash() == prompt_code_hash()
    assert len(prompt_code_hash()) == 16
