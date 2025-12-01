import os
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from .logger import logger

# Initialize Gemini via Pydantic AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")

# Create the AI agent
agent = Agent(
    model="gemini-2.5-flash",
    system_prompt="""You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.""",
)


async def get_ai_response(user_message: str, message_history=None):
    """
    Get AI response for a user message with optional history

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages

    Returns:
        AI response text
    """
    logger.info(f"Getting AI response for message: {user_message[:50]}...")

    result = await agent.run(user_message, message_history=message_history)

    logger.info(f"AI response generated: {result.output[:50]}...")
    return result.output


def format_message_history(db_messages):
    """
    Convert database messages to Pydantic AI message format

    Args:
        db_messages: List of ConversationMessage objects

    Returns:
        List of messages in Pydantic AI format
    """
    from pydantic_ai import (
        ModelMessage,
        ModelRequest,
        ModelResponse,
        UserPromptPart,
        TextPart,
    )

    formatted = []
    for msg in db_messages:
        if msg.role == "user":
            formatted.append(
                ModelRequest(parts=[UserPromptPart(content=msg.content)])
            )
        else:
            formatted.append(
                ModelResponse(parts=[TextPart(content=msg.content)])
            )

    return formatted
