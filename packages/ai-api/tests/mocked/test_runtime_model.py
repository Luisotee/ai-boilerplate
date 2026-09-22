"""Verify that build_runtime_model() honors the runtime_config overrides and
that the wired model flows into agent.run_stream_events from agent/response.py.

When DEEPSEEK_API_KEY is set the factory returns
FallbackModel(DeepSeek -> Gemini), otherwise Gemini alone."""

import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
from pydantic_ai.models import Model


@contextmanager
def _runtime_state(overrides: dict, deepseek_provider):
    """Pin runtime_config overrides + the module-level DeepSeek provider, restoring both."""
    from ai_api.agent import core as agent_core
    from ai_api.agent import model_chain
    from ai_api.runtime_config import runtime_config

    prior_overrides = runtime_config._overrides
    prior_loaded_at = runtime_config._loaded_at
    prior_provider = model_chain.deepseek_provider
    try:
        runtime_config._overrides = overrides
        runtime_config._loaded_at = time.monotonic()
        model_chain.deepseek_provider = deepseek_provider
        yield agent_core, model_chain
    finally:
        runtime_config._overrides = prior_overrides
        runtime_config._loaded_at = prior_loaded_at
        model_chain.deepseek_provider = prior_provider


def test_gemini_only_when_deepseek_not_configured():
    """No DeepSeek key -> a bare GoogleModel built from the gemini_model override."""
    with _runtime_state({"gemini_model": "gemini-3.1-pro"}, None) as (agent_core, chain):
        with (
            patch.object(chain, "GoogleModel") as mock_google,
            patch.object(chain, "FallbackModel") as mock_fallback,
        ):
            model = agent_core.build_runtime_model()
    mock_fallback.assert_not_called()
    assert model is mock_google.return_value
    assert mock_google.call_args.args[0] == "gemini-3.1-pro"
    assert mock_google.call_args.kwargs.get("provider") is chain.google_provider


def test_gemini_falls_back_to_settings_default():
    """With no override, the factory uses settings.gemini_model."""
    from ai_api.config import settings

    with _runtime_state({}, None) as (agent_core, chain):
        with patch.object(chain, "GoogleModel") as mock_google:
            agent_core.build_runtime_model()
    assert mock_google.call_args.args[0] == settings.gemini_model


def test_deepseek_then_gemini_when_configured():
    """DeepSeek configured -> FallbackModel(deepseek, gemini) with the SDK error
    types in fallback_on (first-chunk timeouts surface as raw openai errors)."""
    provider = MagicMock(name="deepseek_provider")
    overrides = {"deepseek_model": "deepseek-custom", "gemini_model": "gemini-3.1-pro"}
    with _runtime_state(overrides, provider) as (agent_core, chain):
        with (
            patch.object(chain, "GoogleModel") as mock_google,
            patch.object(
                chain, "OpenAIChatModel", return_value=MagicMock(spec=Model)
            ) as mock_openai_chat,
            patch.object(chain, "FallbackModel") as mock_fallback,
        ):
            model = agent_core.build_runtime_model()

    assert model is mock_fallback.return_value
    mock_fallback.assert_called_once()
    guarded, gemini = mock_fallback.call_args.args
    assert isinstance(guarded, chain.GuardedModel)
    assert guarded.wrapped is mock_openai_chat.return_value
    assert guarded.cooldown is chain.deepseek_cooldown
    assert gemini is mock_google.return_value
    fallback_on = mock_fallback.call_args.kwargs["fallback_on"]
    assert openai.APIError in fallback_on
    assert httpx.TransportError in fallback_on

    assert mock_openai_chat.call_args.args[0] == "deepseek-custom"
    assert mock_openai_chat.call_args.kwargs["provider"] is provider
    assert mock_google.call_args.args[0] == "gemini-3.1-pro"


def test_deepseek_thinking_disabled_only_on_deepseek():
    """extra_body lives in DeepSeek's own settings; Gemini gets none."""
    provider = MagicMock(name="deepseek_provider")
    with _runtime_state({}, provider) as (agent_core, chain):
        with (
            patch.object(chain, "GoogleModel") as mock_google,
            patch.object(
                chain, "OpenAIChatModel", return_value=MagicMock(spec=Model)
            ) as mock_openai_chat,
            patch.object(chain, "FallbackModel"),
        ):
            agent_core.build_runtime_model()

    settings = mock_openai_chat.call_args.kwargs["settings"]
    assert settings["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "settings" not in mock_google.call_args.kwargs


async def test_run_stream_events_receives_model_override():
    """agent.run_stream_events is invoked with model=build_runtime_model() — confirms
    every chat run picks up the current runtime_config value, not the
    module-level Agent default."""
    from pydantic_ai import AgentRunResultEvent

    from ai_api.agent import response as agent_response

    async def _events(*args, **kwargs):
        yield MagicMock(name="intermediate_event")
        yield AgentRunResultEvent(result=MagicMock(output="hello world"))

    fake_run_stream_events = MagicMock(side_effect=_events)
    sentinel_model = object()

    with (
        patch.object(agent_response.agent, "run_stream_events", fake_run_stream_events),
        patch.object(agent_response, "build_runtime_model", return_value=sentinel_model),
    ):
        chunks = []
        async for chunk in agent_response.get_ai_response(
            user_message="hi",
            message_history=[],
            agent_deps=AsyncMock(),
        ):
            chunks.append(chunk)

    assert chunks == ["hello world"]
    fake_run_stream_events.assert_called_once()
    assert fake_run_stream_events.call_args.kwargs["model"] is sentinel_model


def test_real_chain_objects_construct():
    """Unpatched construction: the real pydantic-ai classes accept our wiring."""
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.deepseek import DeepSeekProvider

    from ai_api.config import settings

    provider = DeepSeekProvider(
        openai_client=openai.AsyncOpenAI(
            base_url="https://api.deepseek.com", api_key="test", max_retries=0
        )
    )
    with _runtime_state({}, provider) as (agent_core, chain):
        model = agent_core.build_runtime_model()

    assert isinstance(model, FallbackModel)
    guarded, gemini = model.models
    deepseek = guarded.wrapped
    assert isinstance(deepseek, OpenAIChatModel)
    assert deepseek.model_name == settings.deepseek_model
    assert deepseek.settings["extra_body"] == {"thinking": {"type": "disabled"}}
    assert not (gemini.settings or {}).get("extra_body")
