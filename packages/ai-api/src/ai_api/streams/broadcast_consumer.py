"""Broadcast sender: delivers ``broadcasts`` rows, one platform lane at a time.

Runs as the third task of the stream worker (``scripts/run_stream_worker.py``).
State lives in Postgres (``broadcasts`` / ``broadcast_recipients``), not in a
Redis stream: a broadcast can take days on Baileys, must survive restarts, and
is paused/resumed/cancelled by the admin API simply by changing its status.

- **One sender fleet-wide.** A Redis lease (``broadcast:lock``) makes sure only
  one worker process sends: two senders would double the rate on the single
  WhatsApp account the pacing is protecting. If the lease can't be renewed for
  long enough that it might have expired, sending stops (``_heartbeat``).
- **A supervisor, one lane per platform** (``run_broadcast``). Lanes run
  concurrently, so Telegram finishes in minutes while Baileys crawls. Every
  tick the supervisor stops everything once the broadcast isn't running, and
  (re)starts a lane for each platform that still has pending recipients — so a
  lane that exited during a pause/resume, or crashed, comes back. Lanes also
  re-read the status before each send and while sleeping.
- **Baileys anti-ban pacing** (all hot settings, re-read every message): a
  jittered delay between messages, a longer pause after every batch, a rolling
  24h cap across broadcasts, a daytime send window, and a "typing…" indicator
  before each message. Where the lane is in that schedule is rebuilt from
  ``broadcast_recipients.sent_at`` when it starts (``resume_pacing``), so a
  resume, lease handover or worker restart never skips a gap or a batch pause.
  A run of failures pauses the broadcast (on every lane: on Baileys a failure
  spike is often the first sign of a restriction, on the fast lanes a bad API
  key would otherwise fail the whole audience in seconds). Pauses are
  broadcast-wide: every lane stops, finishing its in-flight send first.
- **Delivery results**: sent → saved to the chat's history as an assistant
  message (so a reply like "what's this?" has context). 404 (not on WhatsApp)
  and 403 (Telegram: blocked, deactivated or kicked) fail for good — the chat
  is NOT opted out, since the same user row may also be reachable elsewhere
  (a /link-merged user) and a group may re-add the bot. A disconnected client
  (503) keeps the recipient pending and, after ``DISCONNECT_PAUSE_AFTER``,
  pauses the broadcast. Anything else is retried up to ``MAX_ATTEMPTS`` times.
- **Status writes are conditional** (``UPDATE … WHERE status = …``), so a
  cancel from the admin API is never overwritten by the worker.

A crash between a successful send and recording it re-sends that one message
after a restart. That is the accepted cost of not holding a DB transaction
open across a network call.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from redis.asyncio import Redis
from sqlalchemy import func

from ..broadcast_pacing import (
    PacingSettings,
    batch_pause,
    jittered_delay,
    parse_send_window,
    parse_timezone,
    resume_pacing,
    seconds_until_window,
    typing_seconds,
)
from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import (
    Broadcast,
    BroadcastRecipient,
    ConversationMessage,
    SessionLocal,
    User,
)
from ..logger import logger
from ..runtime_config import runtime_config
from ..services.broadcast import (
    NOT_WHITELISTED,
    SKIP_CLOUD_WINDOW,
    SKIP_OPTED_OUT,
    SKIP_USER_DELETED,
    Route,
    in_cloud_window,
    render_message,
    whitelist_filter,
)
from ..whatsapp import (
    WhatsAppClient,
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
    create_whatsapp_client,
)

LOCK_KEY = "broadcast:lock"
LOCK_TTL_SECONDS = 60
HEARTBEAT_SECONDS = 20
IDLE_POLL_SECONDS = 10
#: Supervisor tick, and the longest single sleep inside a lane, so a
#: pause/cancel is noticed while waiting.
SLEEP_SLICE_SECONDS = 15.0
#: How long the supervisor waits for lanes to finish their in-flight send
#: after asking them to stop. Longer than one send (HTTP timeout + typing).
LANE_DRAIN_SECONDS = 60.0
#: Attempts at recording a message that WAS sent before giving up (a failed
#: write would leave it pending, i.e. sent again).
SENT_RECORD_ATTEMPTS = 3

MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_FAILURES = 5
DISCONNECT_RETRY_SECONDS = 30.0
DISCONNECT_PAUSE_AFTER = timedelta(minutes=30)
#: Fast lanes: Telegram allows ~30 msg/s per bot; Cloud API has generous limits.
LANE_DELAY_SECONDS = {"telegram": 0.05, "cloud": 0.2}

PAUSE_CONSECUTIVE_FAILURES = "consecutive_failures"
PAUSE_CLIENT_DISCONNECTED = "client_disconnected"

_EXTEND_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _utcnow() -> datetime:
    """Naive UTC, matching the DB's ``datetime.utcnow`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


