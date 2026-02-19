"""
Unit tests for ai_api.rag.knowledge_base — format_knowledge_base_results pure function.

Tests cover:
- Empty results
- Single result formatting
- Multiple results formatting
- Source citation with page numbers and headings
- HTML comment cleaning
- Excessive blank line removal
"""

import pytest

from ai_api.rag.knowledge_base import format_knowledge_base_results


def _make_result(
    content="Sample chunk content.",
    original_filename="document.pdf",
    page_number=None,
    heading=None,
    similarity_score=0.85,
):
    """Helper to create a result dict matching the search output format."""
    return {
        "chunk": {
            "id": "chunk-1",
            "content": content,
            "content_type": "text",
            "page_number": page_number,
            "heading": heading,
            "chunk_index": 0,
            "token_count": 50,
            "metadata": None,
        },
        "document": {
            "document_id": "doc-1",
            "filename": "stored_doc.pdf",
            "original_filename": original_filename,
            "upload_date": "2025-01-15",
            "metadata": None,
        },
        "similarity_score": similarity_score,
    }


# ---------------------------------------------------------------------------
# format_knowledge_base_results
# ---------------------------------------------------------------------------


class TestFormatKnowledgeBaseResults:
    def test_empty_results(self):
        result = format_knowledge_base_results([])
        assert result == "No relevant information found in the knowledge base."

    def test_single_result_basic(self):
        results = [_make_result(content="Important info here.")]
        result = format_knowledge_base_results(results)
        assert "Found 1 relevant passages in knowledge base:" in result
        assert "=== Source 1 (relevance: 0.85) ===" in result
        assert "document.pdf" in result
        assert "Important info here." in result

    def test_multiple_results(self):
        results = [
            _make_result(content="First chunk", similarity_score=0.90),
            _make_result(content="Second chunk", similarity_score=0.75),
        ]
        result = format_knowledge_base_results(results)
        assert "Found 2 relevant passages in knowledge base:" in result
        assert "=== Source 1 (relevance: 0.90) ===" in result
        assert "=== Source 2 (relevance: 0.75) ===" in result
        assert "First chunk" in result
        assert "Second chunk" in result

    def test_with_page_number(self):
        results = [_make_result(page_number=42)]
        result = format_knowledge_base_results(results)
        assert "page 42" in result

    def test_without_page_number(self):
        results = [_make_result(page_number=None)]
        result = format_knowledge_base_results(results)
        assert "page" not in result

    def test_with_heading(self):
        results = [_make_result(heading="Introduction")]
        result = format_knowledge_base_results(results)
        assert "section 'Introduction'" in result
        assert "## Introduction" in result

    def test_without_heading(self):
        results = [_make_result(heading=None)]
        result = format_knowledge_base_results(results)
        assert "section" not in result
        assert "##" not in result

    def test_with_page_and_heading(self):
        results = [_make_result(page_number=5, heading="Methods")]
        result = format_knowledge_base_results(results)
        assert "page 5" in result
        assert "section 'Methods'" in result

    def test_html_comments_removed(self):
        content_with_comments = "Before <!-- HTML comment --> After"
        results = [_make_result(content=content_with_comments)]
        result = format_knowledge_base_results(results)
        assert "<!-- HTML comment -->" not in result
        assert "Before" in result
        assert "After" in result

    def test_multiline_html_comments_removed(self):
        content = "Start <!-- multi\nline\ncomment --> End"
        results = [_make_result(content=content)]
        result = format_knowledge_base_results(results)
        assert "<!--" not in result
        assert "-->" not in result
        assert "Start" in result
        assert "End" in result

    def test_excessive_blank_lines_removed(self):
        content = "Line 1\n\n\n\n\nLine 2"
        results = [_make_result(content=content)]
        result = format_knowledge_base_results(results)
        # Should have at most one blank line between content lines
        assert "\n\n\n" not in result

    def test_content_stripped(self):
        content = "  \n  Content with whitespace  \n  "
        results = [_make_result(content=content)]
        result = format_knowledge_base_results(results)
        assert "Content with whitespace" in result

    def test_similarity_score_formatting(self):
        results = [_make_result(similarity_score=0.123456)]
        result = format_knowledge_base_results(results)
        assert "0.12" in result

    def test_document_emoji_in_output(self):
        results = [_make_result()]
        result = format_knowledge_base_results(results)
        # The format includes a document emoji
        assert "\U0001f4c4" in result  # U+1F4C4 is the page facing up emoji

    def test_source_citation_format(self):
        results = [
            _make_result(
                original_filename="report.pdf",
                page_number=10,
                heading="Results",
            )
        ]
        result = format_knowledge_base_results(results)
        # Source line should contain filename, page, and section
        assert "report.pdf" in result
        assert "page 10" in result
        assert "section 'Results'" in result
