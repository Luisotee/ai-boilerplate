"""
Integration tests for the /health and /health/ready endpoints.

Tests cover:
- GET /health is an always-200 liveness probe, no auth required
- GET /health/ready probes Postgres + Redis and returns 503 when either
  dependency fails (for Uptime Kuma / Kubernetes readiness probes)
"""

from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# /health/ready — readiness probe (DB + Redis)
# ---------------------------------------------------------------------------


def _make_healthy_redis() -> AsyncMock:
    """Return a mock arq Redis pool whose ping() succeeds."""
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    return redis


@patch("ai_api.main.init_db")
@patch("ai_api.main.cleanup_expired_documents")
@patch("ai_api.routes.health._probe_db_sync")
@patch("ai_api.routes.health.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
async def test_ready_returns_200_when_all_deps_ok(
    mock_main_redis, mock_route_redis, mock_probe_db, mock_cleanup, mock_init_db
):
    """/health/ready returns 200 with status=ready when DB and Redis succeed."""
    mock_probe_db.return_value = None  # Success: no exception
    mock_route_redis.return_value = _make_healthy_redis()

    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["checks"] == {"database": "ok", "redis": "ok"}


@patch("ai_api.main.init_db")
@patch("ai_api.main.cleanup_expired_documents")
@patch("ai_api.routes.health._probe_db_sync")
@patch("ai_api.routes.health.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
async def test_ready_returns_503_when_db_fails(
    mock_main_redis, mock_route_redis, mock_probe_db, mock_cleanup, mock_init_db
):
    """/health/ready returns 503 when the DB probe raises."""
    mock_probe_db.side_effect = RuntimeError("connection refused")
    mock_route_redis.return_value = _make_healthy_redis()

    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["checks"]["database"].startswith("fail:")
    assert data["checks"]["redis"] == "ok"


@patch("ai_api.main.init_db")
@patch("ai_api.main.cleanup_expired_documents")
@patch("ai_api.routes.health._probe_db_sync")
@patch("ai_api.routes.health.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
async def test_ready_returns_503_when_redis_fails(
    mock_main_redis, mock_route_redis, mock_probe_db, mock_cleanup, mock_init_db
):
    """/health/ready returns 503 when Redis ping raises."""
    mock_probe_db.return_value = None
    failing_redis = MagicMock()
    failing_redis.ping = AsyncMock(side_effect=ConnectionError("redis down"))
    mock_route_redis.return_value = failing_redis

    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["checks"]["database"] == "ok"
    assert data["checks"]["redis"].startswith("fail:")


@patch("ai_api.main.init_db")
@patch("ai_api.main.cleanup_expired_documents")
@patch("ai_api.routes.health._probe_db_sync")
@patch("ai_api.routes.health.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
async def test_ready_no_auth_required(
    mock_main_redis, mock_route_redis, mock_probe_db, mock_cleanup, mock_init_db
):
    """/health/ready is exempt from API key auth (prefix match on /health)."""
    mock_probe_db.return_value = None
    mock_route_redis.return_value = _make_healthy_redis()

    from ai_api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")  # no X-API-Key header

    assert response.status_code == 200
