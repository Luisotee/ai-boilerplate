"""Broadcast sender (streams/broadcast_consumer.py) with the DB and chat client mocked.

- ``run_lane`` is driven over a scripted queue of recipients: the outcome per
  error class, the circuit breaker, pause/cancel, history on success, and the
  Baileys pacing (including where a restarted lane resumes the schedule).
- ``run_broadcast`` (the supervisor) is driven with fake lanes: restarting a
  lane that exited during a pause/resume or crashed, and stopping everything
  on pause, cancel or a lost lease.
- ``_heartbeat``: gives up once the lease may have expired, not on one blip.
- Claims: a send that may have been delivered is never retried
  (``classify_outcome``), and a lane never starts a send the lease can't cover.
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
from ai_api.whatsapp.client import SendMessageResponse

BID = uuid.uuid4()
TEXT = "New: voice replies!\n\n_opt out_"
PACING = PacingSettings(
    min_delay_seconds=20, max_delay_seconds=60, batch_size=2, batch_pause_seconds=600
)


OK = SendMessageResponse(success=True, message_id="m1")


@pytest.fixture(autouse=True)
def fresh_disconnect_clock():
    bc._disconnected_since.clear()
    yield
    bc._disconnected_since.clear()


def claimed(n: int, attempts: int = 0) -> bc._Claimed:
    return bc._Claimed(uuid.uuid4(), uuid.uuid4(), f"55119000000{n:02d}@s.whatsapp.net", attempts)


class Harness:
    """Patches every I/O edge of run_lane and records what it did."""

    def __init__(
        self, recipients, *, statuses=None, cap_waits=(), recent_sends=(), claims=(), lease=None
    ):
        self.queue = list(recipients)
        self.claims = list(claims)  # scripted _claim results; True once exhausted
        self.lease = lease
        self.statuses = list(statuses or [])
        self.cap_waits = list(cap_waits)
        self.recent_sends = list(recent_sends)
        self.client = MagicMock()
        self.client.send_text = AsyncMock(return_value=OK)
        self.client.send_typing = AsyncMock()
        self.record = MagicMock(return_value=1)
        self.release = MagicMock()
        self.claimed_ids: list = []
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

    def _claim(self, rec_id):
        ok = self.claims.pop(0) if self.claims else True
        if ok:
            self.claimed_ids.append(rec_id)
        return ok

    async def _sleep(self, _bid, seconds, stop=None):
        self.sleeps.append(seconds)
        return not (stop is not None and stop.is_set())

    async def run(self, platform="baileys", stop=None):
        with ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(bc, "create_whatsapp_client", return_value=self.client))
            enter(patch.object(bc, "_broadcast_status", side_effect=self._status))
            enter(patch.object(bc, "_next_recipient", side_effect=self._next))
            enter(patch.object(bc, "_record", self.record))
            enter(patch.object(bc, "_claim", side_effect=self._claim))
            enter(patch.object(bc, "_release", self.release))
            enter(patch.object(bc, "_pause_broadcast", self.pause))
            enter(patch.object(bc, "_baileys_wait_seconds", side_effect=self._wait))
            enter(patch.object(bc, "_pacing_settings", return_value=PACING))
            enter(patch.object(bc, "_recent_baileys_sends", return_value=self.recent_sends))
            enter(patch.object(bc, "_sleep_while_running", self._sleep))
            enter(patch.object(bc, "typing_seconds", return_value=0))
            await bc.run_lane(BID, platform, TEXT, MagicMock(), stop=stop, lease=self.lease)

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


async def test_rate_limit_keeps_recipient_pending_until_max_attempts():
    """A 429 provably sent nothing, so it is retried (up to MAX_ATTEMPTS)."""
    first, last = claimed(1, attempts=0), claimed(2, attempts=bc.MAX_ATTEMPTS - 1)
    h = Harness([first, last])
    h.client.send_text.side_effect = WhatsAppClientError("slow down", status_code=429)
    await h.run()
    (_, first_kwargs), (_, last_kwargs) = h.outcomes()
    assert first_kwargs == {"status": None, "error_code": "http_429"}
    assert last_kwargs == {"status": "failed", "error_code": "http_429"}


@pytest.mark.parametrize(
    "error",
    [
        WhatsAppClientError("boom", status_code=500),
        WhatsAppClientError("maybe sent", status_code=502),
        httpx.ReadTimeout("no answer"),
        httpx.RemoteProtocolError("dropped"),
        ValueError("non-JSON 200 body"),
    ],
    ids=["500", "502", "read-timeout", "protocol-error", "garbled-body"],
)
async def test_a_send_that_may_have_landed_is_never_retried(error):
    """Review finding: a timeout or 500 after Baileys delivered was retried, so one
    person could get the broadcast three times."""
    rec = claimed(1)
    h = Harness([rec])
    h.client.send_text.side_effect = error
    await h.run("telegram")
    h.client.send_text.assert_awaited_once()
    assert h.outcomes() == [(rec.id, {"status": "failed", "error_code": "unknown"})]


async def test_success_false_is_recorded_unknown_not_sent():
    """Review finding: a 2xx with success:false was recorded as sent."""
    h = Harness([claimed(1)])
    h.client.send_text.return_value = SendMessageResponse(success=False)
    await h.run("telegram")
    assert h.outcomes()[0][1] == {"status": "failed", "error_code": "unknown"}


async def test_a_row_claimed_elsewhere_is_not_sent():
    first, second = claimed(1), claimed(2)
    h = Harness([first, second], claims=[False])
    await h.run("telegram")
    assert [c.args[0] for c in h.client.send_text.await_args_list] == [second.address]
    assert h.claimed_ids == [second.id]


async def test_every_send_is_claimed_first():
    recs = [claimed(1), claimed(2)]
    h = Harness(recs)
    await h.run("telegram")
    assert h.claimed_ids == [r.id for r in recs]


async def test_rejected_on_a_fast_lane_is_final_and_does_not_pause():
    """A 422 is the client refusing one recipient (dead number, closed window)."""
    h = Harness([claimed(n) for n in range(8)])
    h.client.send_text.side_effect = WhatsAppClientError("rejected", status_code=422)
    await h.run("cloud")
    assert h.client.send_text.await_count == 8
    assert {kw["error_code"] for _, kw in h.outcomes()} == {"rejected"}
    h.pause.assert_not_called()


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


async def test_fast_lanes_have_the_circuit_breaker_too():
    """Review finding: a wrong API key failed the whole Telegram audience in seconds."""
    h = Harness([claimed(n) for n in range(10)])
    h.client.send_text.side_effect = WhatsAppClientError("Unauthorized", status_code=401)
    await h.run("telegram")
    assert h.client.send_text.await_count == bc.MAX_CONSECUTIVE_FAILURES
    h.pause.assert_called_once_with(BID, bc.PAUSE_CONSECUTIVE_FAILURES)


async def test_a_failed_sent_write_is_retried_not_resent():
    h = Harness([claimed(1)])
    h.record.side_effect = [RuntimeError("db blip"), 1]
    with patch.object(bc.asyncio, "sleep", AsyncMock()):
        await h.run("telegram")
    h.client.send_text.assert_awaited_once()
    assert [c.kwargs["status"] for c in h.record.call_args_list] == ["sent", "sent"]


async def test_not_found_does_not_trip_the_circuit_breaker():
    h = Harness([claimed(n) for n in range(8)])
    h.client.send_text.side_effect = WhatsAppNotFoundError()
    await h.run()
    h.pause.assert_not_called()
    assert h.client.send_text.await_count == 8


async def test_disconnected_client_keeps_recipient_and_retries():
    rec = claimed(1)
    h = Harness([rec, rec])  # the same recipient is still pending on the retry
    h.client.send_text.side_effect = [WhatsAppNotConnectedError(), OK]
    await h.run()
    assert h.sleeps[0] == bc.DISCONNECT_RETRY_SECONDS
    # The 503 released the claim without costing an attempt; the retry was sent.
    h.release.assert_called_once_with(rec.id)
    assert [kw["status"] for _, kw in h.outcomes()] == ["sent"]


async def test_long_disconnect_pauses_the_broadcast():
    h = Harness([claimed(1), claimed(1)])
    h.client.send_text.side_effect = WhatsAppNotConnectedError()
    start = bc._utcnow()
    # resume_pacing's "now", then one clock read per 503.
    times = [start, start, start + bc.DISCONNECT_PAUSE_AFTER]
    with patch.object(bc, "_utcnow", side_effect=times):
        await h.run()
    h.pause.assert_called_once_with(BID, bc.PAUSE_CLIENT_DISCONNECTED)


async def test_the_disconnect_clock_survives_a_lane_restart():
    """Review finding: the clock lived in the lane, so every restart (each tick
    after a crash, each pause/resume) reset it and the pause never came."""
    bc._disconnected_since[(BID, "baileys")] = bc._utcnow() - bc.DISCONNECT_PAUSE_AFTER
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = WhatsAppNotConnectedError()
    await h.run()
    h.client.send_text.assert_awaited_once()
    h.pause.assert_called_once_with(BID, bc.PAUSE_CLIENT_DISCONNECTED)


async def test_a_send_clears_the_disconnect_clock():
    bc._disconnected_since[(BID, "telegram")] = bc._utcnow()
    await Harness([claimed(1)]).run("telegram")
    assert (BID, "telegram") not in bc._disconnected_since


async def test_an_expiring_lease_stops_the_lane_before_sending():
    loop = asyncio.get_running_loop()
    lease = bc._Lease(loop.time() - (bc.LOCK_TTL_SECONDS - bc.SEND_BUDGET_SECONDS) - 1)
    h = Harness([claimed(1)], lease=lease)
    await h.run("telegram")
    h.client.send_text.assert_not_awaited()
    assert h.claimed_ids == []


async def test_a_fresh_lease_sends():
    h = Harness([claimed(1)], lease=bc._Lease(asyncio.get_running_loop().time()))
    await h.run("telegram")
    h.client.send_text.assert_awaited_once()


async def test_a_lost_lease_stops_the_lane():
    h = Harness([claimed(1)], lease=bc._Lease(None))
    await h.run("telegram")
    h.client.send_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (WhatsAppNotConnectedError(), (bc.RELEASE, bc.PAUSE_CLIENT_DISCONNECTED)),
        (WhatsAppClientError("x", status_code=400), (bc.FINAL, "invalid_address")),
        (WhatsAppClientError("x", status_code=403), (bc.FINAL, "blocked")),
        (WhatsAppNotFoundError(), (bc.FINAL, "not_on_whatsapp")),
        (WhatsAppClientError("x", status_code=422), (bc.FINAL, "rejected")),
        (WhatsAppClientError("x", status_code=401), (bc.RETRY, "http_401")),
        (WhatsAppClientError("x", status_code=429), (bc.RETRY, "http_429")),
        (WhatsAppClientError("x", status_code=500), (bc.UNKNOWN, "unknown")),
        (WhatsAppClientError("x", status_code=502), (bc.UNKNOWN, "unknown")),
        (WhatsAppClientError("x"), (bc.UNKNOWN, "unknown")),
        (httpx.ConnectError("refused"), (bc.RETRY, "transport_error")),
        (httpx.ConnectTimeout("slow"), (bc.RETRY, "transport_error")),
        (httpx.PoolTimeout("busy"), (bc.RETRY, "transport_error")),
        (httpx.ReadTimeout("no answer"), (bc.UNKNOWN, "unknown")),
        (httpx.WriteTimeout("stuck"), (bc.UNKNOWN, "unknown")),
        (httpx.ReadError("reset"), (bc.UNKNOWN, "unknown")),
        (httpx.WriteError("reset"), (bc.UNKNOWN, "unknown")),
        (httpx.RemoteProtocolError("dropped"), (bc.UNKNOWN, "unknown")),
        (bc.SendNotConfirmedError("success=false"), (bc.UNKNOWN, "unknown")),
        (ValueError("not JSON"), (bc.UNKNOWN, "unknown")),
    ],
)
def test_classify_outcome(error, expected):
    assert bc.classify_outcome(error) == expected


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
            patch.object(bc, "_has_pending", side_effect=lambda _b: bool(self.pending)),
            patch.object(bc, "_sweep_orphaned_claims", return_value=0),
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

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
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

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        if len(fleet.starts) == 1:
            raise RuntimeError("bug")
        fleet.pending.discard(platform)

    await fleet.supervise(lane)
    assert fleet.starts == ["telegram", "telegram"]


@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_supervisor_drains_lanes_instead_of_cancelling_them(status):
    """Review finding: cancelling a lane mid-send left the message unrecorded,
    so it was sent again on resume. Lanes get ``stop`` and finish instead."""
    fleet = Fleet({"baileys", "telegram"})
    drained: list[str] = []

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        await stop.wait()  # e.g. between messages, or finishing a send
        drained.append(platform)

    async def stop_soon():
        while len(fleet.starts) < 2:
            await asyncio.sleep(0.01)
        fleet.status = status

    stopper = asyncio.create_task(stop_soon())
    await fleet.supervise(lane)
    await stopper
    assert sorted(drained) == ["baileys", "telegram"]


async def test_supervisor_cancels_a_lane_that_does_not_stop_in_time(monkeypatch):
    monkeypatch.setattr(bc, "LANE_DRAIN_SECONDS", 0.05)
    fleet = Fleet({"baileys"})
    cancelled = asyncio.Event()

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        try:
            await asyncio.Event().wait()  # ignores stop: a hung HTTP call
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def stop_soon():
        while not fleet.starts:
            await asyncio.sleep(0.01)
        fleet.status = "paused"

    stopper = asyncio.create_task(stop_soon())
    await fleet.supervise(lane)
    await stopper
    assert cancelled.is_set()


async def test_supervisor_drains_lanes_when_the_lease_is_lost():
    fleet = Fleet({"baileys"})
    drained = asyncio.Event()

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        await stop.wait()
        drained.set()

    async def lost_heartbeat(_redis, _token, _lease=None):
        while not fleet.starts:
            await asyncio.sleep(0.01)

    await fleet.supervise(lane, heartbeat=lost_heartbeat)
    assert drained.is_set()


async def test_in_flight_send_is_finished_and_recorded_when_stopped():
    stop = asyncio.Event()
    first, second = claimed(1), claimed(2)
    h = Harness([first, second])
    h.client.send_text.side_effect = lambda *_a: (stop.set(), OK)[1]  # pause lands mid-send
    await h.run("telegram", stop=stop)
    h.client.send_text.assert_awaited_once()
    assert h.outcomes() == [(first.id, {"status": "sent", "history_text": TEXT})]


async def test_stop_wakes_a_sleeping_lane_immediately():
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    with patch.object(bc, "_broadcast_status", return_value="running"):
        assert await asyncio.wait_for(bc._sleep_while_running(BID, 600, stop), timeout=1) is False


async def test_a_db_error_in_a_tick_does_not_cancel_an_in_flight_lane():
    """Review finding: one failed status read skipped the drain and cancelled
    every lane mid-send."""
    fleet = Fleet({"baileys"})
    reads = {"n": 0}
    cancelled = []

    def flaky_status(_bid):
        reads["n"] += 1
        if reads["n"] == 2:
            raise RuntimeError("db blip")
        return fleet.status

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        try:
            while reads["n"] < 4:  # mid-send across the failed tick
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.append(platform)
            raise
        fleet.pending.discard(platform)

    with ExitStack() as stack:
        for p in fleet.patches(lane):
            stack.enter_context(p)
        stack.enter_context(patch.object(bc, "_broadcast_status", side_effect=flaky_status))
        await asyncio.wait_for(bc.run_broadcast(MagicMock(), "t", BID, tick=0.01), 5)
    assert cancelled == []
    assert fleet.starts == ["baileys"]
    fleet.finish.assert_called_once_with(BID)


async def test_a_supervisor_crash_still_drains_the_lanes():
    fleet = Fleet({"baileys"})
    drained = asyncio.Event()

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        fleet.starts.append(platform)
        await stop.wait()
        drained.set()

    def boom(platforms):
        # A bug outside the guarded DB reads, once a lane is running.
        if fleet.starts:
            raise RuntimeError("bug in lane bookkeeping")
        return sorted(platforms)

    with ExitStack() as stack:
        for p in fleet.patches(lane):
            stack.enter_context(p)
        # run_broadcast's only sorted() call is the lane-start loop.
        stack.enter_context(patch.object(bc, "sorted", side_effect=boom, create=True))
        await asyncio.wait_for(bc.run_broadcast(MagicMock(), "t", BID, tick=0.01), 5)
    assert drained.is_set()


async def test_telegram_lane_gets_the_long_timeout_client():
    fleet = Fleet({"baileys", "telegram"})
    timeouts = {}

    async def lane(_bid, platform, _text, http, stop=None, lease=None):
        timeouts[platform] = http.timeout.read
        fleet.pending.discard(platform)

    await fleet.supervise(lane)
    assert timeouts["telegram"] == bc.TELEGRAM_SEND_TIMEOUT_SECONDS
    assert timeouts["baileys"] < bc.TELEGRAM_SEND_TIMEOUT_SECONDS


async def test_lanes_share_the_supervisors_lease():
    fleet = Fleet({"telegram"})
    seen = []
    lease = bc._Lease(0.0)

    async def lane(_bid, platform, _text, _http, stop=None, lease=None):
        seen.append(lease)
        fleet.pending.discard(platform)

    async def heartbeat(_redis, _token, hb_lease=None):
        seen.append(hb_lease)
        await asyncio.Event().wait()

    with ExitStack() as stack:
        for p in fleet.patches(lane, heartbeat):
            stack.enter_context(p)
        await asyncio.wait_for(bc.run_broadcast(MagicMock(), "t", BID, tick=0.01, lease=lease), 5)
    assert seen and all(x is lease for x in seen)


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
        # The real ratios (20s / 10s / 300s / 130s), scaled down 2000x.
        monkeypatch.setattr(bc, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(bc, "RENEW_TIMEOUT_SECONDS", 0.005)
        monkeypatch.setattr(bc, "LOCK_TTL_SECONDS", 0.15)
        monkeypatch.setattr(bc, "SEND_BUDGET_SECONDS", 0.065)

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

    async def test_a_hung_renewal_counts_as_a_failure(self):
        """Review finding: a half-open Redis connection blocked the heartbeat for
        minutes while the lanes kept sending on an expired lease."""

        async def hang(*_a):
            await asyncio.Event().wait()

        with patch.object(bc, "acquire_lock", side_effect=hang):
            await asyncio.wait_for(bc._heartbeat(MagicMock(), "tok"), timeout=1)

    async def test_renews_immediately(self):
        acquire = AsyncMock(return_value=True)
        with patch.object(bc, "acquire_lock", acquire):
            task = asyncio.create_task(bc._heartbeat(MagicMock(), "tok"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert acquire.await_count >= 1
            task.cancel()

    async def test_returns_when_another_worker_holds_the_lease(self):
        lease = bc._Lease(asyncio.get_running_loop().time())
        with patch.object(bc, "acquire_lock", AsyncMock(return_value=False)):
            await asyncio.wait_for(bc._heartbeat(MagicMock(), "tok", lease), timeout=1)
        assert lease.remaining() == 0.0  # lanes stop at once

    async def test_stamps_last_ok_with_the_time_the_renewal_was_sent(self):
        loop = asyncio.get_running_loop()
        lease = bc._Lease(loop.time() - 100)
        before = loop.time()
        with patch.object(bc, "acquire_lock", AsyncMock(return_value=True)):
            task = asyncio.create_task(bc._heartbeat(MagicMock(), "tok", lease))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
        assert before <= lease.last_ok <= loop.time()

    async def test_gives_up_while_a_full_send_still_fits_in_the_lease(self):
        """Review finding: it gave up only when the lease was already expiring,
        and the drain then let lanes keep sending past it."""
        loop = asyncio.get_running_loop()
        lease = bc._Lease(loop.time())
        with patch.object(bc, "acquire_lock", AsyncMock(side_effect=ConnectionError("down"))):
            await asyncio.wait_for(bc._heartbeat(MagicMock(), "tok", lease), timeout=1)
        # Stopped with at least one worst-case send left on the lease (small
        # tolerance for event-loop scheduling of the last sleep).
        assert lease.remaining() >= bc.SEND_BUDGET_SECONDS - bc.HEARTBEAT_SECONDS