# --- Lease lock -------------------------------------------------------------


async def acquire_lock(redis: Redis, token: str) -> bool:
    """Take the lease, or extend it if this worker already holds it."""
    if await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS):
        return True
    return bool(await redis.eval(_EXTEND_LUA, 1, LOCK_KEY, token, LOCK_TTL_SECONDS))


async def release_lock(redis: Redis, token: str) -> None:
    try:
        await redis.eval(_RELEASE_LUA, 1, LOCK_KEY, token)
    except Exception:
        logger.warning("Broadcast: failed to release the sender lock", exc_info=True)


async def _heartbeat(redis: Redis, token: str) -> None:
    """Keep the lease alive; returns (ending the run) once it may be lost.

    A failed renewal isn't fatal on its own (a Redis blip), but once the next
    renewal would land after the lease could have expired, another worker may
    take it — so stop sending BEFORE that can happen, not after.

    - The first renewal is immediate, so the safety margin is measured from a
      renewal we actually saw succeed, not from whenever this task started.
    - Each renewal is bounded by ``HEARTBEAT_SECONDS / 2``: the worker's shared
      Redis client has no socket timeout (the stream consumers block in
      XREADGROUP), so a half-open connection would otherwise hang here for
      minutes while the lanes keep sending on an expired lease.
    """
    loop = asyncio.get_running_loop()
    last_ok = loop.time()
    first = True
    while True:
        if not first:
            await asyncio.sleep(HEARTBEAT_SECONDS)
        first = False
        try:
            async with asyncio.timeout(HEARTBEAT_SECONDS / 2):
                renewed = await acquire_lock(redis, token)
            if not renewed:
                logger.error("Broadcast: sender lock taken by another worker")
                return
            last_ok = loop.time()
        except Exception:  # includes TimeoutError from a stalled connection
            logger.warning("Broadcast: lock heartbeat failed", exc_info=True)
            if loop.time() - last_ok + HEARTBEAT_SECONDS >= LOCK_TTL_SECONDS:
                logger.error("Broadcast: cannot renew the sender lock; stopping sends")
                return


# --- DB helpers (sync; run through asyncio.to_thread) -----------------------


def _next_active_broadcast_id() -> uuid.UUID | None:
    """Running first (a resumed/interrupted one), then the oldest queued."""
    db = SessionLocal()
    try:
        row = (
            db.query(Broadcast)
            .filter(Broadcast.status.in_(("running", "queued")))
            .order_by((Broadcast.status == "running").desc(), Broadcast.created_at)
            .first()
        )
        return row.id if row else None
    finally:
        db.close()


def _set_status_if(broadcast_id: uuid.UUID, expected: str, values: dict) -> bool:
    """``UPDATE broadcasts SET … WHERE id = :id AND status = :expected``."""
    db = SessionLocal()
    try:
        updated = (
            db.query(Broadcast)
            .filter(Broadcast.id == broadcast_id, Broadcast.status == expected)
            .update(values, synchronize_session=False)
        )
        db.commit()
        return bool(updated)
    finally:
        db.close()


def _start_broadcast(broadcast_id: uuid.UUID) -> str | None:
    """queued → running (conditionally); return the text to send, or None."""
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        if broadcast is None:
            return None
        text = render_message(broadcast.text, broadcast.footer)
        started_at = broadcast.started_at
        status = broadcast.status
    finally:
        db.close()
    now = _utcnow()
    if status == "queued":
        _set_status_if(broadcast_id, "queued", {"status": "running", "started_at": now})
    elif status == "running" and started_at is None:
        # Paused before it ever started, then resumed straight to running.
        _set_status_if(broadcast_id, "running", {"started_at": now})
    return text if _broadcast_status(broadcast_id) == "running" else None


def _broadcast_status(broadcast_id: uuid.UUID) -> str | None:
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        return broadcast.status if broadcast else None
    finally:
        db.close()


