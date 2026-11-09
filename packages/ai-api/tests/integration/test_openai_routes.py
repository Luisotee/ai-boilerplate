"""OpenAI-compatible /v1 API (routes/openai_compat.py): auth, middleware, request
rules, the enqueue → poll mapping, and the SSE wire format."""

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers.factories import make_user

MOD = "ai_api.routes.openai_compat"
APP_KEY = "sk-" + "a" * 43
BEARER = {"Authorization": f"Bearer {APP_KEY}"}
WEB_JID = "web:0123456789abcdef"


@pytest.fixture
def app():
    from ai_api.database import get_db
    from ai_api.main import app

    app.dependency_overrides[get_db] = lambda: (yield MagicMock())
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def user():
    return make_user(WEB_JID, name="Ana")


@pytest.fixture
def authed(user):
    """A valid key resolving to `user`."""
    record = MagicMock(id=uuid.uuid4())
    with patch(f"{MOD}.authenticate", return_value=(record, user)) as auth:
        yield auth


@pytest.fixture
def no_redis():
    @asynccontextmanager
    async def ctx():
        yield AsyncMock()

    with (
        patch(f"{MOD}.get_redis_client", side_effect=lambda: ctx()),
        patch(f"{MOD}.POLL_INTERVAL_SECONDS", 0),
    ):
        yield


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _body(*messages, **extra):
    return {"model": "whatever", "messages": list(messages), **extra}


USER_HI = {"role": "user", "content": "Hi there"}


# --- Auth and middleware -----------------------------------------------------


class TestAuth:
    @patch("ai_api.main.init_db")
    async def test_missing_key(self, _init, app):
        with patch(f"{MOD}.authenticate", return_value=None):
            async with _client(app) as c:
                r = await c.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"
        assert r.headers["www-authenticate"] == "Bearer"

    @patch("ai_api.main.init_db")
    async def test_internal_ai_api_key_is_not_accepted(self, _init, app):
        """X-API-Key or a Bearer AI_API_KEY: neither is a per-user key."""
        async with _client(app) as c:
            r1 = await c.get("/v1/models", headers={"X-API-Key": "test-api-key"})
            r2 = await c.get("/v1/models", headers={"Authorization": "Bearer test-api-key"})
        assert r1.status_code == 401
        assert r2.status_code == 401

    @patch("ai_api.main.init_db")
    async def test_unknown_or_revoked_key(self, _init, app):
        with patch(f"{MOD}.authenticate", return_value=None) as auth:
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(USER_HI), headers=BEARER)
        assert r.status_code == 401
        auth.assert_called_once()
        assert auth.call_args.args[1] == APP_KEY

    @patch("ai_api.main.init_db")
    async def test_valid_key_lists_the_model(self, _init, app, authed):
        async with _client(app) as c:
            r = await c.get("/v1/models", headers=BEARER)
        assert r.status_code == 200
        data = r.json()
        assert data["object"] == "list"
        assert data["data"][0]["id"] == "ai-agent"
        assert data["data"][0]["object"] == "model"

    @patch("ai_api.main.init_db")
    async def test_skipping_the_middleware_opens_nothing_else(self, _init, app, authed):
        """Only /v1/ skips APIKeyMiddleware, and routing matches the raw path."""
        async with _client(app) as c:
            for path in ("/admin/overview", "/chat/job/x", "/v1"):
                r = await c.get(path, headers=BEARER)
                assert r.status_code == 401, path
        # httpx normalizes dot segments, so send the raw path through ASGI.
        status = {}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/v1/../admin/overview",
            "raw_path": b"/v1/../admin/overview",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {APP_KEY}".encode())],
            "client": ("1.2.3.4", 1),
            "server": ("test", 80),
            "scheme": "http",
            "root_path": "",
            "http_version": "1.1",
        }
        await app(scope, receive, send)
        assert status["code"] == 404

    def test_every_v1_route_requires_an_app_key(self):
        from ai_api.main import app
        from ai_api.routes.openai_compat import require_app_key

        v1 = [r for r in app.routes if getattr(r, "path", "").startswith("/v1")]
        assert {r.path for r in v1} == {"/v1/chat/completions", "/v1/models"}
        for route in v1:
            deps = [d.call for d in route.dependant.dependencies]
            assert require_app_key in deps, route.path


