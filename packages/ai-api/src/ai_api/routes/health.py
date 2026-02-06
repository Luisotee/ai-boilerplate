from fastapi import APIRouter
from starlette.requests import Request

from ..deps import limiter

router = APIRouter()


@router.get("/health", tags=["Health"])
@limiter.exempt
async def health_check(request: Request):
    """
    Health check endpoint

    Returns the service health status.
    """
    return {"status": "healthy"}
