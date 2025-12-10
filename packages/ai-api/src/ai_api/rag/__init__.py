"""
RAG (Retrieval-Augmented Generation) module.

This module provides different RAG implementations:
- ConversationRAG: Search through conversation history
- KnowledgeBaseRAG: Search through PDF knowledge base

All RAG implementations inherit from BaseRAG.
"""

from .conversation import ConversationRAG
from .knowledge_base import KnowledgeBaseRAG

__all__ = ['ConversationRAG', 'KnowledgeBaseRAG']