# --- Request rules -------------------------------------------------------------


class TestRequestRules:
    @pytest.mark.parametrize(
        "messages,code",
        [
            # LibreChat's title request: one system message.
            (
                [{"role": "system", "content": "Analyze this conversation..."}],
                "unsupported_request",
            ),
            ([USER_HI, {"role": "assistant", "content": "Hello"}], "unsupported_request"),
            # Open WebUI's chores.
            (
                [{"role": "user", "content": "### Task:\nGenerate a concise title"}],
                "unsupported_request",
            ),
            ([{"role": "user", "content": "   "}], "empty_message"),
            ([{"role": "user", "content": "x" * 32_001}], "message_too_long"),
            (
                [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "data:"}}],
                    }
                ],
                "unsupported_content",
            ),
        ],
    )
    @patch("ai_api.main.init_db")
    async def test_refused_and_never_queued(self, _init, app, authed, messages, code):
        with patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock) as enqueue:
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(*messages), headers=BEARER)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == code
        enqueue.assert_not_awaited()

    @patch("ai_api.main.init_db")
    async def test_only_the_last_message_is_queued(self, _init, app, authed, user, no_redis):
        """Clients resend the whole transcript; the server keeps the history."""
        history = [
            {"role": "system", "content": "You are a pirate."},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "second"}, {"type": "text", "text": "part"}],
            },
        ]
        with (
            patch(
                f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"
            ) as enqueue,
            patch(f"{MOD}._poll_once", new_callable=AsyncMock, return_value=("complete", "ok")),
        ):
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(*history), headers=BEARER)
        assert r.status_code == 200
        args, kwargs = enqueue.call_args
        assert args[1] is user
        assert args[2] == "second\npart"
        assert kwargs["client_id"] == "web"


# --- Non-streaming ------------------------------------------------------------------


class TestCompletion:
    @patch("ai_api.main.init_db")
    async def test_waits_for_the_job_and_returns_a_completion(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(
                f"{MOD}._poll_once",
                new_callable=AsyncMock,
                side_effect=[("pending", None), ("pending", None), ("complete", "**Hello**")],
            ),
        ):
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(USER_HI), headers=BEARER)
        assert r.status_code == 200
        data = r.json()
        assert data["object"] == "chat.completion"
        assert data["id"].startswith("chatcmpl-")
        assert data["model"] == "ai-agent"
        assert data["choices"][0]["message"] == {"role": "assistant", "content": "**Hello**"}
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["total_tokens"] == 0

    @patch("ai_api.main.init_db")
    async def test_failed_job_is_a_502(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(f"{MOD}._poll_once", new_callable=AsyncMock, return_value=("failed", None)),
        ):
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(USER_HI), headers=BEARER)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "job_failed"

    @patch("ai_api.main.init_db")
    async def test_timeout_is_a_504(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(f"{MOD}._poll_once", new_callable=AsyncMock, return_value=("pending", None)),
            patch(f"{MOD}.JOB_TIMEOUT_SECONDS", 0),
        ):
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(USER_HI), headers=BEARER)
        assert r.status_code == 504

    @patch("ai_api.main.init_db")
    async def test_enqueue_error_is_generic(self, _init, app, authed):
        with patch(
            f"{MOD}.enqueue_text_job",
            new_callable=AsyncMock,
            side_effect=RuntimeError("postgres://secret-host"),
        ):
            async with _client(app) as c:
                r = await c.post("/v1/chat/completions", json=_body(USER_HI), headers=BEARER)
        assert r.status_code == 500
        assert "secret-host" not in r.text

    @patch("ai_api.main.init_db")
    async def test_commands_run_like_on_chat_enqueue(self, _init, app, authed, user):
        result = MagicMock(is_command=True, response_text="Available commands: ...")
        with (
            patch(f"{MOD}.parse_and_execute", return_value=result) as run,
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock) as enqueue,
        ):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions",
                    json=_body({"role": "user", "content": "/help"}),
                    headers=BEARER,
                )
        assert r.json()["choices"][0]["message"]["content"] == "Available commands: ..."
        assert run.call_args.args[2] == user.whatsapp_jid
        enqueue.assert_not_awaited()

    @patch("ai_api.main.init_db")
    async def test_link_goes_through_the_shared_helper(self, _init, app, authed, user):
        with patch(
            f"{MOD}.link_command_reply", new_callable=AsyncMock, return_value="code 123"
        ) as lk:
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions",
                    json=_body({"role": "user", "content": "/link"}),
                    headers=BEARER,
                )
        assert r.json()["choices"][0]["message"]["content"] == "code 123"
        args = lk.call_args.args
        assert args[2] is user and args[3] == WEB_JID and args[5] == ["/link"]


