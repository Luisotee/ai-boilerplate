"""
Integration tests for /chat/enqueue dispatching /link and /unlink commands.

Patches the Redis client manager to use fakeredis so the route's async
get_redis_client() context yields a working in-memory Redis. The user
lookup is patched to return a controllable mock User.
"""

import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_user

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}
WHATSAPP_JID = "5511999999999@s.whatsapp.net"
TELEGRAM_JID = "tg:42"


def _patch_whitelist():
    return patch("ai_api.routes.chat._is_whitelisted", return_value=True)


def _make_mock_db():
    db = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.add = MagicMock()
    db.rollback = MagicMock()
    db.delete = MagicMock()
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


@pytest.fixture
async def fake_redis():
    """Shared fakeredis instance used by the patched get_redis_client."""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


def _patch_redis_client(fake_redis):
    """Patch get_redis_client() to yield our fake_redis under `async with`."""

    @asynccontextmanager
    async def fake_ctx():
        yield fake_redis

    return patch("ai_api.routes.chat.get_redis_client", side_effect=lambda: fake_ctx())


# ---------------------------------------------------------------------------
# /link without args — code generation
# ---------------------------------------------------------------------------


class TestLinkGenerate:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_whatsapp_user_gets_telegram_instruction(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        whatsapp_user = make_user(whatsapp_jid=WHATSAPP_JID)

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=whatsapp_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": WHATSAPP_JID,
                            "message": "/link",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                data = response.json()
                assert data["is_command"] is True
                # Reply mentions "Telegram" because the requester is on WhatsApp
                assert "Telegram" in data["response"]
                # Six-digit code present
                assert re.search(r"`\d{6}`", data["response"])
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_telegram_user_gets_whatsapp_instruction(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        telegram_user = make_user(whatsapp_jid=TELEGRAM_JID)

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=telegram_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": TELEGRAM_JID,
                            "message": "/link",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                assert "WhatsApp" in response.json()["response"]
            finally:
                _cleanup_overrides()


# ---------------------------------------------------------------------------
# /link <code> — code consumption
# ---------------------------------------------------------------------------


class TestLinkConsume:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_invalid_code_returns_helpful_message(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        telegram_user = make_user(whatsapp_jid=TELEGRAM_JID)

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=telegram_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": TELEGRAM_JID,
                            "message": "/link 123456",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                assert "invalid or has expired" in response.json()["response"]
            finally:
                _cleanup_overrides()


# ---------------------------------------------------------------------------
# Group rejection
# ---------------------------------------------------------------------------


class TestLinkInGroup:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_link_in_group_is_rejected(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        group_user = make_user(whatsapp_jid="120363012345678@g.us", conversation_type="group")

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=group_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": "120363012345678@g.us",
                            "message": "/link",
                            "conversation_type": "group",
                            "is_group_admin": True,
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                assert "private chats" in response.json()["response"]
            finally:
                _cleanup_overrides()


# ---------------------------------------------------------------------------
# /unlink
# ---------------------------------------------------------------------------


class TestUnlinkRoute:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_unlink_clears_link(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        whatsapp_user = make_user(whatsapp_jid=WHATSAPP_JID, telegram_jid=TELEGRAM_JID)

        # Make `unlink` find the user via db.query(User).filter(...).first()
        mock_db.query.return_value.filter.return_value.first.return_value = whatsapp_user

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=whatsapp_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": WHATSAPP_JID,
                            "message": "/unlink",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                assert "Unlinked" in response.json()["response"]
                assert whatsapp_user.telegram_jid is None
            finally:
                _cleanup_overrides()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_unlink_when_no_active_link(
        self, mock_cleanup, mock_redis_main, mock_init_db, fake_redis
    ):
        mock_db = _make_mock_db()
        unlinked_user = make_user(whatsapp_jid=WHATSAPP_JID)

        mock_db.query.return_value.filter.return_value.first.return_value = unlinked_user

        with (
            _patch_whitelist(),
            _patch_redis_client(fake_redis),
            patch("ai_api.routes.chat.get_or_create_user", return_value=unlinked_user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": WHATSAPP_JID,
                            "message": "/unlink",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                assert "No active link" in response.json()["response"]
            finally:
                _cleanup_overrides()


# ---------------------------------------------------------------------------
# /help text mentions /link
# ---------------------------------------------------------------------------


class TestHelpMentionsLink:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_help_includes_link_unlink(self, mock_cleanup, mock_redis_main, mock_init_db):
        mock_db = _make_mock_db()
        user = make_user(whatsapp_jid=WHATSAPP_JID)

        with (
            _patch_whitelist(),
            patch("ai_api.routes.chat.get_or_create_user", return_value=user),
        ):
            app = _get_app_with_db_override(mock_db)
            try:
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post(
                        "/chat/enqueue",
                        json={
                            "whatsapp_jid": WHATSAPP_JID,
                            "message": "/help",
                            "conversation_type": "private",
                        },
                        headers=AUTH_HEADERS,
                    )
                assert response.status_code == 200
                body = response.json()["response"]
                assert "/link" in body
                assert "/unlink" in body
            finally:
                _cleanup_overrides()
