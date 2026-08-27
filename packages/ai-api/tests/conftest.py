"""
Shared test configuration and fixtures.

Sets environment variables and patches module-level side effects before
any production code imports.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars BEFORE any production import.
# Use a PostgreSQL-style URL to avoid SQLite pool arg incompatibility
# in database.py's module-level create_engine() call.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GEMINI_API_KEY", "test-key-placeholder")
os.environ.setdefault("AI_API_KEY", "test-api-key")
os.environ.setdefault("WHATSAPP_API_KEY", "test-wa-key")
os.environ.setdefault("REDIS_HOST", "localhost")
# Force Logfire off in tests. Hard assignment, not setdefault: a developer (or CI)
# with a real LOGFIRE_TOKEN exported must not ship telemetry from a test run.
# setup_instrumentation() returns early on a falsy token, so this is the live guard.
# (LOGFIRE_SEND_TO_LOGFIRE has no effect here — instrument.py passes
# send_to_logfire as an explicit kwarg, which short-circuits the env lookup.)
os.environ["LOGFIRE_TOKEN"] = ""

# Patch create_engine at the sqlalchemy level BEFORE database.py is imported.
# This prevents actual DB connection attempts while accepting all pool args.
_mock_engine = MagicMock()
_mock_session_factory = MagicMock()

_engine_patch = patch("sqlalchemy.create_engine", return_value=_mock_engine)
_engine_patch.start()

# Also patch the import in database.py directly (Python caches the name binding)
_db_engine_patch = patch("ai_api.database.create_engine", return_value=_mock_engine)


@pytest.fixture(scope="session", autouse=True)
def patch_google_provider():
    """Patch GoogleProvider before agent/core.py is first imported."""
    with patch("pydantic_ai.providers.google.GoogleProvider") as mock:
        mock.return_value = MagicMock()
        yield mock
