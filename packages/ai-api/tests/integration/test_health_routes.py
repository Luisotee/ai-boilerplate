"""
Integration tests for the /health endpoint.

Tests cover:
- GET /health returns 200 with JSON status
- No API key authentication required for health endpoint
- Health endpoint works even with invalid/missing API key
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_health_returns_200(mock_cleanup, mock_redis, mock_init_db):
    """GET /health returns 200 with a JSON body containing status."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "healthy"}


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_health_no_auth_required(mock_cleanup, mock_redis, mock_init_db):
    """Health endpoint does not require X-API-Key header."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Explicitly send NO auth header
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_health_ignores_invalid_api_key(mock_cleanup, mock_redis, mock_init_db):
    """Health endpoint succeeds even with a wrong API key."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/health",
            headers={"X-API-Key": "totally-wrong-key"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_health_response_content_type(mock_cleanup, mock_redis, mock_init_db):
    """Health endpoint returns application/json content type."""
    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
