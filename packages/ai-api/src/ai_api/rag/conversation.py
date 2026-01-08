"""
Conversation history RAG implementation.

Provides semantic search over user's conversation history using vector similarity.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..database import ConversationMessage
from ..logger import logger


def get_context_messages(
    db: Session, matched_message: ConversationMessage, window_size: int
) -> dict:
    """
    Retrieve messages before and after a matched message for context.

    Args:
        db: Database session
        matched_message: The semantically matched message
        window_size: Number of messages to retrieve before/after

    Returns:
        dict with 'before', 'match', 'after' message lists
    """
    # Query messages from same user
    # Get window_size messages before the match (by timestamp)
    messages_before = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.user_id == matched_message.user_id,
            ConversationMessage.timestamp < matched_message.timestamp,
        )
        .order_by(ConversationMessage.timestamp.desc())
        .limit(window_size)
        .all()
    )

    # Reverse to get chronological order
    messages_before = list(reversed(messages_before))

    # Get window_size messages after the match
    messages_after = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.user_id == matched_message.user_id,
            ConversationMessage.timestamp > matched_message.timestamp,
        )
        .order_by(ConversationMessage.timestamp.asc())
        .limit(window_size)
        .all()
    )

    logger.info(
        f"Context window: {len(messages_before)} before, {len(messages_after)} after (window_size={window_size})"
    )

    return {
        "before": messages_before,
        "match": matched_message,
        "after": messages_after,
    }


async def search_conversation_history(
    db: Session,
    query_embedding: list[float],
    user_id: str,
    query_text: str = None,
    limit: int = None,
    exclude_message_ids: list[str] | None = None,
    include_context: bool = True,
    similarity_threshold: float = None,
    context_window: int = None,
    **kwargs,
) -> list[dict]:
    """
    Search for semantically similar messages with optional context window.

    Args:
        db: Database session
        query_embedding: Pre-generated embedding vector for the query
        query_text: Optional query text (for logging only)
        limit: Maximum results to return (default from SEMANTIC_SEARCH_LIMIT env)
        user_id: User ID to search within (required)
        exclude_message_ids: Message IDs to exclude (e.g., recent window)
        include_context: Whether to include surrounding messages (default True)
        similarity_threshold: Minimum similarity score (default from SEMANTIC_SIMILARITY_THRESHOLD env)
        context_window: Number of messages before/after (default from SEMANTIC_CONTEXT_WINDOW env)
        **kwargs: Additional parameters

    Returns:
        List of dicts, each containing:
        - 'messages_before': List of messages before the match
        - 'matched_message': The semantically matched message
        - 'messages_after': List of messages after the match
        - 'similarity_score': Cosine similarity score
    """
    # Apply defaults from settings if not provided
    if similarity_threshold is None:
        similarity_threshold = settings.semantic_similarity_threshold
    if limit is None:
        limit = settings.semantic_search_limit
    if context_window is None:
        context_window = settings.semantic_context_window

    if not user_id:
        logger.error("user_id is required for conversation search")
        return []

    if not query_embedding:
        logger.error("query_embedding is required for search")
        return []

    query_preview = query_text[:50] if query_text else "embedding"
    logger.info(f"Semantic search for user {user_id}: '{query_preview}...' (limit: {limit})")

    # Build exclusion clause
    exclude_clause = ""
    params = {
        "user_id": user_id,
        "embedding": query_embedding,
        "limit": limit,
        "threshold": similarity_threshold,
    }

    if exclude_message_ids:
        # Convert to tuple for SQL IN clause
        exclude_clause = "AND id NOT IN :exclude_ids"
        params["exclude_ids"] = tuple(str(id) for id in exclude_message_ids)

    # Vector similarity query using cosine distance
    # pgvector uses <=> for cosine distance (lower = more similar)
    # We convert to similarity score: 1 - distance
    # NOTE: Cast :embedding to vector type for pgvector compatibility
    query_sql = text(f"""
        SELECT
            id,
            user_id,
            role,
            content,
            sender_jid,
            sender_name,
            timestamp,
            embedding,
            embedding_generated_at,
            (1 - (embedding <=> CAST(:embedding AS vector))) AS similarity
        FROM conversation_messages
        WHERE user_id = :user_id
          AND embedding IS NOT NULL
          {exclude_clause}
          AND (1 - (embedding <=> CAST(:embedding AS vector))) >= :threshold
        ORDER BY similarity DESC
        LIMIT :limit
    """)

    result = db.execute(query_sql, params)
    rows = result.fetchall()

    logger.info(f"Semantic search found {len(rows)} results (threshold: {similarity_threshold})")

    # Convert to ConversationMessage objects with context
    results = []
    for row in rows:
        msg = ConversationMessage(
            id=row.id,
            user_id=row.user_id,
            role=row.role,
            content=row.content,
            sender_jid=row.sender_jid,
            sender_name=row.sender_name,
            timestamp=row.timestamp,
            embedding=row.embedding,
            embedding_generated_at=row.embedding_generated_at,
        )
        # Attach similarity score as metadata
        msg._similarity_score = float(row.similarity)

        logger.debug(f"  - [{row.role}] (similarity: {row.similarity:.3f})\n{row.content}")

        if include_context and context_window > 0:
            # Get surrounding context
            context = get_context_messages(db, msg, context_window)
            results.append(
                {
                    "messages_before": context["before"],
                    "matched_message": msg,
                    "messages_after": context["after"],
                    "similarity_score": msg._similarity_score,
                }
            )
        else:
            # No context - return just the matched message (backward compatible)
            results.append(
                {
                    "messages_before": [],
                    "matched_message": msg,
                    "messages_after": [],
                    "similarity_score": msg._similarity_score,
                }
            )

    return results


def format_conversation_results(results: list[dict]) -> str:
    """
    Format conversation messages with context for agent consumption.

    Args:
        results: List of dicts with 'messages_before', 'matched_message', 'messages_after'

    Returns:
        Formatted string with conversation snippets
    """
    if not results:
        return "No relevant messages found in conversation history."

    formatted_snippets = []

    for i, result in enumerate(results, 1):
        snippet_parts = []

        # Header for this match
        similarity = result["similarity_score"]
        snippet_parts.append(f"=== Match {i} (similarity: {similarity:.2f}) ===\n")

        # Format messages before (if any)
        if result["messages_before"]:
            snippet_parts.append("Context before:")
            for msg in result["messages_before"]:
                snippet_parts.append(format_conversation_message(msg, is_match=False))
            snippet_parts.append("")

        # Format the matched message (highlighted)
        matched = result["matched_message"]
        snippet_parts.append("→ MATCHED MESSAGE:")
        snippet_parts.append(format_conversation_message(matched, is_match=True))
        snippet_parts.append("")

        # Format messages after (if any)
        if result["messages_after"]:
            snippet_parts.append("Context after:")
            for msg in result["messages_after"]:
                snippet_parts.append(format_conversation_message(msg, is_match=False))

        formatted_snippets.append("\n".join(snippet_parts))

    result = "\n\n".join(formatted_snippets)
    return f"Found {len(results)} relevant conversation snippets:\n\n{result}"


def format_conversation_message(msg: ConversationMessage, is_match: bool = False) -> str:
    """
    Helper to format a single message.

    Args:
        msg: Message to format
        is_match: Whether this is the matched message (for highlighting)

    Returns:
        Formatted message string
    """
    parts = []

    # Add match indicator
    if is_match:
        parts.append("→→→")

    # Add timestamp
    time_str = msg.timestamp.strftime("%Y-%m-%d %H:%M")
    parts.append(f"[{time_str}]")

    # Add role
    parts.append(f"[{msg.role.upper()}]")

    # Add content
    if msg.sender_name:
        parts.append(f"{msg.sender_name}: {msg.content}")
    else:
        parts.append(msg.content)

    return " ".join(parts)


def merge_and_deduplicate_messages(
    recent_messages: list[ConversationMessage],
    semantic_messages: list[ConversationMessage],
) -> list[ConversationMessage]:
    """
    Merge recent and semantic messages, deduplicate, and order chronologically.

    Args:
        recent_messages: Messages from recent window (chronological order)
        semantic_messages: Messages from semantic search (relevance order)

    Returns:
        Deduplicated list ordered chronologically (oldest to newest)
    """
    # Create set of recent message IDs for fast lookup
    recent_ids = {str(msg.id) for msg in recent_messages}

    # Filter out semantic messages already in recent window
    unique_semantic = [msg for msg in semantic_messages if str(msg.id) not in recent_ids]

    logger.info(
        f"Merging {len(recent_messages)} recent + {len(semantic_messages)} semantic "
        f"= {len(recent_messages) + len(unique_semantic)} unique messages"
    )

    # Combine and sort by timestamp
    all_messages = list(recent_messages) + unique_semantic
    all_messages.sort(key=lambda m: m.timestamp)

    return all_messages
