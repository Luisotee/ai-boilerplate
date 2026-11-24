"""Scheduled sends ("Envios programados"): FleetView's tab for timed operator sends.

A send is a fixed text or a prompt for the agent, on a schedule (once, daily,
weekly, monthly in the bot's time zone), to an audience (private chats, groups,
all, or a list of chats). The template has no region concept: ``regions`` is
always null (a non-empty one is a 422) and the list says ``region_filter:
false`` (an additive hint; castanha-bot answers true). The stream worker fires
it (``streams/scheduled_send_scheduler.py``) and every run is delivered as an
ordinary broadcast, so pause/resume/cancel and the recipients of a run's
delivery are the ``/admin/broadcasts/{broadcast_id}/…`` routes.

The response shapes and the Literal enums in ``schemas.py`` are FleetView's
contract (``scheduled_sends/CLAUDE.md``). FleetView requires a ``weekly_recap``
summary on the list; the template has no weekly recap, so it is a fixed
disabled one. Every status change is a conditional
``UPDATE … WHERE status IN (…)``; a lost race answers 409.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import (
    Broadcast,
    ScheduledSend,
    ScheduledSendRun,
    ScheduledSendRunText,
    get_db,
)
from ..deps import limiter
from ..logger import logger
from ..runtime_config import runtime_config
from ..scheduled_sends.agent_run import MAX_PROMPT_CHARS, build_prompt, generate_text
from ..scheduled_sends.planning import chat_type_of, plan_send
from ..scheduled_sends.runner import create_manual_run
from ..scheduled_sends.schedule import (
    ScheduleInputError,
    compile_schedule,
    first_run,
    next_runs,
    period_of,
)
from ..schemas import (
    BroadcastCounts,
    BroadcastPlatformPreview,
    ScheduledSendCreateRequest,
    ScheduledSendFields,
    ScheduledSendPreviewRequest,
    ScheduledSendPreviewResponse,
    ScheduledSendResponse,
    ScheduledSendRunDetail,
    ScheduledSendRunItem,
    ScheduledSendRunsResponse,
    ScheduledSendSample,
    ScheduledSendSchedule,
    ScheduledSendsResponse,
    WeeklyRecapSummary,
)
from ..schemas import ScheduledSendRunText as RunTextItem
from ..services.broadcast import SKIP_CLOUD_WINDOW, SKIP_UNKNOWN_CLIENT
from ..services.schedule import format_local
from ..services.scheduled_tasks import bot_timezone, utcnow, zone
from .broadcasts import _count_rows, _resolve_platforms, estimate_seconds

router = APIRouter(prefix="/admin/scheduled-sends", tags=["Admin", "Scheduled sends"])

#: pg_advisory_xact_lock key serialising the ``number`` assignment.
SCHEDULED_SEND_LOCK_KEY = 0x736368736E64  # "schsnd"
MAX_TEXT_CHARS = 3500
_ACTION = {"paused": "pause", "active": "resume", "cancelled": "cancel"}
_RUN_OPEN_BROADCAST = ("queued", "running", "paused")


# --- Payloads -------------------------------------------------------------------


def _run_status(run: ScheduledSendRun, broadcast_status: str | None) -> str:
    """A delivering run's status follows its broadcast (gone = finished)."""
    if run.status != "delivering":
        return run.status
    if broadcast_status is None or broadcast_status == "completed":
        return "completed"
    if broadcast_status == "cancelled":
        return "cancelled"
    return "delivering"


