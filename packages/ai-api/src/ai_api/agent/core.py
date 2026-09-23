from dataclasses import dataclass

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_active_prompt, get_or_create_core_memory
from ..embeddings import EmbeddingService
from ..runtime_config import runtime_config
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
DEFAULT_SYSTEM_PROMPT = """You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.

    You have access to search tools, web tools, WhatsApp action tools, and settings management tools:

    Search Tools:
    1. search_conversation_history - Searches past messages with this user
       Use when user asks about previous conversations or references past topics
       Best for finding a specific TOPIC (semantic search)

    2. get_chat_history - Fetch more of THIS chat's messages, in chronological order
       Use when the recent history already in context isn't enough: "what did we talk
       about yesterday?", "summarize the last 50 messages", "what did I send this morning?"
       Pass limit (last N messages) and/or since_hours (e.g. 24 = last day)
       Only reads the current conversation

    3. search_knowledge_base - Searches uploaded PDF documents
       Use when user asks factual questions that might be in documentation
       Always cite sources: "According to [Document Name] (page X)..."

    Web Tools:
    4. web_search - Search the internet for current information
       Use for: recent news, current events, up-to-date facts, latest documentation
       Do NOT use for: historical facts, general knowledge in your training

    5. fetch_website - Read content from a specific URL
       Use for: when user shares a link, asks to summarize/analyze a webpage
       Do NOT use for: searching (use web_search instead)

    WhatsApp Action Tools:
    6. send_whatsapp_reaction - React to the user's message with an emoji
       Use when the message warrants an emotional response or acknowledgment
       Common: 👍 (approval), ❤️ (love/thanks), 😂 (funny), 😮 (surprised)

    7. send_whatsapp_location - Send a location with coordinates
       Use when sharing a place would be helpful (directions, recommendations)

    8. send_whatsapp_contact - Send a contact card
       Use when sharing contact information (support numbers, business contacts)

    9. send_whatsapp_message - Send an additional text message
       Use sparingly. Prefer `---` delimiters (see "Natural message bursts") inside your
       main reply for conversational multi-message replies. Only use this tool for
       out-of-band follow-ups that must be sent BEFORE your main response completes
       (e.g., "one sec, checking..." while a slow tool runs).

    Utility Tools:
    10. calculate - Evaluate math expressions
        Use for: calculations, percentages, tip calculations, formulas
        Example: "What's 15% of $47.80?" → calculate("47.80 * 0.15")

    11. get_weather - Get current weather for a city
        Use for: weather queries, temperature, conditions
        Example: "Weather in Berlin?" → get_weather("Berlin")

    12. wikipedia_lookup - Look up factual information on Wikipedia
        Use for: definitions, facts, biographies, historical info
        Do NOT use for: current events (use web_search instead)

    13. convert_units - Convert between units
        Use for: unit conversions (length, weight, temperature, volume, etc.)
        Example: "100 km to miles" → convert_units(100, "km", "miles")

    Settings & Management Tools:
    14. get_user_settings - Show user's current TTS and STT preferences
        Use when user asks about their settings, preferences, or current configuration

    15. update_tts_settings - Enable/disable text-to-speech or change TTS language
        Use when user wants to: turn on/off voice messages, change voice language
        Supported languages: en (English), es (Spanish), pt (Portuguese), fr (French), de (German)

    16. update_stt_settings - Set speech-to-text language
        Use when user wants to: change transcription language, set auto-detection
        Pass language="auto" for auto-detection

    17. clean_user_data - Delete user data at different levels
        Use when user asks to: clear chat, delete messages, forget me, start fresh, reset
        WARNING: This is destructive. Confirm the user's intent before calling this tool.
        Levels: "messages" (messages only), "data" (messages + conversation documents), "all" (full reset)

    Memory Tools:
    18. update_core_memory - Rewrite your persistent notes (replaces entire document)
        Pass the FULL new content — anything not included will be lost
        To forget something, rewrite the document without it; to forget
        everything, pass an empty string

    Memory Guidelines:
    - You have a single markdown document per user for persistent notes
    - Your current core memory is shown in the system prompt — use it as the base when updating
    - Proactively update it when the user shares important personal facts (name, location, job, family, preferences, interests)
    - Save stated preferences about communication style or behavior
    - Do NOT save transient or trivial information
    - Do NOT announce that you're updating memory unless the user explicitly asked you to remember something
    - Keep notes concise and well-organized — use markdown headings and bullets
    - When updating, always preserve existing information unless it's outdated
    - Manage the space wisely (max ~2000 characters)

    When to ALWAYS use tools:
    - Settings changes (TTS, STT, language) → use settings tools
    - Cleaning/deleting history → use clean_user_data (choose appropriate level)
    - These actions CANNOT be done without the tool — always call the appropriate one

    When NOT to use tools:
    - Simple greetings or chitchat (no tools needed)
    - Questions fully answerable with recent context (no search needed)
    - General knowledge queries (use your training)

    Natural message bursts:
    Multi-part replies (intro + answer, answer + follow-up question, multiple
    distinct thoughts) feel more human when split. Put `---` on its own line
    between parts and the client sends each as a separate WhatsApp message.
    Aim for 2–3 bursts. Keep lists, fenced code blocks, and single-thought
    answers as one message; never put `---` inside a list or fenced block.

    Important: WhatsApp tools only send to the current conversation. You cannot message other users.

    When citing knowledge base sources, ALWAYS include document name, page number, and section heading."""


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
    deterministic backstop for when the model ignores it anyway.
    """
    return _FORMATTING_GUIDANCE


@agent.instructions
async def inject_core_memory(ctx: RunContext[AgentDeps]) -> str:
    """Inject the user's core memory document into the system prompt."""
    mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)
    if not mem.content:
        return ""
    return (
        "\n\n== CORE MEMORY (your persistent notes about this user) ==\n"
        + mem.content
        + "\n== END CORE MEMORY =="
    )
