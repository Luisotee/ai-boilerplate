"""get_ai_response must run every tool call, even when the model writes text first.

DeepSeek often answers "Let me check…" and a tool call in the SAME response.
``agent.run_stream`` treats that first text as the final output and never executes
the tool, so the user got only the preamble. These tests drive the real
pydantic-ai / OpenAI SDK stack with a fake DeepSeek transport."""

import json
from unittest.mock import patch

import httpx
import openai
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from ai_api.agent.model_chain import DEEPSEEK_MODEL_SETTINGS, Cooldown, GuardedModel
from ai_api.agent.response import get_ai_response

SSE = {"content-type": "text/event-stream"}
PREAMBLE = "Let me check that for you."
ANSWER = "You have 3 saved memories."


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    payload = {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _sse(*chunks: bytes) -> httpx.Response:
    return httpx.Response(200, headers=SSE, content=b"".join(chunks) + b"data: [DONE]\n\n")


class _FakeDeepSeek:
    """First request: preamble text + a tool call. After the tool result: the answer."""

    def __init__(self, *, call_tool: bool):
        self.call_tool = call_tool
        self.requests: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.call_tool or any(m["role"] == "tool" for m in body["messages"]):
            return _sse(_chunk({"role": "assistant", "content": ANSWER}), _chunk({}, "stop"))
        tool_call = {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_subscriptions", "arguments": "{}"},
        }
        return _sse(
            _chunk({"role": "assistant", "content": PREAMBLE}),
            _chunk({"tool_calls": [tool_call]}),
            _chunk({}, "tool_calls"),
        )


def _deepseek(fake: _FakeDeepSeek) -> GuardedModel:
    client = openai.AsyncOpenAI(
        base_url="https://api.deepseek.com",
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return GuardedModel(
        OpenAIChatModel(
            "deepseek-flash",
            provider=DeepSeekProvider(openai_client=client),
            settings=DEEPSEEK_MODEL_SETTINGS,
        ),
        first_chunk_timeout=5.0,
        cooldown=Cooldown(60),
    )


async def _reply(fake: _FakeDeepSeek, tool_calls: list[int]) -> str:
    test_agent = Agent()

    @test_agent.tool_plain
    def list_subscriptions() -> str:
        tool_calls.append(1)
        return "3 memories"

    with (
        patch("ai_api.agent.response.agent", test_agent),
        patch("ai_api.agent.response.build_runtime_model", return_value=_deepseek(fake)),
    ):
        return "".join([chunk async for chunk in get_ai_response("What do you remember about me?")])


async def test_tool_call_after_preamble_text_is_executed():
    fake = _FakeDeepSeek(call_tool=True)
    tool_calls: list[int] = []

    reply = await _reply(fake, tool_calls)

    assert tool_calls == [1]
    assert len(fake.requests) == 2
    assert reply == ANSWER  # the preamble is not sent to the user


async def test_plain_text_reply_is_returned_unchanged():
    fake = _FakeDeepSeek(call_tool=False)
    tool_calls: list[int] = []

    assert await _reply(fake, tool_calls) == ANSWER
    assert tool_calls == []
    assert len(fake.requests) == 1
