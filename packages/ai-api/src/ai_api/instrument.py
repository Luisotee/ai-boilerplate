"""Opt-in Logfire instrumentation for LLM token/cost tracking.

Disabled entirely unless ``LOGFIRE_TOKEN`` is set — mirrors the TypeScript
``instrument.ts`` Sentry pattern used by the WhatsApp/Telegram clients.

Two things that are easy to get wrong here:

1. The token MUST be passed to ``logfire.configure()`` explicitly. Logfire reads
   ``LOGFIRE_TOKEN`` from ``os.environ`` only, and pydantic-settings loads the
   root ``.env`` into the ``Settings`` object *without* exporting to the process
   environment. Docker works either way (``env_file:`` sets real env vars), but
   ``pnpm dev:server`` / ``pnpm dev:queue`` would silently ship nothing.

2. Instrumentation must be active before the first agent *run* — not before the
   ``Agent`` is constructed. ``instrument_pydantic_ai()`` sets the
   ``Agent._instrument_default`` ClassVar, which is read per-run, so import
   order does not matter. (The worker already builds the Agent at import time,
   via ``streams.consumer`` → ``processor`` → ``agent``, before ``main()`` calls
   this.) That only holds while ``agent/core.py`` never passes ``instrument=``.

The agent runs in the *worker* process, not the API — instrumenting only the API
yields zero LLM spans.
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
    if not settings.logfire_token:
        # Nothing at all happens: no OTel providers, no atexit hook, no network.
        return

    logfire.configure(
        # See note 1 in the module docstring — .env never reaches os.environ.
        token=settings.logfire_token,
        service_name=service_name,
        environment=settings.logfire_environment,
        send_to_logfire="if-token-present",  # belt-and-braces behind the guard above
        console=False,  # stdout logging is already handled by logger.py
    )
    # include_content=False keeps token usage and operation.cost while excluding
    # prompts, completions, and tool arguments/results. Real user conversations
    # must never leave our infrastructure — see tests/unit/test_instrument.py.
    logfire.instrument_pydantic_ai(include_content=False)

    logger.info(f"Logfire enabled (service={service_name}, env={settings.logfire_environment})")
