"""Tests for scripts/reembed_kb.py — re-embedding legacy KB chunks (DB mocked)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors as genai_errors

from ai_api.scripts import reembed_kb


def _row(cid, content="text", heading=None, metadata=None, filename="Manual_Tecnico.pdf"):
    return SimpleNamespace(
        id=cid,
        document_id="doc-1",
        content=content,
        heading=heading,
        chunk_metadata=metadata,
        original_filename=filename,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(reembed_kb.asyncio, "sleep", sleep)
    return sleep


async def _no_pace():
    return None


class TestChunkEmbeddingText:
    def test_uses_stored_heading_path(self):
        row = _row("c", content="Body", heading="Prices", metadata={"heading_path": ["A", "B"]})
        assert reembed_kb.chunk_embedding_text(row) == "A > B\n\nBody"

    def test_falls_back_to_heading(self):
        row = _row("c", content="Body", heading="Prices", metadata={"all_headings": ["Prices"]})
        assert reembed_kb.chunk_embedding_text(row) == "Prices\n\nBody"

    def test_no_heading(self):
        assert reembed_kb.chunk_embedding_text(_row("c", content="Body")) == "Body"


class TestIsRetriable:
    def test_rate_limit_and_server_errors(self):
        assert reembed_kb.is_retriable(genai_errors.APIError(429, {}))
        assert reembed_kb.is_retriable(genai_errors.APIError(503, {}))
        assert reembed_kb.is_retriable(TimeoutError())

    def test_client_errors(self):
        assert not reembed_kb.is_retriable(genai_errors.APIError(400, {}))
        assert not reembed_kb.is_retriable(ValueError("bad"))


class TestEmbedBatch:
    async def test_backs_off_then_succeeds(self, _no_sleep):
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=[genai_errors.APIError(429, {}), [[0.1]]])
        assert await reembed_kb.embed_batch(service, ["t"], "T", _no_pace) == [[0.1]]
        _no_sleep.assert_awaited_once_with(reembed_kb.BACKOFF_BASE_SECONDS)

    async def test_gives_up_after_retries(self):
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=genai_errors.APIError(503, {}))
        with pytest.raises(genai_errors.APIError):
            await reembed_kb.embed_batch(service, ["t"], "T", _no_pace)
        assert service.embed_documents.await_count == reembed_kb.MAX_RETRIES + 1

    async def test_non_retriable_returns_none(self):
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=ValueError("bad input"))
        assert await reembed_kb.embed_batch(service, ["t"], "T", _no_pace) is None


class TestReembed:
    @pytest.fixture
    def db(self):
        return MagicMock()

    @pytest.fixture
    def stored(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reembed_kb, "store", lambda db, cid, vec: calls.append(cid))
        return calls

    async def test_dry_run_only_counts(self, db, monkeypatch):
        monkeypatch.setattr(reembed_kb, "count_pending", lambda _db: (12, 3))
        next_batch = MagicMock()
        monkeypatch.setattr(reembed_kb, "next_batch", next_batch)
        result = await reembed_kb.reembed(dry_run=True, db_factory=lambda: db)
        assert result == {"done": 0, "skipped": 0, "remaining": 12}
        next_batch.assert_not_called()

    async def test_processes_batches_until_none_left(self, db, stored, monkeypatch):
        counts = iter([(3, 1), (0, 0)])
        monkeypatch.setattr(reembed_kb, "count_pending", lambda _db: next(counts))
        batches = iter([[_row("a"), _row("b")], [_row("c")], []])
        monkeypatch.setattr(reembed_kb, "next_batch", lambda _db, size, skip: next(batches))
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=lambda texts, title: [[0.1]] * len(texts))

        result = await reembed_kb.reembed(
            db_factory=lambda: db, service=service, requests_per_minute=6000
        )

        assert result == {"done": 3, "skipped": 0, "remaining": 0}
        assert stored == ["a", "b", "c"]
        assert db.commit.call_count == 2  # one per batch: resumable
        assert service.embed_documents.await_args_list[0].kwargs["title"] == "Manual Tecnico"

    async def test_unembeddable_chunk_is_skipped_not_retried_forever(self, db, stored, monkeypatch):
        counts = iter([(2, 1), (1, 1)])
        monkeypatch.setattr(reembed_kb, "count_pending", lambda _db: next(counts))
        seen_skips = []

        def next_batch(_db, size, skip):
            seen_skips.append(list(skip))
            return [_row("good"), _row("bad")] if not skip else []

        monkeypatch.setattr(reembed_kb, "next_batch", next_batch)
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=ValueError("batch rejected"))
        service.generate = AsyncMock(side_effect=[[0.1], None])

        result = await reembed_kb.reembed(
            db_factory=lambda: db, service=service, requests_per_minute=6000
        )

        assert result == {"done": 1, "skipped": 1, "remaining": 1}
        assert stored == ["good"]
        assert seen_skips[-1] == ["bad"]

    async def test_limit(self, db, stored, monkeypatch):
        monkeypatch.setattr(reembed_kb, "count_pending", lambda _db: (10, 1))
        sizes = []

        def next_batch(_db, size, skip):
            sizes.append(size)
            return [_row(f"c{len(sizes)}-{i}") for i in range(size)]

        monkeypatch.setattr(reembed_kb, "next_batch", next_batch)
        service = MagicMock()
        service.embed_documents = AsyncMock(side_effect=lambda texts, title: [[0.1]] * len(texts))

        result = await reembed_kb.reembed(
            db_factory=lambda: db, service=service, batch_size=4, limit=6, requests_per_minute=6000
        )

        assert sizes == [4, 2]
        assert result["done"] == 6


class TestMain:
    def test_rejects_bad_batch_size(self):
        with pytest.raises(SystemExit):
            reembed_kb.main(["--batch-size", "500"])

    def test_returns_1_on_failure(self, monkeypatch):
        monkeypatch.setattr(reembed_kb, "reembed", AsyncMock(side_effect=RuntimeError("boom")))
        assert reembed_kb.main([]) == 1
