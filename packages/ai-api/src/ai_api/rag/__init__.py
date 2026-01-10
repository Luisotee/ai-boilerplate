"""
RAG (Retrieval-Augmented Generation) module.

This module provides pure functions for semantic search:
- Conversation history search (search_conversation_history, format_conversation_results)
- Knowledge base search (search_knowledge_base, format_knowledge_base_results)
"""

from .conversation import (
    format_conversation_message,
    format_conversation_results,
    get_context_messages,
    merge_and_deduplicate_messages,
    search_conversation_history,
)
from .knowledge_base import format_knowledge_base_results, search_knowledge_base

__all__ = [
    # Conversation RAG functions
    "search_conversation_history",
    "format_conversation_results",
    "get_context_messages",
    "format_conversation_message",
    "merge_and_deduplicate_messages",
    # Knowledge Base RAG functions
    "search_knowledge_base",
    "format_knowledge_base_results",
]
