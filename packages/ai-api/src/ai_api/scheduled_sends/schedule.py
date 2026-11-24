"""Structured schedules of scheduled sends (pure).

FleetView sends ``{kind, …}``; it is compiled to the ``run_at`` / ``cron_expr``
pair that ``services/schedule.py`` already fires (DST-correct, in the send's
time zone). Weekdays use cron numbering (0 = Sunday), as there.

- ``{"kind": "once", "run_at": "2026-12-01T08:00"}`` (naive = in ``tz``)
- ``{"kind": "daily", "time": "08:00"}``
- ``{"kind": "weekly", "time": "08:00", "weekdays": [1]}``
- ``{"kind": "monthly", "time": "08:00", "day_of_month": 5}`` (1-28, so no month is skipped)

Every datetime in or out is naive UTC, like the DB columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..services.schedule import (
    MAX_ONE_SHOT_AHEAD,
    _describe_days,
    format_local,
    next_fire,
    to_utc_naive,
)

KINDS = ("once", "daily", "weekly", "monthly")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleInputError(ValueError):
    """The schedule can't be used; the message is safe to return in a 422."""


@dataclass(frozen=True)
class Compiled:
    run_at: datetime | None
    cron_expr: str | None
    description: str


def _time(value) -> tuple[int, int]:
    match = _TIME.match(str(value or ""))
    if not match:
        raise ScheduleInputError("time must be HH:MM (00:00-23:59)")
    return int(match.group(1)), int(match.group(2))


def compile_schedule(schedule: dict, tz: ZoneInfo, now: datetime) -> Compiled:
    """Validate and compile; a ``once`` must be in the future and at most a year out."""
    kind = schedule.get("kind")
    if kind == "once":
        raw = schedule.get("run_at")
        try:
            when = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        except ValueError as e:
            raise ScheduleInputError("run_at must be an ISO 8601 date and time") from e
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        run_at = to_utc_naive(when).replace(second=0, microsecond=0)
        if run_at <= now:
            raise ScheduleInputError("run_at is in the past")
        if run_at - now > MAX_ONE_SHOT_AHEAD:
            raise ScheduleInputError("run_at is more than a year ahead")
        return Compiled(run_at, None, f"once, {format_local(run_at, tz)}")
    h, m = _time(schedule.get("time"))
    at = f"at {h:02d}:{m:02d}"
    if kind == "daily":
        return Compiled(None, f"{m} {h} * * *", f"every day {at}")
    if kind == "weekly":
        days = schedule.get("weekdays") or []
        if (
            not isinstance(days, list)
            or not days
            or any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days)
            or len(set(days)) != len(days)
        ):
            raise ScheduleInputError("weekdays must be 1-7 distinct numbers 0-6 (0 = Sunday)")
        days = sorted(days)
        dow = ",".join(str(d) for d in days)
        label = _describe_days([] if len(days) == 7 else days)
        return Compiled(None, f"{m} {h} * * {dow}", f"every {label} {at}")
    if kind == "monthly":
        day = schedule.get("day_of_month")
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 28:
            raise ScheduleInputError("day_of_month must be 1-28")
        return Compiled(None, f"{m} {h} {day} * *", f"every month on day {day} {at}")
    raise ScheduleInputError(f"kind must be one of {', '.join(KINDS)}")


def first_run(compiled: Compiled, tz: ZoneInfo, now: datetime) -> datetime | None:
    """The first slot after ``now``."""
    if compiled.run_at is not None:
        return compiled.run_at if compiled.run_at > now else None
    return next_fire(tz=tz, after=now, cron_expr=compiled.cron_expr)


def next_runs(
    *, run_at: datetime | None, cron_expr: str | None, tz: ZoneInfo, now: datetime, count: int = 5
) -> list[datetime]:
    """Up to ``count`` upcoming slots (for the preview)."""
    if run_at is not None:
        return [run_at] if run_at > now else []
    out: list[datetime] = []
    cursor, previous = now, None
    while len(out) < count:
        nxt = next_fire(tz=tz, after=cursor, cron_expr=cron_expr, previous=previous)
        if nxt is None:
            break
        out.append(nxt)
        cursor = previous = nxt
    return out


def period_of(cron_expr: str | None, tz: ZoneInfo, slot: datetime) -> timedelta | None:
    """Time to the slot after ``slot`` (None for a one-shot)."""
    if not cron_expr:
        return None
    nxt = next_fire(tz=tz, after=slot, cron_expr=cron_expr, previous=slot)
    return nxt - slot if nxt is not None else None
