"""OpenAI-compatible chat API (``/v1``) for web, desktop and phone apps.

Open WebUI, LibreChat, Chatbox and similar clients talk to the bot through the
same pipeline as WhatsApp: ``POST /v1/chat/completions`` saves the user's turn,
queues it on their Redis stream, waits for the stream worker's reply and returns
it as a ``chat.completion`` (or SSE ``chat.completion.chunk`` events with
``stream: true``). ``GET /v1/models`` lists one model.

- **Auth**: ``Authorization: Bearer sk-…``, a per-user key from
  ``services/api_keys.py`` (issued with ``python -m ai_api.scripts.api_keys``).
  ``APIKeyMiddleware`` lets ``/v1/`` through to this router's own dependency;
  ``AI_API_KEY`` is never accepted here. ``WHITELIST_PHONES`` doesn't apply: an
  operator-issued key is the grant.
- **One message per request**: only the last message is used and it must be a
  user message. The server keeps the history (shared with WhatsApp/Telegram once
  the key's user is linked with ``/link``), so a client resending the whole
  transcript is harmless, and its system prompt is ignored. *Regenerate* or
  *edit and resend* does resend the last user turn, which is saved again. Client background
  chores (Open WebUI's ``### Task:`` titles/tags/follow-ups, LibreChat's
  ``system``-role title request) are refused, never queued.
- **Rendering**: the job runs with ``client_id="web"``, so the reply keeps its
  Markdown (``surfaces.py``) and WhatsApp-only tools are hidden.

Schemas live here, not in ``schemas.py``, which is FleetView's contract file.
Details and client setup: ``packages/ai-api/docs/openai-api.md``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from limits import RateLimitItem, parse
from limits.strategies import RateLimiter
from pydantic import BaseModel, ConfigDict, Field
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..commands import is_command, normalize_command, parse_and_execute
from ..config import settings
from ..database import User, get_db
from ..deps import limiter
from ..logger import logger
from ..queue.connection import get_redis_client
from ..queue.utils import get_job_chunks
from ..services.api_keys import authenticate
from ..services.chat_jobs import enqueue_text_job
from ..services.link import link_command_reply
from ..surfaces import WEB_CLIENT_ID
from .chat import get_stream_job_status

#: Same cadence and cap as the TS clients' pollers.
POLL_INTERVAL_SECONDS = 0.5
JOB_TIMEOUT_SECONDS = 120.0
#: SSE comment sent while the worker runs, so proxies don't drop an idle stream.
PING_INTERVAL_SECONDS = 15.0
MAX_MESSAGE_CHARS = 32_000
#: Open WebUI's background chores (title, tags, follow-ups, autocomplete, …)
#: all start with this header; see its *_PROMPT_TEMPLATE defaults.
_CHORE_PREFIX = "### Task:"
_UNSUPPORTED_PARTS = {"image_url", "input_image", "image", "file", "input_audio", "audio"}


class OpenAIError(Exception):
    """An error rendered in OpenAI's shape by the handler in ``main.py``."""

    def __init__(self, status: int, message: str, type_: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.type, self.code = status, message, type_, code


def openai_error_response(exc: OpenAIError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
        headers=headers,
    )


#: Failed key checks allowed per client IP. The route limits below are per key and
#: run after auth, and a route with its own limit skips the middleware's per-IP
#: default, so without this a flood of bad keys would be unthrottled (each one a
#: DB lookup). Only failures count: one Open WebUI server proxies all its users
#: from one IP.
AUTH_FAILURE_LIMIT: RateLimitItem = parse(f"{settings.rate_limit_global}/minute")


def _auth_limiter() -> RateLimiter | None:
    """slowapi's backend (same Redis storage), or None when limiting is off."""
    return limiter.limiter if limiter.enabled else None


def _auth_throttled(ip: str, *, hit: bool) -> bool:
    """Test (or record) a failed key check for ``ip``. Fails open on storage errors."""
    backend = _auth_limiter()
    if backend is None:
        return False
    try:
        if hit:
            backend.hit(AUTH_FAILURE_LIMIT, "v1-auth-fail", ip)
            return False
        return not backend.test(AUTH_FAILURE_LIMIT, "v1-auth-fail", ip)
    except Exception:
        logger.warning("/v1 auth throttle unavailable", exc_info=True)
        return False


def require_app_key(request: Request, db: Session = Depends(get_db)) -> User:
    """Router-wide dependency: a valid, unrevoked per-user key, or 401 (429 once
    an IP has failed ``AUTH_FAILURE_LIMIT`` times)."""
    ip = get_remote_address(request)
    if _auth_throttled(ip, hit=False):
        raise OpenAIError(
            429, "Too many invalid API key attempts.", "rate_limit_error", "rate_limit_exceeded"
        )
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    found = authenticate(db, token.strip()) if scheme.lower() == "bearer" else None
    if found is None:
        _auth_throttled(ip, hit=True)
        raise OpenAIError(
            401, "Invalid or missing API key.", "invalid_request_error", "invalid_api_key"
        )
    record, user = found
    request.state.app_key_id = str(record.id)
    request.state.app_user = user
    return user


def _rate_limit_key(request: Request) -> str:
    """Per key, not per IP: one Open WebUI server proxies many users from one IP.
    Runs after `require_app_key` (slowapi checks route limits inside the endpoint
    wrapper), so the key id is always set."""
    return f"v1:{getattr(request.state, 'app_key_id', 'anonymous')}"


router = APIRouter(
    prefix="/v1", tags=["OpenAI-compatible"], dependencies=[Depends(require_app_key)]
)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    """The OpenAI request body. Only ``messages[-1]``, ``stream`` and
    ``stream_options.include_usage`` are used; everything else is accepted and
    ignored (``model``, ``temperature``, ``tools``, …)."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None


def _text_of(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    texts = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind in _UNSUPPORTED_PARTS:
            raise OpenAIError(
                400,
                "Only text messages are supported.",
                "invalid_request_error",
                "unsupported_content",
            )
    return "\n".join(texts)


def _user_message(body: ChatCompletionRequest) -> str:
    """The one message this request asks the bot to answer."""
    last = body.messages[-1]
    if last.role.lower() != "user":
        # LibreChat's title request is a single `system` message by default.
        raise OpenAIError(
            400,
            "The last message must be a user message. Background tasks such as "
            "title generation are not supported; turn them off in your client.",
            "invalid_request_error",
            "unsupported_request",
        )
    text = _text_of(last.content).strip()
    if text.startswith(_CHORE_PREFIX):
        raise OpenAIError(
            400,
            "Background tasks (titles, tags, follow-ups, autocomplete) are not "
            "supported; turn them off in your client.",
            "invalid_request_error",
            "unsupported_request",
        )
    if not text:
        raise OpenAIError(400, "The message is empty.", "invalid_request_error", "empty_message")
    if len(text) > MAX_MESSAGE_CHARS:
        raise OpenAIError(
            400,
            f"The message is too long (max {MAX_MESSAGE_CHARS} characters).",
            "invalid_request_error",
            "message_too_long",
        )
    return text


async def _command_reply(db: Session, user: User, text: str) -> str | None:
    """The reply to a slash command, run exactly as on /chat/enqueue, or None."""
    if not is_command(text):
        return None
    parts = normalize_command(text).split()
    if parts and parts[0].lower() == "/unlink":
        # The key may sit on a WhatsApp row linked to Telegram; /unlink would cut
        # that link, not detach this app.
        return (
            "/unlink only separates WhatsApp and Telegram, so it isn't available "
            "here. To detach this app, ask the operator to revoke its key."
        )
    if parts and parts[0].lower() == "/link":
        return await link_command_reply(
            db, get_redis_client, user, user.whatsapp_jid, "private", parts
        )
    result = parse_and_execute(
        db, str(user.id), user.whatsapp_jid, text, conversation_type="private"
    )
    return result.response_text if result.is_command else None


async def _poll_once(redis, job_id: str) -> tuple[str, str | None]:
    """``("complete", text)``, ``("failed", None)`` or ``("pending", None)``.

    Reads only the job's status and chunks, so extra fields other changes add to
    a job's result (e.g. a ``media`` list) don't matter here."""
    status = await get_stream_job_status(redis, job_id)
    if status == "complete":
        chunks = await get_job_chunks(redis, job_id)
        return "complete", "".join(chunk["content"] for chunk in chunks)
    if status == "failed":
        return "failed", None
    return "pending", None


def _failed() -> OpenAIError:
    return OpenAIError(
        502, "The assistant failed to answer. Please try again.", "server_error", "job_failed"
    )


def _timeout() -> OpenAIError:
    return OpenAIError(
        504,
        "The assistant is taking too long. Its answer will be in the conversation; "
        "ask again shortly.",
        "server_error",
        "timeout",
    )


def _completion(completion_id: str, created: int, text: str) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": settings.openai_api_model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        # Token usage isn't tracked per request on this path (Logfire has it).
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse(payload: dict[str, Any] | str) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


async def _event_stream(
    completion_id: str,
    created: int,
    *,
    job_id: str | None,
    text: str | None,
    include_usage: bool,
) -> AsyncIterator[str]:
    """SSE frames: a role delta, pings while the job runs, the reply, stop, [DONE].

    The worker publishes the whole reply at once, so the content arrives as one
    delta. A failure or timeout is sent as an ``error`` event before ``[DONE]``.
    If the client disconnects, Starlette cancels this generator; the job still
    finishes and is saved to history.
    """

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        return _sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": settings.openai_api_model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )

    yield chunk({"role": "assistant", "content": ""})

    error: OpenAIError | None = None
    if text is None and job_id is not None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + JOB_TIMEOUT_SECONDS
        next_ping = loop.time() + PING_INTERVAL_SECONDS
        async with get_redis_client() as redis:
            while True:
                state, text = await _poll_once(redis, job_id)
                if state == "failed":
                    error = _failed()
                    break
                if state == "complete":
                    break
                now = loop.time()
                if now >= deadline:
                    error = _timeout()
                    break
                if now >= next_ping:
                    yield ": ping\n\n"
                    next_ping = now + PING_INTERVAL_SECONDS
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    if error is not None:
        logger.warning(f"/v1 stream for job {job_id} ended with {error.code}")
        yield _sse({"error": {"message": error.message, "type": error.type, "code": error.code}})
    else:
        if text:
            yield chunk({"content": text})
        yield chunk({}, "stop")
        if include_usage:
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": settings.openai_api_model_id,
                    "choices": [],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            )
    yield _sse("[DONE]")


async def _wait_for_reply(request: Request, job_id: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOB_TIMEOUT_SECONDS
    async with get_redis_client() as redis:
        while True:
            state, text = await _poll_once(redis, job_id)
            if state == "complete":
                return text or ""
            if state == "failed":
                raise _failed()
            if loop.time() >= deadline:
                raise _timeout()
            if await request.is_disconnected():
                # Nobody is waiting; the job still finishes and lands in history.
                raise OpenAIError(499, "Client disconnected.", "client_error", "disconnected")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@router.post("/chat/completions")
@limiter.limit(f"{settings.rate_limit_expensive}/minute", key_func=_rate_limit_key)
async def chat_completions(
    request: Request, body: ChatCompletionRequest, db: Session = Depends(get_db)
):
    """Answer the last user message as the bot (see the module docstring)."""
    user: User = request.state.app_user
    text = _user_message(body)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    try:
        reply = await _command_reply(db, user, text)
        job_id = None
        if reply is None:
            job_id = await enqueue_text_job(db, user, text, client_id=WEB_CLIENT_ID)
    except OpenAIError:
        raise
    except Exception:
        logger.error(f"/v1 enqueue failed for user {user.id}", exc_info=True)
        raise OpenAIError(500, "Internal server error.", "server_error", "internal_error")
    finally:
        # Nothing below touches the DB. Release the connection now: the wait can
        # last JOB_TIMEOUT_SECONDS, and the request-scoped session would otherwise
        # hold a pooled connection idle in transaction until the response ends.
        db.close()

    if body.stream:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        return StreamingResponse(
            _event_stream(
                completion_id, created, job_id=job_id, text=reply, include_usage=include_usage
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if reply is None:
        reply = await _wait_for_reply(request, job_id)
    return _completion(completion_id, created, reply)


@router.get("/models")
@limiter.limit(f"{settings.rate_limit_global}/minute", key_func=_rate_limit_key)
async def list_models(request: Request):
    return {
        "object": "list",
        "data": [
            {
                "id": settings.openai_api_model_id,
                "object": "model",
                "created": 0,
                "owned_by": "ai-agent",
            }
        ],
    }
