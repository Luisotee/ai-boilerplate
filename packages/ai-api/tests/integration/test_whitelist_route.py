"""GET /chat/whitelist: the effective whitelist the chat clients gate on."""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient


async def _get(headers):
    from ai_api.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/chat/whitelist", headers=headers)


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
class TestWhitelistRoute:
    async def test_returns_the_effective_value(self, *_):
        with patch("ai_api.routes.access.runtime_config") as rc:
            rc.get.return_value = "5511999999999, 120363@g.us"
            r = await _get({"X-API-Key": "test-api-key"})
        assert r.status_code == 200
        assert r.json() == {"whitelist_phones": "5511999999999, 120363@g.us"}
        rc.get.assert_called_once_with("whitelist_phones")

    async def test_disabled_whitelist_is_an_empty_string(self, *_):
        with patch("ai_api.routes.access.runtime_config") as rc:
            rc.get.return_value = None
            r = await _get({"X-API-Key": "test-api-key"})
        assert r.json() == {"whitelist_phones": ""}

    async def test_requires_the_api_key(self, *_):
        r = await _get({})
        assert r.status_code == 401
        r = await _get({"X-API-Key": "wrong"})
        assert r.status_code == 401
