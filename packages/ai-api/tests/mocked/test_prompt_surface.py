"""Snapshot of what every model request carries before the history: the
instructions and the tool definitions, per kind of chat, with character totals.

Every model request (one per tool round trip) resends all of it, so growth
here is real cost. The snapshot makes it visible in review: a change to the
default prompt, an instructions hook or any tool's docstring/arguments fails
this test until the snapshot is regenerated, and the diff shows the new text
and the char totals.

Regenerate after an intended change, and commit the JSON with it:

    cd packages/ai-api
    UPDATE_PROMPT_SURFACE=1 uv run pytest tests/mocked/test_prompt_surface.py

Tokens are estimated as chars / 4.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tests.evals.harness import EvalInput, run_case

SNAPSHOT_DIR = Path(__file__).parent.parent / "fixtures" / "prompt_surface"
UPDATE = os.environ.get("UPDATE_PROMPT_SURFACE") == "1"

# name -> (chat, client_id, hot-setting overrides)
SURFACES = {
    "private": ("private", None, {}),
    "group": ("group", None, {}),
    "private_all_features": (
        "private",
        None,
        {"shared_group_tools_enabled": True, "bug_reports_enabled": True},
    ),
}


async def _capture(chat: str, client_id: str | None, overrides: dict) -> dict:
    captured: dict = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions or ""
        captured["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_json_schema,
            }
            for t in info.function_tools
        ]
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages, info: AgentInfo):
        respond(messages, info)
        yield "ok"

    from ai_api.runtime_config import runtime_config

    with patch.object(runtime_config, "_overrides", overrides):
        # run_case stubs the DB/network and resets _overrides to {}, so apply
        # the surface's flags through get() instead.
        real_get = runtime_config.get.__func__

        def get(self, key):
            return overrides[key] if key in overrides else real_get(self, key)

        with patch.object(type(runtime_config), "get", get):
            await run_case(
                EvalInput("oi", chat=chat, is_group_admin=False, client_id=client_id),
                model=FunctionModel(respond, stream_function=stream),
            )

    tools_chars = sum(len(json.dumps(t, ensure_ascii=False)) for t in captured["tools"])
    instructions_chars = len(captured["instructions"])
    total = tools_chars + instructions_chars
    return {
        "totals": {
            "tools": len(captured["tools"]),
            "tools_chars": tools_chars,
            "instructions_chars": instructions_chars,
            "total_chars": total,
            "approx_tokens": total // 4,
        },
        "instructions": captured["instructions"],
        "tools": captured["tools"],
    }


@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_prompt_surface_matches_snapshot(surface):
    current = await _capture(*SURFACES[surface])
    path = SNAPSHOT_DIR / f"{surface}.json"

    if UPDATE or not path.exists():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
        if not UPDATE:
            pytest.fail(f"wrote missing snapshot {path.name}; review and commit it")
        return

    saved = json.loads(path.read_text())
    if saved != current:
        before, after = saved["totals"], current["totals"]
        delta = after["total_chars"] - before["total_chars"]
        pytest.fail(
            f"{surface}: prompt surface changed ({before['total_chars']} -> "
            f"{after['total_chars']} chars, {delta:+d}; ~{delta // 4:+d} tokens per model "
            f"request). If intended, regenerate with UPDATE_PROMPT_SURFACE=1 and commit "
            f"tests/fixtures/prompt_surface/{surface}.json."
        )


async def test_no_tool_description_carries_a_returns_block():
    # pydantic-ai turns a docstring "Returns:" section into <returns>...</returns>
    # in the description; the model never needs it. Keep Returns: out of tools.
    surface = await _capture(*SURFACES["private_all_features"])
    offenders = [t["name"] for t in surface["tools"] if "<returns>" in (t["description"] or "")]
    assert not offenders, f"drop the Returns: section from {offenders}"