def _pending_platforms(broadcast_id: uuid.UUID) -> set[str]:
    db = SessionLocal()
    try:
        return {
            p
            for (p,) in db.query(BroadcastRecipient.platform)
            .filter(
                BroadcastRecipient.broadcast_id == broadcast_id,
                BroadcastRecipient.status == "pending",
            )
            .distinct()
            .all()
        }
    finally:
        db.close()


def _pause_broadcast(broadcast_id: uuid.UUID, reason: str) -> None:
    if _set_status_if(broadcast_id, "running", {"status": "paused", "pause_reason": reason}):
        logger.warning("Broadcast %s paused: %s", broadcast_id, reason)


def _finish_if_done(broadcast_id: uuid.UUID) -> None:
    """Complete a still-running broadcast once nothing is pending."""
    if _pending_platforms(broadcast_id):
        return
    if _set_status_if(broadcast_id, "running", {"status": "completed", "finished_at": _utcnow()}):
        logger.info("Broadcast %s completed", broadcast_id)


@dataclass(frozen=True)
class _Claimed:
    id: uuid.UUID
    user_id: uuid.UUID
    address: str
    attempts: int


def _next_recipient(broadcast_id: uuid.UUID, platform: str) -> _Claimed | None:
    """Next pending recipient on this lane, re-checking eligibility right before sending.

    Recipients that can no longer be sent to (opted out since the snapshot, user
    row deleted, removed from the whitelist, Cloud window closed) are marked
    skipped here and passed over. A broadcast runs for days on Baileys, so a
    whitelist removal must stop it, like it stops chat replies at once.
    """
    allowed = whitelist_filter(runtime_config.get("whitelist_phones"))
    db = SessionLocal()
    try:
        while True:
            rec = (
                db.query(BroadcastRecipient)
                .filter(
                    BroadcastRecipient.broadcast_id == broadcast_id,
                    BroadcastRecipient.platform == platform,
                    BroadcastRecipient.status == "pending",
                )
                .order_by(BroadcastRecipient.attempts, BroadcastRecipient.id)
                .first()
            )
            if rec is None:
                return None
            user = db.get(User, rec.user_id) if rec.user_id else None
            reason = None
            if user is None:
                reason = SKIP_USER_DELETED
            elif user.broadcast_opt_out:
                reason = SKIP_OPTED_OUT
            elif not allowed(user, Route(platform, rec.address)):
                reason = NOT_WHITELISTED
            elif platform == "cloud" and not in_cloud_window(user.cloud_last_inbound_at, _utcnow()):
                reason = SKIP_CLOUD_WINDOW
            if reason is None:
                return _Claimed(rec.id, rec.user_id, rec.address, rec.attempts)
            rec.status = "skipped"
            rec.error_code = reason
            db.commit()
    finally:
        db.close()


def _record(
    recipient_id: uuid.UUID,
    *,
    status: str | None,
    error_code: str | None = None,
    history_text: str | None = None,
) -> int:
    """Store a delivery outcome; returns the recipient's attempt count."""
    db = SessionLocal()
    try:
        rec = db.get(BroadcastRecipient, recipient_id)
        if rec is None:
            return 0
        rec.attempts += 1
        if status:
            rec.status = status
        rec.error_code = error_code
        if status == "sent":
            rec.sent_at = _utcnow()
        if rec.user_id and history_text is not None:
            db.add(ConversationMessage(user_id=rec.user_id, role="assistant", content=history_text))
        db.commit()
        return rec.attempts
    finally:
        db.close()


def _recent_baileys_sends(limit: int) -> list[datetime]:
    """Latest Baileys ``sent_at`` values, newest first, across all broadcasts."""
    db = SessionLocal()
    try:
        rows = (
            db.query(BroadcastRecipient.sent_at)
            .filter(
                BroadcastRecipient.platform == "baileys",
                BroadcastRecipient.sent_at.isnot(None),
            )
            .order_by(BroadcastRecipient.sent_at.desc())
            .limit(max(1, limit))
            .all()
        )
        return [sent_at for (sent_at,) in rows]
    finally:
        db.close()


