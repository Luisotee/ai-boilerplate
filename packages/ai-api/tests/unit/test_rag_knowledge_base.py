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

from types import SimpleNamespace
from unittest.mock import MagicMock

from ai_api.rag import knowledge_base
from ai_api.rag.knowledge_base import RRF_K, format_knowledge_base_results, rrf_fuse


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


# ---------------------------------------------------------------------------
# rrf_fuse — fixed rankings
# ---------------------------------------------------------------------------


class TestRrfFuse:
    def test_both_arms_add_up(self):
        fused = dict(rrf_fuse(["a", "b"], ["b", "c"]))
        assert fused["b"] == 1 / (RRF_K + 2) + 1 / (RRF_K + 1)
        assert fused["a"] == 1 / (RRF_K + 1)
        assert fused["c"] == 1 / (RRF_K + 2)

    def test_fixed_ranking(self):
        # b is in both lists, so it beats a (vector #1 only) and c.
        assert [cid for cid, _ in rrf_fuse(["a", "b", "d"], ["b", "c"])] == ["b", "a", "c", "d"]

    def test_keyword_only_when_no_vector_hit(self):
        # Nothing passed the threshold: keyword hits must survive on their own.
        assert [cid for cid, _ in rrf_fuse([], ["law-12651", "other"])] == ["law-12651", "other"]

    def test_vector_only(self):
        assert [cid for cid, _ in rrf_fuse(["x", "y"], [])] == ["x", "y"]

    def test_empty(self):
        assert rrf_fuse([], []) == []

    def test_ties_break_by_vector_rank_then_keyword_rank_then_id(self):
        # a: vector #1; z: keyword #1 -> same score; vector rank wins the tie.
        assert [cid for cid, _ in rrf_fuse(["a"], ["z"])] == ["a", "z"]
        # Same scores, neither in the vector list: keyword rank decides.
        assert [cid for cid, _ in rrf_fuse([], ["q", "p"])] == ["q", "p"]

    def test_deterministic(self):
        runs = {tuple(rrf_fuse(["a", "b", "c"], ["c", "d", "a"])) for _ in range(5)}
        assert len(runs) == 1

    def test_limit(self):
        assert len(rrf_fuse(["a", "b", "c"], ["d", "e"], limit=2)) == 2

    def _overlap_case(self):
        # Reviewer's case: five chunks found by both arms, far down each list
        # (ranks 16-20, 2/76..2/80 ~ 0.026), outscore the top hit of either arm
        # alone (1/61 ~ 0.016).
        overlap = [f"o{i}" for i in range(5)]
        vector = ["vec-top"] + [f"v{i}" for i in range(14)] + overlap
        keyword = ["law-8666"] + [f"k{i}" for i in range(14)] + overlap
        return vector, keyword

    def test_plain_rrf_would_drop_both_top_hits(self):
        vector, keyword = self._overlap_case()
        top5 = sorted(rrf_fuse(vector, keyword), key=lambda item: -item[1])[:5]
        assert {cid for cid, _ in top5} == {f"o{i}" for i in range(5)}

    def test_top_hit_of_each_arm_keeps_a_slot(self):
        vector, keyword = self._overlap_case()
        fused = rrf_fuse(vector, keyword, limit=5)
        ids = [cid for cid, _ in fused]
        assert len(ids) == 5
        assert "vec-top" in ids and "law-8666" in ids
        # Still ordered by score, and the overlap hits kept the other slots.
        scores = [score for _, score in fused]
        assert scores == sorted(scores, reverse=True)
        assert ids[:3] == ["o0", "o1", "o2"]

    def test_top_hit_guarantee_with_limit_one_keeps_the_fused_best(self):
        vector, keyword = self._overlap_case()
        assert [cid for cid, _ in rrf_fuse(vector, keyword, limit=1)] == ["o0"]

    def test_same_top_hit_in_both_arms_takes_one_slot(self):
        fused = rrf_fuse(["a", "b", "c"], ["a", "d", "e"], limit=2)
        assert [cid for cid, _ in fused] == ["a", "b"]


# ---------------------------------------------------------------------------
# search_knowledge_base — rows from the hybrid query, fused
# ---------------------------------------------------------------------------


