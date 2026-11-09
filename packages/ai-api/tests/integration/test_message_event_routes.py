"""POST /chat/revoke and POST /chat/edit (routes/message_events.py)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}
GROUP = "120363012345678@g.us"
ROUTES = "ai_api.routes.message_events"


def _app(db):
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return app


async def _post(db, path, payload, headers=AUTH_HEADERS):
    from ai_api.main import app

    try:
        async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://test") as c:
            return await c.post(path, json=payload, headers=headers)
    finally:
        app.dependency_overrides.clear()


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
class TestRevokeRoute:
    async def test_revoke_passes_the_actor_and_admin_flag(self, *_):
        with patch(f"{ROUTES}.revoke_message", return_value=1) as revoke:
            r = await _post(
                MagicMock(),
                "/chat/revoke",
                {
                    "whatsapp_jid": GROUP,
                    "whatsapp_message_id": "M1",
                    "sender_jid": "5511@s.whatsapp.net",
                    "is_group_admin": True,
                },
            )
        assert r.status_code == 200
        assert r.json() == {"changed": 1}
        kwargs = revoke.call_args.kwargs
        assert kwargs["sender_jid"] == "5511@s.whatsapp.net"
        assert kwargs["is_group_admin"] is True

    async def test_revoke_requires_the_api_key(self, *_):
        r = await _post(
            MagicMock(), "/chat/revoke", {"whatsapp_jid": GROUP, "whatsapp_message_id": "M1"}, {}
        )
        assert r.status_code == 401

    async def test_revoke_db_error_is_generic(self, *_):
        db = MagicMock()
        with patch(f"{ROUTES}.revoke_message", side_effect=RuntimeError("db host x")):
            r = await _post(
                db, "/chat/revoke", {"whatsapp_jid": GROUP, "whatsapp_message_id": "M1"}
            )
        assert r.status_code == 500
        assert "db host" not in r.text
        db.rollback.assert_called_once()


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
class TestEditRoute:
    payload = {"whatsapp_jid": GROUP, "whatsapp_message_id": "M1", "message": "new"}

    async def test_edit_is_refused_for_a_non_whitelisted_chat(self, *_):
        with (
            patch(f"{ROUTES}._is_whitelisted", return_value=False),
            patch(f"{ROUTES}.edit_message") as edit,
        ):
            r = await _post(MagicMock(), "/chat/edit", self.payload)
        assert r.status_code == 403
        edit.assert_not_called()

    async def test_edit_reembeds_and_commits(self, *_):
        row = SimpleNamespace(content="Alice: new", embedding=[0.1], embedding_generated_at=None)
        service = MagicMock()
        service.generate = AsyncMock(return_value=[0.9, 0.8])
        db = MagicMock()
        with (
            patch(f"{ROUTES}._is_whitelisted", return_value=True),
            patch(f"{ROUTES}.edit_message", return_value=[row]),
            patch(f"{ROUTES}.create_embedding_service", return_value=service),
        ):
            r = await _post(db, "/chat/edit", self.payload)
        assert r.json() == {"changed": 1}
        service.generate.assert_awaited_once_with("Alice: new")
        assert row.embedding == [0.9, 0.8]
        assert row.embedding_generated_at is not None
        db.commit.assert_called_once()

    async def test_failed_reembed_drops_the_stale_vector(self, *_):
        row = SimpleNamespace(content="new", embedding=[0.1], embedding_generated_at="then")
        service = MagicMock()
        service.generate = AsyncMock(side_effect=RuntimeError("quota"))
        db = MagicMock()
        with (
            patch(f"{ROUTES}._is_whitelisted", return_value=True),
            patch(f"{ROUTES}.edit_message", return_value=[row]),
            patch(f"{ROUTES}.create_embedding_service", return_value=service),
        ):
            r = await _post(db, "/chat/edit", self.payload)
        assert r.status_code == 200
        assert row.embedding is None
        assert row.embedding_generated_at is None
        db.commit.assert_called_once()

    async def test_edit_with_nothing_to_change(self, *_):
        db = MagicMock()
        with (
            patch(f"{ROUTES}._is_whitelisted", return_value=True),
            patch(f"{ROUTES}.edit_message", return_value=[]),
        ):
            r = await _post(db, "/chat/edit", self.payload)
        assert r.json() == {"changed": 0}
        db.commit.assert_not_called()

    async def test_empty_text_is_rejected(self, *_):
        r = await _post(MagicMock(), "/chat/edit", {**self.payload, "message": ""})
        assert r.status_code == 422


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_chat_save_stores_the_platform_message_id(*_):
    """Without the id on the row, a later revoke or edit can't find it."""
    with (
        patch("ai_api.routes.chat._is_whitelisted", return_value=True),
        patch("ai_api.routes.chat.create_embedding_service", return_value=None),
        patch("ai_api.routes.chat.save_message") as save,
    ):
        r = await _post(
            MagicMock(),
            "/chat/save",
            {
                "whatsapp_jid": GROUP,
                "message": "hi",
                "conversation_type": "group",
                "whatsapp_message_id": "3EB0ABC",
            },
        )
    assert r.status_code == 200
    assert save.call_args.kwargs["whatsapp_message_id"] == "3EB0ABC"


def test_save_message_persists_the_id():
    from ai_api.database import save_message

    db = MagicMock()
    with patch("ai_api.database.get_or_create_user", return_value=SimpleNamespace(id="u1")):
        msg = save_message(db, GROUP, "user", "hi", "group", whatsapp_message_id="3EB0ABC")
    assert msg.whatsapp_message_id == "3EB0ABC"
