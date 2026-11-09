"""Run one golden-set case through the production agent path, offline.

``run_case`` mirrors what a chat job does: slash commands stop at
``commands.is_command`` (``routes/chat.py`` answers them without the agent);
everything else goes through ``agent.response.get_ai_response`` (so
``run_stream_events`` and ``build_runtime_model``), with the tool calls read
back through ``capture_run_messages``.

What is stubbed, so a case touches no database, network or chat client:
the DB session (a MagicMock), the active prompt (the default), core memory and
preferences (in-memory objects), runtime settings (env defaults), the HTTP
client (``httpx.MockTransport`` with canned weather / web-page replies), DDGS
web search, Wikipedia, the WhatsApp client and the destructive helpers behind
``clean_user_data`` and the announcements setting. The model is whatever the
caller passes: a ``FunctionModel`` oracle locally, the real chain otherwise.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from pydantic_ai import capture_run_messages
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ..commands import is_command

PRIVATE_JID = "5591999990000@s.whatsapp.net"
GROUP_JID = "120363000000000000@g.us"


@dataclass(frozen=True)
class EvalInput:
    """One incoming message and the chat it arrives in."""

    message: str
    chat: Literal["private", "group"] = "private"
    #: Only meaningful in a group: what the client says about the sender.
    is_group_admin: bool | None = None
    client_id: str | None = None
    #: Earlier turns, oldest first, as (role, text) with role "user" or "assistant".
    history: tuple[tuple[str, str], ...] = ()
    #: The chat's core memory document at the start of the run.
    memory: str = ""


@dataclass(frozen=True)
class ExpectedCall:
    """A tool the model should call; ``args`` lists only the arguments that matter."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpectedBehaviour:
    """What a case checks (its ``metadata``); also scripts the local oracle."""

    route: Literal["agent", "command"] = "agent"
    tools: list[ExpectedCall] = field(default_factory=list)
    no_tools: bool = False
    forbidden_tools: list[str] = field(default_factory=list)
    #: tool name -> text its reply must contain (e.g. the admin refusal).
    tool_return_contains: dict[str, str] = field(default_factory=dict)
    reply_contains: list[str] = field(default_factory=list)
    #: Language the reply must be in ("pt", "en", "es"), or None to skip.
    language: str | None = "pt"
    #: The local oracle's final answer (pt-BR, chat markup only).
    oracle_reply: str = "Pronto! Posso ajudar com mais alguma coisa?"


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]

    def matches(self, want: ExpectedCall) -> bool:
        if self.name != want.name:
            return False
        for key, value in want.args.items():
            got = self.args.get(key)
            if isinstance(value, str) and isinstance(got, str):
                if value.lower() not in got.lower():
                    return False
            elif got != value:
                return False
        return True


@dataclass
class EvalOutput:
    route: Literal["agent", "command"]
    reply: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: tool name -> its last reply in the run.
    tool_returns: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- stubs


def _http_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "geocoding-api.open-meteo.com":
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "Belém", "country": "Brasil", "latitude": -1.45, "longitude": -48.5}
                ]
            },
        )
    if host == "api.open-meteo.com":
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 31.0,
                    "relative_humidity_2m": 74,
                    "weather_code": 2,
                    "wind_speed_10m": 9.0,
                }
            },
        )
    return httpx.Response(200, text="Página de exemplo: texto do artigo.")


class _FakeDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, query, max_results=10):
        return [
            {
                "title": "Resultado de exemplo",
                "body": f"Notícia recente sobre {query}.",
                "href": "https://exemplo.com.br/noticia",
            }
        ]


def _fake_wikipedia() -> MagicMock:
    page = MagicMock()
    page.exists.return_value = True
    page.title = "Castanha-do-pará"
    page.summary = "A castanha-do-pará é a semente da Bertholletia excelsa."
    page.fullurl = "https://pt.wikipedia.org/wiki/Castanha-do-par%C3%A1"
    module = MagicMock()
    module.Wikipedia.return_value.page.return_value = page
    return module


