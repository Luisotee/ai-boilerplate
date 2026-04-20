"""Tests for the PDF parser dispatcher, LlamaParse/Docling adapters, and chunker."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import tiktoken
from pydantic import ValidationError

from ai_api import processing
from ai_api.config import Settings, settings
from ai_api.processing import (
    ParsedChunk,
    _chunk_pages_tiktoken,
    _parse_pdf,
    _parse_pdf_with_docling,
    _parse_pdf_with_llamaparse,
    process_pdf_document,
)


def _make_success_page(page_number: int, markdown: str | None) -> MagicMock:
    """Build a mock of `MarkdownPageMarkdownResultPage` (success=True)."""
    return MagicMock(markdown=markdown, page_number=page_number, success=True)


def _make_failed_page(page_number: int, error: str = "ocr failure") -> MagicMock:
    """Build a mock of `MarkdownPageFailedMarkdownPage` — no `.markdown` attribute."""
    page = MagicMock(page_number=page_number, success=False, error=error)
    # `MarkdownPageFailedMarkdownPage` has no `markdown` field. Auto-created
    # MagicMock attrs would lie; force AttributeError on access to match reality.
    del page.markdown
    return page


def _mock_llama_client_cls(pages_text: list[str | None]) -> MagicMock:
    """Build a fake `llama_cloud.AsyncLlamaCloud` class — each item is one success page."""
    page_mocks = [_make_success_page(i + 1, t) for i, t in enumerate(pages_text)]
    return _mock_llama_client_cls_with_pages(page_mocks)


def _mock_llama_client_cls_with_pages(page_mocks: list[MagicMock]) -> MagicMock:
    """Build a fake `AsyncLlamaCloud` class with explicit page objects."""
    result = MagicMock()
    result.markdown.pages = page_mocks

    file_obj = MagicMock(id="fake-file-id")

    instance = MagicMock()
    instance.files.create = AsyncMock(return_value=file_obj)
    instance.files.delete = AsyncMock(return_value=None)
    instance.parsing.parse = AsyncMock(return_value=result)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)

    cls = MagicMock(return_value=instance)
    cls._instance = instance  # for assertion access
    return cls


def _patch_llama_module(cls: MagicMock):
    """Patch sys.modules so `from llama_cloud import AsyncLlamaCloud` finds our mock."""
    return patch.dict("sys.modules", {"llama_cloud": MagicMock(AsyncLlamaCloud=cls)})


def _mock_docling_module(*, page_count: int, per_page_supported: bool = True) -> MagicMock:
    """Build a fake `docling.document_converter` module with a controllable converter."""
    doc = MagicMock()

    if per_page_supported:
        doc.export_to_markdown = MagicMock(
            side_effect=lambda page_no=None: (
                f"page {page_no}" if page_no is not None else "WHOLE DOC"
            )
        )
    else:
        # Older docling: page_no kwarg unsupported, no-arg export returns whole doc.
        def _export(*args, **kwargs):
            if "page_no" in kwargs:
                raise TypeError("export_to_markdown() got an unexpected keyword 'page_no'")
            return "WHOLE DOC"

        doc.export_to_markdown = MagicMock(side_effect=_export)

    result = MagicMock()
    result.document = doc
    result.input.page_count = page_count

    converter = MagicMock()
    converter.convert = MagicMock(return_value=result)

    converter_cls = MagicMock(return_value=converter)
    return MagicMock(DocumentConverter=converter_cls)


class TestLlamaParseAdapter:
    @pytest.mark.asyncio
    async def test_parses_pages(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["page one", "page two", "page three"])
        with _patch_llama_module(cls):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(1, "page one"), (2, "page two"), (3, "page three")]

    @pytest.mark.asyncio
    async def test_honours_tier_config(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        monkeypatch.setattr(settings, "llamaparse_tier", "agentic")
        cls = _mock_llama_client_cls(["only page"])
        with _patch_llama_module(cls):
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

    @pytest.mark.asyncio
    async def test_client_closed_on_success(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        with _patch_llama_module(cls):
            await _parse_pdf_with_llamaparse("fake.pdf")
        cls._instance.__aenter__.assert_awaited_once()
        cls._instance.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_client_closed_on_error(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        cls._instance.parsing.parse = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with _patch_llama_module(cls), pytest.raises(httpx.ConnectError):
            await _parse_pdf_with_llamaparse("fake.pdf")
        cls._instance.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        cls._instance.parsing.parse.return_value.markdown = None
        with _patch_llama_module(cls), pytest.raises(ValueError, match="empty result"):
            await _parse_pdf_with_llamaparse("fake.pdf")

    @pytest.mark.asyncio
    async def test_skips_none_page_markdown_with_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["page 1", None, "page 3"])
        with _patch_llama_module(cls), caplog.at_level("WARNING", logger="ai-api"):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(1, "page 1"), (3, "page 3")]
        assert any("None markdown for 1 page" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_uses_sdk_page_number_when_sparse(self, monkeypatch):
        """Use `page.page_number` from the SDK, not enumerate — pages may be sparse."""
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        # Simulate pages 2, 5, 7 — SDK could legitimately return non-contiguous indices.
        page_mocks = [
            _make_success_page(2, "second"),
            _make_success_page(5, "fifth"),
            _make_success_page(7, "seventh"),
        ]
        cls = _mock_llama_client_cls_with_pages(page_mocks)
        with _patch_llama_module(cls):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(2, "second"), (5, "fifth"), (7, "seventh")]

    @pytest.mark.asyncio
    async def test_skips_failed_pages_without_crashing(self, monkeypatch, caplog):
        """MarkdownPageFailedMarkdownPage has no .markdown attr — must not AttributeError."""
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        page_mocks = [
            _make_success_page(1, "first"),
            _make_failed_page(2, error="bad OCR"),
            _make_success_page(3, "third"),
        ]
        cls = _mock_llama_client_cls_with_pages(page_mocks)
        with _patch_llama_module(cls), caplog.at_level("WARNING", logger="ai-api"):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(1, "first"), (3, "third")]
        assert any("failed page 2" in r.message for r in caplog.records)
        assert any("bad OCR" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_deletes_uploaded_file_on_success(self, monkeypatch):
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        with _patch_llama_module(cls):
            await _parse_pdf_with_llamaparse("fake.pdf")
        cls._instance.files.delete.assert_awaited_once_with("fake-file-id")

    @pytest.mark.asyncio
    async def test_deletes_uploaded_file_on_parse_error(self, monkeypatch):
        """Even if parse fails, we must still clean up the uploaded file."""
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        cls._instance.parsing.parse = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with _patch_llama_module(cls), pytest.raises(httpx.ConnectError):
            await _parse_pdf_with_llamaparse("fake.pdf")
        cls._instance.files.delete.assert_awaited_once_with("fake-file-id")

    @pytest.mark.asyncio
    async def test_delete_failure_is_logged_not_raised(self, monkeypatch, caplog):
        """A failed cleanup must not mask the successful parse result."""
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        cls = _mock_llama_client_cls(["only"])
        cls._instance.files.delete = AsyncMock(side_effect=RuntimeError("delete failed"))
        with _patch_llama_module(cls), caplog.at_level("WARNING", logger="ai-api"):
            pages = await _parse_pdf_with_llamaparse("fake.pdf")
        assert pages == [(1, "only")]
        assert any("Failed to delete uploaded LlamaCloud file" in r.message for r in caplog.records)


class TestDoclingAdapter:
    @pytest.mark.asyncio
    async def test_raises_without_extra(self, monkeypatch):
        monkeypatch.setattr(processing, "_docling_available", lambda: False)
        with pytest.raises(RuntimeError, match="Docling is not installed"):
            await _parse_pdf_with_docling("fake.pdf")

    @pytest.mark.asyncio
    async def test_extracts_pages_when_supported(self, monkeypatch):
        monkeypatch.setattr(processing, "_docling_available", lambda: True)
        mod = _mock_docling_module(page_count=3, per_page_supported=True)
        with patch.dict("sys.modules", {"docling.document_converter": mod}):
            pages = await _parse_pdf_with_docling("fake.pdf")
        assert pages == [(1, "page 1"), (2, "page 2"), (3, "page 3")]

    @pytest.mark.asyncio
    async def test_logs_warning_on_typeerror(self, monkeypatch, caplog):
        monkeypatch.setattr(processing, "_docling_available", lambda: True)
        mod = _mock_docling_module(page_count=5, per_page_supported=False)
        with (
            patch.dict("sys.modules", {"docling.document_converter": mod}),
            caplog.at_level("WARNING", logger="ai-api"),
        ):
            pages = await _parse_pdf_with_docling("fake.pdf")
        assert pages == [(1, "WHOLE DOC")]
        assert any("lacks per-page export" in r.message for r in caplog.records)


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
    async def test_auto_falls_back_on_recoverable_llamaparse_error(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=httpx.ConnectError("llamaparse 500"))
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        with caplog.at_level("WARNING", logger="ai-api"):
            pages, metadata = await _parse_pdf("fake.pdf")

        assert pages == [(1, "from docling")]
        assert metadata["parser"] == "docling"
        llama_mock.assert_awaited_once()
        docling_mock.assert_awaited_once()
        # The fallback warning must include the LlamaParse traceback (exc_info=True).
        warning_records = [r for r in caplog.records if "falling back to Docling" in r.message]
        assert warning_records
        assert warning_records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_auto_does_not_recover_from_programming_errors(self, monkeypatch):
        """TypeError/AttributeError from SDK drift must propagate, not silently fall back."""
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=TypeError("attribute renamed"))
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        with pytest.raises(TypeError, match="attribute renamed"):
            await _parse_pdf("fake.pdf")
        docling_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_chains_docling_failure_from_llamaparse(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_err = httpx.ConnectError("llamaparse down")
        llama_mock = AsyncMock(side_effect=llama_err)
        docling_mock = AsyncMock(side_effect=RuntimeError("docling died"))
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        with pytest.raises(RuntimeError, match="docling died") as excinfo:
            await _parse_pdf("fake.pdf")
        assert excinfo.value.__cause__ is llama_err

    @pytest.mark.asyncio
    async def test_auto_no_fallback_without_docling_extra(self, monkeypatch):
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        llama_mock = AsyncMock(side_effect=httpx.ConnectError("llamaparse down"))
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: False)

        with pytest.raises(httpx.ConnectError, match="llamaparse down"):
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
        llama_mock = AsyncMock(side_effect=httpx.ConnectError("network down"))
        docling_mock = AsyncMock()
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        with pytest.raises(httpx.ConnectError, match="network down"):
            await _parse_pdf("fake.pdf")

        docling_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llamaparse_inner_timeout_triggers_fallback_in_auto(self, monkeypatch):
        """The llamaparse_timeout_seconds wrapper must actually fire and trigger fallback."""
        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        monkeypatch.setattr(settings, "llamaparse_timeout_seconds", 0.01)

        async def _slow_llama(_path: str):
            await asyncio.sleep(1)
            return [(1, "never reached")]

        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", _slow_llama)
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        pages, metadata = await _parse_pdf("fake.pdf")
        assert metadata["parser"] == "docling"
        docling_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_falls_back_on_llama_api_error(self, monkeypatch):
        """APIError from the real SDK must be recoverable — locks in the import at module load."""
        from llama_cloud import APIConnectionError

        monkeypatch.setattr(settings, "pdf_parser", "auto")
        monkeypatch.setattr(settings, "llama_cloud_api_key", "llx-test")
        # APIConnectionError is an APIError subclass; constructs with just a request.
        fake_req = httpx.Request("POST", "https://api.cloud.llamaindex.ai/parse")
        llama_mock = AsyncMock(side_effect=APIConnectionError(request=fake_req))
        docling_mock = AsyncMock(return_value=[(1, "from docling")])
        monkeypatch.setattr(processing, "_parse_pdf_with_llamaparse", llama_mock)
        monkeypatch.setattr(processing, "_parse_pdf_with_docling", docling_mock)
        monkeypatch.setattr(processing, "_docling_available", lambda: True)

        pages, metadata = await _parse_pdf("fake.pdf")

        assert pages == [(1, "from docling")]
        assert metadata["parser"] == "docling"
        docling_mock.assert_awaited_once()


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
        long_text = ("the quick brown fox jumps over the lazy dog\n\n" * 400).strip()
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

    def test_heading_regex_ignores_fenced_code(self, encoder):
        """`#` lines inside ``` fences must not be captured as headings."""
        md = (
            "# Real Heading\n\n"
            "Intro paragraph.\n\n"
            "```python\n"
            "# not-a-heading python comment\n"
            "## also-not-a-heading\n"
            "def foo():\n"
            "    pass\n"
            "```\n\n"
            "## Another Real Heading\n\n"
            "Body text."
        )
        chunks = _chunk_pages_tiktoken([(1, md)], max_tokens=512, encoder=encoder)
        assert len(chunks) == 1
        headings = chunks[0].headings
        assert "Real Heading" in headings
        assert "Another Real Heading" in headings
        assert all("not-a-heading" not in h for h in headings)

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

    def test_packs_short_paragraphs(self, encoder):
        """Many short paragraphs that fit inside max_tokens should pack into one chunk."""
        md = "\n\n".join(f"paragraph {i}" for i in range(20))
        chunks = _chunk_pages_tiktoken([(1, md)], max_tokens=512, encoder=encoder)
        assert len(chunks) == 1
        assert "paragraph 0" in chunks[0].text
        assert "paragraph 19" in chunks[0].text

    def test_packs_then_splits_on_overflow(self, encoder):
        """Paragraphs are packed greedily and a new chunk starts when adding would overflow."""
        # Each paragraph ~10 tokens. With max_tokens=20 we fit ~2 paragraphs per chunk.
        paragraphs = [f"this is paragraph number {i} with extra words" for i in range(6)]
        md = "\n\n".join(paragraphs)
        chunks = _chunk_pages_tiktoken([(1, md)], max_tokens=20, encoder=encoder)
        assert len(chunks) >= 2
        for c in chunks:
            assert c.token_count <= 20

    def test_does_not_split_mid_codepoint(self, encoder):
        """Multi-byte UTF-8 must not surface as U+FFFD when an oversized paragraph is windowed."""
        # Build a paragraph (no blank lines) of accented Portuguese characters so the
        # entire thing is one paragraph that exceeds max_tokens and triggers the
        # token-window fallback.
        para = "ação coração não é só português também ção ção ção ção. " * 30
        chunks = _chunk_pages_tiktoken([(1, para)], max_tokens=20, encoder=encoder)
        assert len(chunks) > 1
        for c in chunks:
            assert "\ufffd" not in c.text


