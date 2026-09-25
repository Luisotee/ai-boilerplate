"""Broadcast lanes (streams/broadcast_consumer.py) with the DB and chat client mocked.

Each test drives ``run_lane`` over a scripted queue of recipients and checks
what gets recorded: the outcome per error class, the circuit breaker, pause /
cancel, history on success, and the Baileys pacing calls.
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai_api.streams import broadcast_consumer as bc
from ai_api.whatsapp import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)

BID = uuid.uuid4()
TEXT = "New: voice replies!\n\n_opt out_"

SETTINGS = {
    "broadcast_batch_size": 2,
    "broadcast_batch_pause_seconds": 600,
    "broadcast_min_delay_seconds": 20,
    "broadcast_max_delay_seconds": 60,
    "broadcast_daily_limit": 150,
    "broadcast_send_window": "",
    "broadcast_timezone": "UTC",
}


def claimed(n: int, attempts: int = 0) -> bc._Claimed:
    return bc._Claimed(uuid.uuid4(), uuid.uuid4(), f"55119000000{n:02d}@s.whatsapp.net", attempts)


class Harness:
    """Patches every I/O edge of run_lane and records what it did."""

    def __init__(self, recipients, *, statuses=None, cap_waits=()):
        self.queue = list(recipients)
        self.cap_waits = list(cap_waits)
        self.statuses = list(statuses or [])
        self.client = MagicMock()
        self.client.send_text = AsyncMock()
        self.client.send_typing = AsyncMock()
        self.record = MagicMock(return_value=1)
        self.pause = MagicMock()
        self.sleeps: list[float] = []

    def _status(self, _bid):
        return self.statuses.pop(0) if self.statuses else "running"

    def _cap_wait(self):
        # Never let a side effect raise StopIteration: inside asyncio.to_thread
        # it can't be set on the future and the test hangs.
        return self.cap_waits.pop(0) if self.cap_waits else 0.0

    def _next(self, _bid, _platform):
        return self.queue.pop(0) if self.queue else None

    async def _sleep(self, _bid, seconds):
        self.sleeps.append(seconds)
        return True

    async def run(self, platform="baileys", **kwargs):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(bc, "create_whatsapp_client", return_value=self.client)
            )
            stack.enter_context(patch.object(bc, "_broadcast_status", side_effect=self._status))
            stack.enter_context(patch.object(bc, "_next_recipient", side_effect=self._next))
            stack.enter_context(patch.object(bc, "_record", self.record))
            stack.enter_context(patch.object(bc, "_pause_broadcast", self.pause))
            stack.enter_context(
                patch.object(bc, "_baileys_cap_wait_seconds", side_effect=self._cap_wait)
            )
            stack.enter_context(patch.object(bc, "_window_wait_seconds", return_value=0.0))
            stack.enter_context(patch.object(bc, "_sleep_while_running", self._sleep))
            stack.enter_context(patch.object(bc, "typing_seconds", return_value=0))
            stack.enter_context(
                patch.object(bc.runtime_config, "get", side_effect=SETTINGS.__getitem__)
            )
            await bc.run_lane(BID, platform, TEXT, MagicMock(), **kwargs)

    def outcomes(self):
        return [(c.args[0], c.kwargs) for c in self.record.call_args_list]


async def test_sends_everyone_saves_history_and_shows_typing_first():
    recipients = [claimed(1), claimed(2)]
    h = Harness(recipients)
    await h.run()

    assert [c.args[0] for c in h.client.send_text.await_args_list] == [
        r.address for r in recipients
    ]
    assert all(c.args[1] == TEXT for c in h.client.send_text.await_args_list)
    h.client.send_typing.assert_any_await(recipients[0].address, "composing")
    for rec_id, kwargs in h.outcomes():
        assert kwargs["status"] == "sent"
        assert kwargs["history_text"] == TEXT  # the reply to "what's this?" has context


async def test_baileys_pacing_delay_then_batch_pause():
    h = Harness([claimed(1), claimed(2), claimed(3)])
    await h.run()
    # batch_size=2: delay after #1, batch pause after #2, delay after #3.
    assert 20 <= h.sleeps[0] <= 60
    assert 420 <= h.sleeps[1] <= 780
    assert 20 <= h.sleeps[2] <= 60


async def test_telegram_lane_is_fast_and_skips_typing():
    h = Harness([claimed(1)])
    await h.run("telegram")
    h.client.send_typing.assert_not_awaited()
    assert h.sleeps == [bc.LANE_DELAY_SECONDS["telegram"]]


async def test_not_on_whatsapp_fails_for_good():
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = WhatsAppNotFoundError()
    await h.run()
    assert h.outcomes()[0][1] == {"status": "failed", "error_code": "not_on_whatsapp"}


async def test_telegram_blocked_fails_and_opts_out():
    h = Harness([claimed(1)])
    h.client.send_text.side_effect = WhatsAppClientError("Forbidden: blocked", status_code=403)
    await h.run("telegram")
    assert h.outcomes()[0][1] == {"status": "failed", "error_code": "blocked", "opt_out": True}


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
    times = [start, start + bc.DISCONNECT_PAUSE_AFTER]
    with patch.object(bc, "_utcnow", side_effect=times):
        await h.run()
    h.pause.assert_called_once_with(BID, bc.PAUSE_CLIENT_DISCONNECTED)


@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_stops_before_sending_when_not_running(status):
    h = Harness([claimed(1)], statuses=[status])
    await h.run()
    h.client.send_text.assert_not_awaited()


async def test_waits_for_daily_cap_before_sending():
    h = Harness([claimed(1)], cap_waits=[3600.0])
    await h.run()
    assert h.sleeps[0] == 3600.0
    h.client.send_text.assert_awaited_once()


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
