import re
from dataclasses import dataclass, field

import httpx
from pydantic_ai import Agent, RunContext, ToolDefinition
from pydantic_ai.models import Model
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_active_prompt, get_or_create_core_memory
from ..embeddings import EmbeddingService
from ..logger import logger
from ..reply_media import ReplyImage
from ..runtime_config import runtime_config
from ..services.shared_groups import shared_group_tools_available
from ..surfaces import renders_markdown, surface_of
from ..whatsapp import WhatsAppClient
from .model_chain import build_model


@dataclass
class AgentDeps:
    """
    Dependencies for agent tools.

    Follows Pydantic AI best practices by injecting all dependencies
    via this dataclass instead of using global singletons.
    """

    db: Session
    user_id: str
    whatsapp_jid: str
    recent_message_ids: list[str]
    embedding_service: EmbeddingService | None = None
    http_client: httpx.AsyncClient | None = None
    whatsapp_client: WhatsAppClient | None = None
    current_message_id: str | None = None
    #: Which chat client the message came from: "baileys"/None (Baileys),
    #: "cloud" (Meta Cloud API) or "telegram". The shared-group tools dispatch
    #: on it, since each platform answers "which groups does this user share
    #: with the bot" differently (or, for Cloud API, not at all).
    client_id: str | None = None
    #: Whether the sender is an admin of the group this message came from
    #: (None = private chat or unknown). Tools that change a GROUP's settings
    #: must require `is True` — fail closed, like the admin slash commands.
    is_group_admin: bool | None = None
    #: Stream job id of the message being answered (recorded on bug reports).
    job_id: str | None = None
    #: True for a scheduled task's agent run (streams/scheduled_runner.py): no
    #: user is waiting, so only read-only tools are offered (SCHEDULED_RUN_TOOLS).
    scheduled_run: bool = False
    #: Id of the saved user message being answered (conversation_messages.id):
    #: its sender_jid / sender_name record who asked for a scheduled task.
    user_message_id: str | None = None
    #: Images to send after this run's text, in the current chat only. The
    #: processor merges them with the `![caption](https://…)` directives in the
    #: reply; a tool that produces an image URL can append here instead of
    #: relying on the model to repeat it.
    reply_media: list[ReplyImage] = field(default_factory=list)


#: The only tools a scheduled agent run may use: read-only ones. Nothing that
#: sends (that would bypass the run's claim, Cloud-window and daily-cap checks),
#: changes settings or memory, or creates more scheduled tasks (recursion).
SCHEDULED_RUN_TOOLS = frozenset(
    {
        "search_conversation_history",
        "get_chat_history",
        "search_knowledge_base",
        "web_search",
        "fetch_website",
        "calculate",
        "get_weather",
        "wikipedia_lookup",
        "convert_units",
    }
)


