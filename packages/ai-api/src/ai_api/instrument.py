"""Opt-in Logfire instrumentation for LLM token/cost tracking.

No-op unless ``LOGFIRE_TOKEN`` is set — mirrors the TypeScript ``instrument.ts``
Sentry pattern used by the WhatsApp/Telegram clients.

Must be imported and invoked FIRST in both entrypoints (``main.py`` and
``scripts/run_stream_worker.py``), before ``agent/core.py`` constructs the
module-level ``Agent``.

The agent runs in the *worker* process, not the API — instrumenting only the
API yields zero LLM spans.
"""

import logfire

from .config import settings
from .logger import logger


def setup_instrumentation(service_name: str) -> None:
    """Configure Logfire and instrument Pydantic AI.

    Args:
        service_name: Distinguishes the two processes in the Logfire UI
            ("ai-api" vs "ai-api-worker").
    """
    logfire.configure(
        service_name=service_name,
        environment=settings.logfire_environment,
        # Ships data only when a write token is present; otherwise silently
        # disabled with no error and no network calls, so tests and local
        # development stay free of side effects.
        send_to_logfire="if-token-present",
        console=False,  # stdout logging is already handled by logger.py
    )
    # include_content=False keeps token usage and operation.cost while excluding
    # prompts, completions, and tool arguments/results. Real user conversations
    # must never leave our infrastructure — see tests/unit/test_instrument.py.
    logfire.instrument_pydantic_ai(include_content=False)

    if settings.logfire_token:
        logger.info(f"Logfire enabled (service={service_name}, env={settings.logfire_environment})")
