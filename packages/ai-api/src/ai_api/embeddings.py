"""
Embedding generation and management for semantic search.

This module handles:
- Google Gemini API integration for gemini-embedding-001
- Embedding generation with error handling
- Batch processing for backfills
- Graceful degradation when API key not configured

Reusable for all RAG implementations (conversation history, knowledge base, etc.)
"""

import asyncio

from google import genai
from google.genai import types

from .config import settings
from .logger import logger

# Configuration constants
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 3072  # gemini-embedding-001 default
MAX_EMBEDDING_LENGTH = 8000  # Characters
# Texts per batchEmbedContents call when embedding KB chunks (the API accepts
# up to 100; chunks are <= KB_MAX_CHUNK_TOKENS each).
EMBED_BATCH_SIZE = 32


class EmbeddingService:
    """
    Embedding service that wraps Google GenAI client.

    Follows Pydantic AI best practices by using dependency injection
    instead of global singletons.
    """

    def __init__(self, client: genai.Client):
        """
        Initialize embedding service with a GenAI client.

        Args:
            client: Authenticated Google GenAI client
        """
        self.client = client
        self.model = EMBEDDING_MODEL
        self.dimensions = EMBEDDING_DIMENSIONS
        self.max_length = MAX_EMBEDDING_LENGTH
        logger.info(f"EmbeddingService initialized (model: {self.model}, dims: {self.dimensions})")

    async def generate(
        self, text: str, task_type: str = "RETRIEVAL_DOCUMENT", title: str | None = None
    ) -> list[float] | None:
        """
        Generate embedding for a single text string.

        Args:
            text: Text to embed (will be truncated if too long)
            task_type: Gemini task type ('RETRIEVAL_DOCUMENT' for storing,
                      'RETRIEVAL_QUERY' for searching)
            title: Optional document title; Gemini uses it with RETRIEVAL_DOCUMENT only

        Returns:
            List of floats (3072 dimensions) or None if generation fails
        """
        if not text or not text.strip():
            logger.debug("Empty text, skipping embedding")
            return None

        # Truncate if too long (prevents API errors)
        text = text[: self.max_length]

        try:
            response = self.client.models.embed_content(
                model=self.model,
                contents=text,
                config=self._config(task_type, title),
            )
            embedding = response.embeddings[0].values
            logger.debug(f"Generated embedding: {len(embedding)} dimensions (task: {task_type})")
            return embedding

        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}", exc_info=True)
            return None

    def _config(self, task_type: str, title: str | None = None) -> types.EmbedContentConfig:
        config: dict = {"task_type": task_type, "output_dimensionality": self.dimensions}
        if title and task_type == "RETRIEVAL_DOCUMENT":
            config["title"] = title
        return types.EmbedContentConfig(**config)

    async def embed_documents(
        self, texts: list[str], title: str | None = None
    ) -> list[list[float]]:
        """Embed several document texts in ONE call (batchEmbedContents).

        For knowledge-base chunks: task type RETRIEVAL_DOCUMENT, with ``title``
        (the document title) applied to every text. Pass at most
        EMBED_BATCH_SIZE texts. Unlike ``generate``, errors raise, so the caller
        can fall back to one call per text.
        """
        if not texts:
            return []
        if any(not t or not t.strip() for t in texts):
            raise ValueError("embed_documents got an empty text")
        response = await self.client.aio.models.embed_content(
            model=self.model,
            contents=[t[: self.max_length] for t in texts],
            config=self._config("RETRIEVAL_DOCUMENT", title),
        )
        vectors = [e.values for e in (response.embeddings or [])]
        if len(vectors) != len(texts) or not all(vectors):
            raise ValueError(f"Expected {len(texts)} embeddings, got {len(vectors)}")
        return vectors

    async def generate_batch(self, texts: list[str]) -> list[list[float] | None]:
        """
        Generate embeddings for multiple texts in parallel.

        Args:
            texts: List of texts to embed

        Returns:
            List of embeddings (same length as texts, None for failures)
        """
        logger.info(f"Generating embeddings for {len(texts)} texts in batch")

        # Process in parallel
        tasks = [self.generate(text) for text in texts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to None
        embeddings = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Batch embedding error: {result}")
                embeddings.append(None)
            else:
                embeddings.append(result)

        success_count = sum(1 for e in embeddings if e is not None)
        logger.info(f"Successfully generated {success_count}/{len(texts)} embeddings")

        return embeddings


def create_embedding_service(api_key: str) -> EmbeddingService | None:
    """
    Create embedding service from API key.

    Factory function for initializing the service with proper error handling.

    Args:
        api_key: Google Gemini API key

    Returns:
        EmbeddingService instance or None if API key not provided
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY not set - embeddings will be disabled")
        return None

    try:
        client = genai.Client(api_key=api_key)
        return EmbeddingService(client)
    except Exception as e:
        logger.error(f"Failed to create embedding service: {str(e)}", exc_info=True)
        return None


# Backward compatibility: keep old function signature for gradual migration
async def generate_embedding(text: str) -> list[float] | None:
    """
    DEPRECATED: Use EmbeddingService.generate() instead.

    Kept for backward compatibility during migration.
    """
    service = create_embedding_service(settings.gemini_api_key)
    if not service:
        return None
    return await service.generate(text)
