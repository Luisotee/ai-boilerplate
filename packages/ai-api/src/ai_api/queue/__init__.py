"""
Redis job-state package.

Message and PDF processing live in `streams/` (Redis Streams). What remains here
is the shared Redis plumbing that outlived the retired arq worker (the `arq`
dependency itself is gone):

- Redis connection management (`connection.py`) — the process-wide client used
  by the API lifespan and readiness probe, plus the standalone client used for
  direct key operations
- Job status tracking and response-chunk storage (`utils.py`)
- Job/response Pydantic schemas (`schemas.py`)
"""

from .connection import create_arq_pool, get_arq_redis
from .utils import get_job_chunks, get_job_metadata, save_job_chunk, set_job_metadata

__all__ = [
    "get_arq_redis",
    "create_arq_pool",
    "save_job_chunk",
    "get_job_chunks",
    "set_job_metadata",
    "get_job_metadata",
]
