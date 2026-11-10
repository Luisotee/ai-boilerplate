"""streams/scheduled_sender.py: send-time checks, delivery outcomes and the lanes."""

import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai_api.database import ScheduledTask, TaskRun, User
from ai_api.streams import scheduled_runner as runner
from ai_api.streams import scheduled_sender as sender
from ai_api.whatsapp import WhatsAppClientError, WhatsAppNotConnectedError

NOW = datetime(2026, 11, 9, 15, 0)


def _task(**kw) -> ScheduledTask:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        address="5511999999999@s.whatsapp.net",
        kind="reminder",
        text="drink water",
        schedule="every day at 09:00",
        cron_expr="0 9 * * *",
        timezone="UTC",
        status="active",
        consecutive_failures=0,
        run_count=0,
        next_run_at=NOW + timedelta(days=1),
    )
    defaults.update(kw)
    return ScheduledTask(**defaults)


def _settings(**overrides):
    values = {
        "whitelist_phones": "",
        "scheduled_tasks_max_runs_per_day": 24,
        "scheduled_tasks_enabled": True,
    }
    values.update(overrides)
    return patch.object(runner.runtime_config, "get", side_effect=lambda k: values[k])


class TestPrepare:
    def _go(self, task, user, run_status="sending", message="⏰ drink water", **settings):
        run = TaskRun(
            id=uuid.uuid4(),
            task_id=task.id,
            status=run_status,
            message=message,
            created_at=NOW,
        )
        db = MagicMock()
        db.get.side_effect = lambda model, _id: {TaskRun: run, ScheduledTask: task, User: user}[
            model
        ]
        db.query.return_value.filter.return_value.scalar.return_value = settings.pop(
            "sent_today", 0
        )
        db.query.return_value.filter.return_value.filter.return_value.scalar.return_value = 0
        with (
            _settings(**settings),
            patch.object(sender, "SessionLocal", return_value=db),
            patch.object(sender, "utcnow", return_value=NOW),
        ):
            send = sender._prepare(run.id)
        return send, run, db

    def test_claimed_run_becomes_a_send(self):
        task = _task(client_id="telegram", address="tg:42")
        send, _, db = self._go(task, User(whatsapp_jid="tg:42"))
        assert (send.platform, send.address, send.text) == ("telegram", "tg:42", "⏰ drink water")
        db.commit.assert_not_called()

    def test_cancelled_meanwhile_is_skipped(self):
        task = _task(status="cancelled")
        send, run, db = self._go(task, User(whatsapp_jid=task.address))
        assert send is None
        assert (run.status, run.error_code) == ("skipped", "cancelled")
        db.commit.assert_called_once()

    def test_daily_cap_skips_without_counting_a_failure(self):
        task = _task(consecutive_failures=2)
        send, run, _ = self._go(
            task, User(whatsapp_jid=task.address), scheduled_tasks_max_runs_per_day=1, sent_today=1
        )
        assert send is None
        assert run.error_code == "daily_limit"
        assert task.status == "active" and task.consecutive_failures == 2

    def test_run_no_longer_sending_is_left_alone(self):
        send, run, db = self._go(_task(), User(), run_status="unknown")
        assert send is None
        assert run.status == "unknown"
        db.commit.assert_not_called()


def _send():
    return sender._Send(
        run_id=uuid.uuid4(),
        platform="baileys",
        client_id=None,
        address="5511@s.whatsapp.net",
        text="⏰ drink water",
    )


class TestSendRun:
    async def _run(self, send, *, error=None):
        with (
            patch.object(sender, "_prepare", return_value=send),
            patch.object(sender, "_deliver_to", new_callable=AsyncMock, side_effect=error) as d,
            patch.object(sender, "record") as record,
        ):
            attempted = await sender.send_run(send.run_id if send else uuid.uuid4())
        return attempted, d, record

    async def test_sent_is_recorded_with_history(self):
        s = _send()
        attempted, deliver, record = await self._run(s)
        assert attempted
        deliver.assert_awaited_once_with(s)
        record.assert_called_once_with(s.run_id, "sending", "sent", history_text=s.text)

    async def test_skipped_run_is_not_delivered(self):
        attempted, deliver, record = await self._run(None)
        assert not attempted
        deliver.assert_not_awaited()
        record.assert_not_called()

    async def test_maybe_delivered_is_unknown_and_never_retried(self):
        s = _send()
        _, _, record = await self._run(s, error=httpx.ReadTimeout("slow"))
        record.assert_called_once_with(
            s.run_id, "sending", "unknown", "unknown", provably_not_sent=False
        )

    async def test_disconnected_client_counts_as_not_sent(self):
        s = _send()
        _, _, record = await self._run(s, error=WhatsAppNotConnectedError())
        assert record.call_args.args[2] == "failed"
        assert record.call_args.kwargs["provably_not_sent"] is True

    async def test_refused_address_is_final(self):
        s = _send()
        _, _, record = await self._run(s, error=WhatsAppClientError("x", status_code=404))
        record.assert_called_once_with(
            s.run_id, "sending", "failed", "not_on_whatsapp", provably_not_sent=False
        )

    async def test_never_raises(self):
        with patch.object(sender, "_prepare", side_effect=RuntimeError("db down")):
            await sender.send_run(uuid.uuid4())


