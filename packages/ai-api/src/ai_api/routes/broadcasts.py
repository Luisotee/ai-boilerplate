"""Operator broadcasts: send one message to every chat the bot has talked to.

FleetView's future "broadcast" page is the intended caller. Routes live under
``/admin/broadcasts`` and are protected by the standard ``X-API-Key``
middleware like the rest of ``/admin``.

Creating a broadcast only snapshots its recipients (``services/broadcast.py``
decides who and on which platform); the stream worker's broadcast consumer
(``streams/broadcast_consumer.py``) does the sending, paced per platform. The
routes here change a broadcast's status; the worker notices before its next
send.

Only one broadcast may be queued or running at a time: they share the same
WhatsApp account and the same daily cap. That rule is enforced under a
transaction-scoped Postgres advisory lock (``_lock_active``), so two
concurrent creates (a double-click, two operators) or a create racing a resume
can never both commit an active broadcast. Every status change is a
conditional ``UPDATE … WHERE status IN (…)``, so it can't overwrite a change
the worker (or another request) made in between.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..broadcast_pacing import estimate_baileys_seconds, parse_send_window
from ..config import settings
from ..database import Broadcast, BroadcastRecipient, User, get_db
from ..deps import limiter
from ..logger import logger
from ..runtime_config import runtime_config
from ..schemas import (
    BroadcastCounts,
    BroadcastCreateRequest,
    BroadcastPlatformAvailability,
    BroadcastPlatformCounts,
    BroadcastPlatformPreview,
    BroadcastPlatformsResponse,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastRecipientItem,
    BroadcastRecipientsResponse,
    BroadcastRecipientStatus,
    BroadcastResponse,
    BroadcastsResponse,
)
from ..services.broadcast import (
    PLATFORMS,
    SKIP_CLOUD_WINDOW,
    BroadcastPlan,
    audience_users,
    plan_broadcast,
    probe_platforms,
    whitelist_filter,
)

router = APIRouter(prefix="/admin/broadcasts", tags=["Admin", "Broadcasts"])

_ACTIVE = ("queued", "running")
#: Target status -> the verb used in 409 messages ("Cannot pause a … that is …").
_ACTION = {"paused": "pause", "running": "resume", "cancelled": "cancel"}
_RECIPIENT_STATUSES = ("pending", "sent", "failed", "skipped")

#: pg_advisory_xact_lock key serialising "is a broadcast active?" + the write
#: that makes one active. Any constant works; it only has to be unique to this.
BROADCAST_LOCK_KEY = 0x62726F6164  # "broad"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _lock_active(db: Session) -> None:
    """Serialise the active-broadcast check with its write (released at commit/rollback)."""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BROADCAST_LOCK_KEY})


async def _resolve_platforms(requested: list[str] | None) -> list[str]:
    """Explicit platforms must be reachable; omitted = every reachable one."""
    reachable = await probe_platforms()
    if requested is None:
        platforms = [p for p in PLATFORMS if reachable[p]]
        if not platforms:
            raise HTTPException(status_code=503, detail="No chat client is reachable")
        return platforms
    down = [p for p in requested if not reachable[p]]
    if down:
        raise HTTPException(
            status_code=400,
            detail=f"Chat client not reachable (not deployed or down): {', '.join(down)}",
        )
    # De-duplicate, keeping the canonical order.
    return [p for p in PLATFORMS if p in requested]


def _plan(db: Session, audience: str, platforms: list[str]) -> BroadcastPlan:
    """Resolve the audience (sync DB work: call it through the threadpool)."""
    allowed = whitelist_filter(runtime_config.get("whitelist_phones"), settings.group_gating)
    return plan_broadcast(audience_users(db, audience), platforms, allowed, _utcnow())


def _estimate(plan: BroadcastPlan, text_length: int = 280) -> int:
    try:
        window = parse_send_window(runtime_config.get("broadcast_send_window"))
    except ValueError:
        window = None
    return estimate_baileys_seconds(
        len([r for r in plan.pending() if r.platform == "baileys"]),
        min_delay=runtime_config.get("broadcast_min_delay_seconds"),
        max_delay=runtime_config.get("broadcast_max_delay_seconds"),
        batch_size=runtime_config.get("broadcast_batch_size"),
        batch_pause_seconds=runtime_config.get("broadcast_batch_pause_seconds"),
        daily_limit=runtime_config.get("broadcast_daily_limit"),
        window=window,
        text_length=text_length,
    )


def _count_rows(db: Session, broadcast_ids: list) -> dict:
    """``broadcast_id -> [(platform, status, count)]`` for many broadcasts in ONE query."""
    if not broadcast_ids:
        return {}
    rows = (
        db.query(
            BroadcastRecipient.broadcast_id,
            BroadcastRecipient.platform,
            BroadcastRecipient.status,
            func.count(BroadcastRecipient.id),
        )
        .filter(BroadcastRecipient.broadcast_id.in_(broadcast_ids))
        .group_by(
            BroadcastRecipient.broadcast_id,
            BroadcastRecipient.platform,
            BroadcastRecipient.status,
        )
        .all()
    )
    grouped: dict = defaultdict(list)
    for broadcast_id, platform, status, count in rows:
        grouped[broadcast_id].append((platform, status, count))
    return grouped


def _payload(broadcast: Broadcast, rows) -> BroadcastResponse:
    totals = BroadcastCounts()
    per_platform = {p: BroadcastPlatformCounts(platform=p) for p in broadcast.platforms}
    for platform, status, count in rows:
        bucket = per_platform.setdefault(platform, BroadcastPlatformCounts(platform=platform))
        for counts in (totals, bucket):
            counts.total += count
            if status in _RECIPIENT_STATUSES:
                setattr(counts, status, getattr(counts, status) + count)
    return BroadcastResponse(
        id=str(broadcast.id),
        text=broadcast.text,
        footer=broadcast.footer,
        audience=broadcast.audience,
        platforms=broadcast.platforms,
        status=broadcast.status,
        pause_reason=broadcast.pause_reason,
        created_at=broadcast.created_at,
        started_at=broadcast.started_at,
        finished_at=broadcast.finished_at,
        counts=totals,
        per_platform=list(per_platform.values()),
    )


def _broadcast_payload(db: Session, broadcast: Broadcast) -> BroadcastResponse:
    return _payload(broadcast, _count_rows(db, [broadcast.id]).get(broadcast.id, []))


def _get_or_404(db: Session, broadcast_id: uuid.UUID) -> Broadcast:
    broadcast = db.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    return broadcast


def _find_by_key(db: Session, key: str | None) -> Broadcast | None:
    if not key:
        return None
    return db.query(Broadcast).filter(Broadcast.idempotency_key == key).first()


def _ensure_none_active(db: Session, exclude: uuid.UUID | None = None) -> None:
    """409 if another broadcast is active. Call after ``_lock_active``."""
    query = db.query(Broadcast).filter(Broadcast.status.in_(_ACTIVE))
    if exclude is not None:
        query = query.filter(Broadcast.id != exclude)
    if query.first() is not None:
        db.rollback()  # release the advisory lock before answering
        raise HTTPException(
            status_code=409,
            detail="Another broadcast is already queued or running; pause or cancel it first",
        )


# --- Routes -----------------------------------------------------------------


@router.get("/platforms", response_model=BroadcastPlatformsResponse)
async def broadcast_platforms():
    """Which chat clients are reachable, i.e. can be selected for a broadcast."""
    reachable = await probe_platforms()
    return BroadcastPlatformsResponse(
        platforms=[
            BroadcastPlatformAvailability(platform=p, reachable=reachable[p]) for p in PLATFORMS
        ]
    )


@router.post("/preview", response_model=BroadcastPreviewResponse)
async def preview_broadcast(request: BroadcastPreviewRequest, db: Session = Depends(get_db)):
    """Count who a broadcast would reach, per platform. Creates nothing."""
    platforms = await _resolve_platforms(request.platforms)
    plan = await run_in_threadpool(_plan, db, request.audience, platforms)
    per_platform = [
        BroadcastPlatformPreview(
            platform=p,
            recipients=plan.count(p),
            skipped_cloud_window=plan.count(p, skipped=SKIP_CLOUD_WINDOW),
        )
        for p in platforms
    ]
    return BroadcastPreviewResponse(
        audience=request.audience,
        platforms=platforms,
        total_recipients=len(plan.pending()),
        per_platform=per_platform,
        opted_out=plan.opted_out,
        not_whitelisted=plan.not_whitelisted,
        no_selected_platform=plan.no_selected_platform,
        skipped_cloud_window=sum(p.skipped_cloud_window for p in per_platform),
        estimated_baileys_seconds=_estimate(plan),
    )


def _create_locked(
    db: Session, request: BroadcastCreateRequest, platforms: list[str]
) -> tuple[BroadcastResponse, bool]:
    """Lock, re-check, snapshot and insert in one transaction. Returns (payload, created)."""
    try:
        _lock_active(db)
        existing = _find_by_key(db, request.idempotency_key)
        if existing is not None:
            db.rollback()
            return _broadcast_payload(db, existing), False
        _ensure_none_active(db)
        plan = _plan(db, request.audience, platforms)

        broadcast = Broadcast(
            text=request.text.strip(),
            footer=(runtime_config.get("broadcast_footer") or "").strip(),
            audience=request.audience,
            platforms=platforms,
            status="queued",
            idempotency_key=request.idempotency_key,
        )
        db.add(broadcast)
        db.flush()
        db.add_all(
            BroadcastRecipient(
                broadcast_id=broadcast.id,
                user_id=r.user_id,
                platform=r.platform,
                address=r.address,
                status="skipped" if r.skip_reason else "pending",
                error_code=r.skip_reason,
            )
            for r in plan.recipients
        )
        db.commit()
    except HTTPException:
        raise
    except IntegrityError as e:
        # Backstop for the idempotency-key unique constraint.
        db.rollback()
        existing = _find_by_key(db, request.idempotency_key)
        if existing is not None:
            return _broadcast_payload(db, existing), False
        logger.error("Error creating broadcast: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    except Exception as e:
        db.rollback()
        logger.error("Error creating broadcast: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    db.refresh(broadcast)
    logger.info(
        "Broadcast %s queued: audience=%s platforms=%s recipients=%d",
        broadcast.id,
        broadcast.audience,
        ",".join(platforms),
        len(plan.pending()),
    )
    return _broadcast_payload(db, broadcast), True


@router.post("", response_model=BroadcastResponse, status_code=201)
async def create_broadcast(
    request: BroadcastCreateRequest, response: Response, db: Session = Depends(get_db)
):
    """Snapshot the recipients and queue the broadcast for the worker (201).

    Re-sending the same ``idempotency_key`` returns the broadcast it created
    with a 200 (so a timed-out request is safe to retry), whatever its state.
    """
    # Fast path: a replay needs neither reachable clients nor the lock.
    existing = _find_by_key(db, request.idempotency_key)
    if existing is not None:
        response.status_code = 200
        return _broadcast_payload(db, existing)

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Broadcast text cannot be blank")
    # Probe the clients BEFORE taking the lock: it's up to 3s of network I/O.
    platforms = await _resolve_platforms(request.platforms)
    payload, created = await run_in_threadpool(_create_locked, db, request, platforms)
    if not created:
        response.status_code = 200
    return payload


@router.get("", response_model=BroadcastsResponse)
async def list_broadcasts(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Broadcasts, newest first, with delivery counts."""
    total = db.query(Broadcast).count()
    page = (
        db.query(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit).offset(offset).all()
    )
    counts = _count_rows(db, [b.id for b in page])
    return BroadcastsResponse(
        broadcasts=[_payload(b, counts.get(b.id, [])) for b in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{broadcast_id}", response_model=BroadcastResponse)
@limiter.exempt
async def get_broadcast(request: Request, broadcast_id: uuid.UUID, db: Session = Depends(get_db)):
    """One broadcast with its delivery counts. Rate-limit exempt: FleetView polls it."""
    return _broadcast_payload(db, _get_or_404(db, broadcast_id))


@router.get("/{broadcast_id}/recipients", response_model=BroadcastRecipientsResponse)
async def list_recipients(
    broadcast_id: uuid.UUID,
    status: BroadcastRecipientStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Per-chat delivery rows, optionally filtered by status.

    Ordered by the immutable row id, so pages don't shift while it is sending.
    """
    _get_or_404(db, broadcast_id)
    query = (
        db.query(BroadcastRecipient, User.name)
        .outerjoin(User, User.id == BroadcastRecipient.user_id)
        .filter(BroadcastRecipient.broadcast_id == broadcast_id)
    )
    if status:
        query = query.filter(BroadcastRecipient.status == status)
    total = query.count()
    rows = query.order_by(BroadcastRecipient.id).limit(limit).offset(offset).all()
    return BroadcastRecipientsResponse(
        recipients=[
            BroadcastRecipientItem(
                address=rec.address,
                name=name,
                platform=rec.platform,
                status=rec.status,
                error_code=rec.error_code,
                attempts=rec.attempts,
                sent_at=rec.sent_at,
            )
            for rec, name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def _transition(
    db: Session,
    broadcast_id: uuid.UUID,
    allowed_from: tuple[str, ...],
    to: str,
    *,
    pause_reason: str | None = None,
) -> BroadcastResponse:
    """Conditionally move a broadcast to ``to``; 409 if it isn't in ``allowed_from``.

    The write is ``UPDATE … WHERE status IN allowed_from``: if the worker (or
    another request) changed the status since we read it, nothing is written
    and the caller gets a 409 with the current status instead of overwriting it.
    """
    broadcast = _get_or_404(db, broadcast_id)
    if broadcast.status not in allowed_from:
        raise HTTPException(
            status_code=409, detail=f"Cannot {_ACTION[to]} a broadcast that is {broadcast.status}"
        )
    values: dict = {"status": to, "pause_reason": pause_reason}
    if to == "cancelled":
        values["finished_at"] = _utcnow()
    try:
        if to == "running":
            _lock_active(db)
            _ensure_none_active(db, exclude=broadcast.id)
        updated = (
            db.query(Broadcast)
            .filter(Broadcast.id == broadcast_id, Broadcast.status.in_(allowed_from))
            .update(values, synchronize_session=False)
        )
        if not updated:
            db.rollback()
            db.refresh(broadcast)
            raise HTTPException(
                status_code=409,
                detail=f"Cannot {_ACTION[to]} a broadcast that is {broadcast.status}",
            )
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("Error updating broadcast %s: %s", broadcast_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    db.refresh(broadcast)
    logger.info("Broadcast %s -> %s", broadcast_id, to)
    return _broadcast_payload(db, broadcast)


@router.post("/{broadcast_id}/pause", response_model=BroadcastResponse)
async def pause_broadcast(broadcast_id: uuid.UUID, db: Session = Depends(get_db)):
    """Stop sending before the next message; resume picks up where it left off."""
    return _transition(db, broadcast_id, _ACTIVE, "paused", pause_reason="manual")


@router.post("/{broadcast_id}/resume", response_model=BroadcastResponse)
async def resume_broadcast(broadcast_id: uuid.UUID, db: Session = Depends(get_db)):
    """Resume a paused broadcast (also after an automatic pause)."""
    return _transition(db, broadcast_id, ("paused",), "running")


@router.post("/{broadcast_id}/cancel", response_model=BroadcastResponse)
async def cancel_broadcast(broadcast_id: uuid.UUID, db: Session = Depends(get_db)):
    """Stop for good. Unsent recipients stay 'pending' in the record."""
    return _transition(db, broadcast_id, (*_ACTIVE, "paused"), "cancelled")
