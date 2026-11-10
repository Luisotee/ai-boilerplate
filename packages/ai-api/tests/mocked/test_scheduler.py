"""streams/scheduler.py: advance before dispatch, no burst after downtime, sweeps."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import ResponseError
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
        # A reminder goes straight to the sender, with its text.
        assert run.status == "ready"
        assert run.message == "⏰ drink water"
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
        assert run.status == "ready"
        assert run.late_seconds == 2400
        assert task.next_run_at == datetime(2026, 11, 10, 14, 0)

    def test_one_shot_inside_grace_fires_late(self):
        task = _task(run_at=NOW - timedelta(hours=1), next_run_at=NOW - timedelta(hours=1))
        run = scheduler._advance(_db(), task, NOW)
        assert run.status == "ready"
        assert task.next_run_at is None
        assert task.status == "active"  # completed only once delivered

    def test_one_shot_past_grace_is_disabled_as_missed(self):
        task = _task(run_at=NOW - timedelta(hours=3), next_run_at=NOW - timedelta(hours=3))
        run = scheduler._advance(_db(), task, NOW)
        assert run.status == "skipped"
        assert task.status == "disabled"
        assert task.disabled_reason == "missed"
        assert task.next_run_at is None

    def test_agent_task_is_dispatched_without_text(self):
        task = _task(kind="agent", interval_seconds=6 * 3600, next_run_at=NOW)
        run = scheduler._advance(_db(), task, NOW)
        assert run.status == "dispatched"
        assert run.message is None

    def test_recurring_with_no_next_slot_is_disabled(self):
        task = _task(cron_expr="0 9 * * *", next_run_at=NOW)
        with patch.object(scheduler, "next_fire", return_value=None):
            scheduler._advance(_db(), task, NOW)
        assert (task.status, task.disabled_reason) == ("disabled", "no_next_slot")

    def test_a_slot_that_already_ran_is_never_fired_again(self):
        task = _task(interval_seconds=3600, next_run_at=NOW)
        db = _db()
        db.begin_nested.return_value.__enter__.side_effect = IntegrityError("x", {}, Exception())
        assert scheduler._advance(db, task, NOW) is None
        assert task.next_run_at == NOW + timedelta(hours=1)


def _claim_db(tasks):
    db = _db()
    query = db.query.return_value.filter.return_value.order_by.return_value.limit.return_value
    query.with_for_update.return_value.all.return_value = tasks
    return db, query


class TestClaimDue:
    def test_locks_with_skip_locked_and_commits_once(self):
        reminder = _task(interval_seconds=3600, next_run_at=NOW)
        agent = _task(kind="agent", interval_seconds=6 * 3600, next_run_at=NOW)
        db, query = _claim_db([reminder, agent])
        with patch.object(scheduler, "SessionLocal", return_value=db):
            fired, claimed = scheduler.claim_due(NOW)
        query.with_for_update.assert_called_once_with(skip_locked=True)
        db.commit.assert_called_once()
        assert claimed == 2
        # Only the agent run goes on a chat stream; the reminder is already ready.
        assert [f.user_id for f in fired] == [agent.user_id]
        db.close.assert_called_once()

    def test_one_bad_task_is_disabled_and_the_batch_goes_on(self):
        bad = _task(interval_seconds=3600, next_run_at=NOW)
        good = _task(kind="agent", interval_seconds=6 * 3600, next_run_at=NOW)
        db, _ = _claim_db([bad, good])
        real = scheduler._advance

        def advance(session, task, now):
            if task is bad:
                raise ValueError("corrupt row")
            return real(session, task, now)

        with (
            patch.object(scheduler, "SessionLocal", return_value=db),
            patch.object(scheduler, "_advance", side_effect=advance),
        ):
            fired, claimed = scheduler.claim_due(NOW)
        assert (bad.status, bad.disabled_reason, bad.next_run_at) == (
            "disabled",
            "advance_error",
            None,
        )
        assert [f.user_id for f in fired] == [good.user_id]
        db.commit.assert_called_once()
        db.rollback.assert_not_called()

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
    def _sweep(self, task, run, position):
        db = _db()
        results = iter([[run] if i == position else [] for i in range(4)])
        db.query.return_value.filter.return_value.with_for_update.return_value.all.side_effect = (
            lambda: next(results)
        )
        db.get.return_value = task
        with patch.object(scheduler, "SessionLocal", return_value=db):
            assert scheduler.sweep_orphans(NOW) == 1
        db.commit.assert_called_once()

    # Rule order: dispatched, ready, running, sending.
    def test_orphaned_send_becomes_unknown_and_counts(self):
        task = _task(interval_seconds=3600, next_run_at=NOW + timedelta(hours=1))
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="sending")
        self._sweep(task, run, 3)
        assert (run.status, run.error_code) == ("unknown", "unknown")
        assert task.consecutive_failures == 1

    def test_crashed_agent_phase_is_a_failure_not_unknown(self):
        task = _task(kind="agent", interval_seconds=6 * 3600)
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="running")
        self._sweep(task, run, 2)
        assert (run.status, run.error_code) == ("failed", "abandoned")

    def test_stale_dispatched_run_expires_as_a_skip(self):
        task = _task(kind="agent", interval_seconds=6 * 3600, consecutive_failures=2)
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="dispatched")
        self._sweep(task, run, 0)
        assert (run.status, run.error_code) == ("skipped", "expired")
        assert task.status == "active" and task.consecutive_failures == 2

    def test_stale_ready_run_expires(self):
        task = _task(interval_seconds=3600)
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="ready")
        self._sweep(task, run, 1)
        # The bot's own backlog: a skip, which never counts toward auto-disable.
        assert (run.status, run.error_code) == ("skipped", "expired")
        assert task.consecutive_failures == 0 and task.status == "active"


def _redis():
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[b"1-0"])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=pipe)
    ctx.__aexit__ = AsyncMock(return_value=False)
    redis = MagicMock()
    redis.pipeline.return_value = ctx
    return redis, pipe


class TestTick:
    async def test_dispatches_agent_runs_in_one_pipeline(self):
        fired = [scheduler.Fired(uuid.uuid4(), uuid.uuid4()) for _ in range(2)]
        redis, pipe = _redis()
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=(fired, 2)),
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            assert await scheduler.tick(redis) == 2
        redis.pipeline.assert_called_once_with(transaction=False)
        assert pipe.xadd.call_count == 2
        key, job = pipe.xadd.call_args_list[0].args
        assert key == f"stream:user:{fired[0].user_id}"
        assert job == {
            "trigger": "scheduled",
            "run_id": str(fired[0].run_id),
            "user_id": str(fired[0].user_id),
        }
        pipe.execute.assert_awaited_once()

    async def test_only_the_xadds_redis_refused_are_failed(self):
        fired = [scheduler.Fired(uuid.uuid4(), uuid.uuid4()) for _ in range(3)]
        redis, pipe = _redis()
        pipe.execute.return_value = [b"1-0", ResponseError("OOM"), b"1-1"]
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=(fired, 3)),
            patch.object(scheduler, "fail_dispatched") as fail,
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            assert await scheduler.tick(redis) == 2
        pipe.execute.assert_awaited_once_with(raise_on_error=False)
        fail.assert_called_once_with([fired[1].run_id], "enqueue_failed")

    async def test_lost_connection_leaves_the_runs_to_the_sweep(self):
        # Some entries may already be in Redis: failing them would lose them.
        fired = [scheduler.Fired(uuid.uuid4(), uuid.uuid4())]
        redis, pipe = _redis()
        pipe.execute.side_effect = ConnectionError
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=(fired, 1)),
            patch.object(scheduler, "fail_dispatched") as fail,
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            assert await scheduler.tick(redis) == 0
        fail.assert_not_called()

    async def test_a_full_batch_claims_again_at_once(self):
        batches = iter([([], scheduler.BATCH_SIZE), ([], scheduler.BATCH_SIZE), ([], 3)])
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", side_effect=lambda _now: next(batches)) as claim,
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            await scheduler.tick(MagicMock())
        assert claim.call_count == 3

    async def test_batches_per_tick_are_capped(self):
        with (
            patch.object(scheduler, "sweep_orphans"),
            patch.object(scheduler, "claim_due", return_value=([], scheduler.BATCH_SIZE)) as claim,
            patch.object(scheduler.runtime_config, "get", return_value=True),
        ):
            await scheduler.tick(MagicMock())
        assert claim.call_count == scheduler.MAX_BATCHES_PER_TICK

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
