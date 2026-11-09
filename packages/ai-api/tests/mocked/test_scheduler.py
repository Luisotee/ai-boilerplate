"""streams/scheduler.py: advance before dispatch, no burst after downtime, sweeps."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from ai_api.database import ScheduledTask, TaskRun
from ai_api.streams import scheduler

NOW = datetime(2026, 11, 9, 15, 0)  # 10:00 in Rio Branco


def _task(**kw) -> ScheduledTask:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        address="123@s.whatsapp.net",
        kind="reminder",
        text="drink water",
        schedule="every 1h",
        timezone="America/Rio_Branco",
        status="active",
        consecutive_failures=0,
        run_count=0,
    )
    defaults.update(kw)
    return ScheduledTask(**defaults)


def _db():
    db = MagicMock()
    added = []
    db.add.side_effect = added.append
    db.added = added
    return db


class TestAdvance:
    def test_on_time_recurring_fires_and_moves_to_the_next_slot(self):
        task = _task(interval_seconds=3600, next_run_at=NOW)
        db = _db()
        run = scheduler._advance(db, task, NOW + timedelta(seconds=10))
        assert run.status == "queued"
        assert run.scheduled_for == NOW
        assert task.next_run_at == NOW + timedelta(hours=1)
        assert db.added == [run]

    def test_slot_late_beyond_its_window_is_skipped_not_replayed(self):
        # Hourly task, worker down for 5h: one skipped run, next slot after now.
        task = _task(interval_seconds=3600, next_run_at=NOW - timedelta(hours=5))
        run = scheduler._advance(_db(), task, NOW + timedelta(minutes=10))
        assert run.status == "skipped"
        assert run.error_code == "missed"
        assert task.next_run_at == NOW + timedelta(hours=1)
        assert task.last_status == "skipped"
        assert task.status == "active"

    def test_slot_late_inside_its_window_still_fires_once(self):
        # Daily at 09:00 local; worker back 40 min late (window = 2h cap).
        slot = datetime(2026, 11, 9, 14, 0)
        task = _task(cron_expr="0 9 * * *", next_run_at=slot)
        run = scheduler._advance(_db(), task, slot + timedelta(minutes=40))
        assert run.status == "queued"
        assert run.late_seconds == 2400
        assert task.next_run_at == datetime(2026, 11, 10, 14, 0)

    def test_one_shot_inside_grace_fires_late(self):
        task = _task(run_at=NOW - timedelta(hours=1), next_run_at=NOW - timedelta(hours=1))
        run = scheduler._advance(_db(), task, NOW)
        assert run.status == "queued"
        assert task.next_run_at is None
        assert task.status == "active"  # completed only once delivered

    def test_one_shot_past_grace_is_disabled_as_missed(self):
        task = _task(run_at=NOW - timedelta(hours=3), next_run_at=NOW - timedelta(hours=3))
        run = scheduler._advance(_db(), task, NOW)
        assert run.status == "skipped"
        assert task.status == "disabled"
        assert task.disabled_reason == "missed"
        assert task.next_run_at is None

    def test_a_slot_that_already_ran_is_never_fired_again(self):
        task = _task(interval_seconds=3600, next_run_at=NOW)
        db = _db()
        db.begin_nested.return_value.__enter__.side_effect = IntegrityError("x", {}, Exception())
        assert scheduler._advance(db, task, NOW) is None
        assert task.next_run_at == NOW + timedelta(hours=1)


class TestClaimDue:
    def test_locks_with_skip_locked_and_commits_once(self):
        task = _task(interval_seconds=3600, next_run_at=NOW)
        db = _db()
        query = db.query.return_value.filter.return_value.order_by.return_value.limit.return_value
        query.with_for_update.return_value.all.return_value = [task]
        with patch.object(scheduler, "SessionLocal", return_value=db):
            fired = scheduler.claim_due(NOW)
        query.with_for_update.assert_called_once_with(skip_locked=True)
        db.commit.assert_called_once()
        assert len(fired) == 1
        assert fired[0].user_id == task.user_id
        db.close.assert_called_once()

    def test_rolls_back_on_error(self):
        db = _db()
        db.query.side_effect = RuntimeError("db down")
        with (
            patch.object(scheduler, "SessionLocal", return_value=db),
            pytest.raises(RuntimeError),
        ):
            scheduler.claim_due(NOW)
        db.rollback.assert_called_once()


class TestSweep:
    def test_orphaned_running_run_becomes_unknown_and_counts(self):
        task = _task(interval_seconds=3600, next_run_at=NOW + timedelta(hours=1))
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="running")
        db = _db()
        results = iter([[], [], [run]])  # queued, dispatched, running
        db.query.return_value.filter.return_value.with_for_update.return_value.all.side_effect = (
            lambda: next(results)
        )
        db.get.return_value = task
        with patch.object(scheduler, "SessionLocal", return_value=db):
            assert scheduler.sweep_orphans(NOW) == 1
        assert run.status == "unknown"
        assert task.consecutive_failures == 1
        db.commit.assert_called_once()


class TestTick:
    async def test_dispatches_onto_the_chats_stream_and_marks_it(self):
        fired = [scheduler.Fired(uuid.uuid4(), uuid.uuid4())]
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=fired),
            patch.object(scheduler, "mark_dispatched") as mark,
            patch.object(scheduler, "add_message_to_stream", new_callable=AsyncMock) as xadd,
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            assert await scheduler.tick(MagicMock()) == 1
        _redis, user_id, job = xadd.await_args.args
        assert user_id == str(fired[0].user_id)
        assert job == {
            "trigger": "scheduled",
            "run_id": str(fired[0].run_id),
            "user_id": str(fired[0].user_id),
        }
        mark.assert_called_once_with(fired[0].run_id)

    async def test_failed_enqueue_is_left_for_the_sweep(self):
        fired = [scheduler.Fired(uuid.uuid4(), uuid.uuid4())]
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=fired),
            patch.object(scheduler, "mark_dispatched") as mark,
            patch.object(
                scheduler,
                "add_message_to_stream",
                new_callable=AsyncMock,
                side_effect=ConnectionError,
            ),
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            assert await scheduler.tick(MagicMock()) == 0
        mark.assert_not_called()

    async def test_disabled_scheduler_fires_nothing(self):
        with (
            patch.object(scheduler, "sweep_orphans") as sweep,
            patch.object(scheduler, "claim_due") as claim,
            patch.object(scheduler.runtime_config, "get", return_value=False),
        ):
            assert await scheduler.tick(MagicMock()) == 0
        sweep.assert_called_once()
        claim.assert_not_called()

    async def test_a_failing_tick_does_not_stop_the_loop(self):
        calls = 0

        async def fake_tick(_redis):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            raise KeyboardInterrupt  # leave the loop

        with (
            patch.object(scheduler, "tick", fake_tick),
            patch.object(scheduler.asyncio, "sleep", new_callable=AsyncMock),
            pytest.raises(KeyboardInterrupt),
        ):
            await scheduler.run_scheduler(MagicMock())
        assert calls == 2
