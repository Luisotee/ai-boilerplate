"""Unit tests for the runtime-settings overlay (runtime_config.py).

These tests exercise the pure resolution + validation logic without a database
by manipulating the in-process cache directly.
"""

import time

import pytest

from ai_api.config import settings
from ai_api.runtime_config import (
    REGISTRY,
    REGISTRY_BY_KEY,
    SettingSpec,
    coerce_value,
    runtime_config,
)


@pytest.fixture(autouse=True)
def _freeze_cache():
    """Pin the cache fresh + empty before each test so get() never hits the DB."""
    runtime_config._overrides = {}
    runtime_config._loaded_at = time.monotonic()
    yield
    runtime_config._overrides = {}
    runtime_config._loaded_at = 0.0


# --- Registry integrity ---


def test_registry_keys_are_unique():
    keys = [spec.key for spec in REGISTRY]
    assert len(keys) == len(set(keys))


def test_every_registered_key_exists_on_settings():
    for spec in REGISTRY:
        assert hasattr(settings, spec.key), f"{spec.key} is not a real Settings field"


def test_choices_only_on_str_specs():
    for spec in REGISTRY:
        if spec.choices is not None:
            assert spec.type == "str"


# --- get() resolution ---


def test_get_returns_env_default_for_hot_key_without_override():
    assert runtime_config.get("tts_default_voice") == settings.tts_default_voice


def test_get_returns_override_for_hot_key():
    runtime_config._overrides = {"tts_default_voice": "Puck"}
    assert runtime_config.get("tts_default_voice") == "Puck"


def test_get_ignores_override_for_non_hot_key():
    # log_level is display-only; even a cached value must not win.
    runtime_config._overrides = {"log_level": "DEBUG"}
    assert runtime_config.get("log_level") == settings.log_level


def test_get_unknown_key_falls_back_to_settings():
    # An attribute that exists on settings but is not in the registry.
    assert runtime_config.get("gemini_api_key") == settings.gemini_api_key


def test_invalidate_forces_reload_on_next_get(monkeypatch):
    calls = {"n": 0}

    def fake_refresh():
        calls["n"] += 1
        runtime_config._overrides = {"tts_default_voice": "Charon"}
        runtime_config._loaded_at = time.monotonic()

    monkeypatch.setattr(runtime_config, "_refresh_locked", fake_refresh)
    runtime_config.invalidate()
    assert runtime_config.get("tts_default_voice") == "Charon"
    assert calls["n"] == 1


# --- coerce_value ---


def test_coerce_int_accepts_int_rejects_bool_and_str():
    spec = REGISTRY_BY_KEY["history_limit_private"]
    assert coerce_value(spec, 42) == 42
    with pytest.raises(ValueError):
        coerce_value(spec, True)
    with pytest.raises(ValueError):
        coerce_value(spec, "10")


def test_coerce_float_accepts_int_and_float():
    spec = REGISTRY_BY_KEY["semantic_similarity_threshold"]
    assert coerce_value(spec, 0.5) == 0.5
    assert coerce_value(spec, 1) == 1.0
    with pytest.raises(ValueError):
        coerce_value(spec, "high")


def test_coerce_str_with_choices_enforces_membership():
    spec = REGISTRY_BY_KEY["pdf_parser"]
    assert coerce_value(spec, "llamaparse") == "llamaparse"
    with pytest.raises(ValueError):
        coerce_value(spec, "bogus")


def test_coerce_bool_requires_bool():
    spec = SettingSpec("x", "bool", True, "test", "desc")
    assert coerce_value(spec, False) is False
    with pytest.raises(ValueError):
        coerce_value(spec, 1)