class TestLanes:
    async def test_baileys_lane_waits_the_gap_between_sends(self):
        rid = uuid.uuid4()
        sleep = AsyncMock()
        with (
            patch.object(sender, "claim_ready", return_value=[rid]) as claim,
            patch.object(sender, "send_run", new_callable=AsyncMock, return_value=True) as send,
            patch.object(sender.asyncio, "sleep", sleep),
            patch.object(sender.random, "uniform", return_value=5.0),
        ):
            loop = __import__("asyncio").get_running_loop()
            found, last = await sender.baileys_lane_step(loop.time())
        claim.assert_called_once_with(("baileys",), 1)
        send.assert_awaited_once_with(rid)
        assert found and last is not None
        assert 4.0 < sleep.await_args.args[0] <= 5.0

    async def test_baileys_lane_first_send_has_no_gap(self):
        sleep = AsyncMock()
        with (
            patch.object(sender, "claim_ready", return_value=[uuid.uuid4()]),
            patch.object(sender, "send_run", new_callable=AsyncMock, return_value=True),
            patch.object(sender.asyncio, "sleep", sleep),
        ):
            await sender.baileys_lane_step(None)
        sleep.assert_not_awaited()

    async def test_idle_baileys_lane_sends_nothing(self):
        with (
            patch.object(sender, "claim_ready", return_value=[]),
            patch.object(sender, "send_run", new_callable=AsyncMock) as send,
        ):
            found, last = await sender.baileys_lane_step(None)
        assert not found and last is None
        send.assert_not_awaited()

    async def test_fast_lane_sends_concurrently(self):
        ids = [uuid.uuid4() for _ in range(3)]
        inflight = set()
        with (
            patch.object(sender, "claim_ready", return_value=ids) as claim,
            patch.object(sender, "send_run", new_callable=AsyncMock) as send,
        ):
            assert await sender.fast_lane_step(inflight)
            assert len(inflight) == 3
            await asyncio.gather(*inflight)
        claim.assert_called_once_with(("telegram", "cloud"), sender.FAST_CONCURRENCY)
        assert sorted(c.args[0] for c in send.await_args_list) == sorted(ids)

    async def test_fast_lane_claims_only_for_free_slots(self):
        # One slow send doesn't hold the lane: the other slots keep claiming.
        loop = asyncio.get_running_loop()
        slow = {loop.create_future()}
        with patch.object(sender, "claim_ready", return_value=[]) as claim:
            await sender.fast_lane_step(slow)
        claim.assert_called_once_with(("telegram", "cloud"), sender.FAST_CONCURRENCY - 1)
        full = {loop.create_future() for _ in range(sender.FAST_CONCURRENCY)}
        with patch.object(sender, "claim_ready") as claim:
            assert not await sender.fast_lane_step(full)
        claim.assert_not_called()


def _claim_db(runs):
    db = MagicMock()
    chain = db.query.return_value.join.return_value.filter.return_value.order_by.return_value
    chain.limit.return_value.with_for_update.return_value.all.return_value = runs
    return db, chain


@pytest.mark.parametrize("platforms", [("baileys",), ("telegram", "cloud")])
def test_claim_ready_moves_runs_to_sending(platforms):
    runs = [
        TaskRun(id=uuid.uuid4(), user_id=uuid.uuid4(), status="ready"),
        TaskRun(id=uuid.uuid4(), user_id=uuid.uuid4(), status="ready"),
    ]
    db, chain = _claim_db(runs)
    with (
        patch.object(sender, "SessionLocal", return_value=db),
        patch.object(sender, "utcnow", return_value=NOW),
    ):
        ids = sender.claim_ready(platforms, 5)
    assert ids == [r.id for r in runs]
    assert all(r.status == "sending" and r.started_at == NOW for r in runs)
    chain.limit.assert_called_once_with(5 * sender.CLAIM_SCAN_FACTOR)
    chain.limit.return_value.with_for_update.assert_called_once_with(skip_locked=True, of=TaskRun)
    # Chats with a run already 'sending' are excluded in SQL (NOT EXISTS).
    assert "NOT (EXISTS" in str(db.query.return_value.join.return_value.filter.call_args.args[2])
    db.commit.assert_called_once()


def test_claim_ready_takes_one_run_per_chat():
    chat_a, chat_b = uuid.uuid4(), uuid.uuid4()
    a1, a2, b1 = (
        TaskRun(id=uuid.uuid4(), user_id=chat_a, status="ready"),
        TaskRun(id=uuid.uuid4(), user_id=chat_a, status="ready"),
        TaskRun(id=uuid.uuid4(), user_id=chat_b, status="ready"),
    )
    db, _ = _claim_db([a1, a2, b1])
    with (
        patch.object(sender, "SessionLocal", return_value=db),
        patch.object(sender, "utcnow", return_value=NOW),
    ):
        assert sender.claim_ready(("telegram", "cloud"), 10) == [a1.id, b1.id]
    assert a2.status == "ready"  # waits for a later claim, after a1 is settled
