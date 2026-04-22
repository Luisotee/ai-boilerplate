"""
Integration tests for the /transcribe endpoint error mapping.

Verifies that the route maps each class of dispatcher outcome to the right
HTTP status:
  - transient upstream errors           -> 502
  - backend 4xx (SttClientError)        -> 400
  - Groq rate-limit (RateLimitError)    -> 429 (with Retry-After passthrough)

In every case, the raw SDK error is still captured in logs for operators.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import groq
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from ai_api.transcription import SttClientError

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}


def _make_mock_db():
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    return db


def _get_app_with_db_override(mock_db):
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _cleanup_overrides():
    from ai_api.main import app

    app.dependency_overrides.clear()


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_transcribe_maps_recoverable_upstream_error_to_502(
    mock_cleanup, mock_redis, mock_init_db, monkeypatch, caplog
):
    """A recoverable upstream error (e.g. httpx.ConnectError) from the
    dispatcher must surface as 502 with a generic, stable detail — the raw
    SDK error must still be captured in logs for operators, just not echoed
    back to API clients."""
    app = _get_app_with_db_override(_make_mock_db())

    async def _raise_upstream(*_args, **_kwargs):
        raise httpx.ConnectError("upstream down")

    # Patch the name bound inside routes.speech (not the source module) so
    # the route's `except RECOVERABLE_STT_ERRORS` branch is exercised.
    monkeypatch.setattr("ai_api.routes.speech.transcribe_audio_dispatcher", _raise_upstream)

    transport = ASGITransport(app=app)
    try:
        with caplog.at_level(logging.ERROR, logger="ai-api"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/transcribe",
                    headers=AUTH_HEADERS,
                    files={"file": ("clip.mp3", b"fake-audio-bytes", "audio/mpeg")},
                )
    finally:
        _cleanup_overrides()

    assert response.status_code == 502, response.text
    assert (
        response.json()["detail"]
        == "Transcription service is temporarily unavailable. Please try again."
    )
    assert any("upstream down" in r.message for r in caplog.records), (
        "raw upstream error must still be logged for operators"
    )


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_transcribe_maps_client_error_to_400(
    mock_cleanup, mock_redis, mock_init_db, monkeypatch, caplog
):
    """SttClientError (backend 4xx) must surface as HTTP 400 with the
    backend-provided detail — not 500 (old tuple path) and not 502
    (recoverable-error path)."""
    app = _get_app_with_db_override(_make_mock_db())

    async def _raise_client_error(*_args, **_kwargs):
        raise SttClientError("Self-hosted Whisper rejected request (400 Bad Request)")

    monkeypatch.setattr("ai_api.routes.speech.transcribe_audio_dispatcher", _raise_client_error)

    transport = ASGITransport(app=app)
    try:
        with caplog.at_level(logging.WARNING, logger="ai-api"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/transcribe",
                    headers=AUTH_HEADERS,
                    files={"file": ("clip.mp3", b"fake-audio-bytes", "audio/mpeg")},
                )
    finally:
        _cleanup_overrides()

    assert response.status_code == 400, response.text
    assert "rejected request" in response.json()["detail"]
    assert any("STT client error" in r.message for r in caplog.records), (
        "client-side rejections must still be logged"
    )


@pytest.mark.parametrize(
    "retry_after,expect_header",
    [
        ("42", "42"),
        (None, None),
    ],
    ids=["with_retry_after", "without_retry_after"],
)
@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_transcribe_maps_groq_rate_limit_to_429(
    mock_cleanup,
    mock_redis,
    mock_init_db,
    retry_after,
    expect_header,
    monkeypatch,
    caplog,
):
    """groq.RateLimitError must surface as HTTP 429 with the provider's
    Retry-After hint forwarded verbatim — not 502 'temporarily unavailable'."""
    app = _get_app_with_db_override(_make_mock_db())

    request = httpx.Request("POST", "http://groq.test")
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, request=request, headers=headers)
    err = groq.RateLimitError("rate limited", response=response, body=None)

    async def _raise_rate_limit(*_args, **_kwargs):
        raise err

    monkeypatch.setattr("ai_api.routes.speech.transcribe_audio_dispatcher", _raise_rate_limit)

    transport = ASGITransport(app=app)
    try:
        with caplog.at_level(logging.WARNING, logger="ai-api"):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/transcribe",
                    headers=AUTH_HEADERS,
                    files={"file": ("clip.mp3", b"fake-audio-bytes", "audio/mpeg")},
                )
    finally:
        _cleanup_overrides()

    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"] == (
        "Transcription provider rate limit exceeded. Please try again later."
    )
    assert resp.headers.get("retry-after") == expect_header
    assert any("rate limited by Groq" in r.message for r in caplog.records), (
        "rate-limit errors must still be logged"
    )
