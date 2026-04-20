"""Tests for the STT provider dispatcher and self-hosted Whisper adapter."""

import json
from unittest.mock import AsyncMock, MagicMock

import groq
import httpx
import pytest

from ai_api import transcription
from ai_api.config import settings
from ai_api.transcription import (
    SttNotConfiguredError,
    transcribe_audio_dispatcher,
    transcribe_audio_via_whisper,
)

AUDIO = b"fake-audio-bytes"
FILENAME = "clip.mp3"


def _mock_groq_path(monkeypatch, *, text="groq result", error=None, side_effect=None):
    """Stub `create_groq_client` + `transcribe_audio` so the Groq path is observable."""
    client_sentinel = MagicMock(name="groq-client")
    monkeypatch.setattr(
        transcription, "create_groq_client", MagicMock(return_value=client_sentinel)
    )
    groq_mock = (
        AsyncMock(side_effect=side_effect) if side_effect else AsyncMock(return_value=(text, error))
    )
    monkeypatch.setattr(transcription, "transcribe_audio", groq_mock)
    return groq_mock, client_sentinel


def _mock_whisper_path(monkeypatch, *, text="whisper result", error=None, side_effect=None):
    """Stub `transcribe_audio_via_whisper` so the self-hosted path is observable."""
    whisper_mock = (
        AsyncMock(side_effect=side_effect) if side_effect else AsyncMock(return_value=(text, error))
    )
    monkeypatch.setattr(transcription, "transcribe_audio_via_whisper", whisper_mock)
    return whisper_mock


class TestDispatcherAutoMode:
    async def test_auto_prefers_groq_when_both_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        groq_mock, _ = _mock_groq_path(monkeypatch, text="from groq")
        whisper_mock = _mock_whisper_path(monkeypatch, text="from whisper")

        text, error = await transcribe_audio_dispatcher(AUDIO, FILENAME)

        assert (text, error) == ("from groq", None)
        groq_mock.assert_awaited_once()
        whisper_mock.assert_not_awaited()

    async def test_auto_uses_whisper_when_groq_missing(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", None)
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        groq_create = MagicMock(name="groq-create-should-not-run")
        monkeypatch.setattr(transcription, "create_groq_client", groq_create)
        whisper_mock = _mock_whisper_path(monkeypatch, text="from whisper")

        text, error = await transcribe_audio_dispatcher(AUDIO, FILENAME)

        assert (text, error) == ("from whisper", None)
        groq_create.assert_not_called()
        whisper_mock.assert_awaited_once()

    async def test_auto_falls_back_to_whisper_on_recoverable_groq_error(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        # Build a real APIConnectionError — the class requires a request kwarg.
        err = groq.APIConnectionError(request=httpx.Request("POST", "http://groq.test"))
        groq_mock, _ = _mock_groq_path(monkeypatch, side_effect=err)
        whisper_mock = _mock_whisper_path(monkeypatch, text="fallback text")

        with caplog.at_level("WARNING", logger="ai-api"):
            text, error = await transcribe_audio_dispatcher(AUDIO, FILENAME)

        assert (text, error) == ("fallback text", None)
        groq_mock.assert_awaited_once()
        whisper_mock.assert_awaited_once()
        warnings = [r for r in caplog.records if "falling back to self-hosted" in r.message]
        assert warnings, "expected fallback warning"
        assert warnings[0].exc_info is not None

    async def test_auto_does_not_fall_back_on_programming_error(self, monkeypatch):
        """Non-recoverable errors (TypeError, AttributeError) must propagate so
        SDK drift surfaces as a real bug instead of silently rerouting."""
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        groq_mock, _ = _mock_groq_path(monkeypatch, side_effect=TypeError("SDK drift"))
        whisper_mock = _mock_whisper_path(monkeypatch)

        with pytest.raises(TypeError, match="SDK drift"):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)
        groq_mock.assert_awaited_once()
        whisper_mock.assert_not_awaited()

    async def test_auto_raises_when_nothing_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", None)
        monkeypatch.setattr(settings, "whisper_base_url", None)

        with pytest.raises(SttNotConfiguredError, match="No STT provider configured"):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)

    async def test_auto_reraises_groq_error_without_whisper_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "auto")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", None)

        err = httpx.ConnectError("unreachable")
        groq_mock, _ = _mock_groq_path(monkeypatch, side_effect=err)

        with pytest.raises(httpx.ConnectError):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)
        groq_mock.assert_awaited_once()


