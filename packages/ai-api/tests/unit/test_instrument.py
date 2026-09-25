"""Unit tests for opt-in Logfire instrumentation (instrument.py).

The content-exclusion test is the privacy guarantee: `include_content=False` is
the only thing keeping real WhatsApp conversation text out of a third-party
service, so it is verified against real spans rather than a mock's own kwargs.
"""

from unittest.mock import patch

import logfire
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_api.instrument import setup_instrumentation


class TestTokenGuard:
    """Logfire must stay completely inert without a write token."""

    def test_no_token_never_touches_logfire(self):
        with patch("ai_api.instrument.settings") as mock_settings:
            mock_settings.logfire_token = None
            with patch("ai_api.instrument.logfire") as mock_logfire:
                setup_instrumentation("ai-api")

        mock_logfire.configure.assert_not_called()
        mock_logfire.instrument_pydantic_ai.assert_not_called()

    def test_empty_token_is_treated_as_absent(self):
        with patch("ai_api.instrument.settings") as mock_settings:
            mock_settings.logfire_token = ""
            with patch("ai_api.instrument.logfire") as mock_logfire:
                setup_instrumentation("ai-api")

        mock_logfire.configure.assert_not_called()

    def test_token_is_passed_explicitly(self):
        """Regression: .env reaches Settings but never os.environ, so Logfire
        cannot discover the token on its own — it must be handed over."""
        with patch("ai_api.instrument.settings") as mock_settings:
            mock_settings.logfire_token = "pylf_v1_us_example"
            mock_settings.logfire_environment = "production"
            with patch("ai_api.instrument.logfire") as mock_logfire:
                setup_instrumentation("ai-api-worker")

        kwargs = mock_logfire.configure.call_args.kwargs
        assert kwargs["token"] == "pylf_v1_us_example"
        assert kwargs["service_name"] == "ai-api-worker"
        assert kwargs["environment"] == "production"


class TestContentExclusion:
    """Real spans, real agent run — no mocks standing in for the SDK."""

    @pytest.fixture
    def exported_spans(self):
        """Run an agent through instrumented Logfire, capturing spans locally."""
        from logfire.testing import TestExporter

        exporter = TestExporter()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            additional_span_processors=[SimpleSpanProcessor(exporter)],
        )
        logfire.instrument_pydantic_ai(include_content=False)

        agent = Agent(TestModel(), name="privacy_probe")
        agent.run_sync(self.CANARY)

        yield exporter.exported_spans

        # Leave global instrumentation off for any test that runs after this one.
        Agent.instrument_all(False)

    CANARY = "SUPERSECRETUSERMESSAGE about my bank account"

    def test_message_content_is_not_captured(self, exported_spans):
        for span in exported_spans:
            for key, value in (span.attributes or {}).items():
                assert self.CANARY not in str(value), f"leaked via attribute {key!r}"

    def test_token_usage_is_still_captured(self, exported_spans):
        attrs = {}
        for span in exported_spans:
            attrs.update(dict(span.attributes or {}))

        assert attrs.get("gen_ai.usage.input_tokens", 0) > 0
        assert attrs.get("gen_ai.usage.output_tokens", 0) > 0
