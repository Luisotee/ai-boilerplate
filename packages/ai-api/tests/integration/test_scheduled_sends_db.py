"""Scheduled sends (Envios programados) against a real database (SQLite in memory).

Mocked sessions can't show the facts the design rests on: that a slot fires
once (``UNIQUE(scheduled_send_id, scheduled_for)``), that a run makes one
broadcast, that a send's opt-outs and per-recipient texts reach the broadcast
lane, and that run broadcasts don't block a manual one. ``create_engine`` comes
from ``sqlalchemy.engine`` because ``tests/conftest.py`` patches
``sqlalchemy.create_engine``.
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import create_engine  # NOT sqlalchemy.create_engine (conftest patch)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ai_api.config import settings
from ai_api.database import (
    Base,
    Broadcast,
    BroadcastLaneState,
    BroadcastRecipient,
    BroadcastRecipientText,
    ScheduledSend,
    ScheduledSendOptOut,
    ScheduledSendRun,
    ScheduledSendRunText,
    User,
)
from ai_api.scheduled_sends import runner
from ai_api.scheduled_sends.agent_run import GeneratedText
from ai_api.streams import broadcast_consumer as bc


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):
    return "JSON"


TABLES = [
    User.__table__,
    Broadcast.__table__,
    BroadcastRecipient.__table__,
    BroadcastLaneState.__table__,
    ScheduledSend.__table__,
    ScheduledSendRun.__table__,
    ScheduledSendRunText.__table__,
    BroadcastRecipientText.__table__,
    ScheduledSendOptOut.__table__,
]
GROUP_A = "120363000000000001@g.us"
GROUP_B = "120363000000000002@g.us"
PRIVATE = "5511900000001@s.whatsapp.net"
TG_PRIVATE = "tg:4242"
RUNTIME = {
    "whitelist_phones": "",
    "broadcast_footer": "_broadcast off_",
    "scheduled_send_footer": "_/broadcast off {number}_",
    "bot_timezone": "America/Sao_Paulo",
}


def _runtime_get(key):
    return RUNTIME[key] if key in RUNTIME else getattr(settings, key)


def _patch_runtime(monkeypatch):
    """Patch ``get`` on the CLASS: an instance attribute would survive the undo
    (monkeypatch restores it as a bound method on the instance) and shadow
    later class-level patches such as test_prompt_surface.py's."""
    from ai_api.runtime_config import runtime_config

    monkeypatch.setattr(type(runtime_config), "get", lambda _self, key: _runtime_get(key))


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine, tables=TABLES)
    make = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    for module in ("ai_api.scheduled_sends.runner", "ai_api.streams.broadcast_consumer"):
        monkeypatch.setattr(f"{module}.SessionLocal", make)
    _patch_runtime(monkeypatch)
    yield make
    engine.dispose()


def add_users(db):
    db.add_all(
        [
            User(whatsapp_jid=GROUP_A, conversation_type="group", name="Group A"),
            User(whatsapp_jid=GROUP_B, conversation_type="group", name="Group B"),
            User(whatsapp_jid=PRIVATE, conversation_type="private", phone="5511900000001"),
            User(whatsapp_jid=TG_PRIVATE, conversation_type="private", last_client_id="telegram"),
        ]
    )
    db.commit()


def add_send(db, **overrides) -> ScheduledSend:
    values = dict(
        number=(max([s.number for s in db.query(ScheduledSend)] or [0]) + 1),
        name="Weekly digest",
        content_type="text",
        content="Hello everyone",
        audience="groups",
        schedule={"kind": "weekly", "time": "08:00", "weekdays": [1]},
        cron_expr="0 8 * * 1",
        timezone="America/Sao_Paulo",
        status="active",
        next_run_at=datetime(2026, 11, 23, 11, 0),
    )
    values.update(overrides)
    send = ScheduledSend(**values)
    db.add(send)
    db.commit()
    db.refresh(send)
    return send


def reachable(baileys=True, cloud=False, telegram=True):
    return patch(
        "ai_api.scheduled_sends.runner.probe_platforms",
        AsyncMock(return_value={"baileys": baileys, "cloud": cloud, "telegram": telegram}),
    )


