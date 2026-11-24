"""Firing and preparing scheduled-send runs; delivery is a queued broadcast.

1. **Fire** (``fire_due``): due ``active`` sends are taken with ``FOR UPDATE
   SKIP LOCKED``; the slot's run row (``UNIQUE(scheduled_send_id,
   scheduled_for)``) and the advanced ``next_run_at`` commit together, so a
   slot fires at most once. A recurring slot later than its catch-up window
   (``services/schedule.catchup_window``: half the period, 2 min-2 h) is
   recorded ``missed`` and the next slot is the first after now; a one-shot
   gets 2 h, then ``missed``. No burst after downtime.
2. **Prepare** (``prepare_run``): a ``preparing`` run is claimed with a
   conditional UPDATE on ``claimed_at``. It snapshots the audience
   (``planning.plan_send``) and for a prompt runs the agent once per chat type
   (group, private). Generation sends nothing and writes nothing but this run's
   rows, so a stale claim (``PREPARE_STALE_AFTER``) is safe to redo.
3. **Hand over**: the broadcast, its recipients and their own texts are inserted
   in ONE transaction with the conditional ``UPDATE scheduled_send_runs SET
   status='delivering', broadcast_id=… WHERE status='preparing' AND claimed_at
   = :mine`` (and the send not cancelled). A run makes one broadcast at most;
   the broadcast consumer then sends it like any other (FIFO behind whatever is
   sending), with the shared daily cap, window, lease and ``unknown`` rule.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy.exc import IntegrityError

from ..database import (
    Broadcast,
    BroadcastRecipient,
    BroadcastRecipientText,
    ScheduledSend,
    ScheduledSendRun,
    ScheduledSendRunText,
    SessionLocal,
)
from ..logger import logger
from ..runtime_config import runtime_config
from ..services.broadcast import PLATFORMS, probe_platforms
from ..services.schedule import catchup_window, next_fire
from ..services.scheduled_tasks import utcnow, zone
from .agent_run import generate_text
from .planning import (
    SKIP_GENERATION_FAILED,
    broadcast_audience,
    chat_type_of,
    plan_send,
)
from .schedule import period_of

PREPARING = "preparing"
DELIVERING = "delivering"
COMPLETED = "completed"
FAILED = "failed"
MISSED = "missed"
CANCELLED = "cancelled"

#: A preparing claim older than this belongs to a dead worker and may be retaken.
PREPARE_STALE_AFTER = timedelta(minutes=60)
#: A run still preparing this long after it fired is dropped: never sent hours late.
PREPARE_EXPIRE_AFTER = timedelta(hours=2)
BATCH_SIZE = 50
AGENT_RUN_CONCURRENCY = 3


def footer_for(number: int) -> str:
    """``broadcast_footer`` + this send's own opt-out line, as snapshotted on its broadcast."""
    base = (runtime_config.get("broadcast_footer") or "").strip()
    extra = (runtime_config.get("scheduled_send_footer") or "").strip()
    extra = extra.replace("{number}", str(number))
    return "\n".join(part for part in (base, extra) if part)


# --- 1. Fire -------------------------------------------------------------------


def advance(send: ScheduledSend, slot: datetime, now: datetime) -> None:
    """Arm the next slot (the first after ``now``), or finish a one-shot."""
    nxt = None
    if send.cron_expr:
        nxt = next_fire(tz=zone(send.timezone), after=now, cron_expr=send.cron_expr, previous=slot)
    send.next_run_at = nxt
    if nxt is None:
        send.status = "finished"


