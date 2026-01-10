from dataclasses import dataclass

import httpx
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from sqlalchemy.orm import Session

from .config import settings
from .embeddings import EmbeddingService
from .logger import logger
from .rag.conversation import (
    format_conversation_results,
)
from .rag.conversation import (
    search_conversation_history as search_conversation_fn,
)
from .rag.knowledge_base import (
    format_knowledge_base_results,
)
from .rag.knowledge_base import (
    search_knowledge_base as search_kb_fn,
)
from .whatsapp import WhatsAppClient


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


# Create Google provider and model with API key from settings
google_provider = GoogleProvider(api_key=settings.gemini_api_key)
google_model = GoogleModel("gemini-2.5-flash-lite", provider=google_provider)

# Create the AI agent with dependencies
agent = Agent(
    model=google_model,
    deps_type=AgentDeps,
    system_prompt="""You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.

    You have access to search tools and WhatsApp action tools:

    **Search Tools:**
    1. **search_conversation_history** - Searches past messages with this user
       Use when user asks about previous conversations or references past topics

    2. **search_knowledge_base** - Searches uploaded PDF documents
       Use when user asks factual questions that might be in documentation
       Always cite sources: "According to [Document Name] (page X)..."

    **WhatsApp Action Tools:**
    3. **send_whatsapp_reaction** - React to the user's message with an emoji
       Use when the message warrants an emotional response or acknowledgment
       Common: 👍 (approval), ❤️ (love/thanks), 😂 (funny), 😮 (surprised)

    4. **send_whatsapp_location** - Send a location with coordinates
       Use when sharing a place would be helpful (directions, recommendations)

    5. **send_whatsapp_contact** - Send a contact card
       Use when sharing contact information (support numbers, business contacts)

    6. **send_whatsapp_message** - Send an additional text message
       Use sparingly - only for follow-up messages separate from your main response

    **When NOT to use tools:**
    - Simple greetings or chitchat (no tools needed)
    - Questions fully answerable with recent context (no search needed)
    - General knowledge queries (use your training)

    **Important:** WhatsApp tools only send to the current conversation. You cannot message other users.

    When citing knowledge base sources, ALWAYS include document name, page number, and section heading.""",
)


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
        results = await search_kb_fn(
            db=deps.db,
            query_embedding=query_embedding,
            query_text=search_query,
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


# =============================================================================
# WhatsApp Action Tools
# =============================================================================


@agent.tool
async def send_whatsapp_reaction(ctx: RunContext[AgentDeps], emoji: str) -> str:
    """
    React to the user's message with an emoji.

    Use this when the user says something that warrants an emotional response,
    or to acknowledge receipt while working on a longer response.

    Common reactions:
    - 👍 for approval, agreement, or acknowledgment
    - ❤️ for love, appreciation, or thanks
    - 😂 for something funny
    - 😮 for surprise or amazement
    - 🙏 for gratitude or prayer

    Args:
        ctx: Run context with WhatsApp client and message info
        emoji: The emoji to react with (e.g., "👍", "❤️", "😂")

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("💬 TOOL CALLED: send_whatsapp_reaction")
    logger.info(f"   Emoji: {emoji}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info(f"   Message ID: {ctx.deps.current_message_id}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send reaction."

    if not deps.current_message_id:
        return "No message ID available to react to."

    try:
        await deps.whatsapp_client.send_reaction(
            phone_number=deps.whatsapp_jid,
            message_id=deps.current_message_id,
            emoji=emoji,
        )

        logger.info(f"✅ Reaction {emoji} sent successfully")
        return f"Reaction {emoji} sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send reaction: {e}")
        return f"Failed to send reaction: {str(e)}"


@agent.tool
async def send_whatsapp_location(
    ctx: RunContext[AgentDeps],
    latitude: float,
    longitude: float,
    name: str | None = None,
    address: str | None = None,
) -> str:
    """
    Send a location to the user via WhatsApp.

    Use this when the user asks for directions, wants to know where something is,
    or when sharing a place would be helpful.

    Args:
        ctx: Run context with WhatsApp client
        latitude: Latitude coordinate (-90 to 90)
        longitude: Longitude coordinate (-180 to 180)
        name: Optional name for the location (e.g., "Eiffel Tower")
        address: Optional address string

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("📍 TOOL CALLED: send_whatsapp_location")
    logger.info(f"   Coordinates: {latitude}, {longitude}")
    logger.info(f"   Name: {name}")
    logger.info(f"   Address: {address}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send location."

    # Validate coordinates
    if not -90 <= latitude <= 90:
        return f"Invalid latitude: {latitude}. Must be between -90 and 90."
    if not -180 <= longitude <= 180:
        return f"Invalid longitude: {longitude}. Must be between -180 and 180."

    try:
        await deps.whatsapp_client.send_location(
            phone_number=deps.whatsapp_jid,
            latitude=latitude,
            longitude=longitude,
            name=name,
            address=address,
        )

        location_desc = name or f"{latitude}, {longitude}"
        logger.info(f"✅ Location '{location_desc}' sent successfully")
        return f"Location '{location_desc}' sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send location: {e}")
        return f"Failed to send location: {str(e)}"


@agent.tool
async def send_whatsapp_contact(
    ctx: RunContext[AgentDeps],
    contact_name: str,
    contact_phone: str,
    contact_email: str | None = None,
    contact_organization: str | None = None,
) -> str:
    """
    Send a contact card (vCard) to the user via WhatsApp.

    Use this when sharing contact information would be helpful,
    such as business contacts, support numbers, etc.

    Args:
        ctx: Run context with WhatsApp client
        contact_name: Full name of the contact
        contact_phone: Phone number with country code (e.g., "+1234567890")
        contact_email: Optional email address
        contact_organization: Optional company/organization name

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("👤 TOOL CALLED: send_whatsapp_contact")
    logger.info(f"   Contact: {contact_name} ({contact_phone})")
    logger.info(f"   Email: {contact_email}")
    logger.info(f"   Organization: {contact_organization}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send contact."

    try:
        await deps.whatsapp_client.send_contact(
            phone_number=deps.whatsapp_jid,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            contact_org=contact_organization,
        )

        logger.info(f"✅ Contact '{contact_name}' sent successfully")
        return f"Contact card for '{contact_name}' sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send contact: {e}")
        return f"Failed to send contact: {str(e)}"


@agent.tool
async def send_whatsapp_message(ctx: RunContext[AgentDeps], text: str) -> str:
    """
    Send an additional text message to the user via WhatsApp.

    Use this sparingly - your main response is automatically sent.
    Only use this for:
    - Follow-up information that should be in a separate message
    - Sending multiple distinct pieces of information

    Do NOT use this for your primary response to the user.

    Args:
        ctx: Run context with WhatsApp client
        text: The message text to send

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("📝 TOOL CALLED: send_whatsapp_message")
    logger.info(f"   Text: {text[:100]}...")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send message."

    if not text.strip():
        return "Cannot send empty message."

    try:
        result = await deps.whatsapp_client.send_text(
            phone_number=deps.whatsapp_jid,
            text=text,
        )

        logger.info(f"✅ Message sent successfully (ID: {result.message_id})")
        return "Message sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send message: {e}")
        return f"Failed to send message: {str(e)}"


async def get_ai_response(user_message: str, message_history=None, agent_deps: AgentDeps = None):
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
    logger.info("🤖 AGENT STARTING")
    logger.info(f"   User message: {user_message}")
    logger.info(f"   History messages: {len(message_history) if message_history else 0}")
    logger.info(f"   Has dependencies: {agent_deps is not None}")
    if agent_deps:
        logger.info(f"   - Embedding service: {agent_deps.embedding_service is not None}")
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
