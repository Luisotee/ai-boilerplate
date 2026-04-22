import asyncio

from fastapi import APIRouter, Response
from sqlalchemy import text
from starlette.requests import Request

from ..database import engine
from ..deps import limiter
from ..logger import logger
from ..queue.connection import get_arq_redis

router = APIRouter()


@router.get("/health", tags=["Health"])
@limiter.exempt
async def health_check(request: Request):
    """
    Liveness probe.

    Returns 200 as long as the HTTP server is reachable. Does not probe
    any downstream dependencies — use /health/ready for that.
    """
    return {"status": "healthy"}


def _probe_db_sync() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@router.get("/health/ready", tags=["Health"])
@limiter.exempt
async def readiness_check(request: Request, response: Response):
    """
    Readiness probe.

    Probes PostgreSQL and Redis. Returns 200 when both respond, 503 when
    any check fails. Intended for uptime monitors (e.g. Uptime Kuma) and
    orchestrators (e.g. Kubernetes readiness probes).
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        await asyncio.to_thread(_probe_db_sync)
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning(f"Readiness check: database probe failed: {exc}")
        checks["database"] = f"fail: {type(exc).__name__}"
        healthy = False

    try:
        redis = await get_arq_redis()
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning(f"Readiness check: redis probe failed: {exc}")
        checks["redis"] = f"fail: {type(exc).__name__}"
        healthy = False

    if not healthy:
        response.status_code = 503

    return {"status": "ready" if healthy else "not_ready", "checks": checks}
