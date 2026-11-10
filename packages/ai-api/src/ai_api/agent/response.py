import base64

from pydantic_ai import AgentRunResultEvent, BinaryContent, RunUsage

from ..logger import logger
from .core import AgentDeps, agent, build_runtime_model


async def get_ai_response(
    user_message: str,
    message_history=None,
    agent_deps: AgentDeps = None,
    image_data: str | None = None,
    image_mimetype: str | None = None,
):
    """
    Run the agent for a user message (with optional history) and yield its reply

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages
        agent_deps: Optional dependencies for agent tools (enables semantic search)
        image_data: Optional base64-encoded image data for vision
        image_mimetype: Optional image MIME type (e.g., 'image/jpeg')

    Yields:
        str: The final reply text, once the run (including every tool call) finishes
    """
    has_image = image_data is not None and image_mimetype is not None

    logger.info("=" * 80)
    logger.info("🤖 AGENT STARTING")
    logger.info(f"   User message: {user_message}")
    logger.info(f"   History messages: {len(message_history) if message_history else 0}")
    logger.info(f"   Has image: {has_image}")
    logger.info(f"   Has dependencies: {agent_deps is not None}")
    if agent_deps:
        logger.info(f"   - Embedding service: {agent_deps.embedding_service is not None}")
    logger.info("=" * 80)

    # Construct the prompt - either text only or text + image
    if has_image:
        # Decode base64 image and create BinaryContent
        image_bytes = base64.b64decode(image_data)
        prompt = [
            user_message,
            BinaryContent(data=image_bytes, media_type=image_mimetype),
        ]
        logger.info(f"   Image size: {len(image_bytes)} bytes, type: {image_mimetype}")
    else:
        prompt = user_message

    # Track full response for logging
    full_response = ""
    usage: RunUsage | None = None

    # NOT agent.run_stream: it treats the first text as the final output and skips any
    # tool call made after it in the same response. DeepSeek often writes a preamble
    # ("Let me check...") and then calls a tool, so the user got only the
    # preamble. run_stream_events runs every tool call; only the final output is sent.
    # Model requests are still streamed, so GuardedModel's first-chunk timeout holds.
    async for event in agent.run_stream_events(
        prompt,
        message_history=message_history,
        deps=agent_deps,
        model=build_runtime_model(),  # honor /admin model overrides per-run
    ):
        if isinstance(event, AgentRunResultEvent):
            full_response = event.result.output
            usage = event.result.usage()
            yield full_response

    if isinstance(usage, RunUsage):
        try:
            log_usage(usage)
        except Exception:  # accounting must never cost the user their reply
            logger.warning("Could not log agent usage", exc_info=True)

    logger.info("=" * 80)
    logger.info("✅ AGENT COMPLETED")
    logger.info(f"   Final response length: {len(full_response)} characters")
    logger.info(f"   Full response:\n{full_response}")
    logger.info("=" * 80)


def cached_input_tokens(usage: RunUsage) -> int:
    """Input tokens the provider served from its prompt cache in this run.

    Checked against pydantic-ai 1.61, per provider, summed over the run's
    requests by ``RunUsage.incr``:
      - DeepSeek (``models/openai.py`` ``_map_usage``): ``prompt_cache_hit_tokens``
        always lands in ``details``; ``cache_read_tokens`` only gets the same
        count when the reply also has ``prompt_tokens_details.cached_tokens``.
      - Gemini (``models/google.py`` ``_metadata_as_usage``): ``details``
        ``cached_content_tokens``, which genai-prices also maps to
        ``cache_read_tokens``.
    The two ``details`` fields never describe the same request, so their sum
    counts a run that fell back from DeepSeek to Gemini midway; each overlaps
    ``cache_read_tokens``, so that one is only a floor (other providers). A
    None value (a provider sending null) counts as 0.
    """
    details = usage.details or {}
    per_provider = (details.get("prompt_cache_hit_tokens") or 0) + (
        details.get("cached_content_tokens") or 0
    )
    return max(usage.cache_read_tokens or 0, per_provider)


def log_usage(usage: RunUsage) -> None:
    """One line per run with token counts only (never content), for cache tuning.

    Logfire, when on, records the same per model request as
    ``gen_ai.usage.*`` span attributes; this line also works without it.
    """
    cached = cached_input_tokens(usage)
    share = cached / usage.input_tokens if usage.input_tokens else 0.0
    logger.info(
        f"📊 Agent usage: requests={usage.requests} input_tokens={usage.input_tokens} "
        f"cached_input_tokens={cached} ({share:.0%}) output_tokens={usage.output_tokens}"
    )


def format_message_history(db_messages):
    """
    Convert database messages to Pydantic AI message format

    Args:
        db_messages: List of ConversationMessage objects

    Returns:
        List of messages in Pydantic AI format
    """
    from pydantic_ai import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    formatted = []
    for msg in db_messages:
        if msg.role == "user":
            formatted.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        else:
            formatted.append(ModelResponse(parts=[TextPart(content=msg.content)]))

    return formatted
