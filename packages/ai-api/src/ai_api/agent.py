import os
from dataclasses import dataclass
from typing import List, Optional
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.gemini import GeminiModel
from sqlalchemy.orm import Session
from .logger import logger
from .rag.conversation import ConversationRAG
from .embeddings import EmbeddingService

# Initialize Gemini via Pydantic AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is required")


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
    recent_message_ids: List[str]
    embedding_service: Optional[EmbeddingService] = None
    conversation_rag: Optional[ConversationRAG] = None
    # Future: knowledge_base_context, etc.


# Create the AI agent with dependencies
agent = Agent(
    model="gemini-2.5-flash",
    deps_type=AgentDeps,
    system_prompt="""You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.

    You have access to a semantic search tool that can find relevant past messages.
    Use this tool when:
    - User asks about previous conversations ("What did I say about...", "Do you remember when...")
    - Query requires context from older messages beyond recent history
    - User references past topics that aren't in immediate context

    Do NOT use semantic search for:
    - Simple greetings or chitchat
    - Questions fully answerable with recent context
    - General knowledge queries

    When you receive messages marked [RELEVANT], they are semantically similar
    to the current query and should be given appropriate weight.""",
)


@agent.tool
async def search_conversation_history(
    ctx: RunContext[AgentDeps], search_query: str
) -> str:
    """
    Search through conversation history for messages related to a specific topic.

    Use this tool when the user asks about past conversations or when you need
    context from older messages that aren't in the recent history.

    Args:
        ctx: Run context with database and user info
        search_query: The topic or question to search for in past messages

    Returns:
        Formatted string with relevant past messages or error message
    """
    logger.info(f"Agent calling semantic search: '{search_query}'")

    deps = ctx.deps

    # Check if semantic search dependencies are available
    if not deps.embedding_service or not deps.conversation_rag:
        return (
            "Semantic search is not available (GEMINI_API_KEY not configured). "
            "I can only access recent messages."
        )

    # Perform semantic search
    try:
        # Generate query embedding using injected service
        query_embedding = await deps.embedding_service.generate(
            search_query,
            task_type="RETRIEVAL_QUERY",  # Different task type for queries
        )

        if not query_embedding:
            return "Failed to generate search embedding. Please try again."

        # Get configuration from environment
        result_limit = int(os.getenv("SEMANTIC_SEARCH_LIMIT", "5"))

        # Use injected RAG engine with pre-generated embedding
        messages = await deps.conversation_rag.search(
            db=deps.db,
            query_embedding=query_embedding,
            query_text=search_query,  # For logging/debugging
            limit=result_limit,
            user_id=deps.user_id,
            exclude_message_ids=deps.recent_message_ids,
        )

        if not messages:
            logger.info("No relevant past messages found.")
            return (
                f"No relevant past messages found for: {search_query}. "
                "Either we haven't discussed this topic, or messages are too old/dissimilar."
            )

        logger.info(
            f"Found {len(messages)} relevant past messages for query: '{search_query}'"
        )

        logger.info(f"Messages: {[msg['matched_message'].content for msg in messages]}")
        # Format results with context
        return deps.conversation_rag.format_results(messages)

    except Exception as e:
        logger.error(f"Error in semantic search: {str(e)}", exc_info=True)
        return f"Error searching conversation history: {str(e)}"


async def get_ai_response(
    user_message: str, message_history=None, agent_deps: AgentDeps = None
):
    """
    Stream AI response token by token for a user message with optional history

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages
        agent_deps: Optional dependencies for agent tools (enables semantic search)

    Yields:
        str: Text chunks as they arrive from Gemini
    """
    logger.info(f"Getting AI response for message: {user_message[:50]}...")

    # Use async context manager to enter streaming context
    async with agent.run_stream(
        user_message, message_history=message_history, deps=agent_deps
    ) as result:
        # Call .stream_text(delta=True) to get incremental deltas (NOT cumulative text)
        async for text_chunk in result.stream_text(delta=True):
            yield text_chunk

    logger.info("AI response streaming completed")


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
