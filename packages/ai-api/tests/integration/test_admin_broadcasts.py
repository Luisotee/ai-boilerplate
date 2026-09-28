"""Integration tests for /admin/broadcasts (FleetView broadcast contract).

The DB is a MagicMock (as in test_admin_routes.py); chat-client reachability
and the audience query are patched so each test controls who is in the bot.
The real SQL (advisory lock, conditional updates, grouped counts) is exercised
against Postgres separately — see the PR's verification notes.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

AUTH = {"X-API-Key": "test-api-key"}
NOW = datetime.now(UTC).replace(tzinfo=None)
ALL_UP = {"baileys": True, "cloud": True, "telegram": True}

RUNTIME = {
    "whitelist_phones": "",
    "broadcast_footer": "_Send /broadcast off to stop these._",
    "broadcast_min_delay_seconds": 20,
    "broadcast_max_delay_seconds": 60,
    "broadcast_batch_size": 15,
    "broadcast_batch_pause_seconds": 600,
    "broadcast_daily_limit": 150,
    "broadcast_send_window": "09:00-21:00",
}


def user(
    jid, *, cloud_at=None, opt_out=False, telegram_jid=None, kind="private", wa_client="baileys"
):
    cloud = cloud_at is not None
    return SimpleNamespace(
        id=uuid.uuid4(),
        whatsapp_jid=jid,
        whatsapp_lid=None,
        telegram_jid=telegram_jid,
        last_client_id="cloud" if cloud else None,
        whatsapp_client_id="cloud" if cloud else wa_client,
        cloud_last_inbound_at=cloud_at if cloud_at is not False else None,
        broadcast_opt_out=opt_out,
        conversation_type=kind,
        phone=None,
    )


USERS = [
    user("5511900000001@s.whatsapp.net"),
    user("5511900000002@s.whatsapp.net", opt_out=True),
    user("tg:42"),
    user("5511900000003@s.whatsapp.net", cloud_at=NOW - timedelta(hours=1)),
    user("5511900000004@s.whatsapp.net", cloud_at=NOW - timedelta(days=5)),
    # From before client tracking: Baileys or Cloud? Unknown while Cloud is up.
    user("5511900000005@s.whatsapp.net", wa_client=None),
]


def broadcast(status="running", **overrides):
    values = dict(
        id=uuid.uuid4(),
        text="New feature",
        footer="_opt out_",
        audience="private",
        platforms=["baileys", "telegram"],
        status=status,
        pause_reason=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_db(*, first=None, count_rows=(), target=None, updated=1):
    """A mock session.

    ``first``: what "find by idempotency key" / "is one active?" return.
    ``count_rows``: grouped (broadcast_id, platform, status, count) rows.
    ``target`` + ``updated``: the conditional UPDATE applies its values to
    ``target`` and reports ``updated`` rows (0 = someone changed it first).
    """
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.first.return_value = first
    chain.filter.return_value.first.return_value = first
    chain.group_by.return_value.all.return_value = list(count_rows)

    def update(values, synchronize_session=None):
        if updated and target is not None:
            target.__dict__.update(values)
        return updated

    chain.update.side_effect = update
    added = []

    def flush():
        for obj in added:
            obj.id = obj.id or uuid.uuid4()
            obj.created_at = obj.created_at or NOW

    db.add.side_effect = added.append
    db.flush.side_effect = flush
    db.added = added
    return db


def advisory_lock_taken(db) -> bool:
    return any("pg_advisory_xact_lock" in str(c.args[0]) for c in db.execute.call_args_list)


@pytest.fixture
def client_for():
    from ai_api.database import get_db
    from ai_api.main import app

    patches = [
        patch("ai_api.main.init_db"),
        patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
        patch("ai_api.main.cleanup_expired_documents"),
        patch("ai_api.routes.broadcasts.runtime_config.get", side_effect=RUNTIME.__getitem__),
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


def reachable(mapping=ALL_UP):
    return patch("ai_api.routes.broadcasts.probe_platforms", AsyncMock(return_value=mapping))


def audience(users=USERS):
    return patch("ai_api.routes.broadcasts.audience_users", return_value=list(users))


class TestPlatforms:
    async def test_lists_reachability(self, client_for):
        with reachable({"baileys": True, "cloud": False, "telegram": True}):
            async with client_for(make_db()) as c:
                resp = await c.get("/admin/broadcasts/platforms", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["platforms"] == [
            {"platform": "baileys", "reachable": True},
            {"platform": "cloud", "reachable": False},
            {"platform": "telegram", "reachable": True},
        ]

    async def test_requires_api_key(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.get("/admin/broadcasts/platforms")
        assert resp.status_code == 401


class TestPreview:
    async def test_counts_per_platform(self, client_for):
        with reachable(), audience():
            async with client_for(make_db()) as c:
                resp = await c.post("/admin/broadcasts/preview", json={}, headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["audience"] == "private"
        assert data["platforms"] == ["baileys", "cloud", "telegram"]
        assert data["total_recipients"] == 3  # baileys + tg + cloud-in-window
        assert data["opted_out"] == 1
        assert data["skipped_cloud_window"] == 1
        assert data["skipped_unknown_client"] == 1
        per = {p["platform"]: p for p in data["per_platform"]}
        assert per["baileys"]["recipients"] == 1
        assert per["cloud"] == {"platform": "cloud", "recipients": 1, "skipped_cloud_window": 1}
        assert data["estimated_baileys_seconds"] > 0

    async def test_platform_selection(self, client_for):
        with reachable(), audience():
            async with client_for(make_db()) as c:
                resp = await c.post(
                    "/admin/broadcasts/preview", json={"platforms": ["telegram"]}, headers=AUTH
                )
        data = resp.json()
        assert data["platforms"] == ["telegram"]
        assert data["total_recipients"] == 1
        assert data["no_selected_platform"] == 4

    async def test_unreachable_explicit_platform_is_400(self, client_for):
        with reachable({"baileys": True, "cloud": False, "telegram": True}), audience():
            async with client_for(make_db()) as c:
                resp = await c.post(
                    "/admin/broadcasts/preview", json={"platforms": ["cloud"]}, headers=AUTH
                )
        assert resp.status_code == 400
        assert "cloud" in resp.json()["detail"]

    async def test_default_leaves_out_unreachable_clients(self, client_for):
        with reachable({"baileys": True, "cloud": False, "telegram": False}), audience():
            async with client_for(make_db()) as c:
                resp = await c.post("/admin/broadcasts/preview", json={}, headers=AUTH)
        assert resp.json()["platforms"] == ["baileys"]
        # No Cloud client deployed: an untracked WhatsApp row can only be Baileys.
        assert resp.json()["skipped_unknown_client"] == 0
        assert resp.json()["per_platform"][0]["recipients"] == 2

    async def test_nothing_reachable_is_503(self, client_for):
        with reachable({"baileys": False, "cloud": False, "telegram": False}), audience():
            async with client_for(make_db()) as c:
                resp = await c.post("/admin/broadcasts/preview", json={}, headers=AUTH)
        assert resp.status_code == 503

    @pytest.mark.parametrize(
        "body", [{"audience": "everyone"}, {"platforms": ["sms"]}, {"platforms": []}]
    )
    async def test_validation(self, client_for, body):
        async with client_for(make_db()) as c:
            resp = await c.post("/admin/broadcasts/preview", json=body, headers=AUTH)
        assert resp.status_code == 422


class TestCreate:
    async def test_snapshots_recipients_and_queues_under_the_lock(self, client_for):
        db = make_db()
        with reachable(), audience():
            async with client_for(db) as c:
                resp = await c.post(
                    "/admin/broadcasts",
                    json={"text": "  Voice replies are here!  ", "audience": "private"},
                    headers=AUTH,
                )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "queued"
        assert data["text"] == "Voice replies are here!"
        assert data["footer"] == RUNTIME["broadcast_footer"]
        assert data["platforms"] == ["baileys", "cloud", "telegram"]
        assert advisory_lock_taken(db)

        recipients = list(db.add_all.call_args.args[0])
        by_status = sorted((r.platform, r.status, r.error_code) for r in recipients)
        assert by_status == [
            ("baileys", "pending", None),
            ("baileys", "skipped", "unknown_client"),
            ("cloud", "pending", None),
            ("cloud", "skipped", "cloud_window"),
            ("telegram", "pending", None),
        ]
        db.commit.assert_called_once()

    async def test_idempotent_replay_returns_existing_with_200(self, client_for):
        existing = broadcast("running")
        db = make_db(first=existing)
        with reachable(), audience():
            async with client_for(db) as c:
                resp = await c.post(
                    "/admin/broadcasts",
                    json={"text": "New feature", "idempotency_key": "abc"},
                    headers=AUTH,
                )
        assert resp.status_code == 200
        assert resp.json()["id"] == str(existing.id)
        db.add.assert_not_called()

    async def test_idempotency_key_reused_for_a_different_message_is_422(self, client_for):
        db = make_db(first=broadcast("completed", text="Old news"))
        with reachable(), audience():
            async with client_for(db) as c:
                resp = await c.post(
                    "/admin/broadcasts",
                    json={"text": "Brand new feature", "idempotency_key": "abc"},
                    headers=AUTH,
                )
        assert resp.status_code == 422
        assert "different broadcast" in resp.json()["detail"]
        db.add.assert_not_called()

    async def test_conflicts_with_an_active_broadcast(self, client_for):
        db = make_db(first=broadcast("running"))
        with reachable(), audience():
            async with client_for(db) as c:
                resp = await c.post("/admin/broadcasts", json={"text": "Hi"}, headers=AUTH)
        assert resp.status_code == 409
        # The check ran under the lock, and the lock was released (rollback).
        assert advisory_lock_taken(db)
        db.rollback.assert_called()
        db.add.assert_not_called()

    @pytest.mark.parametrize("text", ["", "x" * 3501])
    async def test_text_validation(self, client_for, text):
        async with client_for(make_db()) as c:
            resp = await c.post("/admin/broadcasts", json={"text": text}, headers=AUTH)
        assert resp.status_code == 422

    async def test_blank_text_is_400(self, client_for):
        async with client_for(make_db()) as c:
            resp = await c.post("/admin/broadcasts", json={"text": "   "}, headers=AUTH)
        assert resp.status_code == 400


class TestReadAndTransitions:
    async def test_detail_counts(self, client_for):
        b = broadcast("running")
        db = make_db(
            count_rows=[
                (b.id, "baileys", "sent", 3),
                (b.id, "baileys", "pending", 5),
                (b.id, "telegram", "failed", 1),
            ]
        )
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.get(f"/admin/broadcasts/{b.id}", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["counts"] == {"total": 9, "pending": 5, "sent": 3, "failed": 1, "skipped": 0}
        per = {p["platform"]: p for p in data["per_platform"]}
        assert per["baileys"]["sent"] == 3 and per["telegram"]["failed"] == 1

    async def test_list_counts_every_broadcast_in_one_query(self, client_for):
        first, second = broadcast("completed"), broadcast("running")
        db = make_db(
            count_rows=[(first.id, "baileys", "sent", 2), (second.id, "telegram", "pending", 4)]
        )
        db.query.return_value.count.return_value = 2
        page = db.query.return_value.order_by.return_value.limit.return_value.offset.return_value
        page.all.return_value = [first, second]
        async with client_for(db) as c:
            resp = await c.get("/admin/broadcasts", headers=AUTH)
        assert resp.status_code == 200
        counts = [b["counts"] for b in resp.json()["broadcasts"]]
        assert counts[0]["sent"] == 2 and counts[1]["pending"] == 4
        grouped = db.query.return_value.filter.return_value.group_by
        assert grouped.call_count == 1  # not one count query per broadcast

    async def test_unknown_broadcast_404(self, client_for):
        db = make_db()
        db.get.return_value = None
        async with client_for(db) as c:
            resp = await c.get(f"/admin/broadcasts/{uuid.uuid4()}", headers=AUTH)
        assert resp.status_code == 404

    async def test_pause_running(self, client_for):
        b = broadcast("running")
        db = make_db(target=b)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/pause", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"
        assert resp.json()["pause_reason"] == "manual"

    async def test_resume_paused_takes_the_lock(self, client_for):
        b = broadcast("paused", pause_reason="consecutive_failures")
        db = make_db(first=None, target=b)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/resume", headers=AUTH)
        assert resp.json()["status"] == "running"
        assert resp.json()["pause_reason"] is None
        assert advisory_lock_taken(db)

    async def test_resume_blocked_by_another_active_broadcast(self, client_for):
        b = broadcast("paused")
        db = make_db(first=broadcast("queued"), target=b)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/resume", headers=AUTH)
        assert resp.status_code == 409
        assert b.status == "paused"

    async def test_cancel(self, client_for):
        b = broadcast("paused")
        db = make_db(target=b)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/cancel", headers=AUTH)
        assert resp.json()["status"] == "cancelled"
        assert b.finished_at is not None

    async def test_transition_lost_to_a_concurrent_change_is_409(self, client_for):
        """The worker completed it between our read and our write: don't overwrite."""
        b = broadcast("running")
        db = make_db(target=b, updated=0)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/pause", headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Cannot pause a broadcast that is running"
        db.commit.assert_not_called()

    @pytest.mark.parametrize(
        "status,action", [("completed", "cancel"), ("running", "resume"), ("cancelled", "pause")]
    )
    async def test_invalid_transitions_409(self, client_for, status, action):
        b = broadcast(status)
        db = make_db(target=b)
        db.get.return_value = b
        async with client_for(db) as c:
            resp = await c.post(f"/admin/broadcasts/{b.id}/{action}", headers=AUTH)
        assert resp.status_code == 409
        assert b.status == status
