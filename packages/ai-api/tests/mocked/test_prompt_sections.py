"""The base prompt as sections with stale-guarded overrides (agent/prompt.py)."""

from types import SimpleNamespace

from ai_api.agent.prompt import (
    DEFAULT_PROMPT_HASH,
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_SECTIONS,
    SECTIONS_BY_KEY,
    compose,
    resolve_prompt,
    section_hash,
)


def _row(content, base):
    return SimpleNamespace(content=content, base_hash=section_hash(base), updated_at=None)


def test_sections_compose_to_the_default_prompt():
    assert DEFAULT_SYSTEM_PROMPT == compose(s.default for s in PROMPT_SECTIONS)
    assert DEFAULT_PROMPT_HASH == section_hash(DEFAULT_SYSTEM_PROMPT)
    assert len({s.key for s in PROMPT_SECTIONS}) == len(PROMPT_SECTIONS)


def test_the_operator_section_is_empty_in_code_so_it_never_goes_stale():
    assert SECTIONS_BY_KEY["operator"].default == ""
    resolved = resolve_prompt({"operator": _row("Always sign as the support team.", "")})
    assert resolved.text == DEFAULT_SYSTEM_PROMPT + "\n\nAlways sign as the support team."


def test_no_overrides_is_the_code_default():
    resolved = resolve_prompt({})
    assert resolved.text == DEFAULT_SYSTEM_PROMPT
    assert {s.status for s in resolved.sections} == {"default"}
    assert resolved.stale_keys == []


def test_a_fresh_section_override_replaces_only_that_section():
    tone = SECTIONS_BY_KEY["tone"]
    resolved = resolve_prompt({"tone": _row("Tone: brief.", tone.default)})
    assert "Tone: brief." in resolved.text
    assert tone.default not in resolved.text
    assert SECTIONS_BY_KEY["identity"].default in resolved.text
    assert [s.section.key for s in resolved.applied_sections] == ["tone"]


def test_a_stale_section_override_is_paused_and_the_code_applies():
    resolved = resolve_prompt({"tone": _row("Tone: old edit.", "an older default")})
    assert resolved.text == DEFAULT_SYSTEM_PROMPT
    tone = next(s for s in resolved.sections if s.section.key == "tone")
    assert tone.status == "stale"
    assert tone.override.content == "Tone: old edit."
    assert resolved.stale_keys == ["tone"]


def test_an_empty_override_leaves_the_section_out():
    bursts = SECTIONS_BY_KEY["bursts"]
    resolved = resolve_prompt({"bursts": _row("", bursts.default)})
    assert bursts.default not in resolved.text
    assert not resolved.text.endswith("\n")


def test_whole_override_applies_only_on_the_current_default():
    fresh = resolve_prompt({"all": _row("WHOLE", DEFAULT_SYSTEM_PROMPT)})
    assert fresh.text == "WHOLE" and fresh.whole.active and not fresh.whole.stale
    stale = resolve_prompt({"all": _row("WHOLE", "older default")})
    assert stale.text == DEFAULT_SYSTEM_PROMPT and stale.whole.stale
    assert stale.stale_keys == ["all"]


def test_rows_for_unknown_keys_are_ignored():
    assert resolve_prompt({"gone": _row("x", "y")}).text == DEFAULT_SYSTEM_PROMPT


def test_stale_overrides_are_logged_once(caplog):
    from ai_api.agent import prompt

    rows = {"tone": _row("Tone: x.", "older")}
    prompt._warned_stale.clear()
    resolved = resolve_prompt(rows)
    prompt.warn_on_stale(resolved, rows)
    prompt.warn_on_stale(resolved, rows)
    assert caplog.text.count("Prompt override 'tone' is stale") == 1
