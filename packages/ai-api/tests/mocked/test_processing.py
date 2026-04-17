"""Tests for the PDF parser dispatcher, LlamaParse/Docling adapters, and chunker."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tiktoken

from ai_api import processing
from ai_api.config import settings
from ai_api.processing import (
    ParsedChunk,
    _chunk_pages_tiktoken,
    _parse_pdf,
    _parse_pdf_with_docling,
    _parse_pdf_with_llamaparse,
)


def _mock_llama_client_cls(pages_text: list[str]) -> MagicMock:
    """Build a fake `llama_cloud.AsyncLlamaCloud` class whose instance returns `pages_text`."""
    page_mocks = [MagicMock(markdown=t) for t in pages_text]
    result = MagicMock()
    result.markdown.pages = page_mocks

    file_obj = MagicMock(id="fake-file-id")

    instance = MagicMock()
    instance.files.create = AsyncMock(return_value=file_obj)
    instance.parsing.parse = AsyncMock(return_value=result)

    cls = MagicMock(return_value=instance)
    cls._instance = instance  # for assertion access
    return cls


class TestLlamaParseAdapter:
    @pytest.mark.asyncio
    async def test_parses_pages(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["page one", "page two", "page three"])
        with patch.dict("sys.modules", {"llama_cloud": MagicMock(AsyncLlamaCloud=cls)}):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(1, "page one"), (2, "page two"), (3, "page three")]

    @pytest.mark.asyncio
    async def test_honours_tier_config(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        monkeypatch.setattr(settings, "llamaparse_tier", "agentic")
        cls = _mock_llama_client_cls(["only page"])
        with patch.dict("sys.modules", {"llama_cloud": MagicMock(AsyncLlamaCloud=cls)}):
            await _parse_pdf_with_llamaparse("fake.pdf")
        call_kwargs = cls._instance.parsing.parse.await_args.kwargs
        assert call_kwargs["tier"] == "agentic"
        assert call_kwargs["version"] == "latest"
        assert call_kwargs["expand"] == ["markdown"]

    @pytest.mark.asyncio
    async def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", None)
        with pytest.raises(ValueError, match="LLAMA_CLOUD_API_KEY not configured"):
            await _parse_pdf_with_llamaparse("fake.pdf")


class TestDoclingAdapter:
    @pytest.mark.asyncio
    async def test_raises_without_extra(self, monkeypatch):
        monkeypatch.setattr(processing, "_docling_available", lambda: False)
        with pytest.raises(RuntimeError, match="Docling is not installed"):
            await _parse_pdf_with_docling("fake.pdf")


class TestParsePdfDispatcher:
    @pytest.mark.asyncio
    async def test_auto_prefers_llamaparse_when_key_set(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(return_value=[(1, "from llamaparse")])
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        pages, metadata = await _parse_pdf("fake.pdf")

        assert pages == [(1, "from llamaparse")]
        assert metadata["parser"] == "llamaparse"
        llama_mock.assert_awaited_once()
        docling_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_falls_back_on_llamaparse_error(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=RuntimeError("llamaparse 500"))
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        pages, metadata = await _parse_pdf("fake.pdf")

        assert pages == [(1, "from docling")]
        assert metadata["parser"] == "docling"
        llama_mock.assert_awaited_once()
        docling_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_no_fallback_without_docling_extra(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=RuntimeError("llamaparse down"))
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: False)

        with pytest.raises(RuntimeError, match="llamaparse down"):
            await _parse_pdf("fake.pdf")

    @pytest.mark.asyncio
    async def test_auto_uses_docling_when_no_key(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", None)
        llama_mock = AsyncMock()
        docling_mock = AsyncMock(return_value=[(1, "docling page")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        pages, metadata = await _parse_pdf("fake.pdf")

        assert metadata["parser"] == "docling"
        llama_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_docling_only_mode_ignores_llamaparse(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "docling")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock()
        docling_mock = AsyncMock(return_value=[(1, "docling page")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)

        pages, metadata = await _parse_pdf("fake.pdf")

        assert metadata["parser"] == "docling"
        llama_mock.assert_not_awaited()
        docling_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llamaparse_only_mode_no_fallback(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "llamaparse")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=RuntimeError("network down"))
        docling_mock = AsyncMock()
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        with pytest.raises(RuntimeError, match="network down"):
            await _parse_pdf("fake.pdf")

        docling_mock.assert_not_awaited()


class TestChunkPagesTiktoken:
    @pytest.fixture
    def encoder(self) -> "tiktoken.Encoding":
        return tiktoken.get_encoding("cl100k_base")

    def test_short_page_stays_one_chunk(self, encoder):
        pages = [(1, "Hello world, this is a short page.")]
        chunks = _chunk_pages_tiktoken(pages, max_tokens=512, encoder=encoder)
        assert len(chunks) == 1
        assert chunks[0].page_numbers == [1]
        assert chunks[0].token_count > 0

    def test_respects_max_tokens(self, encoder):
        # ~4000 tokens of repeated content
        long_text = ("the quick brown fox jumps over the lazy dog " * 400).strip()
        pages = [(1, long_text)]
        chunks = _chunk_pages_tiktoken(pages, max_tokens=512, encoder=encoder)
        assert len(chunks) >= 3
        for c in chunks:
            assert c.token_count <= 512
            assert c.page_numbers == [1]

    def test_extracts_headings(self, encoder):
        md = "# Main Title\n\nSome intro text.\n\n## Section One\n\nBody of section one."
        chunks = _chunk_pages_tiktoken([(3, md)], max_tokens=512, encoder=encoder)
        assert len(chunks) == 1
        assert "Main Title" in chunks[0].headings
        assert "Section One" in chunks[0].headings
        assert chunks[0].page_numbers == [3]

    def test_skips_empty_pages(self, encoder):
        chunks = _chunk_pages_tiktoken(
            [(1, ""), (2, "   \n\n  "), (3, "real content")],
            max_tokens=512,
            encoder=encoder,
        )
        assert len(chunks) == 1
        assert chunks[0].page_numbers == [3]

    def test_preserves_page_order(self, encoder):
        pages = [(1, "first"), (2, "second"), (3, "third")]
        chunks = _chunk_pages_tiktoken(pages, max_tokens=512, encoder=encoder)
        assert [c.page_numbers[0] for c in chunks] == [1, 2, 3]


class TestParsedChunk:
    def test_defaults(self):
        c = ParsedChunk(text="hello", token_count=1)
        assert c.page_numbers == []
        assert c.headings == []
        assert c.doc_item_count == 0
