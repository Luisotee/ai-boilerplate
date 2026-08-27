"""Unit tests for opt-in Logfire instrumentation (instrument.py).

The `include_content=False` assertion is the privacy guarantee: it is the only
thing keeping real WhatsApp conversation text out of a third-party service.
"""

from unittest.mock import patch

from ai_api.instrument import setup_instrumentation


def test_configure_uses_if_token_present():
    """Logfire must be a no-op when no write token is set."""
    with patch("ai_api.instrument.logfire") as mock_logfire:
        setup_instrumentation("ai-api")

    kwargs = mock_logfire.configure.call_args.kwargs
    assert kwargs["send_to_logfire"] == "if-token-present"


def test_message_content_is_not_captured():
    """Prompts, completions, and tool args must never be sent to Logfire."""
    with patch("ai_api.instrument.logfire") as mock_logfire:
        setup_instrumentation("ai-api")

    mock_logfire.instrument_pydantic_ai.assert_called_once_with(include_content=False)


def test_service_name_is_propagated():
    """The two processes must be distinguishable in the Logfire UI."""
    with patch("ai_api.instrument.logfire") as mock_logfire:
        setup_instrumentation("ai-api-worker")

    assert mock_logfire.configure.call_args.kwargs["service_name"] == "ai-api-worker"


def test_console_exporter_disabled():
    """stdout logging is already handled by logger.py — avoid duplicate output."""
    with patch("ai_api.instrument.logfire") as mock_logfire:
        setup_instrumentation("ai-api")

    assert mock_logfire.configure.call_args.kwargs["console"] is False