class TestParsedChunk:
    def test_defaults(self):
        c = ParsedChunk(text="hello", token_count=1)
        assert c.page_numbers == []
        assert c.headings == []
        assert c.doc_item_count == 0


class TestSettingsValidation:
    def test_rejects_inverted_timeouts(self, monkeypatch):
        monkeypatch.setenv("LLAMAPARSE_TIMEOUT_SECONDS", "400")
        monkeypatch.setenv("KB_PROCESSING_TIMEOUT_SECONDS", "300")
        with pytest.raises(ValidationError, match="strictly less than"):
            Settings()

    def test_legacy_kb_docling_timeout_env_still_works(self, monkeypatch, caplog):
        monkeypatch.setenv("KB_DOCLING_TIMEOUT_SECONDS", "99")
        monkeypatch.delenv("KB_PARSE_TIMEOUT_SECONDS", raising=False)
        with caplog.at_level("WARNING", logger="ai-api"):
            s = Settings()
        assert s.kb_parse_timeout_seconds == 99
        assert any("KB_DOCLING_TIMEOUT_SECONDS is deprecated" in r.message for r in caplog.records)

    def test_default_timeouts_are_consistent(self):
        """The default values shipped in config.py must satisfy the ordering invariant."""
        s = Settings()
        assert s.llamaparse_timeout_seconds < s.kb_processing_timeout_seconds


