"""Broadcast sender (streams/broadcast_consumer.py) with the DB and chat client mocked.

- ``run_lane`` is driven over a scripted queue of recipients: the outcome per
  error class, the circuit breaker, pause/cancel, history on success, and the
  Baileys pacing (including where a restarted lane resumes the schedule).
- ``run_broadcast`` (the supervisor) is driven with fake lanes: restarting a
  lane that exited during a pause/resume or crashed, and stopping everything
  on pause, cancel or a lost lease.
- ``_heartbeat``: gives up once the lease may have expired, not on one blip.
"""

import asyncio
import uuid
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai_api.broadcast_pacing import PacingSettings
from ai_api.streams import broadcast_consumer as bc
from ai_api.whatsapp import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)

BID = uuid.uuid4()
TEXT = "New: voice replies!\n\n_opt out_"
PACING = PacingSettings(
    min_delay_seconds=20, max_delay_seconds=60, batch_size=2, batch_pause_seconds=600
)


def claimed(n: int, attempts: int = 0) -> bc._Claimed:
    return bc._Claimed(uuid.uuid4(), uuid.uuid4(), f"55119000000{n:02d}@s.whatsapp.net", attempts)


class Harness:
    """Patches every I/O edge of run_lane and records what it did."""

    def __init__(self, recipients, *, statuses=None, cap_waits=(), recent_sends=()):
        self.queue = list(recipients)
        self.statuses = list(statuses or [])
        self.cap_waits = list(cap_waits)
        self.recent_sends = list(recent_sends)
        self.client = MagicMock()
        self.client.send_text = AsyncMock()
        self.client.send_typing = AsyncMock()
        self.record = MagicMock(return_value=1)
        self.pause = MagicMock()
        self.sleeps: list[float] = []

    def _status(self, _bid):
        return self.statuses.pop(0) if self.statuses else "running"

    def _wait(self):
        # Never let a side effect raise StopIteration: inside asyncio.to_thread
        # it can't be set on the future and the test hangs.
        return self.cap_waits.pop(0) if self.cap_waits else 0.0

    def _next(self, _bid, _platform):
        return self.queue.pop(0) if self.queue else None

    async def _sleep(self, _bid, seconds):
        self.sleeps.append(seconds)
        return True

    async def run(self, platform="baileys"):
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(bc, "create_whatsapp_client", return_value=self.client))
            enter(patch.object(bc, "_broadcast_status", side_effect=self._status))
            enter(patch.object(bc, "_next_recipient", side_effect=self._next))
            enter(patch.object(bc, "_record", self.record))
            enter(patch.object(bc, "_pause_broadcast", self.pause))
            enter(patch.object(bc, "_baileys_wait_seconds", side_effect=self._wait))
            enter(patch.object(bc, "_pacing_settings", return_value=PACING))
            enter(patch.object(bc, "_recent_baileys_sends", return_value=self.recent_sends))
            enter(patch.object(bc, "_sleep_while_running", self._sleep))
            enter(patch.object(bc, "typing_seconds", return_value=0))
            await bc.run_lane(BID, platform, TEXT, MagicMock())

    def outcomes(self):
        return [(c.args[0], c.kwargs) for c in self.record.call_args_list]


# --- run_lane ---------------------------------------------------------------


async def test_sends_everyone_saves_history_and_shows_typing_first():
    recipients = [claimed(1), claimed(2)]
    h = Harness(recipients)
    await h.run()

    assert [c.args[0] for c in h.client.send_text.await_args_list] == [
        r.address for r in recipients
    ]
    assert all(c.args[1] == TEXT for c in h.client.send_text.await_args_list)
    h.client.send_typing.assert_any_await(recipients[0].address, "composing")
    for _rec_id, kwargs in h.outcomes():
        assert kwargs["status"] == "sent"
        assert kwargs["history_text"] == TEXT  # the reply to "what's this?" has context


async def test_baileys_pacing_delay_then_batch_pause():
    h = Harness([claimed(1), claimed(2), claimed(3)])
    await h.run()
    # batch_size=2: delay after #1, batch pause after #2, delay after #3.
    assert 20 <= h.sleeps[0] <= 60
    assert 420 <= h.sleeps[1] <= 780
    assert 20 <= h.sleeps[2] <= 60


async def test_restarted_lane_waits_out_the_gap_before_its_first_send():
    """Review finding: a resume or worker restart used to send immediately."""
    h = Harness([claimed(1)], recent_sends=[bc._utcnow() - timedelta(seconds=5)])
    await h.run()
    # First sleep happens BEFORE any send, and covers the rest of a 20-60s gap.
    assert 14 <= h.sleeps[0] <= 55
    h.client.send_text.assert_awaited_once()


