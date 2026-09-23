"""
Integration tests for POST /chat/link-phone.

This route had no test of any kind, and it is the exact path that a group JID
would have travelled to reach `try_autolink` — where `db.delete()` on a group's
`User` row cascade-deletes the whole group transcript. Under
GROUP_GATING=membership `_is_whitelisted` waves every group JID through, so the
route-level guard is the first thing standing in the way.

Ported from curupira.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_user

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}
TELEGRAM_JID = "tg:42"
TELEGRAM_GROUP_JID = "tg:-1001234567890"


def _make_mock_db():
    db = MagicMock()
    for name in ("commit", "refresh", "add", "rollback", "delete", "query"):
        setattr(db, name, MagicMock())
    return db


def _app_with_db(mock_db):
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _cleanup():
    from ai_api.main import app

    app.dependency_overrides.clear()


async def _post(app, payload):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/chat/link-phone", json=payload, headers=AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _cleanup_after():
    yield
    _cleanup()


class TestLinkPhoneGroupGuard:
    async def test_group_jid_is_refused(self):
        db = _make_mock_db()
        app = _app_with_db(db)

        res = await _post(
            app,
            {
                "whatsapp_jid": TELEGRAM_GROUP_JID,
                "phone": "5511987654321",
                "contact_user_id": 42,
                "sender_user_id": 42,
            },
        )

        assert res.status_code == 200
        assert "private chat" in res.json()["response"].lower()

    async def test_group_jid_never_reaches_the_database(self):
        """The guard runs before `get_or_create_user`, so the group's row is
        never even resolved — let alone deleted."""
        db = _make_mock_db()
        app = _app_with_db(db)

        with (
            patch("ai_api.routes.chat.get_or_create_user") as mock_get_user,
            patch("ai_api.routes.chat.try_autolink") as mock_autolink,
        ):
            await _post(
                app,
                {
                    "whatsapp_jid": TELEGRAM_GROUP_JID,
                    "phone": "5511987654321",
                    "contact_user_id": 42,
                    "sender_user_id": 42,
                },
            )

        mock_get_user.assert_not_called()
        mock_autolink.assert_not_called()
        assert not db.delete.called

    async def test_whatsapp_group_jid_is_also_refused(self):
        db = _make_mock_db()
        app = _app_with_db(db)

        res = await _post(
            app,
            {
                "whatsapp_jid": "120363012345678@g.us",
                "phone": "5511987654321",
                "contact_user_id": 42,
                "sender_user_id": 42,
            },
        )

        assert "private chat" in res.json()["response"].lower()


class TestLinkPhonePrivateChat:
    async def test_private_chat_reaches_autolink(self):
        db = _make_mock_db()
        app = _app_with_db(db)
        user = make_user(TELEGRAM_JID)

        with (
            patch("ai_api.routes.chat._is_whitelisted", return_value=True),
            patch("ai_api.routes.chat.get_or_create_user", return_value=user),
            patch("ai_api.routes.chat.try_autolink") as mock_autolink,
        ):
            mock_autolink.return_value = MagicMock(message="Linked successfully.")

            res = await _post(
                app,
                {
                    "whatsapp_jid": TELEGRAM_JID,
                    "phone": "5511987654321",
                    "contact_user_id": 42,
                    "sender_user_id": 42,
                },
            )

        assert res.status_code == 200
        assert "Linked" in res.json()["response"]
        mock_autolink.assert_called_once()

    async def test_both_contact_ids_are_forwarded_verbatim(self):
        """The anti-hijack comparison lives in the service, so the route must
        pass both ids through unmodified — including a mismatched pair."""
        db = _make_mock_db()
        app = _app_with_db(db)
        user = make_user(TELEGRAM_JID)

        with (
            patch("ai_api.routes.chat._is_whitelisted", return_value=True),
            patch("ai_api.routes.chat.get_or_create_user", return_value=user),
            patch("ai_api.routes.chat.try_autolink") as mock_autolink,
        ):
            mock_autolink.return_value = MagicMock(message="nope")

            await _post(
                app,
                {
                    "whatsapp_jid": TELEGRAM_JID,
                    "phone": "5511000000000",
                    "contact_user_id": 999,
                    "sender_user_id": 42,
                },
            )

        args = mock_autolink.call_args.args
        assert args[2] == "5511000000000"
        assert args[3] == 999
        assert args[4] == 42

    async def test_non_whitelisted_private_jid_is_403(self):
        db = _make_mock_db()
        app = _app_with_db(db)

        with patch("ai_api.routes.chat._is_whitelisted", return_value=False):
            res = await _post(
                app,
                {
                    "whatsapp_jid": TELEGRAM_JID,
                    "phone": "5511987654321",
                    "contact_user_id": 42,
                    "sender_user_id": 42,
                },
            )

        assert res.status_code == 403