def _pacing_settings() -> PacingSettings:
    """Current pacing settings (may refresh runtime_config from the DB)."""
    return PacingSettings(
        min_delay_seconds=runtime_config.get("broadcast_min_delay_seconds"),
        max_delay_seconds=runtime_config.get("broadcast_max_delay_seconds"),
        batch_size=runtime_config.get("broadcast_batch_size"),
        batch_pause_seconds=runtime_config.get("broadcast_batch_pause_seconds"),
    )


def _baileys_cap_wait_seconds() -> float:
    """Seconds until the rolling 24h Baileys cap frees a slot (0 = send now)."""
    limit = runtime_config.get("broadcast_daily_limit")
    if not limit or limit <= 0:
        return 0.0
    since = _utcnow() - timedelta(days=1)
    db = SessionLocal()
    try:
        count, oldest = (
            db.query(func.count(BroadcastRecipient.id), func.min(BroadcastRecipient.sent_at))
            .filter(
                BroadcastRecipient.platform == "baileys",
                BroadcastRecipient.sent_at >= since,
            )
            .one()
        )
    finally:
        db.close()
    if count < limit or oldest is None:
        return 0.0
    return max(1.0, (oldest - since).total_seconds())


def _window_wait_seconds() -> float:
    # config.py and PATCH /admin/settings both reject bad values, so these
    # fallbacks only guard against a value corrupted in the DB by hand.
    try:
        window = parse_send_window(runtime_config.get("broadcast_send_window"))
    except ValueError:
        logger.error("Broadcast: invalid broadcast_send_window; sending at any time")
        window = None
    try:
        tz = parse_timezone(runtime_config.get("broadcast_timezone"))
    except ValueError:
        logger.error("Broadcast: invalid broadcast_timezone; using UTC")
        tz = UTC
    return seconds_until_window(window, datetime.now(tz))


def _baileys_wait_seconds() -> float:
    """How long the Baileys lane must wait before it may send (window + daily cap)."""
    return max(_window_wait_seconds(), _baileys_cap_wait_seconds())


# --- Lanes ------------------------------------------------------------------


