"""Broadcast sender: delivers ``broadcasts`` rows, one platform lane at a time.

Runs as the third task of the stream worker (``scripts/run_stream_worker.py``).
State lives in Postgres (``broadcasts`` / ``broadcast_recipients``), not in a
Redis stream: a broadcast can take days on Baileys, must survive restarts, and
is paused/resumed/cancelled by the admin API simply by changing its status.

- **One sender fleet-wide.** A Redis lease (``broadcast:lock``) makes sure only
  one worker process sends: two senders would double the rate on the single
  WhatsApp account the pacing is protecting.
- **One lane per platform**, concurrently, so Telegram finishes in minutes while
  Baileys crawls. Every lane re-reads the broadcast status before each send
  (and while sleeping), so pause/cancel take effect before the next message.
- **Baileys anti-ban pacing** (all hot settings, re-read every message): a
  jittered delay between messages, a longer pause after every batch, a rolling
  24h cap across broadcasts, a daytime send window, and a "typing…" indicator
  before each message. A run of failures pauses the broadcast — a failure spike
  is often the first sign of a restriction.
- **Delivery results**: sent → saved to the chat's history as an assistant
  message (so a reply like "what's this?" has context). 404 (not on WhatsApp)
  and 403 (Telegram: blocked / deactivated; the chat is also opted out) fail
  for good. A disconnected client (503) keeps the recipient pending and, after
  ``DISCONNECT_PAUSE_AFTER``, pauses the broadcast. Anything else is retried
  up to ``MAX_ATTEMPTS`` times.

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
    SKIP_CLOUD_WINDOW,
    SKIP_OPTED_OUT,
    SKIP_USER_DELETED,
    batch_pause,
    in_cloud_window,
    jittered_delay,
    parse_send_window,
    parse_timezone,
    render_message,
    seconds_until_window,
    typing_seconds,
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
#: Longest single sleep, so a pause/cancel is noticed while waiting.
SLEEP_SLICE_SECONDS = 15.0

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


def _start_broadcast(broadcast_id: uuid.UUID) -> tuple[str, str, list[str]] | None:
    """Mark it running; return (message, status, platforms with pending work)."""
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        if broadcast is None or broadcast.status not in ("queued", "running"):
            return None
        if broadcast.status == "queued" or broadcast.started_at is None:
            # A broadcast paused before it ever started resumes as "running".
            broadcast.status = "running"
            broadcast.started_at = broadcast.started_at or _utcnow()
            db.commit()
        platforms = [
            p
            for (p,) in db.query(BroadcastRecipient.platform)
            .filter(
                BroadcastRecipient.broadcast_id == broadcast_id,
                BroadcastRecipient.status == "pending",
            )
            .distinct()
            .all()
        ]
        return render_message(broadcast.text, broadcast.footer), broadcast.status, platforms
    finally:
        db.close()


def _broadcast_status(broadcast_id: uuid.UUID) -> str | None:
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        return broadcast.status if broadcast else None
    finally:
        db.close()


def _pause_broadcast(broadcast_id: uuid.UUID, reason: str) -> None:
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        if broadcast and broadcast.status == "running":
            broadcast.status = "paused"
            broadcast.pause_reason = reason
            db.commit()
            logger.warning("Broadcast %s paused: %s", broadcast_id, reason)
    finally:
        db.close()


def _finish_if_done(broadcast_id: uuid.UUID) -> None:
    """Complete a still-running broadcast once nothing is pending."""
    db = SessionLocal()
    try:
        broadcast = db.get(Broadcast, broadcast_id)
        if broadcast is None or broadcast.status != "running":
            return
        pending = (
            db.query(func.count(BroadcastRecipient.id))
            .filter(
                BroadcastRecipient.broadcast_id == broadcast_id,
                BroadcastRecipient.status == "pending",
            )
            .scalar()
        )
        if pending == 0:
            broadcast.status = "completed"
            broadcast.finished_at = _utcnow()
            db.commit()
            logger.info("Broadcast %s completed", broadcast_id)
    finally:
        db.close()


@dataclass(frozen=True)
class _Claimed:
    id: uuid.UUID
    user_id: uuid.UUID
    address: str
    attempts: int


def _last_inbound_at(db, user_id) -> datetime | None:
    return (
        db.query(func.max(ConversationMessage.timestamp))
        .filter(ConversationMessage.user_id == user_id, ConversationMessage.role == "user")
        .scalar()
    )


def _next_recipient(broadcast_id: uuid.UUID, platform: str) -> _Claimed | None:
    """Next pending recipient on this lane, re-checking opt-out right before sending.

    Recipients that can no longer be sent to (opted out since the snapshot, user
    row deleted, Cloud window closed) are marked skipped here and passed over.
    """
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
            elif platform == "cloud" and not in_cloud_window(
                _last_inbound_at(db, user.id), _utcnow()
            ):
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
    count_attempt: bool = True,
    history_text: str | None = None,
    opt_out: bool = False,
) -> int:
    """Store a delivery outcome; returns the recipient's attempt count."""
    db = SessionLocal()
    try:
        rec = db.get(BroadcastRecipient, recipient_id)
        if rec is None:
            return 0
        if count_attempt:
            rec.attempts += 1
        if status:
            rec.status = status
        rec.error_code = error_code
        if status == "sent":
            rec.sent_at = _utcnow()
        if rec.user_id and history_text is not None:
            db.add(ConversationMessage(user_id=rec.user_id, role="assistant", content=history_text))
        if rec.user_id and opt_out:
            user = db.get(User, rec.user_id)
            if user:
                user.broadcast_opt_out = True
        db.commit()
        return rec.attempts
    finally:
        db.close()


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


