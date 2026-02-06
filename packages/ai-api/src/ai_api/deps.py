"""Shared application-level dependencies used across route modules."""

from pathlib import Path

from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import settings
from .logger import logger

# Rate limiter (Redis-backed)
_redis_password_part = f":{settings.redis_password}@" if settings.redis_password else ""
_rate_limit_storage = (
    f"redis://{_redis_password_part}{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
)
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_rate_limit_storage,
    default_limits=[f"{settings.rate_limit_global}/minute"],
)

# Upload directory for knowledge base PDFs
UPLOAD_DIR = Path(settings.kb_upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"Knowledge base upload directory: {UPLOAD_DIR}")