async def _sleep_while_running(
    broadcast_id: uuid.UUID, seconds: float, stop: asyncio.Event | None = None
) -> bool:
    """Sleep in slices; False as soon as the broadcast stops running.

    ``stop`` (set by the supervisor) wakes the sleep immediately; the DB status
    is also re-read every slice in case the lane runs without a supervisor.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return True
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(remaining, SLEEP_SLICE_SECONDS))
                return False
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(min(remaining, SLEEP_SLICE_SECONDS))
        if await asyncio.to_thread(_broadcast_status, broadcast_id) != "running":
            return False


async def _deliver(client: WhatsAppClient, platform: str, address: str, text: str) -> None:
    """Send one message; on Baileys, show "typing…" first like a person would."""
    if platform == "baileys":
        try:
            await client.send_typing(address, "composing")
        except WhatsAppNotConnectedError:
            raise
        except Exception as e:  # cosmetic; never block the send on it
            logger.debug("Broadcast: typing indicator failed for %s: %s", address, e)
        await asyncio.sleep(typing_seconds(text))
    await client.send_text(address, text)


async def run_lane(
    broadcast_id: uuid.UUID,
    platform: str,
    text: str,
    http_client: httpx.AsyncClient,
    rng: random.Random | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Send every pending recipient of one platform, until done or stopped.

    Stopping (``stop`` set, or the status no longer ``running``) is only ever
    checked BETWEEN messages: a send that has started is always finished and
    recorded, because cancelling it after the request reached the chat client
    would leave the recipient pending and re-send it on resume.
    """
    rng = rng or random.Random()
    client = create_whatsapp_client(
        http_client=http_client,
        base_url=get_whatsapp_client_url(platform),
        api_key=get_whatsapp_api_key(platform),
    )
    sent_in_batch = 0
    consecutive_failures = 0
    disconnected_since: datetime | None = None
    logger.info("Broadcast %s: %s lane started", broadcast_id, platform)

    if platform == "baileys":
        # Pick up the schedule where the last Baileys lane left it.
        pacing = await asyncio.to_thread(_pacing_settings)
        recent = await asyncio.to_thread(_recent_baileys_sends, pacing.batch_size)
        sent_in_batch, wait = resume_pacing(recent, _utcnow(), rng, pacing)
        if wait > 0:
            logger.info("Broadcast %s: baileys lane resuming in %.0fs", broadcast_id, wait)
            if not await _sleep_while_running(broadcast_id, wait, stop):
                return

    while True:
        if stop is not None and stop.is_set():
            return
        if await asyncio.to_thread(_broadcast_status, broadcast_id) != "running":
            return

        if platform == "baileys":
            wait = await asyncio.to_thread(_baileys_wait_seconds)
            if wait > 0:
                logger.info("Broadcast %s: baileys lane waiting %.0fs", broadcast_id, wait)
                if not await _sleep_while_running(broadcast_id, wait, stop):
                    return
                continue

        rec = await asyncio.to_thread(_next_recipient, broadcast_id, platform)
        if rec is None:
            logger.info("Broadcast %s: %s lane finished", broadcast_id, platform)
            return

        try:
            await _deliver(client, platform, rec.address, text)
        except WhatsAppNotConnectedError:
            now = _utcnow()
            disconnected_since = disconnected_since or now
            if now - disconnected_since >= DISCONNECT_PAUSE_AFTER:
                await asyncio.to_thread(_pause_broadcast, broadcast_id, PAUSE_CLIENT_DISCONNECTED)
                return
            logger.warning("Broadcast %s: %s client not connected", broadcast_id, platform)
            if not await _sleep_while_running(broadcast_id, DISCONNECT_RETRY_SECONDS, stop):
                return
            continue
        except WhatsAppNotFoundError:
            # A number that left WhatsApp: expected in a long-lived user base,
            # so it doesn't count toward the failure circuit breaker.
            disconnected_since = None
            await asyncio.to_thread(_record, rec.id, status="failed", error_code="not_on_whatsapp")
            continue
        except WhatsAppClientError as e:
            disconnected_since = None
            if e.status_code == 403:
                # Telegram: blocked, deactivated, or the bot left the group.
                # Deliberately no opt-out (see the module docstring).
                await asyncio.to_thread(_record, rec.id, status="failed", error_code="blocked")
                continue
            if e.status_code == 400:
                await asyncio.to_thread(
                    _record, rec.id, status="failed", error_code="invalid_address"
                )
                continue
            consecutive_failures += 1
            await _record_transient(broadcast_id, rec, f"http_{e.status_code}")
        except (httpx.HTTPError, OSError) as e:
            disconnected_since = None
            consecutive_failures += 1
            logger.warning("Broadcast %s: transport error on %s: %s", broadcast_id, platform, e)
            await _record_transient(broadcast_id, rec, "transport_error")
        else:
            disconnected_since = None
            consecutive_failures = 0
            await _record_sent(rec, text)
            sent_in_batch += 1

        # Every lane has the breaker: on the fast lanes a wrong API key (401)
        # or an outage would otherwise fail the whole audience in seconds.
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            await asyncio.to_thread(_pause_broadcast, broadcast_id, PAUSE_CONSECUTIVE_FAILURES)
            return

        if platform == "baileys":
            pacing = await asyncio.to_thread(_pacing_settings)
            if sent_in_batch >= max(1, pacing.batch_size):
                sent_in_batch = 0
                pause = batch_pause(rng, pacing.batch_pause_seconds)
            else:
                pause = jittered_delay(rng, pacing.min_delay_seconds, pacing.max_delay_seconds)
            if consecutive_failures:
                pause += 30.0 * 2 ** (consecutive_failures - 1)
        else:
            pause = LANE_DELAY_SECONDS.get(platform, 0.2)
            if consecutive_failures:
                pause += min(60.0, 5.0 * 2 ** (consecutive_failures - 1))
        if not await _sleep_while_running(broadcast_id, pause, stop):
            return


async def _record_sent(rec: _Claimed, text: str) -> None:
    """Record a message that WAS delivered, retrying a failed write.

    If this write is lost the recipient stays pending and gets the message
    again, so a brief DB blip is retried. If the DB stays down the error
    propagates: the lane crashes and the supervisor restarts it a tick later.
    """
    for attempt in range(1, SENT_RECORD_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_record, rec.id, status="sent", history_text=text)
            return
        except Exception:
            if attempt == SENT_RECORD_ATTEMPTS:
                logger.error(
                    "Broadcast: message to %s was sent but could not be recorded; it may "
                    "be sent again",
                    rec.address,
                    exc_info=True,
                )
                raise
            logger.warning("Broadcast: recording a sent message failed; retrying", exc_info=True)
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))