# --- Firing ---------------------------------------------------------------------


class TestFire:
    def test_a_due_slot_fires_once_and_arms_the_next(self, factory):
        db = factory()
        send = add_send(db)
        slot = send.next_run_at
        assert runner.fire_due(slot + timedelta(minutes=1)) == 1
        assert runner.fire_due(slot + timedelta(minutes=2)) == 0
        db.expire_all()
        runs = db.query(ScheduledSendRun).all()
        assert [(r.scheduled_for, r.status) for r in runs] == [(slot, "preparing")]
        assert db.get(ScheduledSend, send.id).next_run_at == slot + timedelta(days=7)

    def test_the_slots_run_row_is_unique(self, factory):
        """Even a second scheduler that saw the old next_run_at can't fire it again."""
        db = factory()
        send = add_send(db)
        slot = send.next_run_at
        runner.fire_due(slot + timedelta(minutes=1))
        db.query(ScheduledSend).update({"next_run_at": slot})  # a stale view of the row
        db.commit()
        assert runner.fire_due(slot + timedelta(minutes=1)) == 0
        assert db.query(ScheduledSendRun).count() == 1

    def test_a_slot_older_than_its_catch_up_window_is_missed_not_sent(self, factory):
        db = factory()
        send = add_send(db)
        slot = send.next_run_at
        now = slot + timedelta(hours=3)  # weekly: catch-up window is 2 h
        assert runner.fire_due(now) == 0
        db.expire_all()
        run = db.query(ScheduledSendRun).one()
        assert (run.status, run.error) == ("missed", "missed")
        # The next slot is the first after now: no burst of missed slots.
        assert db.get(ScheduledSend, send.id).next_run_at == slot + timedelta(days=7)

    def test_a_one_shot_finishes_after_it_fires(self, factory):
        db = factory()
        when = datetime(2026, 11, 25, 12, 0)
        send = add_send(db, cron_expr=None, run_at=when, next_run_at=when, schedule={})
        runner.fire_due(when + timedelta(minutes=1))
        db.expire_all()
        fresh = db.get(ScheduledSend, send.id)
        assert (fresh.status, fresh.next_run_at) == ("finished", None)

    def test_paused_sends_never_fire(self, factory):
        db = factory()
        send = add_send(db, status="paused")
        assert runner.fire_due(send.next_run_at + timedelta(minutes=1)) == 0


# --- Preparing and hand-over --------------------------------------------------------


async def _fire_and_prepare(factory, send, generate=None, **reach):
    runner.fire_due(send.next_run_at + timedelta(minutes=1))
    db = factory()
    run = db.query(ScheduledSendRun).filter_by(scheduled_send_id=send.id).one()
    token = runner.claim_run(run.id, datetime.utcnow())
    assert token is not None
    gen = AsyncMock(side_effect=generate) if generate else AsyncMock()
    with reachable(**reach), patch("ai_api.scheduled_sends.runner.generate_text", gen):
        status = await runner.prepare_run(run.id, token, http_client=AsyncMock())
    db.expire_all()
    return status, db.get(ScheduledSendRun, run.id), gen