async def test_restarted_lane_after_a_full_batch_takes_the_batch_pause():
    now = bc._utcnow()
    h = Harness([claimed(1)], recent_sends=[now - timedelta(seconds=5), now])
    await h.run()
    assert 410 <= h.sleeps[0] <= 780


async def test_telegram_lane_is_fast_and_skips_typing_and_pacing_restore():
    h = Harness([claimed(1)], recent_sends=[bc._utcnow()])
    await h.run("telegram")
    h.client.send_typing.assert_not_awaited()
    assert h.sleeps == [bc.LANE_DELAY_SECONDS["telegram"]]


async def test_not_on_whatsapp_fails_for_good():
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = WhatsAppNotFoundError()
    await h.run()
    assert h.outcomes()[0][1] == {"status": "failed", "error_code": "not_on_whatsapp"}


async def test_telegram_blocked_fails_without_opting_the_user_out():
    """Review finding: a 403 used to opt out the shared (maybe linked) users row."""
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = WhatsAppClientError("Forbidden: blocked", status_code=403)
    await h.run("telegram")
    assert h.outcomes()[0][1] == {"status": "failed", "error_code": "blocked"}


async def test_transient_error_keeps_recipient_pending_until_max_attempts():
    first, last = claimed(1, attempts=0), claimed(2, attempts=bc.MAX_ATTEMPTS - 1)
    h = Harness([first, last])
    h.client.send_text.side_effect = WhatsAppClientError("boom", status_code=500)
    await h.run()
    (_, first_kwargs), (_, last_kwargs) = h.outcomes()
    assert first_kwargs == {"status": None, "error_code": "http_500"}
    assert last_kwargs == {"status": "failed", "error_code": "http_500"}


async def test_transport_error_is_transient():
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = httpx.ConnectError("refused")
    await h.run()
    assert h.outcomes()[0][1]["error_code"] == "transport_error"


async def test_circuit_breaker_pauses_after_consecutive_failures():
    h = Harness([claimed(n) for n in range(10)])
    h.client.send_text.side_effect = WhatsAppClientError("boom", status_code=500)
    await h.run()
    assert h.client.send_text.await_count == bc.MAX_CONSECUTIVE_FAILURES
    h.pause.assert_called_once_with(BID, bc.PAUSE_CONSECUTIVE_FAILURES)


async def test_not_found_does_not_trip_the_circuit_breaker():
    h = Harness([claimed(n) for n in range(8)])
    h.client.send_text.side_effect = WhatsAppNotFoundError()
    await h.run()
    h.pause.assert_not_called()
    assert h.client.send_text.await_count == 8


async def test_disconnected_client_keeps_recipient_and_retries():
    rec = claimed(1)
    h = Harness([rec, rec])  # the same recipient is still pending on the retry
    h.client.send_text.side_effect = [WhatsAppNotConnectedError(), None]
    await h.run()
    assert h.sleeps[0] == bc.DISCONNECT_RETRY_SECONDS
    # Only the successful send is recorded: the 503 cost no attempt.
    assert [kw["status"] for _, kw in h.outcomes()] == ["sent"]


async def test_long_disconnect_pauses_the_broadcast():
    h = Harness([claimed(1), claimed(1)])
    h.client.send_text.side_effect = WhatsAppNotConnectedError()
    start = bc._utcnow()
    times = [start, start, start + bc.DISCONNECT_PAUSE_AFTER]
    with patch.object(bc, "_utcnow", side_effect=times):
        await h.run()
    h.pause.assert_called_once_with(BID, bc.PAUSE_CLIENT_DISCONNECTED)


@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_stops_before_sending_when_not_running(status):
    h = Harness([claimed(1)], statuses=[status])
    await h.run()
    h.client.send_text.assert_not_awaited()


async def test_waits_for_window_or_daily_cap_before_sending():
    h = Harness([claimed(1)], cap_waits=[3600.0])
    await h.run()
    assert h.sleeps[0] == 3600.0
    h.client.send_text.assert_awaited_once()


# --- run_broadcast (supervisor) -------------------------------------------


class Fleet:
    """Fake DB state + fake lanes for the supervisor."""

    def __init__(self, pending):
        self.status = "running"
        self.pending = set(pending)
        self.starts: list[str] = []
        self.finish = MagicMock()

    def patches(self, lane, heartbeat=None):
        async def forever(*_a, **_k):
            await asyncio.Event().wait()

        return [
            patch.object(bc, "_start_broadcast", return_value=TEXT),
            patch.object(bc, "_broadcast_status", side_effect=lambda _b: self.status),
            patch.object(bc, "_pending_platforms", side_effect=lambda _b: set(self.pending)),
            patch.object(bc, "_finish_if_done", self.finish),
            patch.object(bc, "run_lane", lane),
            patch.object(bc, "_heartbeat", heartbeat or forever),
        ]

    async def supervise(self, lane, heartbeat=None, until=None):
        with ExitStack() as stack:
            for p in self.patches(lane, heartbeat):
                stack.enter_context(p)
            task = asyncio.create_task(bc.run_broadcast(MagicMock(), "tok", BID, tick=0.01))
            if until is not None:
                for _ in range(500):
                    if until():
                        break
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(task, timeout=5)


