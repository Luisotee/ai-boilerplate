from pydantic_ai import RunContext

from ...logger import logger
from ...rag.conversation import (
    format_conversation_results,
)
from ...rag.conversation import (
    search_conversation_history as search_conversation_fn,
)
from ...rag.knowledge_base import (
    format_knowledge_base_results,
)
from ...rag.knowledge_base import (
    search_knowledge_base as search_kb_fn,
)
from ..core import AgentDeps, agent


@agent.tool
async def search_conversation_history(ctx: RunContext[AgentDeps], search_query: str) -> str:
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
    logger.info("🔍 TOOL CALLED: search_conversation_history")
    logger.info(f"   Query: '{search_query}'")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info("=" * 80)

    deps = ctx.deps

    # Check if semantic search dependencies are available
    if not deps.embedding_service:
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

        # Call pure function for semantic search (uses env defaults)
        messages = await search_conversation_fn(
            db=deps.db,
            query_embedding=query_embedding,
            user_id=deps.user_id,
            query_text=search_query,  # For logging/debugging
            exclude_message_ids=deps.recent_message_ids,
            # Omit limit, similarity_threshold, context_window to use env defaults
        )

        if not messages:
            logger.info("No relevant past messages found.")
            return (
                f"No relevant past messages found for: {search_query}. "
                "Either we haven't discussed this topic, or messages are too old/dissimilar."
            )

        logger.info(f"Found {len(messages)} relevant past messages for query: '{search_query}'")

        # Format results with context using pure function
        formatted_results = format_conversation_results(messages)

        # Log detailed results for debugging
        logger.info(f"Conversation RAG returned {len(messages)} results:")
        for i, msg in enumerate(messages, 1):
            logger.info(f"  [{i}] Similarity: {msg.get('similarity_score', 'N/A'):.3f}")
            logger.info(f"      Full content: {msg['matched_message'].content}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results:\n{formatted_results}")

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: search_conversation_history")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in semantic search: {str(e)}", exc_info=True)
        error_msg = f"Error searching conversation history: {str(e)}"
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: search_conversation_history")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg


@agent.tool
async def search_knowledge_base(ctx: RunContext[AgentDeps], search_query: str) -> str:
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
    logger.info("📚 TOOL CALLED: search_knowledge_base")
    logger.info(f"   Query: '{search_query}'")
    logger.info("=" * 80)

    deps = ctx.deps

    # Check if knowledge base dependencies are available
    if not deps.embedding_service:
        return (
            "Knowledge base search is not available (GEMINI_API_KEY not configured). "
            "I can only answer based on general knowledge."
        )

    try:
        # Generate query embedding
        query_embedding = await deps.embedding_service.generate(
            search_query,
            task_type="RETRIEVAL_QUERY",  # Different task type for queries
        )

        if not query_embedding:
            return "Failed to generate search embedding. Please try again."

        # Call pure function for knowledge base search (uses env defaults)
        # Pass whatsapp_jid to include conversation-scoped documents
        results = await search_kb_fn(
            db=deps.db,
            query_embedding=query_embedding,
            query_text=search_query,
            whatsapp_jid=deps.whatsapp_jid,
            # Omit limit and similarity_threshold to use env defaults
        )

        if not results:
            logger.info("No relevant documents found in knowledge base")
            return (
                f"No relevant information found in the knowledge base for: {search_query}. "
                "This topic may not be covered in uploaded documents."
            )

        logger.info(f"Found {len(results)} relevant passages from knowledge base")

        # Format results with citations using pure function
        formatted_results = format_knowledge_base_results(results)

        # Log detailed results for debugging
        logger.info(f"Knowledge Base RAG returned {len(results)} results:")
        for i, result in enumerate(results, 1):
            chunk = result["chunk"]
            doc = result["document"]
            similarity = result["similarity_score"]
            logger.info(
                f"  [{i}] Document: {doc['original_filename']} | "
                f"Similarity: {similarity:.3f} | "
                f"Page: {chunk.get('page_number', 'N/A')} | "
                f"Tokens: {chunk.get('token_count', 'N/A')}"
            )
            logger.info(f"      Raw content (before cleaning):\n{chunk['content']}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results (after cleaning):\n{formatted_results}")

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: search_knowledge_base")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in knowledge base search: {str(e)}", exc_info=True)
        error_msg = f"Error searching knowledge base: {str(e)}"
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: search_knowledge_base")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg
