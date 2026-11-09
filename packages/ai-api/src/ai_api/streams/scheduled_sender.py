"""Scheduled sender: delivers ``ready`` scheduled-task runs, one lane per pace.

Runs as the fifth task of the stream worker (``scripts/run_stream_worker.py``).
The chat consumer never sends a scheduled message: it would hold its gather
(and every other chat's reply) while a paced send waits its turn. Instead the
scheduler (reminders) and the agent phase (``scheduled_runner.py``) leave the
text on the run as ``ready``, and this module sends it:

- **Baileys lane**: one send at a time, ``BAILEYS_GAP_SECONDS`` apart, so many
  tasks due at 09:00 don't go out in one burst from the one WhatsApp account.
  Pacing is per worker process and independent of the broadcast lane's (the
  two don't share one spacing).
- **Fast lane** (Telegram, Cloud API): up to ``FAST_CONCURRENCY`` at once,
  claimed continuously as slots free up, so one slow send doesn't hold the rest.

A claim takes at most one run per chat, and none for a chat with a run still
``sending``: one chat's messages go out one at a time and in order, and the
daily cap (``scheduled_runner._counted_today``) sees every message before it.

Each lane claims runs ``ready → sending`` (``FOR UPDATE OF task_runs SKIP
LOCKED``, committed, ``started_at`` = now, which starts the orphan clock),
re-checks the run (``skip_reason``: still active, chat exists, whitelist, Cloud
window, daily cap), delivers with the broadcast sender's ``_deliver`` (typing
first on Baileys) and records the outcome conditionally on ``sending``
(``scheduled_runner.record``). A send that may have gone out is never retried:
``sending`` left by a crash is swept to ``unknown``.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import exists, func
from sqlalchemy.orm import aliased

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import ScheduledTask, SessionLocal, TaskRun, User
from ..logger import logger
from ..services.scheduled_tasks import utcnow
from ..whatsapp import create_whatsapp_client
from .broadcast_consumer import (
    FINAL,
    RELEASE,
    RETRY,
    TELEGRAM_SEND_TIMEOUT_SECONDS,
    _deliver,
    classify_outcome,
)
from .scheduled_runner import (
    FAILED,
    READY,
    SENDING,
    SENT,
    SKIPPED,
    UNKNOWN,
    record,
    settle,
    skip_reason,
)

#: Gap between two scheduled Baileys sends from this process.
BAILEYS_GAP_SECONDS = (3.0, 8.0)
FAST_CONCURRENCY = 10
#: A claim reads this many times its limit, so a chat with many ready runs
#: doesn't crowd the other chats out of one claim.
CLAIM_SCAN_FACTOR = 5
#: How long an idle lane waits before looking for ready runs again.
IDLE_SECONDS = 2.0
#: Attempts at recording a message that WAS sent.
SENT_RECORD_ATTEMPTS = 3

BAILEYS = ("baileys",)
FAST = ("telegram", "cloud")


@dataclass(frozen=True)
class _Send:
    run_id: uuid.UUID
    platform: str
    client_id: str | None
    address: str
    text: str


# --- DB steps (sync; run through asyncio.to_thread) -------------------------


def claim_ready(platforms: tuple[str, ...], limit: int) -> list[uuid.UUID]:
    """Oldest ``ready`` runs of these platforms → ``sending`` (committed).

    At most one per chat, and none for a chat that already has a run
    ``sending``, so one chat never gets two scheduled messages at once.
    """
    now = utcnow()
    busy = aliased(TaskRun)
    db = SessionLocal()
    try:
        candidates = (
            db.query(TaskRun)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .filter(
                TaskRun.status == READY,
                func.coalesce(ScheduledTask.client_id, "baileys").in_(platforms),
                ~exists().where(busy.user_id == TaskRun.user_id, busy.status == SENDING),
            )
            .order_by(TaskRun.created_at)
            .limit(limit * CLAIM_SCAN_FACTOR)
            .with_for_update(skip_locked=True, of=TaskRun)
            .all()
        )
        runs, chats = [], set()
        for run in candidates:
            if run.user_id in chats:
                continue  # this chat's next run waits for a later claim
            chats.add(run.user_id)
            runs.append(run)
            if len(runs) == limit:
                break
        for run in runs:
            run.status = SENDING
            run.started_at = now
        db.commit()
        return [run.id for run in runs]
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _prepare(run_id: uuid.UUID) -> _Send | None:
    """Re-check a claimed run right before its send; records a skip and returns None."""
    now = utcnow()
    db = SessionLocal()
    try:
        run = db.get(TaskRun, run_id)
        if run is None or run.status != SENDING:
            return None
        task = db.get(ScheduledTask, run.task_id)
        if task is None:
            return None
        user = db.get(User, task.user_id)
        reason = skip_reason(db, task, user, now, run)
        if reason is None and not run.message:
            reason = "no_text"  # can't happen: ready runs carry their text
        if reason is not None:
            status = FAILED if reason == "no_text" else SKIPPED
            settle(db, run, task, status, reason, now)
            db.commit()
            logger.info("Scheduled run %s %s (%s)", run_id, status, reason)
            return None
        return _Send(
            run_id=run.id,
            platform=task.client_id or "baileys",
            client_id=task.client_id,
            address=task.address,
            text=run.message,
        )
    finally:
        db.close()


# --- Delivery ---------------------------------------------------------------


async def _deliver_to(send: _Send) -> None:
    timeout = (
        TELEGRAM_SEND_TIMEOUT_SECONDS
        if send.platform == "telegram"
        else settings.whatsapp_client_timeout
    )
    async with httpx.AsyncClient(timeout=timeout) as http:
        client = create_whatsapp_client(
            http_client=http,
            base_url=get_whatsapp_client_url(send.client_id),
            api_key=get_whatsapp_api_key(send.client_id),
        )
        await _deliver(client, send.platform, send.address, send.text)


async def _record_sent(send: _Send) -> None:
    for attempt in range(1, SENT_RECORD_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(record, send.run_id, SENDING, SENT, history_text=send.text)
            return
        except Exception:
            if attempt == SENT_RECORD_ATTEMPTS:
                # Left 'sending': the sweep records it as 'unknown', never re-sent.
                logger.error(
                    "Scheduled run %s was sent but could not be recorded",
                    send.run_id,
                    exc_info=True,
                )
                return
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))


async def send_run(run_id: uuid.UUID) -> bool:
    """Send one claimed run and record it. Returns whether a send was attempted. Never raises."""
    try:
        send = await asyncio.to_thread(_prepare, run_id)
        if send is None:
            return False
        try:
            await _deliver_to(send)
        except Exception as e:
            kind, code = classify_outcome(e)
            logger.warning("Scheduled run %s: send failed (%s, %s)", run_id, kind, code)
            status = FAILED if kind in (FINAL, RELEASE, RETRY) else UNKNOWN
            await asyncio.to_thread(
                record,
                run_id,
                SENDING,
                status,
                code,
                provably_not_sent=kind in (RELEASE, RETRY),
            )
            return True
        await _record_sent(send)
        logger.info("Scheduled run %s sent to %s…", run_id, send.address[:8])
        return True
    except Exception:
        logger.error("Scheduled run %s: sender crashed", run_id, exc_info=True)
        return True


# --- Lanes ------------------------------------------------------------------


async def baileys_lane_step(last_send: float | None) -> tuple[bool, float | None]:
    """Claim and send one Baileys run; returns (found one, time of the last send)."""
    ids = await asyncio.to_thread(claim_ready, BAILEYS, 1)
    if not ids:
        return False, last_send
    loop = asyncio.get_running_loop()
    if last_send is not None:
        wait = last_send + random.uniform(*BAILEYS_GAP_SECONDS) - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
    if await send_run(ids[0]):
        last_send = loop.time()
    return True, last_send


async def fast_lane_step(inflight: set[asyncio.Task]) -> bool:
    """Claim Telegram / Cloud runs for the free send slots and start them; True if any."""
    free = FAST_CONCURRENCY - len(inflight)
    if free <= 0:
        return False
    ids = await asyncio.to_thread(claim_ready, FAST, free)
    for run_id in ids:
        task = asyncio.create_task(send_run(run_id))
        inflight.add(task)
        task.add_done_callback(inflight.discard)
    return bool(ids)


async def _baileys_lane() -> None:
    last_send: float | None = None
    while True:
        try:
            found, last_send = await baileys_lane_step(last_send)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Scheduled sender (baileys) failed", exc_info=True)
            found = False
        if not found:
            await asyncio.sleep(IDLE_SECONDS)


async def _fast_lane() -> None:
    inflight: set[asyncio.Task] = set()
    try:
        while True:
            try:
                found = await fast_lane_step(inflight)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Scheduled sender (fast) failed", exc_info=True)
                found = False
            if found:
                continue
            if inflight:
                # Wake when a send finishes (a slot frees, a chat stops being busy).
                await asyncio.wait(
                    set(inflight), timeout=IDLE_SECONDS, return_when=asyncio.FIRST_COMPLETED
                )
            else:
                await asyncio.sleep(IDLE_SECONDS)
    finally:
        for task in inflight:
            task.cancel()


async def run_scheduled_sender() -> None:
    """Worker loop: send ready scheduled runs (a Baileys lane and a fast lane)."""
    logger.info("📤 Starting scheduled sender")
    await asyncio.gather(_baileys_lane(), _fast_lane())
