"""Tests for the embedding generation service."""

from unittest.mock import MagicMock, patch

import pytest

from ai_api.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_EMBEDDING_LENGTH,
    EmbeddingService,
    create_embedding_service,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_genai_client():
    """Create a mock Google GenAI client."""
    client = MagicMock()
    return client


@pytest.fixture
def embedding_service(mock_genai_client):
    """Create an EmbeddingService with a mocked GenAI client."""
    return EmbeddingService(client=mock_genai_client)


def _make_embed_response(values):
    """Helper to build a mock embed_content response."""
    embedding = MagicMock()
    embedding.values = values
    response = MagicMock()
    response.embeddings = [embedding]
    return response


# ---------------------------------------------------------------------------
# Constructor / initialization
# ---------------------------------------------------------------------------


class TestEmbeddingServiceInit:
    def test_sets_model(self, embedding_service):
        assert embedding_service.model == EMBEDDING_MODEL

    def test_sets_dimensions(self, embedding_service):
        assert embedding_service.dimensions == EMBEDDING_DIMENSIONS

    def test_sets_max_length(self, embedding_service):
        assert embedding_service.max_length == MAX_EMBEDDING_LENGTH

    def test_stores_client(self, embedding_service, mock_genai_client):
        assert embedding_service.client is mock_genai_client


# ---------------------------------------------------------------------------
# generate — empty text guard
# ---------------------------------------------------------------------------


class TestGenerateEmptyText:
    @pytest.mark.asyncio
    async def test_empty_string_returns_none(self, embedding_service):
        result = await embedding_service.generate("")
        assert result is None

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_none(self, embedding_service):
        result = await embedding_service.generate("   \n\t  ")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_text_returns_none(self, embedding_service):
        # The method checks `not text` which is True for None
        result = await embedding_service.generate(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_text_does_not_call_api(self, embedding_service, mock_genai_client):
        await embedding_service.generate("")
        mock_genai_client.models.embed_content.assert_not_called()


# ---------------------------------------------------------------------------
# generate — truncation
# ---------------------------------------------------------------------------


class TestGenerateTruncation:
    @pytest.mark.asyncio
    async def test_long_text_is_truncated(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1] * 3072)

        long_text = "a" * 10000
        await embedding_service.generate(long_text)

        call_args = mock_genai_client.models.embed_content.call_args
        actual_text = call_args[1]["contents"]
        assert len(actual_text) == MAX_EMBEDDING_LENGTH

    @pytest.mark.asyncio
    async def test_short_text_not_truncated(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1] * 3072)

        short_text = "Hello world"
        await embedding_service.generate(short_text)

        call_args = mock_genai_client.models.embed_content.call_args
        actual_text = call_args[1]["contents"]
        assert actual_text == short_text


# ---------------------------------------------------------------------------
# generate — successful response
# ---------------------------------------------------------------------------


class TestGenerateSuccess:
    @pytest.mark.asyncio
    async def test_returns_embedding_values(self, embedding_service, mock_genai_client):
        expected = [0.1, 0.2, 0.3] * 1024
        mock_genai_client.models.embed_content.return_value = _make_embed_response(expected)

        result = await embedding_service.generate("test text")
        assert result == expected

    @pytest.mark.asyncio
    async def test_passes_task_type(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1])

        await embedding_service.generate("query text", task_type="RETRIEVAL_QUERY")

        call_kwargs = mock_genai_client.models.embed_content.call_args[1]
        config = call_kwargs["config"]
        assert config.task_type == "RETRIEVAL_QUERY"

    @pytest.mark.asyncio
    async def test_default_task_type_is_retrieval_document(
        self, embedding_service, mock_genai_client
    ):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1])

        await embedding_service.generate("store this text")

        call_kwargs = mock_genai_client.models.embed_content.call_args[1]
        config = call_kwargs["config"]
        assert config.task_type == "RETRIEVAL_DOCUMENT"

    @pytest.mark.asyncio
    async def test_passes_correct_model(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1])

        await embedding_service.generate("test")

        call_kwargs = mock_genai_client.models.embed_content.call_args[1]
        assert call_kwargs["model"] == EMBEDDING_MODEL


# ---------------------------------------------------------------------------
# generate — API error graceful degradation
# ---------------------------------------------------------------------------


class TestGenerateErrorHandling:
    @pytest.mark.asyncio
    async def test_api_error_returns_none(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.side_effect = Exception("API quota exceeded")

        result = await embedding_service.generate("some text")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_does_not_raise(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.side_effect = RuntimeError("Connection lost")

        # Should not raise — returns None gracefully
        result = await embedding_service.generate("text")
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_response_returns_none(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = MagicMock(embeddings=[])

        # Accessing embeddings[0] on an empty list raises IndexError
        result = await embedding_service.generate("text")
        assert result is None


# ---------------------------------------------------------------------------
# generate_batch
# ---------------------------------------------------------------------------


class TestGenerateBatch:
    @pytest.mark.asyncio
    async def test_returns_list_same_length_as_input(self, embedding_service, mock_genai_client):
        mock_genai_client.models.embed_content.return_value = _make_embed_response([0.1, 0.2])

        texts = ["text1", "text2", "text3"]
        results = await embedding_service.generate_batch(texts)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_exception_in_one_item_returns_none_for_that_item(
        self, embedding_service, mock_genai_client
    ):
        call_count = 0

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("Transient error")
            return _make_embed_response([0.5])

        mock_genai_client.models.embed_content.side_effect = side_effect

        results = await embedding_service.generate_batch(["a", "b", "c"])
        # The second text should produce None, others should succeed
        assert results[0] is not None
        assert results[1] is None
        assert results[2] is not None

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self, embedding_service):
        results = await embedding_service.generate_batch([])
        assert results == []


# ---------------------------------------------------------------------------
# create_embedding_service factory
# ---------------------------------------------------------------------------


class TestCreateEmbeddingService:
    def test_empty_api_key_returns_none(self):
        result = create_embedding_service("")
        assert result is None

    def test_none_api_key_returns_none(self):
        result = create_embedding_service(None)
        assert result is None

    @patch("ai_api.embeddings.genai.Client")
    def test_valid_api_key_returns_service(self, mock_client_cls):
        mock_client_cls.return_value = MagicMock()
        result = create_embedding_service("valid-key")
        assert isinstance(result, EmbeddingService)
        mock_client_cls.assert_called_once_with(api_key="valid-key")

    @patch("ai_api.embeddings.genai.Client")
    def test_client_creation_failure_returns_none(self, mock_client_cls):
        mock_client_cls.side_effect = Exception("Auth failed")
        result = create_embedding_service("bad-key")
        assert result is None