def _run_items(db: Session, runs: list[ScheduledSendRun]) -> list[ScheduledSendRunItem]:
    broadcast_ids = [r.broadcast_id for r in runs if r.broadcast_id]
    statuses = (
        dict(db.query(Broadcast.id, Broadcast.status).filter(Broadcast.id.in_(broadcast_ids)).all())
        if broadcast_ids
        else {}
    )
    counts = _count_rows(db, broadcast_ids)
    items = []
    for run in runs:
        totals = BroadcastCounts()
        for _platform, status, count in counts.get(run.broadcast_id, []):
            totals.total += count
            if status in ("pending", "sent", "failed", "skipped"):
                setattr(totals, status, getattr(totals, status) + count)
        items.append(
            ScheduledSendRunItem(
                id=str(run.id),
                scheduled_send_id=str(run.scheduled_send_id),
                scheduled_for=run.scheduled_for,
                trigger=run.trigger,
                status=_run_status(run, statuses.get(run.broadcast_id)),
                error=run.error,
                broadcast_id=str(run.broadcast_id) if run.broadcast_id else None,
                counts=totals,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
        )
    return items


def _description(send: ScheduledSend) -> str:
    """Schedule text, rebuilt from the stored fields."""
    tz = zone(send.timezone)
    if send.run_at is not None:
        return f"once, {format_local(send.run_at, tz)}"
    try:
        return compile_schedule(dict(send.schedule), tz, utcnow()).description
    except ScheduleInputError:
        return ""


def _payload(db: Session, send: ScheduledSend) -> ScheduledSendResponse:
    last = (
        db.query(ScheduledSendRun)
        .filter(ScheduledSendRun.scheduled_send_id == send.id)
        .order_by(ScheduledSendRun.created_at.desc())
        .first()
    )
    return ScheduledSendResponse(
        id=str(send.id),
        number=send.number,
        name=send.name,
        content_type=send.content_type,
        content=send.content,
        audience=send.audience,
        chat_jids=send.chat_jids,
        regions=send.regions,
        platforms=send.platforms,
        schedule=ScheduledSendSchedule(**send.schedule),
        timezone=send.timezone,
        schedule_description=_description(send),
        status=send.status,
        next_run_at=send.next_run_at,
        last_run=_run_items(db, [last])[0] if last else None,
        created_by=send.created_by,
        created_at=send.created_at,
        updated_at=send.updated_at,
    )


def _weekly_recap() -> WeeklyRecapSummary:
    """FleetView's recap card needs this object; the template has no weekly recap."""
    return WeeklyRecapSummary(
        enabled=False,
        weekday=0,
        hour=0,
        minute=0,
        timezone=bot_timezone().key,
        default_prompt="",
        groups=0,
        subscribers=0,
    )


def _get_or_404(db: Session, send_id: uuid.UUID) -> ScheduledSend:
    send = db.get(ScheduledSend, send_id)
    if send is None:
        raise HTTPException(status_code=404, detail="Scheduled send not found")
    return send


# --- Validation -------------------------------------------------------------------


def _check_content(content_type: str, content: str) -> str:
    content = content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content cannot be blank")
    if content_type == "prompt":
        if len(content) > MAX_PROMPT_CHARS:
            raise HTTPException(
                status_code=422, detail=f"A prompt is at most {MAX_PROMPT_CHARS} characters"
            )
    elif len(content) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=422, detail=f"A text is at most {MAX_TEXT_CHARS} characters"
        )
    return content


def _check_audience(audience: str, chat_jids: list[str] | None) -> list[str] | None:
    if audience == "chats":
        jids = sorted({j.strip() for j in chat_jids or [] if j and j.strip()})
        if not jids:
            raise HTTPException(status_code=422, detail="audience 'chats' needs chat_jids")
        return jids
    if chat_jids:
        raise HTTPException(status_code=422, detail="chat_jids is only for audience 'chats'")
    return None


def _no_regions(regions: list[str] | None) -> None:
    """This bot has no region concept: a region filter can't be honoured."""
    if regions:
        raise HTTPException(status_code=422, detail="This bot has no regions to filter by")


def _compile(schedule: ScheduledSendSchedule, tz, now: datetime):
    try:
        return compile_schedule(schedule.model_dump(mode="json"), tz, now)
    except ScheduleInputError as e:
        raise HTTPException(status_code=422, detail=f"schedule: {e}") from e


def _stored_schedule(schedule: ScheduledSendSchedule) -> dict:
    return schedule.model_dump(mode="json", exclude_none=True)


# --- Create / list / read ----------------------------------------------------------


def _replay(existing: ScheduledSend, request: ScheduledSendCreateRequest) -> None:
    if (
        existing.name != request.name.strip()
        or existing.content_type != request.content_type
        or existing.content != request.content.strip()
        or existing.audience != request.audience
    ):
        raise HTTPException(
            status_code=422, detail="idempotency_key was already used for a different send"
        )


