"""
Integration tests for the knowledge-base upload routes.

Covers:
- uploads are enqueued on the PDF stream (not processed in the API process)
- SHA-256 dedup: 409 on the single route, per-file rejection in a batch,
  intra-batch duplicates, conversation-scoped / failed rows never block
- enqueue failure rolls the upload back (503 / rejected)
- both upload routes are exempt from rate limiting
"""

import hashlib
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

AUTH_HEADERS = {"X-API-Key": "test-api-key"}
PDF_A = b"%PDF-1.4 alpha"
PDF_B = b"%PDF-1.4 bravo"


def _mock_db(existing=None):
    """Session whose duplicate lookup returns `existing` (a doc or None)."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    return db


def _existing_doc(name="old.pdf", status="completed"):
    doc = MagicMock()
    doc.id = "11111111-1111-1111-1111-111111111111"
    doc.original_filename = name
    doc.status = status
    return doc


@pytest.fixture
def upload_dir(tmp_path):
    with patch("ai_api.routes.knowledge_base.UPLOAD_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def enqueue():
    """Patch the Redis client + enqueue call used by the routes."""
    redis = AsyncMock()

    @asynccontextmanager
    async def _client():
        yield redis

    mock = AsyncMock(return_value="1-0")
    with (
        patch("ai_api.routes.knowledge_base.get_redis_client", _client),
        patch("ai_api.routes.knowledge_base.enqueue_pdf_processing", mock),
    ):
        yield mock


@asynccontextmanager
async def _client_for(db):
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _added_documents(db):
    return [call.args[0] for call in db.add.call_args_list]


class TestSingleUpload:
    async def test_upload_is_enqueued_with_hash(self, upload_dir, enqueue):
        db = _mock_db()
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload",
                files={"file": ("a.pdf", PDF_A, "application/pdf")},
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"

        (doc,) = _added_documents(db)
        assert doc.status == "queued"
        assert doc.file_hash == hashlib.sha256(PDF_A).hexdigest()

        enqueue.assert_awaited_once()
        kwargs = enqueue.await_args.kwargs
        assert kwargs["document_id"] == body["document_id"]
        stored = upload_dir / f"{body['document_id']}.pdf"
        assert kwargs["file_path"] == str(stored)
        assert stored.read_bytes() == PDF_A

    async def test_duplicate_returns_409_and_discards_file(self, upload_dir, enqueue):
        db = _mock_db(existing=_existing_doc("handbook.pdf"))
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload",
                files={"file": ("copy.pdf", PDF_A, "application/pdf")},
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 409
        assert "handbook.pdf" in response.json()["detail"]
        assert list(upload_dir.iterdir()) == []
        db.add.assert_not_called()
        enqueue.assert_not_awaited()

    async def test_duplicate_lookup_filters_scope_and_status(self, upload_dir, enqueue):
        db = _mock_db()
        async with _client_for(db) as client:
            await client.post(
                "/knowledge-base/upload",
                files={"file": ("a.pdf", PDF_A, "application/pdf")},
                headers=AUTH_HEADERS,
            )
        criteria = [str(c) for c in db.query.return_value.filter.call_args.args]
        assert any("file_hash" in c for c in criteria)
        assert any("is_conversation_scoped" in c for c in criteria)
        assert any("status" in c for c in criteria)

    async def test_enqueue_failure_rolls_back_upload(self, upload_dir, enqueue):
        enqueue.side_effect = ConnectionError("redis down")
        db = _mock_db()
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload",
                files={"file": ("a.pdf", PDF_A, "application/pdf")},
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 503
        assert "redis down" not in response.text
        (doc,) = _added_documents(db)
        db.delete.assert_called_once_with(doc)
        assert list(upload_dir.iterdir()) == []

    async def test_non_pdf_rejected(self, upload_dir, enqueue):
        async with _client_for(_mock_db()) as client:
            response = await client.post(
                "/knowledge-base/upload",
                files={"file": ("a.txt", b"hello", "text/plain")},
                headers=AUTH_HEADERS,
            )
        assert response.status_code == 400
        enqueue.assert_not_awaited()


class TestBatchUpload:
    async def test_batch_enqueues_each_file(self, upload_dir, enqueue):
        db = _mock_db()
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload/batch",
                files=[
                    ("files", ("a.pdf", PDF_A, "application/pdf")),
                    ("files", ("b.pdf", PDF_B, "application/pdf")),
                ],
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 2
        assert enqueue.await_count == 2
        hashes = {d.file_hash for d in _added_documents(db)}
        assert hashes == {hashlib.sha256(PDF_A).hexdigest(), hashlib.sha256(PDF_B).hexdigest()}

    async def test_intra_batch_duplicate_rejected(self, upload_dir, enqueue):
        db = _mock_db()
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload/batch",
                files=[
                    ("files", ("a.pdf", PDF_A, "application/pdf")),
                    ("files", ("a-copy.pdf", PDF_A, "application/pdf")),
                ],
                headers=AUTH_HEADERS,
            )

        body = response.json()
        assert body["accepted"] == 1
        assert body["rejected"] == 1
        rejected = [r for r in body["results"] if r["status"] == "rejected"]
        assert rejected[0]["filename"] == "a-copy.pdf"
        assert "a.pdf" in rejected[0]["error"]
        assert enqueue.await_count == 1
        assert len(list(upload_dir.iterdir())) == 1

    async def test_existing_duplicate_rejected_per_file(self, upload_dir, enqueue):
        db = _mock_db(existing=_existing_doc("handbook.pdf"))
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload/batch",
                files=[("files", ("a.pdf", PDF_A, "application/pdf"))],
                headers=AUTH_HEADERS,
            )
        body = response.json()
        assert body["accepted"] == 0
        assert "handbook.pdf" in body["results"][0]["error"]
        enqueue.assert_not_awaited()

    async def test_save_error_is_generic_and_rolls_back(self, upload_dir, enqueue):
        db = _mock_db()
        db.commit.side_effect = RuntimeError("psycopg2 OperationalError at db.internal:5432")
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload/batch",
                files=[("files", ("a.pdf", PDF_A, "application/pdf"))],
                headers=AUTH_HEADERS,
            )
        result = response.json()["results"][0]
        assert result["status"] == "rejected"
        assert result["error"] == "Failed to save file"
        assert "db.internal" not in response.text
        db.rollback.assert_called()
        assert list(upload_dir.iterdir()) == []

    async def test_enqueue_failure_rejects_file(self, upload_dir, enqueue):
        enqueue.side_effect = ConnectionError("redis down")
        db = _mock_db()
        async with _client_for(db) as client:
            response = await client.post(
                "/knowledge-base/upload/batch",
                files=[("files", ("a.pdf", PDF_A, "application/pdf"))],
                headers=AUTH_HEADERS,
            )
        result = response.json()["results"][0]
        assert result["status"] == "rejected"
        assert "queue unavailable" in result["error"]
        db.delete.assert_called_once()


class TestRateLimitExemption:
    @pytest.mark.parametrize("path", ["/knowledge-base/upload", "/knowledge-base/upload/batch"])
    def test_upload_routes_are_exempt(self, path):
        from ai_api.deps import limiter
        from ai_api.main import app

        route = next(r for r in app.routes if getattr(r, "path", None) == path)
        name = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        assert name in limiter._exempt_routes
