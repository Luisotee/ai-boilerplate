"""
Integration tests for /chat/* endpoints.

Tests cover:
- POST /chat/enqueue with a command message (e.g., "/help") returns command response
- POST /chat/enqueue with missing required fields returns 422
- POST /chat/enqueue requires API key authentication
- POST /chat/save saves a message successfully
- POST /chat/save with missing required fields returns 422
- GET /chat/job/{id} returns job status
- GET /chat/job/{id} requires API key authentication
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_conversation_message, make_user


# Patch the whitelist check so tests are not affected by env-level WHITELIST_PHONES.
# The _is_whitelisted function reads a module-level set from config.py which may be
# populated by the root .env file; patching it to always return True isolates tests.
def _patch_whitelist():
    """Return a fresh patch for the whitelist check on each use."""
    return patch("ai_api.routes.chat._is_whitelisted", return_value=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

TEST_JID = "5511999999999@s.whatsapp.net"


def _make_mock_db():
    """Create a mock database session with standard query chain support."""
    db = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.add = MagicMock()
    db.rollback = MagicMock()
    return db


def _get_app_with_db_override(mock_db):
    """Import the app and override the get_db dependency."""
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _cleanup_overrides():
    """Remove all dependency overrides from the app."""
    from ai_api.main import app

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /chat/enqueue — Command messages
# ---------------------------------------------------------------------------


class TestEnqueueCommand:
    """Tests for POST /chat/enqueue with slash commands."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_help_command_returns_command_response(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """POST /chat/enqueue with '/help' returns is_command=True and help text."""
        mock_db = _make_mock_db()
        mock_user = make_user(whatsapp_jid=TEST_JID)

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.get_or_create_user", return_value=mock_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": TEST_JID,
                            "message": "/help",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["is_command"] is True
                assert "response" in data
                assert "/settings" in data["response"]
                assert "/tts" in data["response"]
                assert "/help" in data["response"]
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_settings_command_returns_preferences(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """POST /chat/enqueue with '/settings' returns current user preferences."""
        mock_db = _make_mock_db()
        mock_user = make_user(whatsapp_jid=TEST_JID)

        # Mock preferences object for settings command
        mock_prefs = MagicMock()
        mock_prefs.tts_enabled = False
        mock_prefs.tts_language = "en"
        mock_prefs.stt_language = None

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.get_or_create_user", return_value=mock_user),
            patch("ai_api.commands.get_or_create_preferences", return_value=mock_prefs),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": TEST_JID,
                            "message": "/settings",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["is_command"] is True
                assert "TTS" in data["response"]
            finally:
                _cleanup_overrides()


# ---------------------------------------------------------------------------
# POST /chat/enqueue — Validation errors
# ---------------------------------------------------------------------------


class TestEnqueueValidation:
    """Tests for POST /chat/enqueue request validation."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_missing_whatsapp_jid_returns_422(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/enqueue without whatsapp_jid returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "message": "Hello",
                    "conversation_type": "private",
                },
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_missing_message_returns_422(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/enqueue without message field returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "whatsapp_jid": TEST_JID,
                    "conversation_type": "private",
                },
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_missing_conversation_type_returns_422(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """POST /chat/enqueue without conversation_type returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "whatsapp_jid": TEST_JID,
                    "message": "Hello",
                },
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_invalid_conversation_type_returns_422(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """POST /chat/enqueue with invalid conversation_type returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "whatsapp_jid": TEST_JID,
                    "message": "Hello",
                    "conversation_type": "invalid_type",
                },
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_empty_body_returns_422(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/enqueue with empty JSON body returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={},
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /chat/enqueue — Authentication
# ---------------------------------------------------------------------------


class TestEnqueueAuth:
    """Tests for POST /chat/enqueue authentication requirements."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_missing_api_key_returns_401(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/enqueue without API key returns 401."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "whatsapp_jid": TEST_JID,
                    "message": "/help",
                    "conversation_type": "private",
                },
            )

        assert response.status_code == 401
        assert "API key" in response.json()["detail"]

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_wrong_api_key_returns_401(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/enqueue with incorrect API key returns 401."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/enqueue",
                json={
                    "whatsapp_jid": TEST_JID,
                    "message": "/help",
                    "conversation_type": "private",
                },
                headers={"X-API-Key": "wrong-key-value"},
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /chat/save
# ---------------------------------------------------------------------------


class TestSaveMessage:
    """Tests for POST /chat/save endpoint."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_save_message_success(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/save with valid payload returns success."""
        mock_db = _make_mock_db()
        mock_msg = make_conversation_message(role="user", content="Hello world")

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.create_embedding_service", return_value=None),
            patch("ai_api.routes.chat.save_message", return_value=mock_msg),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/save",
                        json={
                            "whatsapp_jid": TEST_JID,
                            "message": "Hello world",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_save_message_with_group_context(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/save with group context (sender_name) prepends name to content."""
        mock_db = _make_mock_db()
        mock_msg = make_conversation_message(
            role="user", content="Alice: Hello group", sender_name="Alice"
        )

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.create_embedding_service", return_value=None),
            patch("ai_api.routes.chat.save_message", return_value=mock_msg) as mock_save,
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/save",
                        json={
                            "whatsapp_jid": "120363001@g.us",
                            "message": "Hello group",
                            "conversation_type": "group",
                            "sender_jid": "456@s.whatsapp.net",
                            "sender_name": "Alice",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                # Verify save_message was called with content prefixed by sender_name
                call_args = mock_save.call_args
                saved_content = (
                    call_args[0][3]
                    if len(call_args[0]) > 3
                    else call_args[1].get("content", call_args[0][3])
                )
                assert "Alice" in saved_content
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_save_message_missing_fields_returns_422(
        self, mock_cleanup, mock_redis, mock_init_db
    ):
        """POST /chat/save with missing required fields returns 422."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/save",
                json={
                    "whatsapp_jid": TEST_JID,
                    # missing message and conversation_type
                },
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 422

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_save_message_requires_auth(self, mock_cleanup, mock_redis, mock_init_db):
        """POST /chat/save without API key returns 401."""
        from ai_api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/save",
                json={
                    "whatsapp_jid": TEST_JID,
                    "message": "Hello",
                    "conversation_type": "private",
                },
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /chat/job/{job_id}
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    """Tests for GET /chat/job/{job_id} endpoint."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_queued_job_returns_status(self, mock_cleanup, mock_redis_init, mock_init_db):
        """GET /chat/job/{id} for a new (queued) job returns status=queued."""
        job_id = str(uuid.uuid4())

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # no metadata
        mock_redis.lrange = AsyncMock(return_value=[])  # no chunks
        mock_redis.close = AsyncMock()

        @asynccontextmanager
        async def mock_get_redis_client():
            yield mock_redis

        with patch("ai_api.routes.chat.get_redis_client", mock_get_redis_client):
            from ai_api.main import app

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/chat/job/{job_id}",
                    headers=AUTH_HEADERS,
                )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == job_id
        assert data["status"] == "queued"
        assert data["complete"] is False
        assert data["chunks"] == []
        assert data["total_chunks"] == 0
        assert data["full_response"] is None

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_complete_job_returns_full_response(
        self, mock_cleanup, mock_redis_init, mock_init_db
    ):
        """GET /chat/job/{id} for a complete job includes full_response."""
        job_id = str(uuid.uuid4())

        import json

        chunks_data = [
            json.dumps({"index": 0, "content": "Hello ", "timestamp": "2025-01-01T00:00:00"}),
            json.dumps({"index": 1, "content": "world!", "timestamp": "2025-01-01T00:00:01"}),
        ]

        metadata = json.dumps(
            {
                "user_id": str(uuid.uuid4()),
                "whatsapp_jid": TEST_JID,
                "message": "Hi",
                "conversation_type": "private",
            }
        )

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=metadata)  # metadata exists -> complete
        mock_redis.lrange = AsyncMock(return_value=[c.encode() for c in chunks_data])
        mock_redis.close = AsyncMock()

        @asynccontextmanager
        async def mock_get_redis_client():
            yield mock_redis

        with patch("ai_api.routes.chat.get_redis_client", mock_get_redis_client):
            from ai_api.main import app

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/chat/job/{job_id}",
                    headers=AUTH_HEADERS,
                )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == job_id
        assert data["status"] == "complete"
        assert data["complete"] is True
        assert data["total_chunks"] == 2
        assert data["full_response"] == "Hello world!"
        assert len(data["chunks"]) == 2
        assert data["chunks"][0]["content"] == "Hello "
        assert data["chunks"][1]["content"] == "world!"

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_in_progress_job_returns_partial_chunks(
        self, mock_cleanup, mock_redis_init, mock_init_db
    ):
        """GET /chat/job/{id} for an in-progress job returns chunks but not complete."""
        job_id = str(uuid.uuid4())

        import json

        chunks_data = [
            json.dumps({"index": 0, "content": "Partial ", "timestamp": "2025-01-01T00:00:00"}),
        ]

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # no metadata yet
        mock_redis.lrange = AsyncMock(return_value=[c.encode() for c in chunks_data])
        mock_redis.close = AsyncMock()

        @asynccontextmanager
        async def mock_get_redis_client():
            yield mock_redis

        with patch("ai_api.routes.chat.get_redis_client", mock_get_redis_client):
            from ai_api.main import app

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/chat/job/{job_id}",
                    headers=AUTH_HEADERS,
                )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "in_progress"
        assert data["complete"] is False
        assert data["total_chunks"] == 1
        assert data["full_response"] is None

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_job_status_requires_auth(self, mock_cleanup, mock_redis_init, mock_init_db):
        """GET /chat/job/{id} without API key returns 401."""
        from ai_api.main import app

        job_id = str(uuid.uuid4())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/chat/job/{job_id}")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /chat/enqueue — Regular message queueing
# ---------------------------------------------------------------------------


class TestEnqueueRegularMessage:
    """Tests for POST /chat/enqueue with non-command messages."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_regular_message_returns_job_id(
        self, mock_cleanup, mock_redis_init, mock_init_db
    ):
        """POST /chat/enqueue with regular text returns job_id and status=queued."""
        mock_db = _make_mock_db()
        mock_user = make_user(whatsapp_jid=TEST_JID)
        mock_msg = make_conversation_message(
            role="user", content="Hello AI", user_id=str(mock_user.id)
        )

        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value=b"1234567890-0")
        mock_redis.set = AsyncMock()
        mock_redis.close = AsyncMock()

        @asynccontextmanager
        async def mock_get_redis_client():
            yield mock_redis

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.get_or_create_user", return_value=mock_user),
            patch("ai_api.routes.chat.create_embedding_service", return_value=None),
            patch("ai_api.routes.chat.save_message", return_value=mock_msg),
            patch("ai_api.routes.chat.get_redis_client", mock_get_redis_client),
            patch("ai_api.routes.chat.add_message_to_stream", new_callable=AsyncMock),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": TEST_JID,
                            "message": "Hello AI",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )

                assert response.status_code == 200
                data = response.json()
                assert "job_id" in data
                assert data["status"] == "queued"
                assert data["message"] == "Job queued successfully"
            finally:
                _cleanup_overrides()