def _build_session_mock(document, chunks_after: int = 0) -> MagicMock:
    """Return a MagicMock SQLAlchemy session whose .query() returns the given doc / count."""
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = document
    session.query.return_value.filter_by.return_value.count.return_value = chunks_after
    return session


class TestProcessPdfDocumentIntegration:
    """End-to-end coverage of the public `process_pdf_document` orchestrator."""

    @pytest.fixture
    def fake_doc(self):
        doc = MagicMock()
        doc.original_filename = "test.pdf"
        doc.doc_metadata = None
        doc.status = None
        doc.error_message = None
        doc.chunk_count = 0
        return doc

    @pytest.mark.asyncio
    async def test_marks_failed_when_zero_chunks_stored(self, monkeypatch, tmp_path, fake_doc):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-stub")
        session = _build_session_mock(fake_doc, chunks_after=0)
        monkeypatch.setattr(processing, "SessionLocal", lambda: session)
        monkeypatch.setattr(
            processing,
            "_parse_pdf",
            AsyncMock(return_value=([(1, "hello world")], {"parser": "llamaparse"})),
        )
        embedder = MagicMock()
        embedder.generate = AsyncMock(return_value=None)  # always fails
        monkeypatch.setattr(processing, "create_embedding_service", lambda _key: embedder)

        await process_pdf_document("doc-id", str(pdf))

        # First branch: stored_count == 0 → status "failed"
        assert fake_doc.status == "failed"

    @pytest.mark.asyncio
    async def test_marks_partial_on_some_embedding_failures(self, monkeypatch, tmp_path, fake_doc):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-stub")
        session = _build_session_mock(fake_doc, chunks_after=2)
        monkeypatch.setattr(processing, "SessionLocal", lambda: session)
        # Three short pages → three single-paragraph chunks.
        monkeypatch.setattr(
            processing,
            "_parse_pdf",
            AsyncMock(
                return_value=(
                    [(1, "alpha"), (2, "beta"), (3, "gamma")],
                    {"parser": "llamaparse"},
                )
            ),
        )
        embedder = MagicMock()
        # First two succeed, third returns None (skip).
        embedder.generate = AsyncMock(side_effect=[[0.1] * 8, [0.1] * 8, None])
        monkeypatch.setattr(processing, "create_embedding_service", lambda _key: embedder)

        await process_pdf_document("doc-id", str(pdf))

        assert fake_doc.status == "partial"
        assert fake_doc.doc_metadata is not None
        assert fake_doc.doc_metadata.get("processing_errors", {}).get("chunks_skipped") == 1

    @pytest.mark.asyncio
    async def test_marks_failed_on_outer_timeout(self, monkeypatch, tmp_path, fake_doc):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"%PDF-stub")
        session = _build_session_mock(fake_doc, chunks_after=0)
        monkeypatch.setattr(processing, "SessionLocal", lambda: session)
        monkeypatch.setattr(settings, "kb_processing_timeout_seconds", 0.05)

        async def _slow_parse(_path: str):
            await asyncio.sleep(1)
            return [], {"parser": "llamaparse"}

        monkeypatch.setattr(processing, "_parse_pdf", _slow_parse)

        await process_pdf_document("doc-id", str(pdf))

        assert fake_doc.status == "failed"
        assert "timeout" in (fake_doc.error_message or "").lower()
