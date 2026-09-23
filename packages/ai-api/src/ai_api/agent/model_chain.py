"""Chat model chain: DeepSeek (primary, when DEEPSEEK_API_KEY is set) -> Gemini.

Providers are built once at import; the Model objects are rebuilt per run by
``core.build_runtime_model()`` so ``/admin`` can change model names live.

Why the DeepSeek model is wrapped in :class:`GuardedModel` instead of being handed
to ``FallbackModel`` as-is:

- pydantic-ai converts OpenAI SDK errors to ``ModelAPIError`` only around
  ``create()``. With streaming, ``create()`` returns once the response HEADERS
  arrive; the first chunk is read afterwards, unconverted. A timeout, a dropped
  connection or an SSE error event there escapes as a raw ``httpx.TransportError``
  / ``openai.APIError`` — hence the explicit :data:`FALLBACK_ON`.
- While a request waits in DeepSeek's queue it streams ``: keep-alive`` comment
  lines (for up to ~10 min), and each one resets httpx's per-read timeout. Only a
  wall-clock limit on "time to first chunk" reliably rolls a stuck DeepSeek over.
- ``FallbackModel`` discards the errors of a model it falls back from, so without
  the wrapper a revoked key or a mistyped model name would run on Gemini silently.
- Fallback is decided per model request, and one reply makes several (one per
  tool round trip). The cooldown makes an outage cost one timeout, not one per
  request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
import openai
from pydantic_ai import RunContext
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.settings import ModelSettings

from ..config import settings

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
#: After an availability failure, skip DeepSeek (straight to Gemini) for this long.
DEEPSEEK_COOLDOWN_SECONDS = 60.0

#: Errors that make FallbackModel move on to the next model. ``openai.APIError``
#: (an SSE error event as the first data) and ``httpx.TransportError`` (ReadTimeout,
#: RemoteProtocolError, ...) are what escape while reading the first chunk.
FALLBACK_ON: tuple[type[Exception], ...] = (
    ModelAPIError,
    openai.APIError,
    httpx.TransportError,
)

#: Failures of a whole model run that callers answer with a generic "try again"
#: reply instead of a crash: ModelAPIError / FallbackExceptionGroup (every model in
#: the chain failed), UnexpectedModelBehavior (output retries exhausted), and the
#: raw SDK/transport errors of a stream that drops AFTER its first chunk — too late
#: for FallbackModel, and pydantic-ai does not convert them.
MODEL_ERRORS: tuple[type[BaseException], ...] = (
    ModelAPIError,
    UnexpectedModelBehavior,
    FallbackExceptionGroup,
    openai.APIError,
    httpx.TransportError,
)

#: HTTP statuses that mean "DeepSeek is unusable for everyone right now" (bad or
#: revoked key, mistyped model name, rate limit, outage) and start the cooldown.
#: Other 4xx (400/422: oversized context, a rejected image) are request-specific —
#: one user's bad request must not push every user onto Gemini.
_COOLDOWN_STATUSES = frozenset({401, 402, 403, 404, 429})

# deepseek-flash reasons ("thinking") by default; those hidden tokens are billed as
# output and delay the reply, so disable it. Kept in the DeepSeek model's OWN
# settings (merged per model) so it is never sent to Gemini — do not move it to
# agent- or run-level model_settings.
DEEPSEEK_MODEL_SETTINGS = OpenAIChatModelSettings(extra_body={"thinking": {"type": "disabled"}})


class Cooldown:
    """Per-process "skip this provider until" marker, shared across runs."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.until = 0.0

    def active(self) -> bool:
        return time.monotonic() < self.until

    def trip(self) -> None:
        self.until = time.monotonic() + self.seconds


def _starts_cooldown(exc: Exception) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status_code >= 500 or exc.status_code in _COOLDOWN_STATUSES
    return isinstance(exc, FALLBACK_ON)


class GuardedModel(WrapperModel):
    """Wraps the primary model so every way it fails before answering falls back.

    Adds a wall-clock timeout on opening the stream (which includes waiting for the
    first chunk), logs each failure that FallbackModel would otherwise swallow, and
    skips the model entirely while its :class:`Cooldown` is active.
    """

    def __init__(
        self,
        wrapped: Model,
        *,
        first_chunk_timeout: float,
        cooldown: Cooldown,
        provider_label: str = "DeepSeek",
    ):
        super().__init__(wrapped)
        self.first_chunk_timeout = first_chunk_timeout
        self.cooldown = cooldown
        self.provider_label = provider_label

    def _skip_if_cooling_down(self) -> None:
        if self.cooldown.active():
            logger.debug("%s skipped: cooling down after a recent failure", self.provider_label)
            raise ModelAPIError(self.model_name, "skipped: cooling down after a recent failure")

    def _record_failure(self, exc: Exception) -> None:
        tripped = _starts_cooldown(exc)
        if tripped:
            self.cooldown.trip()
        logger.warning(
            "%s (%s) failed, falling back to Gemini%s: %s",
            self.provider_label,
            self.model_name,
            f" (skipping it for {self.cooldown.seconds:.0f}s)" if tripped else "",
            exc,
            exc_info=exc,
        )

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._skip_if_cooling_down()
        try:
            return await self.wrapped.request(messages, model_settings, model_request_parameters)
        except FALLBACK_ON as exc:
            self._record_failure(exc)
            raise

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        self._skip_if_cooling_down()
        async with AsyncExitStack() as stack:
            try:
                # The timeout covers only OPENING the stream, which for OpenAIChatModel
                # includes peeking the first chunk — never the rest of the reply.
                async with asyncio.timeout(self.first_chunk_timeout):
                    response = await stack.enter_async_context(
                        self.wrapped.request_stream(
                            messages, model_settings, model_request_parameters, run_context
                        )
                    )
            except TimeoutError as exc:
                error = ModelAPIError(
                    self.model_name, f"no first chunk within {self.first_chunk_timeout:g}s"
                )
                self._record_failure(error)
                raise error from exc
            except FALLBACK_ON as exc:
                self._record_failure(exc)
                raise
            yield response


google_provider = GoogleProvider(api_key=settings.gemini_api_key)

# max_retries=0: the SDK's default 2 retries would triple the wait before Gemini
# takes over. The httpx read timeout still bounds gaps between chunks once the
# reply is flowing; GuardedModel bounds the time to the first chunk.
deepseek_provider: DeepSeekProvider | None = None
if settings.deepseek_api_key:
    deepseek_provider = DeepSeekProvider(
        openai_client=openai.AsyncOpenAI(
            base_url=DEEPSEEK_BASE_URL,
            api_key=settings.deepseek_api_key,
            max_retries=0,
            timeout=httpx.Timeout(settings.deepseek_timeout_seconds, connect=5.0),
        )
    )

deepseek_cooldown = Cooldown(DEEPSEEK_COOLDOWN_SECONDS)


def build_model(deepseek_model_name: str, gemini_model_name: str) -> Model:
    """DeepSeek -> Gemini when DeepSeek is configured, else Gemini alone."""
    gemini = GoogleModel(gemini_model_name, provider=google_provider)
    if deepseek_provider is None:
        return gemini
    deepseek = GuardedModel(
        OpenAIChatModel(
            deepseek_model_name,
            provider=deepseek_provider,
            settings=DEEPSEEK_MODEL_SETTINGS,
        ),
        first_chunk_timeout=settings.deepseek_timeout_seconds,
        cooldown=deepseek_cooldown,
    )
    return FallbackModel(deepseek, gemini, fallback_on=FALLBACK_ON)