# --- Lanes ------------------------------------------------------------------


async def _sleep_while_running(broadcast_id: uuid.UUID, seconds: float) -> bool:
    """Sleep in slices; False as soon as the broadcast stops running."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return True
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
) -> None:
    """Send every pending recipient of one platform, until done or stopped."""
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

    while True:
        if await asyncio.to_thread(_broadcast_status, broadcast_id) != "running":
            return

        if platform == "baileys":
            wait = max(_window_wait_seconds(), await asyncio.to_thread(_baileys_cap_wait_seconds))
            if wait > 0:
                logger.info("Broadcast %s: baileys lane waiting %.0fs", broadcast_id, wait)
                if not await _sleep_while_running(broadcast_id, wait):
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
            if not await _sleep_while_running(broadcast_id, DISCONNECT_RETRY_SECONDS):
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
                # Telegram: the user blocked the bot or deleted their account.
                # Opt them out so no future broadcast tries again.
                await asyncio.to_thread(
                    _record, rec.id, status="failed", error_code="blocked", opt_out=True
                )
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
            await asyncio.to_thread(_record, rec.id, status="sent", history_text=text)
            sent_in_batch += 1

        if platform == "baileys" and consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            await asyncio.to_thread(_pause_broadcast, broadcast_id, PAUSE_CONSECUTIVE_FAILURES)
            return

        if platform == "baileys":
            batch_size = max(1, runtime_config.get("broadcast_batch_size"))
            if sent_in_batch >= batch_size:
                sent_in_batch = 0
                pause = batch_pause(rng, runtime_config.get("broadcast_batch_pause_seconds"))
            else:
                pause = jittered_delay(
                    rng,
                    runtime_config.get("broadcast_min_delay_seconds"),
                    runtime_config.get("broadcast_max_delay_seconds"),
                )
            if consecutive_failures:
                pause += 30.0 * 2 ** (consecutive_failures - 1)
        else:
            pause = LANE_DELAY_SECONDS.get(platform, 0.2)
            if consecutive_failures:
                pause += min(60.0, 5.0 * 2 ** (consecutive_failures - 1))
        if not await _sleep_while_running(broadcast_id, pause):
            return


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


async def run_broadcast(redis: Redis, token: str, broadcast_id: uuid.UUID) -> None:
    """Run every lane of one broadcast while holding the lease."""
    started = await asyncio.to_thread(_start_broadcast, broadcast_id)
    if started is None:
        return
    text, _status, platforms = started
    logger.info("Broadcast %s running on %s", broadcast_id, ", ".join(platforms) or "no lanes")

    async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http:
        lanes = [
            asyncio.create_task(run_lane(broadcast_id, p, text, http), name=f"broadcast-{p}")
            for p in platforms
        ]
        heartbeat = asyncio.create_task(_heartbeat(redis, token), name="broadcast-heartbeat")
        try:
            running = set(lanes)
            while running:
                done, _ = await asyncio.wait(
                    {heartbeat, *running}, return_when=asyncio.FIRST_COMPLETED
                )
                if heartbeat in done:
                    logger.error("Broadcast %s: lost the sender lock; stopping", broadcast_id)
                    break
                running -= done
        finally:
            for task in (*lanes, heartbeat):
                task.cancel()
            results = await asyncio.gather(*lanes, heartbeat, return_exceptions=True)
            for task, result in zip(lanes, results, strict=False):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    logger.error(
                        "Broadcast %s: lane %s crashed",
                        broadcast_id,
                        task.get_name(),
                        exc_info=result,
                    )

    await asyncio.to_thread(_finish_if_done, broadcast_id)


async def _heartbeat(redis: Redis, token: str) -> None:
    """Keep the lease alive; returns (ending the run) once it is lost."""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            if not await acquire_lock(redis, token):
                return
        except Exception:
            logger.warning("Broadcast: lock heartbeat failed", exc_info=True)


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
