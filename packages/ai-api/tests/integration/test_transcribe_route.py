"""
Integration tests for the /transcribe endpoint error mapping.

Specifically verifies that transient STT upstream errors surface as a
descriptive 502 Bad Gateway, rather than being swallowed by the generic
catch-all 500 handler.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from httpx import ASGITransport, AsyncClient

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
    mock_cleanup, mock_redis, mock_init_db, monkeypatch
):
    """A recoverable upstream error (e.g. httpx.ConnectError) from the
    dispatcher must surface as 502 with the original error message in detail,
    not the generic 500 'Internal server error' from the catch-all."""
    app = _get_app_with_db_override(_make_mock_db())

    async def _raise_upstream(*_args, **_kwargs):
        raise httpx.ConnectError("upstream down")

    # Patch the name bound inside routes.speech (not the source module) so
    # the route's `except RECOVERABLE_STT_ERRORS` branch is exercised.
    monkeypatch.setattr("ai_api.routes.speech.transcribe_audio_dispatcher", _raise_upstream)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/transcribe",
                headers=AUTH_HEADERS,
                files={"file": ("clip.mp3", b"fake-audio-bytes", "audio/mpeg")},
            )
    finally:
        _cleanup_overrides()

    assert response.status_code == 502, response.text
    assert "upstream down" in response.json()["detail"]
