"""
Integration tests for /preferences/* endpoints.

Tests cover:
- GET /preferences/{jid} returns user preferences
- GET /preferences/{jid} returns 404 for unknown user
- PATCH /preferences/{jid} updates TTS/STT settings
- PATCH /preferences/{jid} validates language codes
- Authentication is required for all preferences endpoints
"""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_conversation_preferences

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

TEST_JID = "5511999999999@s.whatsapp.net"


def _make_mock_db():
    """Create a mock database session."""
    db = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.add = MagicMock()
    db.rollback = MagicMock()
    return db


def _get_app_with_db_override(mock_db):
    """Import app and override get_db dependency."""
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _cleanup_overrides():
    """Remove all dependency overrides."""
    from ai_api.main import app

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /preferences/{jid}
# ---------------------------------------------------------------------------


class TestGetPreferences:
    """Tests for GET /preferences/{whatsapp_jid}."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_returns_default_preferences(self, mock_cleanup, mock_redis, mock_init_db):
        """GET /preferences/{jid} returns default preferences for existing user."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get(
                        f"/preferences/{TEST_JID}",
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["tts_enabled"] is False
                assert data["tts_language"] == "en"
                assert data["stt_language"] is None
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_returns_custom_preferences(self, mock_cleanup, mock_redis, mock_init_db):
        """GET /preferences/{jid} returns custom preferences when set."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=True,
            tts_language="es",
            stt_language="fr",
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get(
                        f"/preferences/{TEST_JID}",
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["tts_enabled"] is True
                assert data["tts_language"] == "es"
                assert data["stt_language"] == "fr"
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_unknown_user_returns_404(self, mock_cleanup, mock_redis, mock_init_db):
        """GET /preferences/{jid} returns 404 when user does not exist."""
        mock_db = _make_mock_db()

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=None):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get(
                        "/preferences/unknown_jid@s.whatsapp.net",
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 404
                assert "User not found" in response.json()["detail"]
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_requires_auth(self, mock_cleanup, mock_redis, mock_init_db):
        """GET /preferences/{jid} without API key returns 401."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/preferences/{TEST_JID}")

        assert response.status_code == 401
        assert "API key" in response.json()["detail"]


# ---------------------------------------------------------------------------
# PATCH /preferences/{jid}
# ---------------------------------------------------------------------------


class TestUpdatePreferences:
    """Tests for PATCH /preferences/{whatsapp_jid}."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_enable_tts(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with tts_enabled=true updates preference."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        def refresh_side_effect(obj):
            # After commit+refresh, tts_enabled should be True
            pass

        mock_db.refresh = MagicMock(side_effect=refresh_side_effect)

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"tts_enabled": True},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                # The mock prefs object was mutated by the route handler
                assert data["tts_enabled"] is True
                assert data["tts_language"] == "en"
                mock_db.commit.assert_called()
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_update_tts_language(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with valid tts_language updates it."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"tts_language": "es"},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["tts_language"] == "es"
                mock_db.commit.assert_called()
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_update_stt_language(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with valid stt_language updates it."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"stt_language": "pt"},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["stt_language"] == "pt"
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_stt_language_auto_sets_null(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with stt_language='auto' sets it to null."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language="es",
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"stt_language": "auto"},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["stt_language"] is None
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_invalid_tts_language_returns_400(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with unsupported tts_language returns 400."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"tts_language": "zh"},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 400
                assert "Invalid TTS language" in response.json()["detail"]
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_invalid_stt_language_returns_400(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with unsupported stt_language returns 400."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={"stt_language": "xx"},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 400
                assert "Invalid STT language" in response.json()["detail"]
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_update_nonexistent_user_returns_404(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """PATCH /preferences/{jid} for unknown user returns 404."""
        mock_db = _make_mock_db()

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=None):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        "/preferences/unknown_jid@s.whatsapp.net",
                        json={"tts_enabled": True},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 404
                assert "User not found" in response.json()["detail"]
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_update_multiple_fields(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} can update multiple fields at once."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={
                            "tts_enabled": True,
                            "tts_language": "de",
                            "stt_language": "fr",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["tts_enabled"] is True
                assert data["tts_language"] == "de"
                assert data["stt_language"] == "fr"
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_empty_update_is_noop(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} with empty body preserves existing values."""
        mock_db = _make_mock_db()
        mock_prefs = make_conversation_preferences(
            tts_enabled=True,
            tts_language="pt",
            stt_language="de",
        )

        with patch("ai_api.routes.preferences.get_user_preferences", return_value=mock_prefs):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.patch(
                        f"/preferences/{TEST_JID}",
                        json={},
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                # Values remain unchanged
                assert data["tts_enabled"] is True
                assert data["tts_language"] == "pt"
                assert data["stt_language"] == "de"
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_requires_auth(self, mock_cleanup, mock_redis, mock_init_db):
        """PATCH /preferences/{jid} without API key returns 401."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.patch(
                f"/preferences/{TEST_JID}",
                json={"tts_enabled": True},
            )

        assert response.status_code == 401
        assert "API key" in response.json()["detail"]
