"""Integration tests for /admin/bug-reports (FleetView bug-report contract).

The DB is a MagicMock (as in test_admin_broadcasts.py): each test stubs the
query chain the route walks.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

AUTH = {"X-API-Key": "test-api-key"}
NOW = datetime.now(UTC).replace(tzinfo=None)


def report(**overrides):
    values = dict(
        id=uuid.uuid4(),
        source="model_error",
        status="open",
        category=None,
        title="AI model failed to answer",
        description="",
        user_id=uuid.uuid4(),
        whatsapp_jid="5511900000001@s.whatsapp.net",
        client_id="baileys",
        conversation_type="private",
        job_id="job-1",
        document_id=None,
        models="gemini-x",
        error_type="ModelHTTPError(503)",
        error_detail="Traceback …",
        context=[{"role": "user", "sender": None, "content": "hi", "at": NOW.isoformat()}],
        fingerprint="f" * 64,
        occurrences=3,
        created_at=NOW,
        last_seen_at=NOW,
        resolved_at=None,
        resolution_note=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_db(*, found=None, page=(), total=0, status_counts=()):
    db = MagicMock()
    joined = db.query.return_value.outerjoin.return_value
    joined.filter.return_value.first.return_value = found
    for chain in (
        joined,
        joined.filter.return_value,
        joined.filter.return_value.filter.return_value,
    ):
        chain.count.return_value = total
        chain.order_by.return_value.limit.return_value.offset.return_value.all.return_value = list(
            page
        )
    db.query.return_value.group_by.return_value = list(status_counts)
    return db


@pytest.fixture
def client_for():
    from ai_api.database import get_db
    from ai_api.main import app

    patches = [
        patch("ai_api.main.init_db"),
        patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
        patch("ai_api.main.cleanup_expired_documents"),
    ]
    for p in patches:
        p.start()

    def make(db):
        def override():
            yield db

        app.dependency_overrides[get_db] = override
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield make
    app.dependency_overrides.clear()
    for p in patches:
        p.stop()


class TestList:
    async def test_lists_summaries_with_counts(self, client_for):
        r = report()
        db = make_db(page=[(r, "Ana")], total=1, status_counts=[("open", 1), ("resolved", 4)])
        async with client_for(db) as c:
            resp = await c.get("/admin/bug-reports", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["counts_by_status"] == {"open": 1, "resolved": 4, "ignored": 0}
        item = body["reports"][0]
        assert item["id"] == str(r.id)
        assert item["user_name"] == "Ana"
        assert item["occurrences"] == 3
        # The heavy fields stay on the detail route.
        assert "context" not in item and "error_detail" not in item

    async def test_filters(self, client_for):
        db = make_db()
        async with client_for(db) as c:
            resp = await c.get(
                "/admin/bug-reports?status=open&source=agent&limit=10&offset=20", headers=AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["limit"] == 10 and resp.json()["offset"] == 20
        joined = db.query.return_value.outerjoin.return_value
        assert joined.filter.call_count == 1
        assert joined.filter.return_value.filter.call_count == 1

    async def test_unknown_status_rejected(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get("/admin/bug-reports?status=closed", headers=AUTH)
        assert resp.status_code == 422

    async def test_requires_api_key(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get("/admin/bug-reports")
        assert resp.status_code == 401


class TestDetail:
    async def test_includes_context_and_error(self, client_for):
        r = report()
        async with client_for(make_db(found=(r, None))) as c:
            resp = await c.get(f"/admin/bug-reports/{r.id}", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["context"][0]["content"] == "hi"
        assert body["error_detail"] == "Traceback …"
        assert body["models"] == "gemini-x"

    async def test_cleaned_context_is_null(self, client_for):
        r = report(context=None)
        async with client_for(make_db(found=(r, None))) as c:
            resp = await c.get(f"/admin/bug-reports/{r.id}", headers=AUTH)
        assert resp.json()["context"] is None

    async def test_unknown_404(self, client_for):
        async with client_for(make_db(found=None)) as c:
            resp = await c.get(f"/admin/bug-reports/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404


class TestUpdate:
    async def test_resolve_sets_timestamp_and_note(self, client_for):
        r = report()
        db = make_db(found=(r, None))
        async with client_for(db) as c:
            resp = await c.patch(
                f"/admin/bug-reports/{r.id}",
                json={"status": "resolved", "resolution_note": "fixed in v2"},
                headers=AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"
        assert resp.json()["resolved_at"] is not None
        assert resp.json()["resolution_note"] == "fixed in v2"
        db.commit.assert_called_once()

    async def test_reopen_clears_timestamp(self, client_for):
        r = report(status="resolved", resolved_at=NOW)
        async with client_for(make_db(found=(r, None))) as c:
            resp = await c.patch(
                f"/admin/bug-reports/{r.id}", json={"status": "open"}, headers=AUTH
            )
        assert resp.json()["resolved_at"] is None

    async def test_invalid_status_422(self, client_for):
        r = report()
        async with client_for(make_db(found=(r, None))) as c:
            resp = await c.patch(
                f"/admin/bug-reports/{r.id}", json={"status": "done"}, headers=AUTH
            )
        assert resp.status_code == 422

    async def test_db_error_500(self, client_for):
        r = report()
        db = make_db(found=(r, None))
        db.commit.side_effect = RuntimeError("db down")
        async with client_for(db) as c:
            resp = await c.patch(
                f"/admin/bug-reports/{r.id}", json={"status": "ignored"}, headers=AUTH
            )
        assert resp.status_code == 500
        assert "db down" not in resp.text
        db.rollback.assert_called_once()

    async def test_unknown_404(self, client_for):
        async with client_for(make_db(found=None)) as c:
            resp = await c.patch(
                f"/admin/bug-reports/{uuid.uuid4()}", json={"status": "open"}, headers=AUTH
            )
        assert resp.status_code == 404


class TestDelete:
    async def test_deletes(self, client_for):
        r = report()
        db = make_db(found=(r, None))
        async with client_for(db) as c:
            resp = await c.delete(f"/admin/bug-reports/{r.id}", headers=AUTH)
        assert resp.status_code == 204
        db.delete.assert_called_once_with(r)
        db.commit.assert_called_once()

    async def test_unknown_404(self, client_for):
        async with client_for(make_db(found=None)) as c:
            resp = await c.delete(f"/admin/bug-reports/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404


class TestSettingValidation:
    async def test_negative_cap_rejected(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.patch(
                "/admin/settings",
                json={"overrides": {"bug_reports_per_user_per_hour": -1}},
                headers=AUTH,
            )
        assert resp.status_code == 400