@contextlib.contextmanager
def stubbed_environment(inputs: EvalInput) -> Iterator[SimpleNamespace]:
    """Patch everything a run would reach outside the process; yields the state."""
    from ..runtime_config import runtime_config

    memory = SimpleNamespace(content=inputs.memory, previous_content=None)
    prefs = SimpleNamespace(tts_enabled=False, tts_language="pt", stt_language=None)
    targets = {
        "ai_api.agent.core.get_active_prompt": MagicMock(return_value=None),
        "ai_api.agent.core.get_or_create_core_memory": MagicMock(return_value=memory),
        "ai_api.agent.tools.memory.get_or_create_core_memory": MagicMock(return_value=memory),
        "ai_api.agent.tools.settings.get_or_create_preferences": MagicMock(return_value=prefs),
        "ai_api.agent.tools.settings.handle_clean_command": MagicMock(
            return_value="Deleted 12 messages."
        ),
        "ai_api.agent.tools.broadcast.set_broadcast_opt_out": MagicMock(return_value=False),
        "ai_api.agent.tools.history.get_conversation_messages": MagicMock(return_value=[]),
        "ai_api.agent.tools.web.DDGS": _FakeDDGS,
        "ai_api.agent.tools.utility.wikipediaapi": _fake_wikipedia(),
    }
    with contextlib.ExitStack() as stack:
        for target, value in targets.items():
            stack.enter_context(patch(target, value))
        # Hot settings read their env defaults; no settings table to refresh from.
        stack.enter_context(patch.object(runtime_config, "_ensure_fresh", lambda: None))
        stack.enter_context(patch.object(runtime_config, "_overrides", {}))
        yield SimpleNamespace(memory=memory, prefs=prefs)


def _deps(inputs: EvalInput, http_client: httpx.AsyncClient):
    from ..agent import AgentDeps

    is_group = inputs.chat == "group"
    whatsapp_client = MagicMock()
    for method in ("send_reaction", "send_location", "send_contact", "send_message"):
        setattr(whatsapp_client, method, AsyncMock(return_value={"success": True}))
    return AgentDeps(
        db=MagicMock(),
        user_id="00000000-0000-0000-0000-000000000001",
        whatsapp_jid=GROUP_JID if is_group else PRIVATE_JID,
        recent_message_ids=[],
        embedding_service=None,
        http_client=http_client,
        whatsapp_client=whatsapp_client,
        current_message_id="EVALMSG0001",
        client_id=inputs.client_id,
        is_group_admin=inputs.is_group_admin if is_group else None,
    )


def _history(inputs: EvalInput) -> list[ModelMessage] | None:
    from pydantic_ai.messages import UserPromptPart

    messages: list[ModelMessage] = []
    for role, text in inputs.history:
        if role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=text)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=text)]))
    return messages or None


def _read_tools(messages: list[ModelMessage]) -> tuple[list[ToolCall], dict[str, str]]:
    calls: list[ToolCall] = []
    returns: dict[str, str] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                calls.append(ToolCall(part.tool_name, part.args_as_dict()))
            elif isinstance(part, ToolReturnPart):
                returns[part.tool_name] = part.model_response_str()
    return calls, returns


# --------------------------------------------------------------------------- runner


async def run_case(inputs: EvalInput, model: Model | None = None) -> EvalOutput:
    """Answer ``inputs`` the way a chat job would, offline.

    ``model`` None means the production chain from ``build_runtime_model()``
    (real API calls, real cost); pass a ``FunctionModel`` for a free run.
    """
    if is_command(inputs.message):
        return EvalOutput(route="command")

    from ..agent import agent, get_ai_response

    prompt = inputs.message
    if inputs.chat == "group":
        prompt = f"Maria: {inputs.message}"  # how streams/processor.py labels group senders

    transport = httpx.MockTransport(_http_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        with stubbed_environment(inputs), capture_run_messages() as messages:
            override = agent.override(model=model) if model else contextlib.nullcontext()
            reply = ""
            with override:
                async for text in get_ai_response(
                    prompt, _history(inputs), agent_deps=_deps(inputs, http_client)
                ):
                    reply = text
    calls, returns = _read_tools(messages)
    return EvalOutput(route="agent", reply=reply, tool_calls=calls, tool_returns=returns)


def oracle_model(expected: ExpectedBehaviour) -> FunctionModel:
    """A scripted model that does what the case expects (free, deterministic).

    First request: call every expected tool with the expected arguments.
    After the tool replies come back (or straight away when no tool is
    expected): answer with ``oracle_reply``. It proves the case still fits the
    live tool schemas and that the tools run; tool *choice* is measured only
    against the real models.
    """

    def _tool_round(messages: list[ModelMessage]) -> bool:
        answered = any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        return bool(expected.tools) and not answered

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if _tool_round(messages):
            return ModelResponse(
                parts=[
                    ToolCallPart(call.name, json.dumps(call.args), tool_call_id=f"call_{i}")
                    for i, call in enumerate(expected.tools)
                ]
            )
        return ModelResponse(parts=[TextPart(expected.oracle_reply)])

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        # The production path (run_stream_events) streams every model request.
        if _tool_round(messages):
            yield {
                i: DeltaToolCall(
                    name=call.name, json_args=json.dumps(call.args), tool_call_id=f"call_{i}"
                )
                for i, call in enumerate(expected.tools)
            }
            return
        yield expected.oracle_reply

    return FunctionModel(respond, stream_function=stream)