class TestPrepare:
    async def test_a_text_send_becomes_one_queued_broadcast_with_the_footer(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        status, run, _ = await _fire_and_prepare(factory, send)
        assert status == "delivering"
        broadcast = db.get(Broadcast, run.broadcast_id)
        assert broadcast.status == "queued" and broadcast.audience == "groups"
        assert broadcast.footer == f"_broadcast off_\n_/broadcast off {send.number}_"
        addresses = {r.address for r in db.query(BroadcastRecipient)}
        assert addresses == {GROUP_A, GROUP_B}

    async def test_telegram_chats_are_reached_too(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, audience="private")
        await _fire_and_prepare(factory, send)
        routes = {(r.address, r.platform) for r in db.query(BroadcastRecipient)}
        assert routes == {(PRIVATE, "baileys"), (TG_PRIVATE, "telegram")}

    async def test_a_claimed_run_cannot_be_handed_over_twice(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        status, run, _ = await _fire_and_prepare(factory, send)
        assert status == "delivering"
        # A second worker with a stale claim token writes nothing.
        prepared = runner.Prepared(platforms=["baileys"], audience="groups", broadcast_text="x")
        snap = runner.SendSnapshot.of(db.get(ScheduledSend, send.id))
        assert runner.hand_over(run.id, datetime(2000, 1, 1), snap, prepared) is None
        assert db.query(Broadcast).count() == 1

    async def test_a_prompt_runs_the_agent_once_per_chat_type(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, audience="all", content_type="prompt", content="This week's news")

        async def generate(*, content, chat_type, http_client):
            return GeneratedText(text=f"Text for {chat_type}")

        status, run, gen = await _fire_and_prepare(factory, send, generate)
        assert status == "delivering"
        assert sorted(c.kwargs["chat_type"] for c in gen.await_args_list) == ["group", "private"]
        own = {
            r.address: db.get(BroadcastRecipientText, r.id).text
            for r in db.query(BroadcastRecipient)
        }
        assert own == {
            GROUP_A: "Text for group",
            GROUP_B: "Text for group",
            PRIVATE: "Text for private",
            TG_PRIVATE: "Text for private",
        }
        assert db.query(ScheduledSendRunText).filter_by(run_id=run.id).count() == 2

    async def test_a_failed_chat_type_skips_only_its_chats(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, audience="all", content_type="prompt", content="News")

        async def generate(*, content, chat_type, http_client):
            if chat_type == "private":
                return GeneratedText(error="no_send")
            return GeneratedText(text="ok")

        status, _run, _ = await _fire_and_prepare(factory, send, generate)
        assert status == "delivering"
        by_address = {r.address: (r.status, r.error_code) for r in db.query(BroadcastRecipient)}
        assert by_address[GROUP_A] == ("pending", None)
        assert by_address[PRIVATE] == ("skipped", "generation_failed")

    async def test_every_generation_failing_sends_nothing(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, content_type="prompt", content="News")

        async def generate(**_):
            return GeneratedText(error="timeout")

        status, run, _ = await _fire_and_prepare(factory, send, generate)
        assert (status, run.status, run.error) == ("failed", "failed", "generation_failed")
        assert db.query(Broadcast).count() == 0

    async def test_no_reachable_client_fails_the_run(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        status, run, _ = await _fire_and_prepare(
            factory, send, baileys=False, cloud=False, telegram=False
        )
        assert (status, run.error) == ("failed", "clients_unreachable")

    async def test_a_cancel_that_lands_before_the_hand_over_delivers_nothing(self, factory):
        """The hand-over re-reads the send (FOR UPDATE on Postgres; SQLite can't
        show the lock, only that a cancel committed first wins)."""
        db = factory()
        add_users(db)
        send = add_send(db)
        runner.fire_due(send.next_run_at + timedelta(minutes=1))
        run = db.query(ScheduledSendRun).one()
        token = runner.claim_run(run.id, datetime.utcnow())
        snap = runner.SendSnapshot.of(send)  # taken while still active
        db.query(ScheduledSend).update({"status": "cancelled"})
        db.commit()
        prepared = runner.Prepared(
            platforms=["baileys"],
            audience="groups",
            targets=[runner.Target(user_id=uuid.uuid4(), platform="baileys", address=GROUP_A)],
            texts=[],
            broadcast_text="x",
        )
        assert runner.hand_over(run.id, token, snap, prepared) is None
        db.expire_all()
        assert db.get(ScheduledSendRun, run.id).status == "cancelled"
        assert db.query(Broadcast).count() == 0

    async def test_shutdown_while_preparing_gives_the_claim_back(self, factory):
        import asyncio

        db = factory()
        send = add_send(db)
        runner.fire_due(send.next_run_at + timedelta(minutes=1))
        run = db.query(ScheduledSendRun).one()
        token = runner.claim_run(run.id, datetime.utcnow())
        with (
            patch.object(runner, "prepare_run", AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await runner.safe_prepare(run.id, token, AsyncMock())
        db.expire_all()
        fresh = db.get(ScheduledSendRun, run.id)
        assert (fresh.status, fresh.claimed_at) == ("preparing", None)
        assert run.id in runner.claimable_run_ids(datetime.utcnow())

    async def test_no_recipients_completes_without_a_broadcast(self, factory):
        db = factory()
        send = add_send(db)  # nobody in the DB
        status, run, _ = await _fire_and_prepare(factory, send)
        assert (status, run.error) == ("completed", "no_recipients")
        assert db.query(Broadcast).count() == 0

    async def test_a_send_cancelled_while_preparing_delivers_nothing(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        runner.fire_due(send.next_run_at + timedelta(minutes=1))
        run = db.query(ScheduledSendRun).one()
        token = runner.claim_run(run.id, datetime.utcnow())
        db.query(ScheduledSend).update({"status": "cancelled"})
        db.commit()
        with reachable():
            status = await runner.prepare_run(run.id, token, http_client=AsyncMock())
        assert status == "cancelled"
        assert db.query(Broadcast).count() == 0

    async def test_opted_out_chats_are_left_out(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        group_a = db.query(User).filter_by(whatsapp_jid=GROUP_A).one()
        db.add(ScheduledSendOptOut(scheduled_send_id=send.id, user_id=group_a.id))
        db.commit()
        await _fire_and_prepare(factory, send)
        assert {r.address for r in db.query(BroadcastRecipient)} == {GROUP_B}

    async def test_a_chats_audience_reaches_only_those_chats(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, audience="chats", chat_jids=[GROUP_B, PRIVATE])
        _status, run, _ = await _fire_and_prepare(factory, send)
        assert {r.address for r in db.query(BroadcastRecipient)} == {GROUP_B, PRIVATE}
        assert db.get(Broadcast, run.broadcast_id).audience == "all"


# --- The broadcast lane sees the send's texts and opt-outs ------------------------


class TestLane:
    async def test_next_recipient_carries_its_own_text_and_respects_late_opt_outs(self, factory):
        db = factory()
        add_users(db)
        send = add_send(db, content_type="prompt", content="News")

        async def generate(**kw):
            return GeneratedText(text=f"t-{kw['chat_type']}")

        _status, run, _ = await _fire_and_prepare(factory, send, generate)
        # Group B stops this send after the snapshot: re-checked before the send.
        group_b = db.query(User).filter_by(whatsapp_jid=GROUP_B).one()
        db.add(ScheduledSendOptOut(scheduled_send_id=send.id, user_id=group_b.id))
        db.commit()
        rec = bc._next_recipient(run.broadcast_id, "baileys")
        assert rec.address == GROUP_A
        assert rec.text.startswith("t-group") and f"/broadcast off {send.number}" in rec.text
        assert bc._claim(rec.id)
        assert bc._next_recipient(run.broadcast_id, "baileys") is None
        db.expire_all()
        skipped = db.query(BroadcastRecipient).filter_by(address=GROUP_B).one()
        assert (skipped.status, skipped.error_code) == ("skipped", "opted_out_campaign")

    def test_lane_state_is_written_and_cleared(self, factory):
        db = factory()
        broadcast = Broadcast(text="x", footer="", audience="all", platforms=["baileys"])
        db.add(broadcast)
        db.commit()
        due = datetime(2026, 11, 24, 12, 0)
        bc._set_lane_state(broadcast.id, "baileys", due, bc.WAIT_SEND_WINDOW)
        bc._set_lane_state(broadcast.id, "baileys", due, bc.WAIT_DAILY_CAP)  # an upsert
        row = db.query(BroadcastLaneState).one()
        assert (row.next_send_at, row.waiting_for) == (due, "daily_cap")
        bc._clear_lane_state(broadcast.id, "baileys")
        assert db.query(BroadcastLaneState).count() == 0


# --- Routes ---------------------------------------------------------------------------


@pytest.fixture
def api(factory):
    from ai_api.database import get_db
    from ai_api.main import app

    patches = [
        patch("ai_api.main.init_db"),
        patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
        patch("ai_api.main.cleanup_expired_documents"),
        patch(
            "ai_api.routes.broadcasts.probe_platforms",
            AsyncMock(return_value={"baileys": True, "cloud": False, "telegram": True}),
        ),
    ]
    for p in patches:
        p.start()

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()
    for p in patches:
        p.stop()


AUTH = {"X-API-Key": "test-api-key"}
WEEKLY = {"kind": "weekly", "time": "08:00", "weekdays": [1]}


def body(**overrides):
    values = dict(
        name="Weekly digest",
        content_type="prompt",
        content="Summarise this week's news",
        audience="groups",
        schedule=WEEKLY,
    )
    values.update(overrides)
    return values


class TestRoutes:
    async def test_create_numbers_sends_and_describes_the_schedule(self, api):
        async with api as c:
            first = await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)
            second = await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)
        assert first.status_code == 201
        data = first.json()
        assert data["number"] == 1 and second.json()["number"] == 2
        assert data["status"] == "active" and data["timezone"] == "America/Sao_Paulo"
        assert data["schedule_description"] == "every monday at 08:00"
        assert data["next_run_at"] is not None and data["last_run"] is None
        assert data["regions"] is None

    async def test_a_one_time_send_describes_its_date(self, api):
        once = {"kind": "once", "run_at": "2030-01-07T08:00"}
        with patch("ai_api.scheduled_sends.schedule.MAX_ONE_SHOT_AHEAD", timedelta(days=36500)):
            async with api as c:
                resp = await c.post(
                    "/admin/scheduled-sends", json=body(schedule=once), headers=AUTH
                )
        assert resp.json()["schedule_description"] == "once, Mon 07 Jan 2030 08:00"

    async def test_idempotency_key_replays_and_rejects_a_different_body(self, api):
        async with api as c:
            one = await c.post(
                "/admin/scheduled-sends", json=body(idempotency_key="k1"), headers=AUTH
            )
            again = await c.post(
                "/admin/scheduled-sends", json=body(idempotency_key="k1"), headers=AUTH
            )
            other = await c.post(
                "/admin/scheduled-sends",
                json=body(idempotency_key="k1", content="other"),
                headers=AUTH,
            )
        assert (one.status_code, again.status_code, other.status_code) == (201, 200, 422)
        assert again.json()["id"] == one.json()["id"]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"schedule": {"kind": "weekly", "time": "25:00", "weekdays": [1]}},
            {"schedule": {"kind": "once", "run_at": "2000-01-01T08:00"}},
            {"schedule": {"kind": "monthly", "time": "08:00", "day_of_month": 31}},
            {"regions": ["North"]},
            {"audience": "chats"},
            {"chat_jids": [GROUP_A]},
            {"content": "x" * 2001},
        ],
    )
    async def test_invalid_input_is_a_422(self, api, overrides):
        async with api as c:
            resp = await c.post("/admin/scheduled-sends", json=body(**overrides), headers=AUTH)
        assert resp.status_code == 422, resp.text

    async def test_an_empty_region_list_is_accepted(self, api):
        async with api as c:
            resp = await c.post("/admin/scheduled-sends", json=body(regions=[]), headers=AUTH)
        assert resp.status_code == 201 and resp.json()["regions"] is None

    async def test_list_carries_the_zone_no_regions_and_a_disabled_recap(self, api):
        async with api as c:
            await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)
            resp = await c.get("/admin/scheduled-sends", headers=AUTH)
        data = resp.json()
        assert data["total"] == 1
        assert data["regions"] == [] and data["region_filter"] is False
        assert data["timezone"] == "America/Sao_Paulo"
        recap = data["weekly_recap"]
        assert recap["enabled"] is False and recap["groups"] == 0
        assert set(recap) == {
            "enabled",
            "weekday",
            "hour",
            "minute",
            "timezone",
            "default_prompt",
            "groups",
            "subscribers",
        }

    async def test_pause_resume_cancel_and_their_409s(self, api):
        async with api as c:
            send = (await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)).json()
            sid = send["id"]
            paused = await c.post(f"/admin/scheduled-sends/{sid}/pause", headers=AUTH)
            twice = await c.post(f"/admin/scheduled-sends/{sid}/pause", headers=AUTH)
            resumed = await c.post(f"/admin/scheduled-sends/{sid}/resume", headers=AUTH)
            cancelled = await c.post(f"/admin/scheduled-sends/{sid}/cancel", headers=AUTH)
            edit = await c.patch(f"/admin/scheduled-sends/{sid}", json={"name": "x"}, headers=AUTH)
            run = await c.post(f"/admin/scheduled-sends/{sid}/run", headers=AUTH)
        assert paused.json()["status"] == "paused" and paused.json()["next_run_at"] is None
        assert twice.status_code == 409
        assert resumed.json()["status"] == "active" and resumed.json()["next_run_at"]
        assert cancelled.json()["status"] == "cancelled"
        assert (edit.status_code, run.status_code) == (409, 409)

    async def test_patch_recomputes_the_schedule(self, api):
        async with api as c:
            sid = (await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)).json()["id"]
            resp = await c.patch(
                f"/admin/scheduled-sends/{sid}",
                json={"schedule": {"kind": "daily", "time": "07:30"}, "name": "Daily"},
                headers=AUTH,
            )
        data = resp.json()
        assert data["name"] == "Daily" and data["schedule_description"] == "every day at 07:30"

    async def test_patch_null_content_and_regions_are_422s(self, api):
        async with api as c:
            sid = (await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)).json()["id"]
            content = await c.patch(
                f"/admin/scheduled-sends/{sid}", json={"content": None}, headers=AUTH
            )
            regions = await c.patch(
                f"/admin/scheduled-sends/{sid}", json={"regions": ["North"]}, headers=AUTH
            )
        assert (content.status_code, regions.status_code) == (422, 422)

    async def test_a_rename_keeps_the_armed_slot(self, api, factory):
        armed = datetime(2026, 11, 20, 11, 0)  # due, not fired yet
        db = factory()
        async with api as c:
            sid = (await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)).json()["id"]
            db.query(ScheduledSend).update({"next_run_at": armed})
            db.commit()
            resp = await c.patch(
                f"/admin/scheduled-sends/{sid}", json={"name": "Another name"}, headers=AUTH
            )
        assert resp.status_code == 200
        db.expire_all()
        assert db.get(ScheduledSend, uuid.UUID(sid)).next_run_at == armed

    async def test_run_now_creates_one_manual_run(self, api):
        async with api as c:
            sid = (await c.post("/admin/scheduled-sends", json=body(), headers=AUTH)).json()["id"]
            first = await c.post(f"/admin/scheduled-sends/{sid}/run", headers=AUTH)
            second = await c.post(f"/admin/scheduled-sends/{sid}/run", headers=AUTH)
            runs = await c.get(f"/admin/scheduled-sends/{sid}/runs", headers=AUTH)
            detail = await c.get(f"/admin/scheduled-sends/runs/{first.json()['id']}", headers=AUTH)
        assert first.status_code == 202 and first.json()["trigger"] == "manual"
        assert second.status_code == 409  # still preparing
        assert runs.json()["total"] == 1
        assert detail.json()["texts"] == [] and detail.json()["status"] == "preparing"

    async def test_preview_counts_runs_and_platforms(self, api, factory):
        db = factory()
        add_users(db)
        async with api as c:
            resp = await c.post(
                "/admin/scheduled-sends/preview", json=body(audience="all"), headers=AUTH
            )
        data = resp.json()
        assert resp.status_code == 200, resp.text
        assert data["total_recipients"] == 4
        per = {p["platform"]: p["recipients"] for p in data["per_platform"]}
        assert per == {"baileys": 3, "telegram": 1}
        assert data["estimated_agent_runs"] == 2  # one per chat type
        assert len(data["next_runs"]) == 5
        assert data["sample"] is None

    async def test_preview_sample_runs_the_agent_once(self, api, factory):
        db = factory()
        add_users(db)
        gen = AsyncMock(return_value=GeneratedText(text="Hello group", prompt="P"))
        with patch("ai_api.routes.scheduled_sends.generate_text", gen):
            async with api as c:
                resp = await c.post(
                    "/admin/scheduled-sends/preview",
                    json=body(generate=True, sample_region="ignored"),
                    headers=AUTH,
                )
        sample = resp.json()["sample"]
        assert sample == {
            "region": None,
            "chat_type": "group",
            "text": "Hello group",
            "error": None,
            "prompt": "P",
        }
        gen.assert_awaited_once()

    async def test_run_broadcasts_do_not_block_a_manual_broadcast(self, api, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        await _fire_and_prepare(factory, send)  # a queued run broadcast now exists
        from ai_api.routes.broadcasts import _ensure_none_active, _run_links, is_run_broadcast

        _ensure_none_active(db)  # no 409
        run = db.query(ScheduledSendRun).one()
        assert _run_links(db, [run.broadcast_id])[run.broadcast_id] == (send.id, run.id)
        assert is_run_broadcast(db, run.broadcast_id)

    async def test_a_broadcast_lists_its_scheduled_send(self, api, factory):
        db = factory()
        add_users(db)
        send = add_send(db)
        _status, run, _ = await _fire_and_prepare(factory, send)
        async with api as c:
            resp = await c.get(f"/admin/broadcasts/{run.broadcast_id}", headers=AUTH)
        assert resp.json()["scheduled_send_id"] == str(send.id)
        assert resp.json()["scheduled_send_run_id"] == str(run.id)


def test_footer_names_the_send_number(monkeypatch):
    _patch_runtime(monkeypatch)
    assert runner.footer_for(7) == "_broadcast off_\n_/broadcast off 7_"


def test_snapshot_survives_its_session():
    send = SimpleNamespace(
        id=uuid.uuid4(),
        number=1,
        name="n",
        content_type="text",
        content="c",
        audience="all",
        chat_jids=None,
        platforms=["baileys"],
        status="active",
    )
    assert runner.SendSnapshot.of(send).platforms == ["baileys"]


# --- /broadcast <number> and the settings tool --------------------------------------


class TestBroadcastCommand:
    def _chat(self, factory, jid=PRIVATE):
        db = factory()
        add_users(db)
        send = add_send(db, name="Weekly digest", audience="all")
        return db, send, db.query(User).filter_by(whatsapp_jid=jid).one()

    def _cmd(self, db, user, text, *, group=False, admin=None):
        from ai_api.commands import _handle_broadcast_command

        return _handle_broadcast_command(
            db, user.id, text.split(), is_group=group, is_group_admin=admin
        )

    def test_off_and_on_with_a_number_touch_only_that_send(self, factory):
        db, send, user = self._chat(factory)
        off = self._cmd(db, user, f"/broadcast off {send.number}")
        assert "won't receive scheduled message 1 (Weekly digest)" in off
        assert db.get(ScheduledSendOptOut, (send.id, user.id)) is not None
        assert user.broadcast_opt_out is False  # the global switch is untouched
        listing = self._cmd(db, user, "/broadcast")
        assert "- 1: Weekly digest (off)" in listing
        on = self._cmd(db, user, f"/broadcast on {send.number}")
        assert "will receive scheduled message 1" in on
        assert db.get(ScheduledSendOptOut, (send.id, user.id)) is None

    def test_unknown_number(self, factory):
        db, _send, user = self._chat(factory)
        assert "no scheduled message 9" in self._cmd(db, user, "/broadcast off 9")

    @pytest.mark.parametrize("tail", ["please", "²", "@5511999999999"])
    def test_a_non_number_after_off_runs_the_global_switch(self, factory, tail):
        db, _send, user = self._chat(factory)
        reply = self._cmd(db, user, f"/broadcast off {tail}")
        assert "won't receive update announcements" in reply
        db.refresh(user)
        assert user.broadcast_opt_out is True

    def test_a_finished_send_is_not_claimed_as_stopped(self, factory):
        db, send, user = self._chat(factory)
        send.status = "finished"
        db.commit()
        assert "has ended" in self._cmd(db, user, f"/broadcast off {send.number}")
        assert db.get(ScheduledSendOptOut, (send.id, user.id)) is None

    def test_the_listing_shows_only_sends_that_can_reach_this_chat(self, factory):
        db, _send, _user = self._chat(factory)
        add_send(db, name="Groups only", audience="groups")
        add_send(db, name="Private only", audience="private")
        add_send(db, name="Two chats", audience="chats", chat_jids=[GROUP_B])
        add_send(db, name="One shot", cron_expr=None, run_at=datetime(2030, 1, 1))

        def listing(jid):
            chat = db.query(User).filter_by(whatsapp_jid=jid).one()
            return self._cmd(db, chat, "/broadcast", group=chat.conversation_type == "group")

        private = listing(PRIVATE)
        assert "Private only" in private and "Weekly digest" in private
        assert "Groups only" not in private and "Two chats" not in private
        assert "One shot" not in private  # only recurring sends are listed
        group_a, group_b = listing(GROUP_A), listing(GROUP_B)
        assert "Two chats" in group_b and "Two chats" not in group_a
        assert "Private only" not in group_a and "Groups only" in group_a

    @pytest.mark.parametrize("is_admin", [None, False])
    def test_a_group_needs_a_confirmed_admin(self, factory, is_admin):
        db, send, group = self._chat(factory, GROUP_A)
        self._cmd(db, group, f"/broadcast off {send.number}", group=True, admin=is_admin)
        assert db.get(ScheduledSendOptOut, (send.id, group.id)) is None

    async def test_settings_tool_stops_one_send(self, factory):
        from ai_api.agent.tools.settings import _set_send_announcements

        db, send, user = self._chat(factory)
        deps = SimpleNamespace(db=db, user_id=user.id, whatsapp_jid=PRIVATE, is_group_admin=None)
        reply = await _set_send_announcements(deps, send.number, False)
        assert "is now off" in reply
        assert db.get(ScheduledSendOptOut, (send.id, user.id)) is not None

    async def test_settings_tool_number_without_enabled_names_the_send(self, factory):
        from ai_api.agent.tools.settings import settings as settings_tool

        db, send, user = self._chat(factory)
        ctx = SimpleNamespace(deps=SimpleNamespace(db=db, user_id=user.id, whatsapp_jid=PRIVATE))
        fn = getattr(settings_tool, "function", settings_tool)
        reply = await fn(ctx, action="announcements", scheduled_send_number=send.number)
        assert f"scheduled message {send.number}" in reply

    def test_a_link_merge_keeps_the_orphans_opt_outs(self, factory):
        from ai_api.scheduled_sends.opt_out import move_opt_outs

        db, send, kept = self._chat(factory)
        orphan = db.query(User).filter_by(whatsapp_jid=TG_PRIVATE).one()
        db.add(ScheduledSendOptOut(scheduled_send_id=send.id, user_id=orphan.id))
        db.commit()
        move_opt_outs(db, orphan.id, kept.id)
        db.commit()
        assert db.get(ScheduledSendOptOut, (send.id, kept.id)) is not None
        assert db.get(ScheduledSendOptOut, (send.id, orphan.id)) is None


async def test_worker_tick_fires_and_prepares_each_run_once(factory):
    import asyncio

    from ai_api.streams import scheduled_send_scheduler as loop

    db = factory()
    send = add_send(db, next_run_at=datetime.utcnow() - timedelta(minutes=1))
    prepared = []

    async def fake_prepare(run_id, token, http):
        prepared.append(run_id)

    in_flight: dict = {}
    with patch.object(loop.runner, "safe_prepare", fake_prepare):
        await loop.tick(AsyncMock(), in_flight)
        await asyncio.gather(*in_flight.values())
        await loop.tick(AsyncMock(), in_flight)  # claimed: not taken again
        await asyncio.gather(*in_flight.values())
    run = db.query(ScheduledSendRun).filter_by(scheduled_send_id=send.id).one()
    assert prepared == [run.id]