def _row(cid, vec_rank=None, kw_rank=None, similarity=0.5):
    return SimpleNamespace(
        id=cid,
        document_id="doc-1",
        chunk_index=0,
        content=f"content {cid}",
        content_type="text",
        page_number=1,
        heading=None,
        token_count=10,
        chunk_metadata=None,
        filename="f.pdf",
        original_filename="Manual.pdf",
        upload_date="2026-11-09",
        document_metadata=None,
        is_conversation_scoped=False,
        similarity=similarity,
        vec_rank=vec_rank,
        kw_rank=kw_rank,
    )


def _db_returning(rows):
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    db.execute.return_value.scalar.return_value = "0.8.0"
    return db


class TestSearchKnowledgeBase:
    async def test_fuses_and_labels_matches(self):
        rows = [
            _row("v1", vec_rank=1, similarity=0.9),
            _row("both", vec_rank=2, kw_rank=1, similarity=0.8),
            _row("k1", kw_rank=2, similarity=0.2),
        ]
        results = await knowledge_base.search_knowledge_base(
            _db_returning(rows), [0.1], query_text="lei 12.651", limit=5, similarity_threshold=0.7
        )
        assert [r["chunk"]["id"] for r in results] == ["both", "v1", "k1"]
        assert [r["match"] for r in results] == ["both", "vector", "keyword"]
        # similarity_score stays the real cosine similarity, even for a keyword hit.
        assert results[2]["similarity_score"] == 0.2
        assert results[0]["rrf_score"] > results[1]["rrf_score"]

    async def test_keyword_hits_survive_without_vector_hits(self):
        rows = [_row("k1", kw_rank=1, similarity=0.1), _row("k2", kw_rank=2, similarity=None)]
        results = await knowledge_base.search_knowledge_base(
            _db_returning(rows), [0.1], query_text="Xapuri", limit=5, similarity_threshold=0.99
        )
        assert [r["chunk"]["id"] for r in results] == ["k1", "k2"]
        assert results[1]["similarity_score"] == 0.0

    async def test_limit_applies_after_fusion(self):
        rows = [_row(f"v{i}", vec_rank=i) for i in range(1, 6)] + [_row("k", kw_rank=1)]
        results = await knowledge_base.search_knowledge_base(
            _db_returning(rows), [0.1], query_text="x", limit=2, similarity_threshold=0.0
        )
        assert [r["chunk"]["id"] for r in results] == ["v1", "k"]

    async def test_query_params(self):
        db = _db_returning([])
        await knowledge_base.search_knowledge_base(
            db, [0.1], query_text=None, limit=5, similarity_threshold=0.7, whatsapp_jid="j"
        )
        sql, params = db.execute.call_args_list[-1].args
        assert params["query_text"] == ""  # no text: the keyword arm matches nothing
        assert params["pool"] == 20
        assert params["threshold"] == 0.7 and params["whatsapp_jid"] == "j"
        assert "content_tsv @@" in str(sql)
        # Words match through both configs: kb_stem (stemming needs the
        # accents) and kb_fold (accents folded, for queries typed without them).
        assert "to_tsvector('kb_stem', t.token)" in str(sql)
        assert "to_tsvector('kb_fold', t.token)" in str(sql)

    async def test_pool_is_capped_at_the_ef_search_maximum(self):
        # pool = 4 x 300 = 1200 would exceed hnsw.ef_search's 1000 (DataError).
        db = _db_returning([])
        await knowledge_base.search_knowledge_base(
            db, [0.1], query_text="x", limit=300, similarity_threshold=0.7
        )
        assert db.execute.call_args_list[-1].args[1]["pool"] == 1000

    async def test_missing_embedding_returns_empty(self):
        db = _db_returning([])
        assert await knowledge_base.search_knowledge_base(db, [], query_text="x") == []
        db.execute.assert_not_called()


class TestKeywordMatchLabel:
    def test_keyword_only_hit_is_labelled(self):
        result = _make_result(similarity_score=0.12)
        result["match"] = "keyword"
        out = format_knowledge_base_results([result])
        assert "=== Source 1 (relevance: 0.12, keyword match) ===" in out

    def test_vector_hit_unchanged(self):
        result = _make_result(similarity_score=0.85)
        result["match"] = "both"
        assert "=== Source 1 (relevance: 0.85) ===" in format_knowledge_base_results([result])
