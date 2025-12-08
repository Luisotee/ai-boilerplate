"""
Base class for RAG implementations.

Provides abstract interface that all RAG sources must implement.
This allows for multiple RAG sources (conversation history, knowledge base, etc.)
with a consistent interface.
"""

from abc import ABC, abstractmethod
from typing import List, Any
from sqlalchemy.orm import Session


class BaseRAG(ABC):
    """
    Base class for Retrieval-Augmented Generation implementations.

    All RAG sources should inherit from this class and implement
    the required methods.
    """

    @abstractmethod
    async def search(
        self,
        db: Session,
        query: str,
        limit: int = 5,
        **kwargs
    ) -> List[Any]:
        """
        Search and return relevant documents/messages.

        Args:
            db: Database session
            query: Search query text
            limit: Maximum number of results to return
            **kwargs: Additional implementation-specific parameters

        Returns:
            List of search results (implementation-specific format)
        """
        pass

    @abstractmethod
    def format_results(self, results: List[Any]) -> str:
        """
        Format search results for agent consumption.

        Args:
            results: Search results from search() method

        Returns:
            Formatted string for agent to read
        """
        pass
