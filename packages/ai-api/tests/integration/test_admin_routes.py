"""Integration tests for /admin/* endpoints (management dashboard contract).

Covers prompt GET/PUT/DELETE, settings GET/PATCH/DELETE (hot vs restart-only),
the read-only conversation viewer, and auth enforcement.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_conversation_message, make_user

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

# Patches that keep the app lifespan from touching real infra.
LIFESPAN_PATCHES = (
    patch("ai_api.main.init_db"),
    patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
    patch("ai_api.main.cleanup_expired_documents"),
)


def _make_mock_db():
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
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


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestPrompt:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_prompt_default_when_no_override(self, *_):
        from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT

        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.get_bot_prompt_row", return_value=None):
                async with _client(app) as client:
                    resp = await client.get("/admin/prompt", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_overridden"] is False
            assert data["content"] == DEFAULT_SYSTEM_PROMPT
            assert data["default_length"] == len(DEFAULT_SYSTEM_PROMPT)
            assert data["updated_at"] is None
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_prompt_returns_override(self, *_):
        app = _app_with_db(_make_mock_db())
        row = MagicMock(content="CUSTOM", updated_at=datetime(2026, 5, 25, tzinfo=UTC))
        try:
            with patch("ai_api.routes.admin.get_bot_prompt_row", return_value=row):
                async with _client(app) as client:
                    resp = await client.get("/admin/prompt", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_overridden"] is True
            assert data["content"] == "CUSTOM"
            assert data["updated_at"] is not None
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_put_prompt_sets_override(self, *_):
        app = _app_with_db(_make_mock_db())
        row = MagicMock(content="NEW PROMPT", updated_at=datetime(2026, 5, 25, tzinfo=UTC))
        try:
            with (
                patch("ai_api.routes.admin.set_active_prompt") as mock_set,
                patch("ai_api.routes.admin.get_bot_prompt_row", return_value=row),
            ):
                async with _client(app) as client:
                    resp = await client.put(
                        "/admin/prompt", json={"content": "NEW PROMPT"}, headers=AUTH_HEADERS
                    )
            assert resp.status_code == 200
            assert resp.json()["content"] == "NEW PROMPT"
            mock_set.assert_called_once()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_put_blank_prompt_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.put(
                    "/admin/prompt", json={"content": "   "}, headers=AUTH_HEADERS
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_prompt_reverts_to_default(self, *_):
        from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT

        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.clear_active_prompt") as mock_clear,
                patch("ai_api.routes.admin.get_bot_prompt_row", return_value=None),
            ):
                async with _client(app) as client:
                    resp = await client.delete("/admin/prompt", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            assert resp.json()["is_overridden"] is False
            assert resp.json()["content"] == DEFAULT_SYSTEM_PROMPT
            mock_clear.assert_called_once()
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _find(settings_list, key):
    return next(item for item in settings_list if item["key"] == key)


class TestSettings:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_settings_shape(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.get_setting_overrides", return_value={}):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            items = resp.json()["settings"]
            wl = _find(items, "whitelist_phones")
            assert wl["hot"] is True
            assert wl["source"] == "default"
            db_url = _find(items, "database_url")
            assert db_url["hot"] is False
            assert db_url["secret"] is True
            # secret value is masked, never the real connection string
            assert db_url["value"] in ("********", None)
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_settings_reflects_override(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch(
                "ai_api.routes.admin.get_setting_overrides",
                return_value={"tts_default_voice": '"Puck"'},
            ):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            voice = _find(resp.json()["settings"], "tts_default_voice")
            assert voice["value"] == "Puck"
            assert voice["source"] == "override"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_hot_setting(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_override") as mock_set,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"tts_default_voice": '"Puck"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"tts_default_voice": "Puck"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            mock_set.assert_called_once()
            # set_setting_override(db, key, json_value)
            assert mock_set.call_args.args[1:] == ("tts_default_voice", '"Puck"')
            assert _find(resp.json()["settings"], "tts_default_voice")["value"] == "Puck"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_restart_only_setting_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"database_url": "postgresql://x"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "restart" in resp.json()["detail"].lower()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_unknown_setting_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"not_a_setting": 1}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "Unknown setting" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_wrong_type_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"history_limit_private": "abc"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_bad_choice_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"pdf_parser": "bogus"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_empty_overrides_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings", json={"overrides": {}}, headers=AUTH_HEADERS
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_setting_override(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.delete_setting_override") as mock_del,
                patch("ai_api.routes.admin.get_setting_overrides", return_value={}),
            ):
                async with _client(app) as client:
                    resp = await client.delete(
                        "/admin/settings/tts_default_voice", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 200
            mock_del.assert_called_once()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_unknown_setting_404(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.delete("/admin/settings/not_a_setting", headers=AUTH_HEADERS)
            assert resp.status_code == 404
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Conversation viewer
# ---------------------------------------------------------------------------


class TestConversationViewer:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_list_users(self, *_):
        mock_db = _make_mock_db()
        user = make_user("5511999999999@s.whatsapp.net", name="Alice")
        last = datetime(2026, 5, 25, tzinfo=UTC)
        mock_db.query.return_value.count.return_value = 1
        chain = mock_db.query.return_value.outerjoin.return_value.group_by.return_value
        chain.order_by.return_value.limit.return_value.offset.return_value.all.return_value = [
            (user, 5, last)
        ]
        app = _app_with_db(mock_db)
        try:
            async with _client(app) as client:
                resp = await client.get("/admin/users", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["users"][0]["name"] == "Alice"
            assert data["users"][0]["message_count"] == 5
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_user_messages(self, *_):
        mock_db = _make_mock_db()
        user = make_user("5511999999999@s.whatsapp.net")
        msgs = [
            make_conversation_message("user", "hi"),
            make_conversation_message("assistant", "hello"),
        ]
        filtered = mock_db.query.return_value.filter.return_value
        filtered.count.return_value = 2
        filtered.order_by.return_value.limit.return_value.offset.return_value.all.return_value = (
            msgs
        )
        app = _app_with_db(mock_db)
        try:
            with patch("ai_api.routes.admin._resolve_user", return_value=user):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/users/5511999999999@s.whatsapp.net/messages",
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["messages"]) == 2
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_user_messages_404(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin._resolve_user", return_value=None):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/users/unknown@s.whatsapp.net/messages", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 404
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_overview(self, *_):
        mock_db = _make_mock_db()
        mock_db.query.return_value.count.side_effect = [3, 10, 2]
        app = _app_with_db(mock_db)
        try:
            async with _client(app) as client:
                resp = await client.get("/admin/overview", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data == {"users": 3, "messages": 10, "knowledge_base_documents": 2}
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_admin_requires_auth(*_):
    from ai_api.main import app

    async with _client(app) as client:
        resp = await client.get("/admin/prompt")
    assert resp.status_code == 401
