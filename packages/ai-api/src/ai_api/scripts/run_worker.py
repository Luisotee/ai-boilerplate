#!/usr/bin/env python3
"""
arq worker entry point.

This script starts the arq worker process that consumes jobs
from the Redis queue and processes them asynchronously.

Usage:
    # Development
    python -m ai_api.scripts.run_worker

    # Production (from package root)
    uv run python -m ai_api.scripts.run_worker

The worker will:
1. Connect to Redis using settings from environment variables
2. Listen for jobs on the queue
3. Process chat jobs by streaming from Pydantic AI agent
4. Save results to PostgreSQL and Redis
5. Gracefully handle shutdown signals (SIGINT, SIGTERM)
"""

from arq.worker import run_worker
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from ..logger import logger
from ..queue.worker import WorkerSettings


def main():
    """
    Main entry point for the worker.

    Starts the arq worker with our custom WorkerSettings configuration.
    """
    logger.info("=" * 80)
    logger.info("🚀 Starting arq worker for AI API")
    logger.info("=" * 80)
    logger.info(f"Redis: {WorkerSettings.redis_settings.host}:{WorkerSettings.redis_settings.port}")
    logger.info(f"Database: {WorkerSettings.redis_settings.database}")
    logger.info(f"Max concurrent jobs: {WorkerSettings.max_jobs}")
    logger.info(f"Job timeout: {WorkerSettings.job_timeout}s")
    logger.info(f"Poll delay: {WorkerSettings.poll_delay}s")
    logger.info(f"Keep results: {WorkerSettings.keep_result}s")
    logger.info("=" * 80)

    # Run the worker (blocks until shutdown signal)
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