async def scheduled_run_tools(
    ctx: RunContext["AgentDeps"], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Agent-wide ``prepare_tools``: in a scheduled run, keep only SCHEDULED_RUN_TOOLS.

    Runs after each tool's own ``prepare`` hook; a tool filtered out here can't
    be called either. Ordinary chat runs get every tool unchanged.
    """
    if ctx.deps is not None and ctx.deps.scheduled_run:
        return [td for td in tool_defs if td.name in SCHEDULED_RUN_TOOLS]
    return tool_defs


# Startup default: DeepSeek -> Gemini when DEEPSEEK_API_KEY is set, else Gemini
# alone (see model_chain.py). Every chat run overrides it per-call via
# build_runtime_model() so /admin can change the model names live.

# Create the AI agent with dependencies
# Do NOT pass instrument= here. instrument.py enables Logfire globally via
# instrument_pydantic_ai(), which sets the Agent._instrument_default ClassVar;
# a per-agent instrument= takes precedence over it and would silently disable
# token/cost tracking. Leaving it unset is what makes the global setting apply.
agent = Agent(
    model=build_model(settings.deepseek_model, settings.gemini_model),
    deps_type=AgentDeps,
    prepare_tools=scheduled_run_tools,
    retries=3,  # Increase from default 1 to handle occasional malformed model responses
)


def build_runtime_model() -> Model:
    """Build a Model for this run from the current runtime_config values.

    Called by ``agent/response.py`` for every ``agent.run_stream_events`` invocation, so a
    ``/admin/settings`` change to ``deepseek_model`` or ``gemini_model`` takes
    effect on the next message without restarting the process (≤ ~10s in the
    stream worker, via runtime_config's TTL cache; instant in the API process).

    When ``DEEPSEEK_API_KEY`` is set this is ``FallbackModel(DeepSeek → Gemini)``;
    otherwise Gemini alone (see ``model_chain.py``).
    """
    return build_model(
        runtime_config.get("deepseek_model"),
        runtime_config.get("gemini_model"),
    )


# Default system prompt — used when no /admin override is stored in the DB.
# The active prompt is loaded per-run via the `base_system_prompt` instructions
# function below; editing it through the /admin API does not require a restart.
DEFAULT_SYSTEM_PROMPT = """You are a helpful AI assistant chatting on WhatsApp and Telegram.
Be concise, friendly and helpful; keep replies brief and to the point.
If you don't know something, say so clearly.

Your tools are described in their own definitions. Rules for choosing between them:
- Asking to stop or start audio/voice replies, to change their language or the
  transcription language, or to stop or resume update messages is a settings change:
  call the settings tool. Deleting data: clean_user_data. You cannot do either by
  promising it; never say it is done without calling the tool.
- clean_user_data with level "data" or "all": ALWAYS say what will be deleted and wait
  for the user's yes first, however explicit the request. "messages": confirm unless
  the request is explicit.
- Recall: use what is already in the conversation first. For "what did we talk about
  yesterday" or "the last 50 messages", use get_chat_history (this chat, in order). For
  a topic mentioned some time ago, use search_conversation_history. For questions about
  uploaded documents, use search_knowledge_base and cite document name, page and section.
- Current facts (news, prices, today's events): web_search. Stable facts and
  definitions: your own knowledge or wikipedia_lookup. A link the user sent: fetch_website.
- Arithmetic: calculate. Units: convert_units. Weather: get_weather.
- No tools for greetings, chitchat, or questions the conversation already answers.
- send_whatsapp_message is only for an out-of-band note before a slow tool finishes
  ("one moment, checking..."); split a normal reply with --- instead. The send tools
  only ever reach the current chat.

Natural message bursts:
Multi-part replies (intro + answer, answer + follow-up question, distinct thoughts)
feel more human when split. Put --- on its own line between parts and each part is
sent as a separate message. Aim for 2-3 bursts. Keep lists, code blocks and
single-thought answers as one message; never put --- inside a list or code block."""


# Tools this codebase renamed or merged. A stored /admin prompt (bot_prompt)
# written for them still names them, and the model then calls tools that don't
# exist; warn_on_removed_tools logs it so the operator can edit the prompt.
REMOVED_TOOLS = {
    "update_core_memory": "memory",
    "get_user_settings": "settings",
    "update_tts_settings": "settings",
    "update_stt_settings": "settings",
    "set_broadcast_subscription": "settings",
}


def removed_tools_named(prompt: str | None) -> list[str]:
    """The removed tool names that ``prompt`` mentions, in REMOVED_TOOLS order."""
    if not prompt:
        return []
    return [name for name in REMOVED_TOOLS if re.search(rf"\b{name}\b", prompt)]


def warn_on_removed_tools(prompt: str | None) -> None:
    """Log a warning when a stored prompt names tools that no longer exist."""
    named = removed_tools_named(prompt)
    if named:
        renames = ", ".join(f"{old} -> {REMOVED_TOOLS[old]}" for old in named)
        logger.warning(
            f"⚠️ The stored system prompt (bot_prompt) names removed tools: {renames}. "
            "Edit it via PUT /admin/prompt, or the model will call tools that don't exist."
        )


@agent.instructions
async def base_system_prompt(ctx: RunContext[AgentDeps]) -> str:
    """Load the active system prompt from the DB, falling back to the default.

    Read uncached on every run, so an /admin prompt edit takes effect on the
    next message without a restart. Uses `instructions` (not `system_prompt`)
    so a changed prompt is never shadowed by one retained in message history.
    """
    return get_active_prompt(ctx.deps.db) or DEFAULT_SYSTEM_PROMPT


_FORMATTING_GUIDANCE = (
    "\n\n== FORMATTING ==\n"
    "The chat does NOT render Markdown. Use only this markup:\n"
    "- *bold* (ONE asterisk), _italic_, ~strikethrough~, `code`\n"
    "- Wrong: **text** → Right: *text*\n"
    "- Wrong: [text](https://site.com) → Right: paste the bare link, https://site.com\n"
    "- No # headings and no tables; to highlight a title, put it in *bold* on its own line\n"
    "- A line containing only --- is still how you split a reply into separate messages\n"
    "- To send an image, put ![short caption](https://...) on its own line: it is delivered "
    "as a photo. Only use a real JPEG or PNG link you got from a tool or the user, never a "
    "guessed one\n"
    "== END FORMATTING =="
)


_MARKDOWN_FORMATTING_GUIDANCE = (
    "\n\n== FORMATTING ==\n"
    "This chat is a web or desktop app that renders Markdown: use it normally "
    "(headings, lists, bold, links, tables, code blocks). Write the whole reply as "
    "one message; never use a line containing only --- to split it.\n"
    "== END FORMATTING =="
)


@agent.instructions
async def formatting_guidance(ctx: RunContext[AgentDeps]) -> str:
    """Chat-markup rule appended to the system prompt on every run.

    Lives here rather than in ``DEFAULT_SYSTEM_PROMPT`` because an ``/admin``
    prompt override (``bot_prompt`` row) replaces the default prompt wholesale,
    and this rule must survive that. It is the same for every client: WhatsApp
    renders this markup natively and the Telegram client converts it to
    Telegram HTML on the way out. ``formatting.markdown_to_whatsapp`` is the
    deterministic backstop for when the model ignores it anyway. The web
    surface (``/v1``, surfaces.py) renders Markdown and gets its own rule.
    """
    if renders_markdown(surface_of(ctx.deps.client_id, ctx.deps.whatsapp_jid)):
        return _MARKDOWN_FORMATTING_GUIDANCE
    return _FORMATTING_GUIDANCE


_LANGUAGE_GUIDANCE = (
    "\n\n== LANGUAGE ==\n"
    "Reply in the language of the message you are answering, even when the conversation "
    "so far was in another language. Never infer the language from earlier messages, "
    "other people's messages, the phone number or the timezone, and tool results "
    "written in English don't change it. A standing instruction (these instructions or "
    "your notes about this chat) sets the reply language only when it names one, "
    'e.g. "always answer in Spanish".\n'
    "== END LANGUAGE =="
)


@agent.instructions
async def language_guidance(ctx: RunContext[AgentDeps]) -> str:
    """Reply-language rule appended to the system prompt on every run.

    An instructions hook, like ``formatting_guidance``, so an ``/admin`` prompt
    override (which replaces ``DEFAULT_SYSTEM_PROMPT`` wholesale) can't drop it.
    The default prompt and every tool reply are in English while most users
    write Portuguese, so without it the model may drift to English.
    """
    return _LANGUAGE_GUIDANCE


_SHARED_GROUP_GUIDANCE = (
    "\n\n== SHARED GROUPS ==\n"
    "In a PRIVATE chat you can also reach groups that you and this user are BOTH in:\n"
    "- get_group_context: list shared groups, read a group's recent messages (limit / "
    "since_hours), or search a group's history (search_query). Only ever mention "
    "groups this tool returns; never imply knowledge of any other group.\n"
    "- send_group_message: post a message into a shared group on the user's behalf. "
    "BEFORE calling it, repeat back the exact group name and the exact text and wait "
    "for the user's explicit confirmation — a group message cannot be unsent. The "
    "message is always prefixed with who asked for it; pass the user's name as "
    "sender_name (ask for it if you don't know it).\n"
    "Neither works inside a group chat, and neither works on the WhatsApp Cloud API.\n"
    "== END SHARED GROUPS =="
)


@agent.instructions
async def shared_group_guidance(ctx: RunContext[AgentDeps]) -> str:
    """Describe the shared-group tools — only where the model has them.

    An instructions hook (not part of ``DEFAULT_SYSTEM_PROMPT``) so it follows
    the same predicate as the tools' ``prepare`` hook (the flag, private chat,
    not Cloud API) and survives an ``/admin`` prompt override.
    """
    return _SHARED_GROUP_GUIDANCE if shared_group_tools_available(ctx.deps) else ""


_BUG_REPORT_GUIDANCE = (
    "\n\n== BUG REPORTS ==\n"
    "report_bug files a report for the people who run this bot, with this chat's recent "
    "messages attached. Call it:\n"
    "- when the user asks you to report a bug or problem (category user_request);\n"
    "- when the user clearly says your answer was wrong, or is frustrated with how you "
    "handled something (user_complaint);\n"
    "- when you realise you gave wrong or made-up information (wrong_answer);\n"
    "- when a tool keeps failing for something you should be able to do (tool_failure).\n"
    "Do NOT report ordinary disagreement, matters of taste, or requests outside what you "
    "can do. File one report per issue, not one per message. Write a factual title and a "
    "description of what was asked, what happened and what was expected; never include "
    "passwords or other secrets. After filing, tell the user briefly that it was reported "
    "and give the reference — and still try to help with their actual request.\n"
    "== END BUG REPORTS =="
)


@agent.instructions
async def bug_report_guidance(ctx: RunContext[AgentDeps]) -> str:
    """Describe ``report_bug`` — only when bug reports are enabled.

    An instructions hook so it survives an ``/admin`` prompt override and
    follows BUG_REPORTS_ENABLED, which also hides the tool (``prepare`` hook).
    """
    return _BUG_REPORT_GUIDANCE if runtime_config.get("bug_reports_enabled") else ""


@agent.instructions
async def inject_core_memory(ctx: RunContext[AgentDeps]) -> str:
    """Inject the chat's core memory document, with its size, into the instructions.

    Keep it the LAST instructions hook: everything above it is the same for
    every chat, so it stays a cacheable prefix (test_language_guidance.py).
    """
    mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)
    if not mem.content:
        return ""
    # The size lets the model consolidate before it hits the limit.
    max_length = runtime_config.get("core_memory_max_length")
    return (
        f"\n\n== CORE MEMORY (your persistent notes about this chat; "
        f"{len(mem.content)}/{max_length} chars) ==\n" + mem.content + "\n== END CORE MEMORY =="
    )
