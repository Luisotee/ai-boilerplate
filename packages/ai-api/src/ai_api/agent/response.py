import base64

from pydantic_ai import BinaryContent

from ..logger import logger
from .core import AgentDeps, agent


async def get_ai_response(
    user_message: str,
    message_history=None,
    agent_deps: AgentDeps = None,
    image_data: str | None = None,
    image_mimetype: str | None = None,
):
    """
    Stream AI response token by token for a user message with optional history

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages
        agent_deps: Optional dependencies for agent tools (enables semantic search)
        image_data: Optional base64-encoded image data for vision
        image_mimetype: Optional image MIME type (e.g., 'image/jpeg')

    Yields:
        str: Text chunks as they arrive from Gemini
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

    # Use async context manager to enter streaming context
    async with agent.run_stream(prompt, message_history=message_history, deps=agent_deps) as result:
        # Call .stream_text(delta=True) to get incremental deltas (NOT cumulative text)
        async for text_chunk in result.stream_text(delta=True):
            full_response += text_chunk
            yield text_chunk

    logger.info("=" * 80)
    logger.info("✅ AGENT COMPLETED")
    logger.info(f"   Final response length: {len(full_response)} characters")
    logger.info(f"   Full response:\n{full_response}")
    logger.info("=" * 80)


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