async def test_supervisor_restarts_a_lane_that_exited_during_pause_resume():
    """Review finding: Telegram stopped for good while Baileys kept going."""
    fleet = Fleet({"baileys", "telegram"})
    baileys_done = asyncio.Event()

    async def lane(_bid, platform, _text, _http):
        fleet.starts.append(platform)
        if platform == "telegram":
            if fleet.starts.count("telegram") == 1:
                return  # saw the pause and exited; the broadcast was resumed
            fleet.pending.discard("telegram")
            baileys_done.set()
            return
        await baileys_done.wait()
        fleet.pending.discard("baileys")

    await fleet.supervise(lane)
    assert fleet.starts.count("telegram") == 2
    assert fleet.starts.count("baileys") == 1
    fleet.finish.assert_called_once_with(BID)


async def test_supervisor_restarts_a_crashed_lane():
    fleet = Fleet({"telegram"})

    async def lane(_bid, platform, _text, _http):
        fleet.starts.append(platform)
        if len(fleet.starts) == 1:
            raise RuntimeError("bug")
        fleet.pending.discard(platform)

    await fleet.supervise(lane)
    assert fleet.starts == ["telegram", "telegram"]


@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_supervisor_cancels_lanes_when_the_broadcast_stops(status):
    fleet = Fleet({"baileys", "telegram"})
    cancelled: list[str] = []

    async def lane(_bid, platform, _text, _http):
        fleet.starts.append(platform)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(platform)
            raise

    async def stop_soon():
        while len(fleet.starts) < 2:
            await asyncio.sleep(0.01)
        fleet.status = status

    stopper = asyncio.create_task(stop_soon())
    await fleet.supervise(lane)
    await stopper
    assert sorted(cancelled) == ["baileys", "telegram"]


async def test_supervisor_stops_sending_when_the_lease_is_lost():
    fleet = Fleet({"baileys"})
    cancelled = asyncio.Event()

    async def lane(_bid, platform, _text, _http):
        fleet.starts.append(platform)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def lost_heartbeat(_redis, _token):
        while not fleet.starts:
            await asyncio.sleep(0.01)

    await fleet.supervise(lane, heartbeat=lost_heartbeat)
    assert cancelled.is_set()


async def test_supervisor_does_nothing_if_the_broadcast_cannot_start():
    lane = AsyncMock()
    with (
        patch.object(bc, "_start_broadcast", return_value=None),
        patch.object(bc, "run_lane", lane),
    ):
        await bc.run_broadcast(MagicMock(), "tok", BID, tick=0.01)
    lane.assert_not_called()


# --- Lease ------------------------------------------------------------------


class TestLock:
    async def test_acquires_when_free(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        assert await bc.acquire_lock(redis, "tok") is True

    async def test_extends_own_lease(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=None)
        redis.eval = AsyncMock(return_value=1)
        assert await bc.acquire_lock(redis, "tok") is True

    async def test_refuses_someone_elses_lease(self):
        redis = MagicMock()
        redis.set = AsyncMock(return_value=None)
        redis.eval = AsyncMock(return_value=0)
        assert await bc.acquire_lock(redis, "tok") is False


class TestHeartbeat:
    @pytest.fixture(autouse=True)
    def fast_lease(self, monkeypatch):
        monkeypatch.setattr(bc, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(bc, "LOCK_TTL_SECONDS", 0.03)

    async def test_gives_up_before_the_lease_can_expire_while_redis_fails(self):
        """Review finding: it used to keep sending past the lease timeout."""
        with patch.object(bc, "acquire_lock", AsyncMock(side_effect=ConnectionError("down"))):
            await asyncio.wait_for(bc._heartbeat(MagicMock(), "tok"), timeout=1)

    async def test_survives_a_single_blip(self):
        results = [ConnectionError("blip")] + [True] * 1000
        with patch.object(bc, "acquire_lock", AsyncMock(side_effect=results)):
            task = asyncio.create_task(bc._heartbeat(MagicMock(), "tok"))
            await asyncio.sleep(0.2)
            assert not task.done()
            task.cancel()

    async def test_returns_when_another_worker_holds_the_lease(self):
        with patch.object(bc, "acquire_lock", AsyncMock(return_value=False)):
            await asyncio.wait_for(bc._heartbeat(MagicMock(), "tok"), timeout=1)
