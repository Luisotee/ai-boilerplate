"""Integration tests for the public /contact endpoint.

Tests cover:
- GET /contact returns 200 without API key (unauthenticated)
- GET /contact returns 200 even with an invalid API key (auth exempt)
- Sibling routes like /contact-extra are NOT exempt from API key auth
- Successful contact retrieval with correct formatting for WhatsApp and Telegram
- Baileys prioritized over Cloud API for WhatsApp
- Fallback to Cloud API when Baileys has no number / fails
- Downstream failures return 200 with null channel (never 500)
- In-process ~60s cache avoids re-fetching on subsequent requests
- Negative caching: short TTL (10s) on client failure vs long TTL (60s) on success
- Unconfigured client (empty URL) is skipped without HTTP calls and does not shorten TTL
- OpenAPI schema includes Contact tag metadata
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import ai_api.routes.contact as contact_module
from ai_api.routes.contact import (
    _CACHE_TTL_SECONDS,
    _SHORT_CACHE_TTL_SECONDS,
    _clear_contact_cache_for_tests,
)


@pytest.fixture(autouse=True)
def reset_contact_cache():
    """Reset the module-level contact cache before and after each test."""
    _clear_contact_cache_for_tests()
    yield
    _clear_contact_cache_for_tests()


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_no_auth_required(mock_cleanup, mock_redis, mock_init_db):
    """GET /contact succeeds without X-API-Key header."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact._fetch_whatsapp_contact",
                new_callable=AsyncMock,
            ) as mock_wa,
            patch(
                "ai_api.routes.contact._fetch_telegram_contact",
                new_callable=AsyncMock,
            ) as mock_tg,
        ):
            mock_wa.return_value = (None, False)
            mock_tg.return_value = (None, False)
            response = await client.get("/contact")

    assert response.status_code == 200
    assert response.json() == {"whatsapp": None, "telegram": None}


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_ignores_invalid_api_key(mock_cleanup, mock_redis, mock_init_db):
    """GET /contact succeeds even when an invalid X-API-Key header is supplied."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact._fetch_whatsapp_contact",
                new_callable=AsyncMock,
            ) as mock_wa,
            patch(
                "ai_api.routes.contact._fetch_telegram_contact",
                new_callable=AsyncMock,
            ) as mock_tg,
        ):
            mock_wa.return_value = (None, False)
            mock_tg.return_value = (None, False)
            response = await client.get("/contact", headers={"X-API-Key": "wrong-key"})

    assert response.status_code == 200


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_prefix_leak_prevented(mock_cleanup, mock_redis, mock_init_db):
    """A route like /contact-extra is NOT exempt from API key auth."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/contact-extra")

    # 401 Unauthorized because only exact /contact (or /contact/) is exempt
    assert response.status_code == 401


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_returns_both_channels(mock_cleanup, mock_redis, mock_init_db):
    """GET /contact returns formatted WhatsApp and Telegram handles."""
    from ai_api.main import app
    from ai_api.schemas import TelegramContact, WhatsAppContact

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact._fetch_whatsapp_contact",
                new_callable=AsyncMock,
            ) as mock_wa,
            patch(
                "ai_api.routes.contact._fetch_telegram_contact",
                new_callable=AsyncMock,
            ) as mock_tg,
        ):
            mock_wa.return_value = (
                WhatsAppContact(number="5561999999999", url="https://wa.me/5561999999999"),
                False,
            )
            mock_tg.return_value = (
                TelegramContact(username="curupira_bot", url="https://t.me/curupira_bot"),
                False,
            )
            response = await client.get("/contact")

    assert response.status_code == 200
    assert response.json() == {
        "whatsapp": {"number": "5561999999999", "url": "https://wa.me/5561999999999"},
        "telegram": {"username": "curupira_bot", "url": "https://t.me/curupira_bot"},
    }


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_caching_full_ttl(mock_cleanup, mock_redis, mock_init_db):
    """When all clients answer successfully, result is cached for the full ~60s."""
    from ai_api.main import app
    from ai_api.schemas import TelegramContact, WhatsAppContact

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact._fetch_whatsapp_contact",
                new_callable=AsyncMock,
            ) as mock_wa,
            patch(
                "ai_api.routes.contact._fetch_telegram_contact",
                new_callable=AsyncMock,
            ) as mock_tg,
        ):
            mock_wa.return_value = (
                WhatsAppContact(number="5561999999999", url="https://wa.me/5561999999999"),
                False,
            )
            mock_tg.return_value = (
                TelegramContact(username="curupira_bot", url="https://t.me/curupira_bot"),
                False,
            )

            res1 = await client.get("/contact")
            res2 = await client.get("/contact")

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json() == res2.json()
    assert mock_wa.call_count == 1
    assert mock_tg.call_count == 1
    assert contact_module._cached_ttl == _CACHE_TTL_SECONDS


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_contact_caching_short_ttl_on_failure(mock_cleanup, mock_redis, mock_init_db):
    """When a client fails, response is negatively cached for short TTL (~10s)."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact._fetch_whatsapp_contact",
                new_callable=AsyncMock,
            ) as mock_wa,
            patch(
                "ai_api.routes.contact._fetch_telegram_contact",
                new_callable=AsyncMock,
            ) as mock_tg,
        ):
            mock_wa.return_value = (None, True)  # WhatsApp failed
            mock_tg.return_value = (None, False)

            res1 = await client.get("/contact")
            assert res1.status_code == 200
            assert contact_module._cached_ttl == _SHORT_CACHE_TTL_SECONDS

            # Within short TTL (e.g. 5s later) -> uses cache
            contact_module._cached_at -= 5.0
            res2 = await client.get("/contact")
            assert res2.status_code == 200
            assert mock_wa.call_count == 1

            # Past short TTL (e.g. 11s later) -> re-queries fetchers
            contact_module._cached_at -= 6.0  # total 11s
            res3 = await client.get("/contact")
            assert res3.status_code == 200
            assert mock_wa.call_count == 2


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
@patch("ai_api.config.settings.telegram_client_url", None)
async def test_contact_unconfigured_client_gets_full_ttl(mock_cleanup, mock_redis, mock_init_db):
    """An unconfigured client (e.g. telegram_client_url is None) is skipped with no HTTP call and keeps full TTL."""
    from ai_api.main import app

    mock_client = AsyncMock()
    mock_client.get_client_status.return_value = {
        "status": "connected",
        "phoneNumber": "55 61 99999-9999",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch(
                "ai_api.routes.contact.create_whatsapp_client",
                return_value=mock_client,
            ),
            patch(
                "ai_api.routes.contact._fetch_client_status",
                wraps=contact_module._fetch_client_status,
            ) as spy_fetch_status,
        ):
            res1 = await client.get("/contact")
            assert res1.status_code == 200
            data = res1.json()
            assert data["whatsapp"] == {
                "number": "5561999999999",
                "url": "https://wa.me/5561999999999",
            }
            assert data["telegram"] is None

            # Verify Telegram was never queried via HTTP client status
            called_client_ids = [call.args[0] for call in spy_fetch_status.call_args_list]
            assert "telegram" not in called_client_ids

            # Verify unconfigured Telegram still gets long TTL (60s)
            assert contact_module._cached_ttl == _CACHE_TTL_SECONDS

            # Advancing time by 15s (past short 10s TTL, within 60s TTL)
            contact_module._cached_at -= 15.0
            res2 = await client.get("/contact")
            assert res2.status_code == 200
            assert mock_client.get_client_status.call_count == 1  # Still cached


async def test_fetch_whatsapp_contact_baileys_priority():
    """Baileys paired number is preferred when Baileys is active."""
    from ai_api.routes.contact import _fetch_whatsapp_contact

    mock_client = AsyncMock()
    mock_client.get_client_status.return_value = {
        "status": "connected",
        "phoneNumber": "55 61 99999-9999",
    }

    with patch("ai_api.routes.contact.create_whatsapp_client", return_value=mock_client):
        contact, failed = await _fetch_whatsapp_contact()

    assert not failed
    assert contact is not None
    assert contact.number == "5561999999999"
    assert contact.url == "https://wa.me/5561999999999"


async def test_fetch_whatsapp_contact_cloud_fallback():
    """Cloud API is queried when Baileys has no paired number."""
    from ai_api.routes.contact import _fetch_whatsapp_contact

    baileys_client = AsyncMock()
    baileys_client.get_client_status.return_value = {
        "status": "disconnected",
        "phoneNumber": None,
    }

    cloud_client = AsyncMock()
    cloud_client.get_client_status.return_value = {
        "status": "connected",
        "phoneNumber": "+55 11 98888-8888",
    }

    def client_factory(http_client, base_url, api_key):
        if "3002" in base_url or "cloud" in base_url:
            return cloud_client
        return baileys_client

    with patch("ai_api.routes.contact.create_whatsapp_client", side_effect=client_factory):
        contact, failed = await _fetch_whatsapp_contact()

    assert not failed
    assert contact is not None
    assert contact.number == "5511988888888"
    assert contact.url == "https://wa.me/5511988888888"


async def test_fetch_telegram_contact_cleans_username():
    """Telegram fetcher strips '@' and extra whitespace."""
    from ai_api.routes.contact import _fetch_telegram_contact

    mock_client = AsyncMock()
    mock_client.get_client_status.return_value = {
        "status": "connected",
        "username": " @curupira_bot ",
    }

    with patch("ai_api.routes.contact.create_whatsapp_client", return_value=mock_client):
        contact, failed = await _fetch_telegram_contact()

    assert not failed
    assert contact is not None
    assert contact.username == "curupira_bot"
    assert contact.url == "https://t.me/curupira_bot"


async def test_fetchers_resilient_to_downstream_errors():
    """Downstream network or HTTP errors return (None, True) instead of raising."""
    from ai_api.routes.contact import _fetch_telegram_contact, _fetch_whatsapp_contact

    mock_client = AsyncMock()
    mock_client.get_client_status.side_effect = ConnectionError("Connection refused")

    with patch("ai_api.routes.contact.create_whatsapp_client", return_value=mock_client):
        wa_contact, wa_failed = await _fetch_whatsapp_contact()
        tg_contact, tg_failed = await _fetch_telegram_contact()

    assert wa_contact is None
    assert wa_failed is True
    assert tg_contact is None
    assert tg_failed is True


def test_openapi_contact_tag():
    """OpenAPI schema includes Contact tag metadata."""
    from ai_api.main import app

    schema = app.openapi()
    tags = {t["name"]: t.get("description") for t in schema.get("tags", [])}
    assert "Contact" in tags
    assert "Public bot contact handles" in tags["Contact"]