# --- Streaming ------------------------------------------------------------------------


def _events(text: str) -> list[str]:
    return [block for block in text.split("\n\n") if block]


class TestStreaming:
    @patch("ai_api.main.init_db")
    async def test_sse_frames(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(
                f"{MOD}._poll_once",
                new_callable=AsyncMock,
                side_effect=[("pending", None), ("complete", "Hello **there**")],
            ),
        ):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions",
                    json=_body(USER_HI, stream=True, stream_options={"include_usage": True}),
                    headers=BEARER,
                )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _events(r.text)
        assert events[-1] == "data: [DONE]"
        payloads = [json.loads(e[len("data: ") :]) for e in events[:-1]]
        assert all(p["object"] == "chat.completion.chunk" for p in payloads)
        assert len({p["id"] for p in payloads}) == 1
        assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
        assert payloads[1]["choices"][0]["delta"] == {"content": "Hello **there**"}
        assert payloads[2]["choices"][0]["finish_reason"] == "stop"
        assert payloads[3]["choices"] == [] and "usage" in payloads[3]

    @patch("ai_api.main.init_db")
    async def test_pings_while_waiting(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(
                f"{MOD}._poll_once",
                new_callable=AsyncMock,
                side_effect=[("pending", None), ("pending", None), ("complete", "ok")],
            ),
            patch(f"{MOD}.PING_INTERVAL_SECONDS", 0),
        ):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions", json=_body(USER_HI, stream=True), headers=BEARER
                )
        assert ": ping" in _events(r.text)

    @patch("ai_api.main.init_db")
    async def test_failure_is_an_error_event_then_done(self, _init, app, authed, no_redis):
        with (
            patch(f"{MOD}.enqueue_text_job", new_callable=AsyncMock, return_value="job-1"),
            patch(f"{MOD}._poll_once", new_callable=AsyncMock, return_value=("failed", None)),
        ):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions", json=_body(USER_HI, stream=True), headers=BEARER
                )
        events = _events(r.text)
        assert events[-1] == "data: [DONE]"
        assert json.loads(events[-2][len("data: ") :])["error"]["code"] == "job_failed"

    @patch("ai_api.main.init_db")
    async def test_command_streams_without_a_job(self, _init, app, authed):
        result = MagicMock(is_command=True, response_text="Settings: ...")
        with patch(f"{MOD}.parse_and_execute", return_value=result):
            async with _client(app) as c:
                r = await c.post(
                    "/v1/chat/completions",
                    json=_body({"role": "user", "content": "/settings"}, stream=True),
                    headers=BEARER,
                )
        assert '"content": "Settings: ..."' in r.text
        assert _events(r.text)[-1] == "data: [DONE]"


# --- The poll step ----------------------------------------------------------------------


async def test_poll_once_reads_only_status_and_chunks():
    """Extra job fields (e.g. the images group's `media`) are ignored."""
    from ai_api.routes import openai_compat

    redis = AsyncMock()
    with (
        patch.object(openai_compat, "get_stream_job_status", AsyncMock(return_value="complete")),
        patch.object(
            openai_compat,
            "get_job_chunks",
            AsyncMock(return_value=[{"content": "a", "media": [1]}, {"content": "b"}]),
        ),
    ):
        assert await openai_compat._poll_once(redis, "j") == ("complete", "ab")
    with patch.object(openai_compat, "get_stream_job_status", AsyncMock(return_value="queued")):
        assert await openai_compat._poll_once(redis, "j") == ("pending", None)


def test_rate_limit_key_is_per_app_key():
    from ai_api.routes.openai_compat import _rate_limit_key

    request = MagicMock()
    request.state.app_key_id = "k1"
    assert _rate_limit_key(request) == "v1:k1"
