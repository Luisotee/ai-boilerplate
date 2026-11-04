"""Integration tests for the public /contact endpoint.

Tests cover:
- GET /contact returns 200 without API key (unauthenticated)
- GET /contact returns 200 even with an invalid API key (auth exempt)
- Sibling routes like /contact-admin are NOT exempt from API key auth
- Successful contact retrieval with correct formatting for WhatsApp and Telegram
- Baileys prioritized over Cloud API for WhatsApp
- Fallback to Cloud API when Baileys has no number / fails
- Downstream failures return 200 with null channel (never 500)
- In-process ~60s cache avoids re-fetching on subsequent requests
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ai_api.routes.contact import _clear_contact_cache_for_tests


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
            mock_wa.return_value = None
            mock_tg.return_value = None
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
            mock_wa.return_value = None
            mock_tg.return_value = None
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
            mock_wa.return_value = WhatsAppContact(
                number="5561999999999", url="https://wa.me/5561999999999"
            )
            mock_tg.return_value = TelegramContact(
                username="curupira_bot", url="https://t.me/curupira_bot"
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
async def test_contact_caching(mock_cleanup, mock_redis, mock_init_db):
    """Second call within 60s uses in-process cache and does not call fetchers again."""
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
            mock_wa.return_value = WhatsAppContact(
                number="5561999999999", url="https://wa.me/5561999999999"
            )
            mock_tg.return_value = TelegramContact(
                username="curupira_bot", url="https://t.me/curupira_bot"
            )

            res1 = await client.get("/contact")
            res2 = await client.get("/contact")

    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json() == res2.json()
    assert mock_wa.call_count == 1
    assert mock_tg.call_count == 1


async def test_fetch_whatsapp_contact_baileys_priority():
    """Baileys paired number is preferred when Baileys is active."""
    from ai_api.routes.contact import _fetch_whatsapp_contact

    mock_client = AsyncMock()
    mock_client.get_client_status.return_value = {
        "status": "connected",
        "phoneNumber": "55 61 99999-9999",
    }

    with patch("ai_api.routes.contact.create_whatsapp_client", return_value=mock_client):
        contact = await _fetch_whatsapp_contact()

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
        contact = await _fetch_whatsapp_contact()

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
        contact = await _fetch_telegram_contact()

    assert contact is not None
    assert contact.username == "curupira_bot"
    assert contact.url == "https://t.me/curupira_bot"


async def test_fetchers_resilient_to_downstream_errors():
    """Downstream network or HTTP errors return None instead of raising."""
    from ai_api.routes.contact import _fetch_telegram_contact, _fetch_whatsapp_contact

    mock_client = AsyncMock()
    mock_client.get_client_status.side_effect = ConnectionError("Connection refused")

    with patch("ai_api.routes.contact.create_whatsapp_client", return_value=mock_client):
        wa_contact = await _fetch_whatsapp_contact()
        tg_contact = await _fetch_telegram_contact()

    assert wa_contact is None
    assert tg_contact is None