class TestDispatcherExplicitModes:
    async def test_explicit_groq_raises_without_key(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "groq")
        monkeypatch.setattr(settings, "groq_api_key", None)

        with pytest.raises(SttNotConfiguredError, match="GROQ_API_KEY is not set"):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)

    async def test_explicit_whisper_raises_without_url(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "whisper")
        monkeypatch.setattr(settings, "whisper_base_url", None)

        with pytest.raises(SttNotConfiguredError, match="WHISPER_BASE_URL is not set"):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)

    async def test_explicit_groq_does_not_fall_back(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "groq")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        err = httpx.ConnectError("unreachable")
        groq_mock, _ = _mock_groq_path(monkeypatch, side_effect=err)
        whisper_mock = _mock_whisper_path(monkeypatch)

        with pytest.raises(httpx.ConnectError):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)
        groq_mock.assert_awaited_once()
        whisper_mock.assert_not_awaited()

    async def test_explicit_whisper_does_not_fall_back(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_provider", "whisper")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-test")
        monkeypatch.setattr(settings, "whisper_base_url", "http://whisper:8000")

        err = httpx.ConnectError("whisper unreachable")
        whisper_mock = _mock_whisper_path(monkeypatch, side_effect=err)
        groq_create = MagicMock(name="groq-create-should-not-run")
        monkeypatch.setattr(transcription, "create_groq_client", groq_create)

        with pytest.raises(httpx.ConnectError):
            await transcribe_audio_dispatcher(AUDIO, FILENAME)
        whisper_mock.assert_awaited_once()
        groq_create.assert_not_called()


class TestWhisperAdapter:
    """Integration-style test of the httpx call made by transcribe_audio_via_whisper."""

    async def test_posts_openai_compatible_multipart(self, monkeypatch):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["content_type"] = request.headers.get("content-type", "")
            captured["body"] = request.content
            return httpx.Response(200, json={"text": "  hello world  "})

        transport = httpx.MockTransport(handler)

        # Redirect httpx.AsyncClient so our transport is used — no real network I/O.
        real_cls = httpx.AsyncClient

        def _factory(**kwargs):
            kwargs["transport"] = transport
            return real_cls(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        monkeypatch.setattr(settings, "whisper_model", "Systran/faster-distil-whisper-large-v3")

        text, error = await transcribe_audio_via_whisper(
            "http://whisper:8000/", AUDIO, FILENAME, language="en"
        )

        assert error is None
        assert text == "hello world"
        assert captured["url"] == "http://whisper:8000/v1/audio/transcriptions"
        assert captured["method"] == "POST"
        assert "multipart/form-data" in captured["content_type"]
        body = captured["body"]
        # Multipart body should include our model + language + file bytes.
        assert b"Systran/faster-distil-whisper-large-v3" in body
        assert b"language" in body and b"en" in body
        assert AUDIO in body

    async def test_returns_error_tuple_on_empty_text(self, monkeypatch):
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, content=json.dumps({"text": ""}).encode())
        )
        real_cls = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_cls(transport=transport, **kw),
        )

        text, error = await transcribe_audio_via_whisper("http://whisper:8000", AUDIO, FILENAME)
        assert text is None
        assert error and "no text" in error

    async def test_raises_recoverable_on_5xx(self, monkeypatch):
        transport = httpx.MockTransport(lambda _req: httpx.Response(503, text="down"))
        real_cls = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: real_cls(transport=transport, **kw),
        )

        with pytest.raises(httpx.HTTPStatusError):
            await transcribe_audio_via_whisper("http://whisper:8000", AUDIO, FILENAME)
