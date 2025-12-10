import os
from dataclasses import dataclass
from typing import List, Optional
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.gemini import GeminiModel
from sqlalchemy.orm import Session
from .logger import logger
from .rag.conversation import ConversationRAG
from .rag.knowledge_base import KnowledgeBaseRAG
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
    knowledge_base_rag: Optional[KnowledgeBaseRAG] = None


# Create the AI agent with dependencies
agent = Agent(
    model="gemini-2.5-flash",
    deps_type=AgentDeps,
    system_prompt="""You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.

    You have access to TWO search tools:

    1. **search_conversation_history** - Searches past messages with this user
       Use when:
       - User asks about previous conversations ("What did I say about...", "Do you remember when...")
       - Query requires context from older messages beyond recent history
       - User references past topics that aren't in immediate context

    2. **search_knowledge_base** - Searches uploaded PDF documents
       Use when:
       - User asks factual questions that might be in documentation
       - User asks about manuals, guides, or reference materials
       - User wants information from uploaded documents
       - Always cite sources: "According to [Document Name] (page X)..."

    Do NOT use search tools for:
    - Simple greetings or chitchat
    - Questions fully answerable with recent context
    - General knowledge queries (use your training instead)

    When citing knowledge base sources, ALWAYS include:
    - Document name
    - Page number (if available)
    - Section heading (if available)
    Example: "According to the User Manual (page 42, section 'Installation')..."

    When you receive search results, they are semantically similar and should be given appropriate weight.""",
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
    logger.info("=" * 80)
    logger.info(f"🔍 TOOL CALLED: search_conversation_history")
    logger.info(f"   Query: '{search_query}'")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info("=" * 80)

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

        # Format results with context
        formatted_results = deps.conversation_rag.format_results(messages)

        # Log detailed results for debugging
        logger.info(f"Conversation RAG returned {len(messages)} results:")
        for i, msg in enumerate(messages, 1):
            logger.info(f"  [{i}] Similarity: {msg.get('similarity_score', 'N/A'):.3f}")
            logger.info(f"      Full content: {msg['matched_message'].content}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results:\n{formatted_results}")

        logger.info("=" * 80)
        logger.info(f"✅ TOOL RETURNING: search_conversation_history")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in semantic search: {str(e)}", exc_info=True)
        error_msg = f"Error searching conversation history: {str(e)}"
        logger.info("=" * 80)
        logger.info(f"❌ TOOL ERROR: search_conversation_history")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg


@agent.tool
async def search_knowledge_base(
    ctx: RunContext[AgentDeps], search_query: str
) -> str:
    """
    Search the knowledge base for information from uploaded documents.

    Use this tool when the user asks questions that might be answered by
    documentation, manuals, guides, or other reference materials in the knowledge base.

    DO NOT use this tool for:
    - Questions about past conversations (use search_conversation_history instead)
    - Simple greetings or chitchat
    - Questions that require real-time information

    Args:
        ctx: Run context with database and embedding service
        search_query: The question or topic to search for in documents

    Returns:
        Formatted string with relevant document passages and citations
    """
    logger.info("=" * 80)
    logger.info(f"📚 TOOL CALLED: search_knowledge_base")
    logger.info(f"   Query: '{search_query}'")
    logger.info("=" * 80)

    deps = ctx.deps

    # Check if knowledge base dependencies are available
    if not deps.embedding_service or not deps.knowledge_base_rag:
        return (
            "Knowledge base search is not available (GEMINI_API_KEY not configured). "
            "I can only answer based on general knowledge."
        )

    try:
        # Generate query embedding
        query_embedding = await deps.embedding_service.generate(
            search_query,
            task_type="RETRIEVAL_QUERY"  # Different task type for queries
        )

        if not query_embedding:
            return "Failed to generate search embedding. Please try again."

        # Search knowledge base
        result_limit = int(os.getenv("KB_SEARCH_LIMIT", "5"))
        results = await deps.knowledge_base_rag.search(
            db=deps.db,
            query_embedding=query_embedding,
            query_text=search_query,
            limit=result_limit
        )

        if not results:
            logger.info("No relevant documents found in knowledge base")
            return (
                f"No relevant information found in the knowledge base for: {search_query}. "
                "This topic may not be covered in uploaded documents."
            )

        logger.info(f"Found {len(results)} relevant passages from knowledge base")

        # Format results with citations
        formatted_results = deps.knowledge_base_rag.format_results(results)

        # Log detailed results for debugging
        logger.info(f"Knowledge Base RAG returned {len(results)} results:")
        for i, result in enumerate(results, 1):
            chunk = result['chunk']
            doc = result['document']
            similarity = result['similarity_score']
            logger.info(f"  [{i}] Document: {doc['original_filename']} | "
                       f"Similarity: {similarity:.3f} | "
                       f"Page: {chunk.get('page_number', 'N/A')} | "
                       f"Tokens: {chunk.get('token_count', 'N/A')}")
            logger.info(f"      Raw content (before cleaning):\n{chunk['content']}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results (after cleaning):\n{formatted_results}")

        logger.info("=" * 80)
        logger.info(f"✅ TOOL RETURNING: search_knowledge_base")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in knowledge base search: {str(e)}", exc_info=True)
        error_msg = f"Error searching knowledge base: {str(e)}"
        logger.info("=" * 80)
        logger.info(f"❌ TOOL ERROR: search_knowledge_base")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg


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
    logger.info("=" * 80)
    logger.info(f"🤖 AGENT STARTING")
    logger.info(f"   User message: {user_message}")
    logger.info(f"   History messages: {len(message_history) if message_history else 0}")
    logger.info(f"   Has dependencies: {agent_deps is not None}")
    if agent_deps:
        logger.info(f"   - Embedding service: {agent_deps.embedding_service is not None}")
        logger.info(f"   - Conversation RAG: {agent_deps.conversation_rag is not None}")
        logger.info(f"   - Knowledge Base RAG: {agent_deps.knowledge_base_rag is not None}")
    logger.info("=" * 80)

    # Track full response for logging
    full_response = ""

    # Use async context manager to enter streaming context
    async with agent.run_stream(
        user_message, message_history=message_history, deps=agent_deps
    ) as result:
        # Call .stream_text(delta=True) to get incremental deltas (NOT cumulative text)
        async for text_chunk in result.stream_text(delta=True):
            full_response += text_chunk
            yield text_chunk

    logger.info("=" * 80)
    logger.info(f"✅ AGENT COMPLETED")
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