def fire_due(now: datetime | None = None) -> int:
    """Create the run of every due slot (or record it missed). Returns runs created."""
    now = now or utcnow()
    db = SessionLocal()
    created = 0
    try:
        due = (
            db.query(ScheduledSend)
            .filter(ScheduledSend.status == "active", ScheduledSend.next_run_at <= now)
            .order_by(ScheduledSend.next_run_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
            .all()
        )
        for send in due:
            slot = send.next_run_at
            window = catchup_window(period_of(send.cron_expr, zone(send.timezone), slot))
            missed = now - slot > window
            savepoint = db.begin_nested()
            try:
                db.add(
                    ScheduledSendRun(
                        scheduled_send_id=send.id,
                        scheduled_for=slot,
                        trigger="schedule",
                        status=MISSED if missed else PREPARING,
                        error=MISSED if missed else None,
                        finished_at=now if missed else None,
                    )
                )
                db.flush()
                savepoint.commit()
                created += 0 if missed else 1
            except IntegrityError:
                savepoint.rollback()  # this slot already has its run
            if missed:
                logger.warning(
                    "Scheduled send %s: slot %s missed (%.0fs late)",
                    send.number,
                    slot,
                    (now - slot).total_seconds(),
                )
            advance(send, slot, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return created


def create_manual_run(db, send: ScheduledSend, now: datetime) -> ScheduledSendRun | None:
    """ "Send now": a run for the current minute. None = this minute already has one."""
    run = ScheduledSendRun(
        scheduled_send_id=send.id,
        scheduled_for=now.replace(second=0, microsecond=0),
        trigger="manual",
        status=PREPARING,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(run)
    return run


# --- 2. Prepare ----------------------------------------------------------------


def expire_stale_runs(now: datetime) -> int:
    db = SessionLocal()
    try:
        expired = (
            db.query(ScheduledSendRun)
            .filter(
                ScheduledSendRun.status == PREPARING,
                ScheduledSendRun.created_at < now - PREPARE_EXPIRE_AFTER,
            )
            .update(
                {"status": FAILED, "error": "expired", "finished_at": now},
                synchronize_session=False,
            )
        )
        db.commit()
        return expired
    finally:
        db.close()


def claimable_run_ids(now: datetime) -> list[uuid.UUID]:
    db = SessionLocal()
    try:
        rows = (
            db.query(ScheduledSendRun.id)
            .filter(
                ScheduledSendRun.status == PREPARING,
                (ScheduledSendRun.claimed_at.is_(None))
                | (ScheduledSendRun.claimed_at < now - PREPARE_STALE_AFTER),
            )
            .order_by(ScheduledSendRun.created_at)
            .limit(BATCH_SIZE)
            .all()
        )
        return [run_id for (run_id,) in rows]
    finally:
        db.close()


def claim_run(run_id: uuid.UUID, now: datetime) -> datetime | None:
    """Take a preparing run (conditional UPDATE); returns the claim token or None."""
    db = SessionLocal()
    try:
        taken = (
            db.query(ScheduledSendRun)
            .filter(
                ScheduledSendRun.id == run_id,
                ScheduledSendRun.status == PREPARING,
                (ScheduledSendRun.claimed_at.is_(None))
                | (ScheduledSendRun.claimed_at < now - PREPARE_STALE_AFTER),
            )
            .update({"claimed_at": now, "started_at": now}, synchronize_session=False)
        )
        db.commit()
        return now if taken else None
    finally:
        db.close()


def release_claim(run_id: uuid.UUID, token: datetime) -> bool:
    """Give a preparing run back (conditional on this worker's claim)."""
    db = SessionLocal()
    try:
        done = (
            db.query(ScheduledSendRun)
            .filter(
                ScheduledSendRun.id == run_id,
                ScheduledSendRun.status == PREPARING,
                ScheduledSendRun.claimed_at == token,
            )
            .update({"claimed_at": None}, synchronize_session=False)
        )
        db.commit()
        return bool(done)
    finally:
        db.close()


def finish_run(run_id: uuid.UUID, token: datetime, status: str, error: str | None) -> bool:
    """End a preparing run this worker holds (conditional on the claim)."""
    db = SessionLocal()
    try:
        done = (
            db.query(ScheduledSendRun)
            .filter(
                ScheduledSendRun.id == run_id,
                ScheduledSendRun.status == PREPARING,
                ScheduledSendRun.claimed_at == token,
            )
            .update(
                {"status": status, "error": error, "finished_at": utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(done)
    finally:
        db.close()


@dataclass(frozen=True)
class SendSnapshot:
    id: uuid.UUID
    number: int
    name: str
    content_type: str
    content: str
    audience: str
    chat_jids: list[str] | None
    platforms: list[str] | None
    status: str

    @classmethod
    def of(cls, send: ScheduledSend) -> SendSnapshot:
        return cls(
            id=send.id,
            number=send.number,
            name=send.name,
            content_type=send.content_type,
            content=send.content,
            audience=send.audience,
            chat_jids=list(send.chat_jids) if send.chat_jids else None,
            platforms=list(send.platforms) if send.platforms else None,
            status=send.status,
        )


@dataclass
class Target:
    """One pending recipient of the plan, with what it still needs."""

    user_id: object
    platform: str
    address: str
    skip_reason: str | None = None
    text: str | None = None


@dataclass
class Prepared:
    """Everything the hand-over transaction writes."""

    platforms: list[str]
    audience: str
    targets: list[Target] = field(default_factory=list)
    texts: list[ScheduledSendRunText] = field(default_factory=list)
    broadcast_text: str = ""


def _load(run_id: uuid.UUID) -> tuple[SendSnapshot | None, ScheduledSendRun | None]:
    db = SessionLocal()
    try:
        run = db.get(ScheduledSendRun, run_id)
        send = db.get(ScheduledSend, run.scheduled_send_id) if run else None
        return (SendSnapshot.of(send) if send else None), run
    finally:
        db.close()


async def resolve_platforms(requested: list[str] | None) -> tuple[list[str], bool]:
    """Omitted = every reachable client; explicit ones are used as chosen.

    An explicit client that is down at run time still gets its recipients: its
    lane waits (``disconnected``) and pauses the broadcast after 30 min, which
    FleetView shows, rather than silently dropping them.
    """
    reachable = await probe_platforms()
    if requested is None:
        return [p for p in PLATFORMS if reachable[p]], reachable["cloud"]
    return [p for p in PLATFORMS if p in requested], reachable["cloud"]


def _plan(snap: SendSnapshot, platforms: list[str], cloud_up: bool, now: datetime):
    db = SessionLocal()
    try:
        planned = plan_send(
            db,
            scheduled_send_id=snap.id,
            audience=snap.audience,
            chat_jids=snap.chat_jids,
            platforms=platforms,
            whitelist=runtime_config.get("whitelist_phones"),
            now=now,
            cloud_deployed=cloud_up,
        )
        targets = [
            Target(r.user_id, r.platform, r.address, r.skip_reason) for r in planned.plan.recipients
        ]
        return targets, broadcast_audience(snap.audience, planned.users)
    finally:
        db.close()


async def write_texts(
    snap: SendSnapshot, targets: list[Target], http_client: httpx.AsyncClient
) -> tuple[list[ScheduledSendRunText], int]:
    """Prompt sends: one agent run per chat type in the audience. Returns (texts, ok slices)."""
    slices: dict[str, list[Target]] = {}
    for t in targets:
        if t.skip_reason is None:
            slices.setdefault(chat_type_of(t.address), []).append(t)
    gate = asyncio.Semaphore(AGENT_RUN_CONCURRENCY)

    async def one(chat_type):
        async with gate:
            return chat_type, await generate_text(
                content=snap.content, chat_type=chat_type, http_client=http_client
            )

    rows: list[ScheduledSendRunText] = []
    ok = 0
    for chat_type, generated in await asyncio.gather(*(one(k) for k in sorted(slices))):
        rows.append(
            ScheduledSendRunText(
                region=None, chat_type=chat_type, text=generated.text, error=generated.error
            )
        )
        for t in slices[chat_type]:
            if generated.text is None:
                t.skip_reason = SKIP_GENERATION_FAILED
            else:
                t.text = generated.text
        ok += generated.text is not None
    return rows, ok


def hand_over(
    run_id: uuid.UUID, token: datetime, snap: SendSnapshot, prepared: Prepared
) -> uuid.UUID | None:
    """Insert the broadcast and mark the run delivering, all in one transaction.

    Returns the broadcast id, or None when the claim was lost or the send was
    cancelled meanwhile (then nothing is written but the run's end state).
    """
    db = SessionLocal()
    try:
        # FOR UPDATE: a concurrent cancel waits for this commit, then finds the run
        # delivering and cancels its broadcast (no SELECT-then-write window).
        send_status = (
            db.query(ScheduledSend.status)
            .filter(ScheduledSend.id == snap.id)
            .with_for_update()
            .scalar()
        )
        if send_status == "cancelled":
            db.rollback()
            finish_run(run_id, token, CANCELLED, "send_cancelled")
            return None
        broadcast = Broadcast(
            text=prepared.broadcast_text,
            footer=footer_for(snap.number),
            audience=prepared.audience,
            platforms=prepared.platforms,
            status="queued",
        )
        db.add(broadcast)
        db.flush()
        for t in prepared.targets:
            rec = BroadcastRecipient(
                broadcast_id=broadcast.id,
                user_id=t.user_id,
                platform=t.platform,
                address=t.address,
                status="skipped" if t.skip_reason else "pending",
                error_code=t.skip_reason,
            )
            db.add(rec)
            if t.text is not None and t.skip_reason is None:
                db.flush()
                db.add(BroadcastRecipientText(recipient_id=rec.id, text=t.text))
        for row in prepared.texts:
            row.run_id = run_id
            db.add(row)
        took = (
            db.query(ScheduledSendRun)
            .filter(
                ScheduledSendRun.id == run_id,
                ScheduledSendRun.status == PREPARING,
                ScheduledSendRun.claimed_at == token,
            )
            .update(
                {"status": DELIVERING, "broadcast_id": broadcast.id},
                synchronize_session=False,
            )
        )
        if not took:
            db.rollback()
            logger.warning("Scheduled send run %s: claim lost before hand-over", run_id)
            return None
        db.commit()
        return broadcast.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save_texts(run_id: uuid.UUID, texts: list[ScheduledSendRunText]) -> None:
    if not texts:
        return
    db = SessionLocal()
    try:
        for row in texts:
            row.run_id = run_id
            db.add(row)
        db.commit()
    finally:
        db.close()


async def prepare_run(run_id: uuid.UUID, token: datetime, http_client: httpx.AsyncClient) -> str:
    """Snapshot, write and hand over one claimed run. Returns its resulting status."""
    snap, run = await asyncio.to_thread(_load, run_id)
    if snap is None or run is None:
        return FAILED
    if snap.status == "cancelled":
        await asyncio.to_thread(finish_run, run_id, token, CANCELLED, "send_cancelled")
        return CANCELLED
    now = utcnow()
    platforms, cloud_up = await resolve_platforms(snap.platforms)
    if not platforms:
        await asyncio.to_thread(finish_run, run_id, token, FAILED, "clients_unreachable")
        return FAILED
    targets, audience = await asyncio.to_thread(_plan, snap, platforms, cloud_up, now)

    texts: list[ScheduledSendRunText] = []
    broadcast_text = snap.content
    if snap.content_type == "prompt":
        texts, ok = await write_texts(snap, targets, http_client)
        if texts and not ok:
            await asyncio.to_thread(_save_texts, run_id, texts)
            await asyncio.to_thread(finish_run, run_id, token, FAILED, SKIP_GENERATION_FAILED)
            return FAILED
        made = [row.text for row in texts if row.text]
        broadcast_text = (
            made[0]
            if len(made) == 1
            else f"(Scheduled message «{snap.name}»: {len(made)} versions, one per chat type)"
        )

    if not any(t.skip_reason is None for t in targets):
        await asyncio.to_thread(_save_texts, run_id, texts)
        await asyncio.to_thread(finish_run, run_id, token, COMPLETED, "no_recipients")
        return COMPLETED

    prepared = Prepared(
        platforms=platforms,
        audience=audience,
        targets=targets,
        texts=texts,
        broadcast_text=broadcast_text,
    )
    broadcast_id = await asyncio.to_thread(hand_over, run_id, token, snap, prepared)
    if broadcast_id is None:
        return CANCELLED if snap.status == "cancelled" else PREPARING
    logger.info(
        "Scheduled send %s: run %s queued as broadcast %s (%d recipients)",
        snap.number,
        run_id,
        broadcast_id,
        sum(1 for t in targets if t.skip_reason is None),
    )
    return DELIVERING


async def safe_prepare(run_id: uuid.UUID, token: datetime, http_client: httpx.AsyncClient) -> None:
    """``prepare_run`` for the worker loop: one run's crash ends that run, nothing else."""
    try:
        await prepare_run(run_id, token, http_client)
    except asyncio.CancelledError:
        # Shutdown: give the claim back so the next worker retakes the run on its
        # next tick, not PREPARE_STALE_AFTER later. Nothing was sent: hand-over is
        # one transaction, and a committed one left the run delivering.
        try:
            await asyncio.shield(asyncio.to_thread(release_claim, run_id, token))
        except BaseException:
            logger.warning("Scheduled send run %s: claim not released", run_id)
        raise
    except Exception:
        logger.error("Scheduled send run %s: preparation failed", run_id, exc_info=True)
        try:
            await asyncio.to_thread(finish_run, run_id, token, FAILED, "prepare_error")
        except Exception:
            logger.error("Scheduled send run %s: could not record the failure", run_id)
