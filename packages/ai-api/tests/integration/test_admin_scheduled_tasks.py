"""Integration tests for /admin/scheduled-tasks (new FleetView routes).

The DB is a MagicMock (as in test_admin_bug_reports.py): each test stubs the
query chain the route walks.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

AUTH = {"X-API-Key": "test-api-key"}
NOW = datetime.now(UTC).replace(tzinfo=None)


def task(**overrides):
    values = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="disabled",
        kind="reminder",
        text="drink water",
        schedule="every day at 09:00",
        timezone="America/Rio_Branco",
        address="5511900000001@s.whatsapp.net",
        client_id=None,
        cron_expr="0 9 * * *",
        interval_seconds=None,
        run_at=None,
        next_run_at=None,
        last_run_at=NOW,
        last_status="failed",
        last_error="blocked",
        disabled_reason="consecutive_failures",
        consecutive_failures=3,
        run_count=7,
        created_at=NOW,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def user():
    return SimpleNamespace(name="Ana", conversation_type="private")


def run(**overrides):
    values = dict(
        id=uuid.uuid4(),
        scheduled_for=NOW,
        status="failed",
        error_code="blocked",
        late_seconds=0,
        created_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_db(*, found=None, page=(), total=0, status_counts=(), runs=()):
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
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = list(
        runs
    )
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
    async def test_lists_with_counts(self, client_for):
        t = task()
        db = make_db(page=[(t, user())], total=1, status_counts=[("active", 4), ("disabled", 1)])
        async with client_for(db) as c:
            resp = await c.get("/admin/scheduled-tasks", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["counts_by_status"] == {
            "active": 4,
            "completed": 0,
            "cancelled": 0,
            "disabled": 1,
        }
        item = body["tasks"][0]
        assert item["id"] == str(t.id)
        assert item["whatsapp_jid"] == t.address
        assert item["disabled_reason"] == "consecutive_failures"
        assert item["user_name"] == "Ana"
        assert "runs" not in item

    async def test_unknown_status_rejected(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get("/admin/scheduled-tasks?status=paused", headers=AUTH)
        assert resp.status_code == 422

    async def test_requires_api_key(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get("/admin/scheduled-tasks")
        assert resp.status_code == 401


class TestDetail:
    async def test_includes_runs(self, client_for):
        t = task()
        db = make_db(found=(t, user()), runs=[run()])
        async with client_for(db) as c:
            resp = await c.get(f"/admin/scheduled-tasks/{t.id}", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["cron_expr"] == "0 9 * * *"
        assert body["runs"][0]["error_code"] == "blocked"

    async def test_404(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get(f"/admin/scheduled-tasks/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404


class TestUpdate:
    async def test_reenable_resets_failures_and_arms_next_slot(self, client_for):
        t = task()
        db = make_db(found=(t, user()))
        with patch("ai_api.routes.scheduled_tasks.active_count", return_value=0):
            async with client_for(db) as c:
                resp = await c.patch(
                    f"/admin/scheduled-tasks/{t.id}", json={"status": "active"}, headers=AUTH
                )
        assert resp.status_code == 200
        assert t.status == "active"
        assert t.consecutive_failures == 0
        assert t.disabled_reason is None
        assert t.next_run_at > NOW
        db.commit.assert_called_once()

    async def test_reenable_refused_when_the_chat_is_at_its_limit(self, client_for):
        t = task()
        db = make_db(found=(t, user()))
        with patch("ai_api.routes.scheduled_tasks.active_count", return_value=10):
            async with client_for(db) as c:
                resp = await c.patch(
                    f"/admin/scheduled-tasks/{t.id}", json={"status": "active"}, headers=AUTH
                )
        assert resp.status_code == 409
        assert "10 active" in resp.json()["detail"]
        assert t.status == "disabled"
        db.commit.assert_not_called()

    async def test_past_one_shot_cannot_be_reenabled(self, client_for):
        t = task(cron_expr=None, run_at=NOW - timedelta(hours=5), disabled_reason="missed")
        db = make_db(found=(t, user()))
        async with client_for(db) as c:
            resp = await c.patch(
                f"/admin/scheduled-tasks/{t.id}", json={"status": "active"}, headers=AUTH
            )
        assert resp.status_code == 409
        assert t.status == "disabled"

    async def test_cancel(self, client_for):
        t = task(status="active", next_run_at=NOW)
        db = make_db(found=(t, user()))
        async with client_for(db) as c:
            resp = await c.patch(
                f"/admin/scheduled-tasks/{t.id}", json={"status": "cancelled"}, headers=AUTH
            )
        assert resp.status_code == 200
        assert t.status == "cancelled" and t.next_run_at is None

    async def test_completed_is_final(self, client_for):
        t = task(status="completed")
        async with client_for(make_db(found=(t, user()))) as c:
            resp = await c.patch(
                f"/admin/scheduled-tasks/{t.id}", json={"status": "active"}, headers=AUTH
            )
        assert resp.status_code == 409


class TestDelete:
    async def test_deletes(self, client_for):
        t = task()
        db = make_db(found=(t, user()))
        async with client_for(db) as c:
            resp = await c.delete(f"/admin/scheduled-tasks/{t.id}", headers=AUTH)
        assert resp.status_code == 204
        db.delete.assert_called_once_with(t)


class TestOverview:
    async def test_counts_disabled_tasks(self, client_for):
        db = MagicMock()
        db.query.return_value.count.return_value = 0
        db.query.return_value.filter.return_value.count.return_value = 2
        async with client_for(db) as c:
            resp = await c.get("/admin/overview", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["disabled_scheduled_tasks"] == 2


class TestSettingsValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"bot_timezone": "Mars/Olympus"},
            {"scheduled_tasks_max_per_chat": 0},
            {"scheduled_tasks_min_interval_minutes": 0},
            {"scheduled_tasks_max_runs_per_day": 5000},
        ],
    )
    async def test_rejects_bad_values(self, client_for, overrides):
        async with client_for(MagicMock()) as c:
            resp = await c.patch("/admin/settings", json={"overrides": overrides}, headers=AUTH)
        assert resp.status_code == 400
