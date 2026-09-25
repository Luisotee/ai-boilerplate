"""DeepSeek -> Gemini fallback, driven through the REAL pydantic-ai / OpenAI SDK
stack with a fake HTTP transport.

Asserting on exception classes alone gave false confidence before: the errors that
actually escape while reading the first streamed chunk are raw httpx / openai
errors that pydantic-ai never converts. These tests reproduce each failure at the
network layer and check what the agent really does."""

import asyncio
import json
import logging

import httpx
import openai
import pytest
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from ai_api.agent.model_chain import (
    DEEPSEEK_MODEL_SETTINGS,
    FALLBACK_ON,
    Cooldown,
    GuardedModel,
)

GEMINI_REPLY = "resposta do gemini"
SSE = {"content-type": "text/event-stream"}


def _chunk(content: str, finish: str | None = None) -> bytes:
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class _Stream(httpx.AsyncByteStream):
    """Response body that yields parts in order; an Exception part is raised,
    an ``asyncio.Event`` part blocks forever (a server that never sends more)."""

    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            if isinstance(part, asyncio.Event):
                await part.wait()
            yield part


def _ok_response():
    return httpx.Response(
        200,
        headers=SSE,
        stream=_Stream([_chunk("resposta do deepseek"), _chunk("", "stop"), b"data: [DONE]\n\n"]),
    )


SCENARIOS = {
    "ok": _ok_response,
    "http-500": lambda: httpx.Response(500, json={"error": {"message": "boom"}}),
    "http-400": lambda: httpx.Response(400, json={"error": {"message": "context too long"}}),
    "http-401": lambda: httpx.Response(401, json={"error": {"message": "bad key"}}),
    "read-timeout-first-chunk": lambda: httpx.Response(
        200, headers=SSE, stream=_Stream([httpx.ReadTimeout("slow")])
    ),
    "sse-error-event": lambda: httpx.Response(
        200, headers=SSE, stream=_Stream([b'data: {"error": {"message": "overloaded"}}\n\n'])
    ),
    "keepalive-then-hang": lambda: httpx.Response(
        200, headers=SSE, stream=_Stream([b": keep-alive\n\n", asyncio.Event()])
    ),
    "mid-stream-drop": lambda: httpx.Response(
        200,
        headers=SSE,
        stream=_Stream(
            [_chunk("Olá "), _chunk("mun"), httpx.RemoteProtocolError("peer closed connection")]
        ),
    ),
}


class _FakeDeepSeek:
    """Mock transport recording every request DeepSeek receives."""

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.requests: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.scenario == "connect-error":
            raise httpx.ConnectError("unreachable")
        return SCENARIOS[self.scenario]()


def _agent(fake: _FakeDeepSeek, cooldown: Cooldown, first_chunk_timeout: float = 5.0):
    client = openai.AsyncOpenAI(
        base_url="https://api.deepseek.com",
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    deepseek = GuardedModel(
        OpenAIChatModel(
            "deepseek-flash",
            provider=DeepSeekProvider(openai_client=client),
            settings=DEEPSEEK_MODEL_SETTINGS,
        ),
        first_chunk_timeout=first_chunk_timeout,
        cooldown=cooldown,
    )

    async def gemini(_messages, _info: AgentInfo):
        yield GEMINI_REPLY

    return Agent(
        FallbackModel(deepseek, FunctionModel(stream_function=gemini), fallback_on=FALLBACK_ON)
    )


async def _ask(agent: Agent, prompt="oi") -> str:
    out = ""
    async with agent.run_stream(prompt) as result:
        async for delta in result.stream_text(delta=True):
            out += delta
    return out


async def test_deepseek_answers_when_healthy():
    fake = _FakeDeepSeek("ok")
    assert await _ask(_agent(fake, Cooldown(60))) == "resposta do deepseek"


@pytest.mark.parametrize(
    "scenario",
    [
        "http-500",
        "http-400",
        "http-401",
        "connect-error",
        "read-timeout-first-chunk",
        "sse-error-event",
    ],
)
async def test_open_phase_failures_fall_back_to_gemini(scenario):
    fake = _FakeDeepSeek(scenario)
    assert await _ask(_agent(fake, Cooldown(60))) == GEMINI_REPLY
    assert len(fake.requests) == 1


async def test_keepalive_lines_do_not_defeat_first_chunk_timeout():
    """Queue keep-alives reset httpx's read timeout; the wall-clock limit still fires."""
    fake = _FakeDeepSeek("keepalive-then-hang")
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await _ask(_agent(fake, Cooldown(60), first_chunk_timeout=0.2)) == GEMINI_REPLY
    assert loop.time() - started < 2


async def test_mid_stream_drop_does_not_fall_back():
    """After the first chunk there is no fallback (pydantic-ai design): the raw
    error propagates, and streams/processor.py turns it into the fallback reply."""
    fake = _FakeDeepSeek("mid-stream-drop")
    with pytest.raises(httpx.RemoteProtocolError):
        await _ask(_agent(fake, Cooldown(60)))


@pytest.mark.parametrize("scenario", ["http-500", "http-401", "read-timeout-first-chunk"])
async def test_availability_failure_starts_cooldown(scenario):
    fake = _FakeDeepSeek(scenario)
    cooldown = Cooldown(60)
    agent = _agent(fake, cooldown)

    assert await _ask(agent) == GEMINI_REPLY
    assert await _ask(agent) == GEMINI_REPLY
    assert len(fake.requests) == 1  # second run skipped DeepSeek entirely

    cooldown.until = 0.0  # cooldown expired
    fake.scenario = "ok"
    assert await _ask(agent) == "resposta do deepseek"
    assert len(fake.requests) == 2


async def test_request_specific_400_does_not_start_cooldown():
    fake = _FakeDeepSeek("http-400")
    cooldown = Cooldown(60)
    agent = _agent(fake, cooldown)

    assert await _ask(agent) == GEMINI_REPLY
    assert not cooldown.active()
    fake.scenario = "ok"
    assert await _ask(agent) == "resposta do deepseek"


async def test_fallback_is_logged(caplog):
    fake = _FakeDeepSeek("http-500")
    with caplog.at_level(logging.WARNING, logger="ai_api.agent.model_chain"):
        await _ask(_agent(fake, Cooldown(60)))
    assert any(
        "DeepSeek (deepseek-flash) failed, falling back to Gemini" in r.getMessage()
        for r in caplog.records
    )


async def test_request_body_disables_thinking_and_sends_image():
    fake = _FakeDeepSeek("ok")
    image = BinaryContent(data=b"\x89PNG\r\n\x1a\n0000", media_type="image/png")
    await _ask(_agent(fake, Cooldown(60)), ["descreva", image])

    body = fake.requests[0]
    assert body["thinking"] == {"type": "disabled"}
    parts = body["messages"][-1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