def _insert(db: Session, values: dict) -> tuple[ScheduledSend, bool]:
    """Insert with the next ``number`` (serialised on Postgres, retried on a clash).

    Returns ``(send, created)``; a concurrent create with the same idempotency
    key returns that one with ``created=False``.
    """
    for attempt in range(3):
        try:
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEDULED_SEND_LOCK_KEY}
                )
            number = (db.query(func.max(ScheduledSend.number)).scalar() or 0) + 1
            send = ScheduledSend(number=number, **values)
            db.add(send)
            db.commit()
            db.refresh(send)
            return send, True
        except IntegrityError:
            db.rollback()
            if values.get("idempotency_key"):
                existing = (
                    db.query(ScheduledSend)
                    .filter(ScheduledSend.idempotency_key == values["idempotency_key"])
                    .first()
                )
                if existing is not None:
                    return existing, False
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


@router.post("", response_model=ScheduledSendResponse, status_code=201)
async def create_scheduled_send(
    request: ScheduledSendCreateRequest, response: Response, db: Session = Depends(get_db)
):
    """Create a scheduled send (201). A repeated ``idempotency_key`` returns the original (200)."""
    if request.idempotency_key:
        existing = (
            db.query(ScheduledSend)
            .filter(ScheduledSend.idempotency_key == request.idempotency_key)
            .first()
        )
        if existing is not None:
            _replay(existing, request)
            response.status_code = 200
            return _payload(db, existing)
    content = _check_content(request.content_type, request.content)
    jids = _check_audience(request.audience, request.chat_jids)
    _no_regions(request.regions)
    tz = bot_timezone()
    now = utcnow()
    compiled = _compile(request.schedule, tz, now)
    status = request.status or "active"
    values = dict(
        name=request.name.strip(),
        content_type=request.content_type,
        content=content,
        audience=request.audience,
        chat_jids=jids,
        regions=None,
        platforms=list(request.platforms) if request.platforms else None,
        schedule=_stored_schedule(request.schedule),
        run_at=compiled.run_at,
        cron_expr=compiled.cron_expr,
        timezone=tz.key,
        status=status,
        next_run_at=first_run(compiled, tz, now) if status == "active" else None,
        created_by=request.created_by,
        idempotency_key=request.idempotency_key,
    )
    try:
        send, created = await run_in_threadpool(_insert, db, values)
    except Exception as e:
        logger.error("Error creating scheduled send: %s", type(e).__name__, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e
    if not created:
        _replay(send, request)
        response.status_code = 200
    else:
        logger.info("Scheduled send %s created: %s", send.number, compiled.description)
    return _payload(db, send)


@router.get("", response_model=ScheduledSendsResponse)
async def list_scheduled_sends(
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Scheduled sends, newest first (plus the disabled weekly-recap card FleetView expects)."""
    query = db.query(ScheduledSend)
    if status:
        query = query.filter(ScheduledSend.status == status)
    total = query.count()
    page = query.order_by(ScheduledSend.created_at.desc()).limit(limit).offset(offset).all()
    return ScheduledSendsResponse(
        scheduled_sends=[_payload(db, s) for s in page],
        total=total,
        limit=limit,
        offset=offset,
        weekly_recap=_weekly_recap(),
        regions=[],
        region_filter=False,
        timezone=bot_timezone().key,
    )


@router.get("/runs/{run_id}", response_model=ScheduledSendRunDetail)
@limiter.exempt
async def get_run(request: Request, run_id: uuid.UUID, db: Session = Depends(get_db)):
    """One run with the texts its agent runs wrote. Rate-limit exempt: FleetView polls it."""
    run = db.get(ScheduledSendRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    item = _run_items(db, [run])[0]
    texts = (
        db.query(ScheduledSendRunText)
        .filter(ScheduledSendRunText.run_id == run_id)
        .order_by(ScheduledSendRunText.chat_type)
        .all()
    )
    return ScheduledSendRunDetail(
        **item.model_dump(),
        texts=[
            RunTextItem(region=t.region, chat_type=t.chat_type, text=t.text, error=t.error)
            for t in texts
        ],
    )


@router.get("/{send_id}", response_model=ScheduledSendResponse)
@limiter.exempt
async def get_scheduled_send(request: Request, send_id: uuid.UUID, db: Session = Depends(get_db)):
    """One scheduled send. Rate-limit exempt: FleetView polls it."""
    return _payload(db, _get_or_404(db, send_id))


@router.get("/{send_id}/runs", response_model=ScheduledSendRunsResponse)
async def list_runs(
    send_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """A send's runs, newest first, with their delivery counts."""
    _get_or_404(db, send_id)
    query = db.query(ScheduledSendRun).filter(ScheduledSendRun.scheduled_send_id == send_id)
    total = query.count()
    runs = query.order_by(ScheduledSendRun.created_at.desc()).limit(limit).offset(offset).all()
    return ScheduledSendRunsResponse(
        runs=_run_items(db, runs), total=total, limit=limit, offset=offset
    )


# --- Edit and status ----------------------------------------------------------------


@router.patch("/{send_id}", response_model=ScheduledSendResponse)
async def update_scheduled_send(
    send_id: uuid.UUID, request: ScheduledSendFields, db: Session = Depends(get_db)
):
    """Change any field. 409 once finished or cancelled.

    The next slot is recomputed from now only when the schedule or the status
    changes; a rename or a new text keeps the armed slot (and a due one inside
    its catch-up window still fires).
    """
    send = _get_or_404(db, send_id)
    if send.status in ("finished", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot edit a send that is {send.status}")
    fields = request.model_dump(exclude_unset=True)
    if "content" in fields and fields["content"] is None:
        raise HTTPException(status_code=422, detail="content cannot be null")
    content_type = fields.get("content_type") or send.content_type
    content = fields.get("content", send.content)
    if fields.get("content") is not None or "content_type" in fields:
        content = _check_content(content_type, content or "")
    audience = fields.get("audience") or send.audience
    jids = fields["chat_jids"] if "chat_jids" in fields else send.chat_jids
    if audience != "chats" and "audience" in fields and "chat_jids" not in fields:
        jids = None
    jids = _check_audience(audience, jids)
    _no_regions(fields.get("regions"))
    now = utcnow()
    tz = zone(send.timezone)
    schedule = ScheduledSendSchedule(**send.schedule)
    if request.schedule is not None:
        tz = bot_timezone()
        schedule = request.schedule
        compiled = _compile(schedule, tz, now)
    else:
        compiled = None
    status = fields.get("status") or send.status
    values = dict(
        content_type=content_type,
        content=content,
        audience=audience,
        chat_jids=jids,
        status=status,
    )
    if fields.get("name"):
        values["name"] = fields["name"].strip()
    if "platforms" in fields:
        values["platforms"] = list(fields["platforms"]) if fields["platforms"] else None
    if "created_by" in fields:
        values["created_by"] = fields["created_by"]
    if compiled is not None:
        values.update(
            schedule=_stored_schedule(schedule),
            run_at=compiled.run_at,
            cron_expr=compiled.cron_expr,
            timezone=tz.key,
        )
    else:
        compiled = _compiled_of(send)
    if request.schedule is not None or status != send.status:
        values["next_run_at"] = first_run(compiled, tz, now) if status == "active" else None
        if status == "active" and values["next_run_at"] is None:
            raise HTTPException(
                status_code=409, detail="The schedule has no future slot; change it"
            )
    updated = (
        db.query(ScheduledSend)
        .filter(ScheduledSend.id == send_id, ScheduledSend.status.in_(("active", "paused")))
        .update(values, synchronize_session=False)
    )
    if not updated:
        db.rollback()
        raise HTTPException(status_code=409, detail="The send changed meanwhile; reload it")
    db.commit()
    db.refresh(send)
    return _payload(db, send)


def _compiled_of(send: ScheduledSend):
    from ..scheduled_sends.schedule import Compiled

    return Compiled(run_at=send.run_at, cron_expr=send.cron_expr, description="")


def _transition(db: Session, send_id: uuid.UUID, allowed: tuple[str, ...], to: str):
    send = _get_or_404(db, send_id)
    if send.status not in allowed:
        raise HTTPException(
            status_code=409, detail=f"Cannot {_ACTION[to]} a send that is {send.status}"
        )
    values: dict = {"status": to}
    if to == "active":
        nxt = first_run(_compiled_of(send), zone(send.timezone), utcnow())
        if nxt is None:
            raise HTTPException(
                status_code=409, detail="The schedule has no future slot; edit it first"
            )
        values["next_run_at"] = nxt
    else:
        values["next_run_at"] = None
    updated = (
        db.query(ScheduledSend)
        .filter(ScheduledSend.id == send_id, ScheduledSend.status.in_(allowed))
        .update(values, synchronize_session=False)
    )
    if not updated:
        db.rollback()
        db.refresh(send)
        raise HTTPException(
            status_code=409, detail=f"Cannot {_ACTION[to]} a send that is {send.status}"
        )
    db.commit()
    db.refresh(send)
    logger.info("Scheduled send %s -> %s", send.number, to)
    return _payload(db, send)


@router.post("/{send_id}/pause", response_model=ScheduledSendResponse)
async def pause_scheduled_send(send_id: uuid.UUID, db: Session = Depends(get_db)):
    """Stop future runs; a run already delivering goes on (pause it through its broadcast)."""
    return await run_in_threadpool(_transition, db, send_id, ("active",), "paused")


@router.post("/{send_id}/resume", response_model=ScheduledSendResponse)
async def resume_scheduled_send(send_id: uuid.UUID, db: Session = Depends(get_db)):
    """Arm the next slot from now (missed slots are not caught up)."""
    return await run_in_threadpool(_transition, db, send_id, ("paused",), "active")


@router.post("/{send_id}/cancel", response_model=ScheduledSendResponse)
async def cancel_scheduled_send(send_id: uuid.UUID, db: Session = Depends(get_db)):
    """Stop for good. A run still preparing is dropped (hand-over re-reads the send
    FOR UPDATE, so a cancel either lands first or waits for the hand-over to commit).
    A run already delivering keeps going: cancel it via ``/admin/broadcasts/{id}/cancel``.
    """
    return await run_in_threadpool(_transition, db, send_id, ("active", "paused"), "cancelled")


def _open_run(db: Session, send_id: uuid.UUID) -> ScheduledSendRun | None:
    """A run of this send still preparing, or delivering through a live broadcast."""
    preparing = (
        db.query(ScheduledSendRun)
        .filter(
            ScheduledSendRun.scheduled_send_id == send_id,
            ScheduledSendRun.status == "preparing",
        )
        .first()
    )
    if preparing is not None:
        return preparing
    return (
        db.query(ScheduledSendRun)
        .join(Broadcast, Broadcast.id == ScheduledSendRun.broadcast_id)
        .filter(
            ScheduledSendRun.scheduled_send_id == send_id,
            ScheduledSendRun.status == "delivering",
            Broadcast.status.in_(_RUN_OPEN_BROADCAST),
        )
        .first()
    )


@router.post("/{send_id}/run", response_model=ScheduledSendRunItem, status_code=202)
async def run_now(send_id: uuid.UUID, db: Session = Depends(get_db)):
    """ "Send now": a manual run for this minute (the worker prepares and queues it)."""
    send = _get_or_404(db, send_id)
    if send.status == "cancelled":
        raise HTTPException(status_code=409, detail="Cannot run a cancelled send")
    if _open_run(db, send_id) is not None:
        raise HTTPException(status_code=409, detail="A run of this send is still in progress")
    run = await run_in_threadpool(create_manual_run, db, send, utcnow())
    if run is None:
        raise HTTPException(status_code=409, detail="This send already ran this minute")
    logger.info("Scheduled send %s: manual run %s", send.number, run.id)
    return _run_items(db, [run])[0]


# --- Preview --------------------------------------------------------------------------


def _preview_plan(db: Session, fields: dict, platforms: list[str], cloud_up: bool):
    planned = plan_send(
        db,
        scheduled_send_id=fields.get("id"),
        audience=fields["audience"],
        chat_jids=fields.get("chat_jids"),
        platforms=platforms,
        whitelist=runtime_config.get("whitelist_phones"),
        now=utcnow(),
        cloud_deployed=cloud_up,
    )
    return planned


@router.post("/preview", response_model=ScheduledSendPreviewResponse)
async def preview_scheduled_send(
    request: ScheduledSendPreviewRequest, db: Session = Depends(get_db)
):
    """Who a send would reach, what it would cost, and optionally a sample text. Sends nothing."""
    base: dict = {}
    if request.scheduled_send_id:
        try:
            saved = _get_or_404(db, uuid.UUID(request.scheduled_send_id))
        except ValueError as e:
            raise HTTPException(status_code=404, detail="Scheduled send not found") from e
        base = dict(
            id=saved.id,
            content_type=saved.content_type,
            content=saved.content,
            audience=saved.audience,
            chat_jids=saved.chat_jids,
            platforms=saved.platforms,
            schedule=ScheduledSendSchedule(**saved.schedule),
        )
    overrides = request.model_dump(
        exclude_unset=True,
        exclude={"scheduled_send_id", "generate", "sample_region", "sample_chat_type"},
    )
    if "schedule" in overrides:
        overrides["schedule"] = request.schedule
    fields = {**base, **{k: v for k, v in overrides.items()}}
    for key in ("content_type", "content", "schedule"):
        if fields.get(key) is None:
            raise HTTPException(status_code=422, detail=f"{key} is required for a preview")
    fields.setdefault("audience", "private")
    fields["content"] = _check_content(fields["content_type"], fields["content"])
    fields["chat_jids"] = _check_audience(fields["audience"], fields.get("chat_jids"))
    _no_regions(overrides.pop("regions", None))
    fields.pop("regions", None)

    tz = (
        zone(saved.timezone)
        if request.scheduled_send_id and "schedule" not in overrides
        else bot_timezone()
    )
    now = utcnow()
    compiled = (
        _compile(fields["schedule"], tz, now) if "schedule" in overrides or not base else None
    )
    if compiled is None:
        compiled = _compiled_of(saved)
    upcoming = next_runs(run_at=compiled.run_at, cron_expr=compiled.cron_expr, tz=tz, now=now)

    platforms, cloud_up = await _resolve_platforms(fields.get("platforms"))
    planned = await run_in_threadpool(_preview_plan, db, fields, platforms, cloud_up)
    plan = planned.plan
    pending = plan.pending()
    per_platform = [
        BroadcastPlatformPreview(
            platform=p,
            recipients=sum(1 for r in pending if r.platform == p),
            skipped_cloud_window=plan.count(p, skipped=SKIP_CLOUD_WINDOW),
        )
        for p in platforms
    ]

    # A prompt is written once per chat type in the audience (group, private).
    agent_runs = (
        len({chat_type_of(r.address) for r in pending}) if fields["content_type"] == "prompt" else 0
    )

    baileys = sum(1 for r in pending if r.platform == "baileys")
    seconds = estimate_seconds(baileys)
    warnings: list[str] = []
    recurring = compiled.cron_expr is not None
    if recurring and any(r.platform == "cloud" for r in pending):
        warnings.append("cloud_recurring")
    if recurring and upcoming:
        slot_period = period_of(compiled.cron_expr, tz, upcoming[0])
        if slot_period is not None and seconds > slot_period.total_seconds():
            warnings.append("exceeds_period")
    daily = runtime_config.get("broadcast_daily_limit") or 0
    if daily and baileys > daily:
        warnings.append("exceeds_daily_cap")

    sample = None
    if request.generate and fields["content_type"] == "prompt":
        chat_type = request.sample_chat_type or (
            "group" if any(chat_type_of(r.address) == "group" for r in pending) else "private"
        )
        async with httpx.AsyncClient(timeout=30.0) as http:
            generated = await generate_text(
                content=fields["content"], chat_type=chat_type, http_client=http
            )
        sample = ScheduledSendSample(
            region=None,
            chat_type=chat_type,
            text=generated.text,
            error=generated.error,
            prompt=generated.prompt or build_prompt(content=fields["content"], chat_type=chat_type),
        )

    return ScheduledSendPreviewResponse(
        audience=fields["audience"],
        platforms=platforms,
        total_recipients=len(pending),
        per_platform=per_platform,
        opted_out=plan.opted_out,
        not_whitelisted=plan.not_whitelisted,
        no_selected_platform=plan.no_selected_platform,
        skipped_cloud_window=sum(p.skipped_cloud_window for p in per_platform),
        skipped_unknown_client=plan.count("baileys", skipped=SKIP_UNKNOWN_CLIENT),
        skipped_duplicate=plan.duplicates,
        estimated_baileys_seconds=seconds,
        unknown_chats=planned.unknown_chats,
        opted_out_campaign=plan.opted_out_campaign,
        estimated_agent_runs=agent_runs,
        agent_runs_upper_bound=False,
        next_runs=upcoming,
        warnings=warnings,
        sample=sample,
    )
