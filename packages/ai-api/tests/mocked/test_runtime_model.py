"""Verify that build_runtime_model() honors the runtime_config override and
that the wired model name flows into agent.run_stream from agent/response.py."""

import time
from unittest.mock import AsyncMock, MagicMock, patch


def test_build_runtime_model_uses_runtime_config_override():
    """The factory reads runtime_config.get('gemini_model') and constructs a
    GoogleModel from that name (not the env default)."""
    from ai_api.agent import core as agent_core
    from ai_api.runtime_config import runtime_config

    prior_overrides = runtime_config._overrides
    prior_loaded_at = runtime_config._loaded_at
    try:
        runtime_config._overrides = {"gemini_model": "gemini-3.1-pro"}
        runtime_config._loaded_at = time.monotonic()  # keep cache fresh

        with patch.object(agent_core, "GoogleModel") as mock_google_model:
            agent_core.build_runtime_model()
        # GoogleModel called once with the override name and the module-level provider
        mock_google_model.assert_called_once()
        assert mock_google_model.call_args.args[0] == "gemini-3.1-pro"
        assert mock_google_model.call_args.kwargs.get("provider") is agent_core.google_provider
    finally:
        runtime_config._overrides = prior_overrides
        runtime_config._loaded_at = prior_loaded_at


def test_build_runtime_model_falls_back_to_settings_default():
    """With no override, the factory uses settings.gemini_model."""
    from ai_api.agent import core as agent_core
    from ai_api.config import settings
    from ai_api.runtime_config import runtime_config

    prior_overrides = runtime_config._overrides
    prior_loaded_at = runtime_config._loaded_at
    try:
        runtime_config._overrides = {}
        runtime_config._loaded_at = time.monotonic()

        with patch.object(agent_core, "GoogleModel") as mock_google_model:
            agent_core.build_runtime_model()
        assert mock_google_model.call_args.args[0] == settings.gemini_model
    finally:
        runtime_config._overrides = prior_overrides
        runtime_config._loaded_at = prior_loaded_at


async def test_run_stream_receives_model_override():
    """agent.run_stream is invoked with model=build_runtime_model() — confirms
    every chat run picks up the current runtime_config value, not the
    module-level Agent default."""
    from ai_api.agent import response as agent_response

    # Build a context-manager mock for agent.run_stream(...).
    fake_stream_ctx = MagicMock()

    async def _aenter(self):
        result = MagicMock()

        async def _stream_text(delta: bool = True):
            for chunk in ("hello", " world"):
                yield chunk

        result.stream_text = _stream_text
        return result

    async def _aexit(self, *a):
        return None

    fake_stream_ctx.__aenter__ = _aenter
    fake_stream_ctx.__aexit__ = _aexit

    fake_run_stream = MagicMock(return_value=fake_stream_ctx)
    sentinel_model = object()

    with (
        patch.object(agent_response.agent, "run_stream", fake_run_stream),
        patch.object(agent_response, "build_runtime_model", return_value=sentinel_model),
    ):
        chunks = []
        async for chunk in agent_response.get_ai_response(
            user_message="hi",
            message_history=[],
            agent_deps=AsyncMock(),
        ):
            chunks.append(chunk)

    assert chunks == ["hello", " world"]
    fake_run_stream.assert_called_once()
    assert fake_run_stream.call_args.kwargs["model"] is sentinel_model
