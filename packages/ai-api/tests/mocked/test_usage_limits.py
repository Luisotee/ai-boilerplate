"""The agent loop is capped (AGENT_REQUEST_LIMIT) and a looping model gets the fallback reply."""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai import UsageLimitExceeded
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


def _looping_model(calls: list[int]):
    """A (streaming) model that always asks for the same tool, forever."""

    async def stream(messages, info: AgentInfo):
        calls.append(1)
        yield {0: DeltaToolCall(name=info.function_tools[0].name, json_args="{}")}

    return FunctionModel(stream_function=stream)


async def test_run_stops_at_the_request_limit(monkeypatch):
    from pydantic_ai import Agent

    from ai_api.agent import response as response_module
    from ai_api.config import settings

    monkeypatch.setattr(settings, "agent_request_limit", 3)
    calls: list[int] = []
    loop_agent = Agent(_looping_model(calls))

    @loop_agent.tool_plain
    def ping() -> str:
        return "pong"

    monkeypatch.setattr(response_module, "agent", loop_agent)
    monkeypatch.setattr(response_module, "build_runtime_model", lambda: _looping_model(calls))

    with pytest.raises(UsageLimitExceeded):
        async for _ in response_module.get_ai_response("hi", None, agent_deps=None):
            pass
    assert len(calls) == 3


async def test_processor_answers_a_looping_run_with_the_fallback():
    from ai_api.streams import processor as proc_module

    redis = AsyncMock()

    @asynccontextmanager
    async def get_client():
        yield redis

    async def looping(*_a, **_kw):
        raise UsageLimitExceeded("The next request would exceed the request_limit of 10")
        yield  # pragma: no cover

    with (
        patch.object(proc_module, "SessionLocal", return_value=MagicMock()),
        patch.object(proc_module, "get_conversation_history", return_value=[]),
        patch.object(proc_module, "create_embedding_service", return_value=None),
        patch.object(proc_module, "create_whatsapp_client", return_value=AsyncMock()),
        patch.object(proc_module, "get_ai_response", looping),
        patch.object(proc_module, "get_redis_client", get_client),
        patch.object(proc_module, "save_message") as save,
        patch.object(proc_module, "save_job_chunk", new_callable=AsyncMock) as chunk,
        patch.object(proc_module, "set_job_metadata", new_callable=AsyncMock) as meta,
        patch.object(proc_module, "record_auto_report") as report,
    ):
        result = await proc_module.process_chat_job_direct(
            user_id="u",
            whatsapp_jid="5511999999999@s.whatsapp.net",
            message="hi",
            conversation_type="private",
            user_message_id=str(uuid.uuid4()),
            job_id="job-loop",
        )

    assert result["model_error"] is True
    assert chunk.await_args.args[3] == proc_module.MODEL_ERROR_FALLBACK_TEXT
    assert "status" not in meta.await_args.args[2]  # completed, not failed
    save.assert_not_called()
    assert report.call_args.kwargs["title"] == "Agent hit the request limit"