async def _record_transient(broadcast_id: uuid.UUID, rec: _Claimed, error_code: str) -> None:
    """Count a retriable failure; give up on the recipient after MAX_ATTEMPTS."""
    final = rec.attempts + 1 >= MAX_ATTEMPTS
    await asyncio.to_thread(
        _record, rec.id, status="failed" if final else None, error_code=error_code
    )
    logger.warning(
        "Broadcast %s: send to %s failed (%s, attempt %d/%d)",
        broadcast_id,
        rec.address,
        error_code,
        rec.attempts + 1,
        MAX_ATTEMPTS,
    )


# --- Orchestration ----------------------------------------------------------


async def run_broadcast(
    redis: Redis,
    token: str,
    broadcast_id: uuid.UUID,
    *,
    tick: float = SLEEP_SLICE_SECONDS,
) -> None:
    """Supervise one broadcast's lanes while holding the lease.

    Each tick: stop if the broadcast isn't running or the lease is lost;
    otherwise make sure every platform with pending recipients has a live lane.
    That (re)starts lanes that exited during a pause/resume or crashed — a
    crashed lane waits one tick before its restart so a persistent bug can't
    spin.

    Stopping never cancels a lane mid-send (that would re-send the message on
    resume): the supervisor sets ``stop``, which lanes check between messages
    and which wakes them from any sleep, then waits up to
    ``LANE_DRAIN_SECONDS`` for them to finish. Only a lane still running after
    that, or a worker shutdown, is cancelled.
    """
    text = await asyncio.to_thread(_start_broadcast, broadcast_id)
    if text is None:
        return
    logger.info("Broadcast %s running", broadcast_id)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    lanes: dict[str, asyncio.Task] = {}
    retry_after: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http:
        heartbeat = asyncio.create_task(_heartbeat(redis, token), name="broadcast-heartbeat")
        try:
            while True:
                if heartbeat.done():
                    logger.error("Broadcast %s: lost the sender lock; stopping", broadcast_id)
                    break
                if await asyncio.to_thread(_broadcast_status, broadcast_id) != "running":
                    break

                for platform, task in list(lanes.items()):
                    if not task.done():
                        continue
                    del lanes[platform]
                    if not task.cancelled() and task.exception() is not None:
                        logger.error(
                            "Broadcast %s: %s lane crashed; restarting",
                            broadcast_id,
                            platform,
                            exc_info=task.exception(),
                        )
                        retry_after[platform] = loop.time() + tick

                pending = await asyncio.to_thread(_pending_platforms, broadcast_id)
                if not pending and not lanes:
                    break
                for platform in sorted(pending - lanes.keys()):
                    if loop.time() < retry_after.get(platform, 0.0):
                        continue
                    lanes[platform] = asyncio.create_task(
                        run_lane(broadcast_id, platform, text, http, stop=stop),
                        name=f"broadcast-{platform}",
                    )

                await asyncio.wait(
                    {heartbeat, *lanes.values()},
                    timeout=tick,
                    return_when=asyncio.FIRST_COMPLETED,
                )

            # Normal stop: let every in-flight send finish and be recorded.
            stop.set()
            if lanes:
                _done, stuck = await asyncio.wait(lanes.values(), timeout=LANE_DRAIN_SECONDS)
                for task in stuck:
                    logger.error(
                        "Broadcast %s: %s did not stop within %.0fs; cancelling",
                        broadcast_id,
                        task.get_name(),
                        LANE_DRAIN_SECONDS,
                    )
        finally:
            # Only stragglers (or everything, on a worker shutdown) get here unfinished.
            stop.set()
            for task in (*lanes.values(), heartbeat):
                task.cancel()
            await asyncio.gather(*lanes.values(), heartbeat, return_exceptions=True)

    await asyncio.to_thread(_finish_if_done, broadcast_id)


async def run_broadcast_consumer(redis: Redis) -> None:
    """Worker loop: pick up queued/running broadcasts and send them."""
    token = uuid.uuid4().hex
    logger.info("📣 Starting broadcast consumer")
    try:
        while True:
            try:
                if await acquire_lock(redis, token):
                    broadcast_id = await asyncio.to_thread(_next_active_broadcast_id)
                    if broadcast_id is not None:
                        await run_broadcast(redis, token, broadcast_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Error in broadcast consumer loop", exc_info=True)
            await asyncio.sleep(IDLE_POLL_SECONDS)
    finally:
        await release_lock(redis, token)
