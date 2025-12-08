"""
RAG (Retrieval-Augmented Generation) module.

This module provides different RAG implementations:
- ConversationRAG: Search through conversation history
- KnowledgeBaseRAG: Search through knowledge base (future)

All RAG implementations inherit from BaseRAG.
"""

from .conversation import ConversationRAG

__all__ = ['ConversationRAG']
