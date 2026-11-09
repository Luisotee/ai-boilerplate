"""Schedule strings for scheduled tasks: parsing and next-fire math (pure).

The model never computes dates: the ``manage_scheduled_tasks`` tool hands the
user's intent over in this small English grammar and everything else happens
here, in the bot's time zone (never the server's).

One-shot:
- ``in 30m`` / ``in 2h`` / ``in 1d`` / ``in 1h30m``
- ``at 18:30`` (the next 18:30: today, or tomorrow if it has passed)
- ``today at 18:30`` / ``tomorrow at 9:00`` / ``monday at 9:00`` (the next Monday)
- ``2026-11-20 09:00`` (ISO; a naive time is local)

Recurring:
- ``every 2h`` / ``every 30m`` (anchored at the previous slot)
- ``every day at 9:00`` / ``daily at 9:00`` / ``every weekday at 8:30`` /
  ``every weekend at 10:00`` / ``every monday,friday at 9:00``
- a 5-field cron expression (``0 9 * * 1``), in the bot's time zone

Times accept ``9:00``, ``9``, ``9am``, ``9pm``, ``21h``, ``9h30``. Named
recurring forms compile to cron, so weekdays use cron numbering only
(Sunday = 0) and there is no second numbering to get wrong.

Every datetime that goes in or out is naive UTC, like the DB columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

#: A one-shot can't be scheduled further out than this.
MAX_ONE_SHOT_AHEAD = timedelta(days=366)
#: How long after its time an overdue one-shot still fires (late) after downtime.
ONE_SHOT_GRACE = timedelta(hours=2)
#: Bounds of the catch-up window of a recurring task: half its period, clamped.
CATCHUP_MIN = timedelta(minutes=2)
CATCHUP_MAX = timedelta(hours=2)

_DAYS = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}
_DAY_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60}
_DURATION_PART = re.compile(
    r"(\d+)\s*(d|days?|h|hrs?|hours?|m|mins?|minutes?)(?![a-z])", re.IGNORECASE
)
_TIME = r"(\d{1,2})(?:[:h](\d{2}))?\s*(am|pm|h)?"
_CRON_FIELD = re.compile(r"^[0-9a-z*,/\-]+$")


class ScheduleError(ValueError):
    """The schedule string can't be used; the message is safe to show the user."""


@dataclass(frozen=True)
class Schedule:
    """A parsed schedule. Exactly one of the three timing fields is set."""

    description: str
    run_at: datetime | None = None  # one-shot, naive UTC
    cron_expr: str | None = None
    interval_seconds: int | None = None

    @property
    def recurring(self) -> bool:
        return self.run_at is None


# --- helpers ----------------------------------------------------------------


def to_utc_naive(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def to_local(utc_naive: datetime, tz: ZoneInfo) -> datetime:
    return utc_naive.replace(tzinfo=UTC).astimezone(tz)


def format_local(utc_naive: datetime, tz: ZoneInfo) -> str:
    """``Mon 16 Nov 2026 09:00`` in ``tz``."""
    return to_local(utc_naive, tz).strftime("%a %d %b %Y %H:%M")


def _parse_duration(text: str) -> int | None:
    """Seconds in ``1h30m`` / ``2 hours`` / ``90 minutes``; None if not a duration."""
    text = text.replace(" and ", " ").strip()
    pos = 0
    total = 0
    for match in _DURATION_PART.finditer(text):
        if text[pos : match.start()].strip(" ,"):
            return None
        total += int(match.group(1)) * _UNIT_SECONDS[match.group(2)[0].lower()]
        pos = match.end()
    if pos == 0 or text[pos:].strip():
        return None
    return total


def _parse_time(hour: str, minute: str | None, suffix: str | None) -> tuple[int, int]:
    h = int(hour)
    m = int(minute) if minute else 0
    if suffix == "am":
        if not 1 <= h <= 12:
            raise ScheduleError("Use a time like 9am, 9:30 or 21:00.")
        h = 0 if h == 12 else h
    elif suffix == "pm":
        if not 1 <= h <= 12:
            raise ScheduleError("Use a time like 9pm, 9:30 or 21:00.")
        h = 12 if h == 12 else h + 12
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError("That is not a valid time of day.")
    return h, m


def _days(text: str) -> list[int] | None:
    """Cron weekday numbers for ``day`` / ``weekday`` / ``monday,friday``…; None if unknown."""
    text = text.strip()
    if text in ("day", "days"):
        return []  # every day
    if text in ("weekday", "weekdays"):
        return [1, 2, 3, 4, 5]
    if text in ("weekend", "weekends"):
        return [0, 6]
    names = [n for n in re.split(r"\s*(?:,|\band\b)\s*", text) if n]
    days = []
    for name in names:
        name = name.removesuffix("s") if name.endswith("days") else name
        if name not in _DAYS:
            return None
        if _DAYS[name] not in days:
            days.append(_DAYS[name])
    return sorted(days) or None


def _validate_cron(expr: str) -> str:
    fields = expr.split()
    if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
        raise ScheduleError("A cron schedule needs exactly 5 fields, like '0 9 * * 1'.")
    expr = " ".join(fields)
    if not croniter.is_valid(expr):
        raise ScheduleError("That cron expression is not valid.")
    return expr


# --- parsing ----------------------------------------------------------------


def parse_schedule(text: str, tz: ZoneInfo, now: datetime) -> Schedule:
    """Parse ``text`` in time zone ``tz`` at ``now`` (naive UTC).

    Raises ``ScheduleError`` with a user-facing reason.
    """
    raw = " ".join((text or "").strip().rstrip(".").split())
    s = raw.lower()
    if not s:
        raise ScheduleError("The schedule is empty.")
    local_now = to_local(now, tz)

    # in 30m
    if s.startswith("in "):
        seconds = _parse_duration(s[3:])
        if not seconds:
            raise ScheduleError("Use a delay like 'in 30m', 'in 2h' or 'in 1d'.")
        return _one_shot(now + timedelta(seconds=seconds), tz, now)

    # every 2h / every day at 9:00 / every monday,friday at 9
    if s.startswith("every ") or s.startswith("daily "):
        rest = "day " + s[6:] if s.startswith("daily ") else s[6:]
        seconds = _parse_duration(rest)
        if seconds:
            return Schedule(f"every {_format_duration(seconds)}", interval_seconds=seconds)
        match = re.fullmatch(rf"(.+?)\s+(?:at\s+)?{_TIME}", rest)
        if match:
            days = _days(match.group(1))
            if days is not None:
                h, m = _parse_time(*match.group(2, 3, 4))
                dow = ",".join(str(d) for d in days) if days else "*"
                return Schedule(
                    f"every {_describe_days(days)} at {h:02d}:{m:02d}",
                    cron_expr=f"{m} {h} * * {dow}",
                )
        raise ScheduleError(
            "Use a repeat like 'every 2h', 'every day at 9:00', "
            "'every weekday at 8:30' or 'every monday,friday at 9:00'."
        )

    # 2026-11-20 09:00 / ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            when = datetime.fromisoformat(raw.replace(" ", "T", 1))
        except ValueError as e:
            raise ScheduleError("Use a date and time like '2026-11-20 09:00'.") from e
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        return _one_shot(to_utc_naive(when), tz, now)

    # today/tomorrow/monday at 9:00, at 9:00
    match = re.fullmatch(
        rf"(?:(today|tomorrow|(?:on |next )?(?!at\b)[a-z]+)\s+)?(?:at\s+)?{_TIME}", s
    )
    if match:
        day_word = (match.group(1) or "").removeprefix("on ").removeprefix("next ")
        h, m = _parse_time(*match.group(2, 3, 4))
        base = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if day_word == "today":
            target = base
        elif day_word == "tomorrow":
            target = base + timedelta(days=1)
        elif day_word in _DAYS:
            ahead = (_DAYS[day_word] - _cron_dow(local_now)) % 7
            target = base + timedelta(days=ahead)
            if target <= local_now:
                target += timedelta(days=7)
        elif day_word == "":
            target = base if base > local_now else base + timedelta(days=1)
        else:
            raise ScheduleError(f"I don't know the day '{day_word}'.")
        # Rebuild in tz so a DST change between now and then gets its own offset.
        target = target.replace(tzinfo=None).replace(tzinfo=tz)
        return _one_shot(to_utc_naive(target), tz, now)

    # cron
    if len(s.split()) == 5:
        expr = _validate_cron(s)
        return Schedule(f"cron {expr}", cron_expr=expr)

    raise ScheduleError(
        "I couldn't read that schedule. Use e.g. 'in 30m', 'tomorrow at 9:00', "
        "'2026-11-20 09:00', 'every day at 9:00' or 'every monday at 9:00'."
    )


def _cron_dow(local: datetime) -> int:
    return (local.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0


def _one_shot(when: datetime, tz: ZoneInfo, now: datetime) -> Schedule:
    when = when.replace(second=0, microsecond=0) if when.second or when.microsecond else when
    if when <= now:
        raise ScheduleError("That time is already in the past.")
    if when - now > MAX_ONE_SHOT_AHEAD:
        raise ScheduleError("I can only schedule up to a year ahead.")
    return Schedule(f"once at {format_local(when, tz)}", run_at=when)


def _format_duration(seconds: int) -> str:
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or f"{seconds}s"


def _describe_days(days: list[int]) -> str:
    if not days:
        return "day"
    if days == [1, 2, 3, 4, 5]:
        return "weekday"
    if days == [0, 6]:
        return "weekend"
    return ",".join(_DAY_NAMES[d] for d in days)


# --- next fire --------------------------------------------------------------


def next_fire(
    *,
    tz: ZoneInfo,
    after: datetime,
    run_at: datetime | None = None,
    cron_expr: str | None = None,
    interval_seconds: int | None = None,
    previous: datetime | None = None,
) -> datetime | None:
    """First slot strictly after ``after`` (naive UTC); None for a one-shot.

    An interval is anchored at ``previous`` (the last slot) so it doesn't drift;
    without one it starts ``interval_seconds`` after ``after``.
    """
    if cron_expr:
        local_after = to_local(after, tz)
        return to_utc_naive(croniter(cron_expr, local_after).get_next(datetime))
    if interval_seconds:
        step = timedelta(seconds=interval_seconds)
        if previous is None:
            return after + step
        if previous > after:
            return previous
        missed = int((after - previous) / step) + 1
        return previous + missed * step
    if run_at is not None:
        return run_at if run_at > after else None
    return None


def period(*, tz: ZoneInfo, slot: datetime, cron_expr: str | None, interval_seconds: int | None):
    """Time between ``slot`` and the slot after it (None for a one-shot)."""
    if interval_seconds:
        return timedelta(seconds=interval_seconds)
    if cron_expr:
        nxt = next_fire(tz=tz, after=slot, cron_expr=cron_expr)
        return nxt - slot
    return None


def catchup_window(slot_period: timedelta | None) -> timedelta:
    """How late a slot may still fire after downtime; later ones are skipped.

    Half the period, clamped to 2 min - 2 h (hermes-agent's rule); a one-shot
    gets ``ONE_SHOT_GRACE``.
    """
    if slot_period is None:
        return ONE_SHOT_GRACE
    return max(CATCHUP_MIN, min(CATCHUP_MAX, slot_period / 2))


def upcoming(schedule: Schedule, tz: ZoneInfo, now: datetime, horizon: timedelta) -> list[datetime]:
    """Slots of a recurring schedule from ``now`` within ``horizon`` (capped at 5000)."""
    out: list[datetime] = []
    cursor = now
    previous = None
    end = now + horizon
    while len(out) < 5000:
        nxt = next_fire(
            tz=tz,
            after=cursor,
            cron_expr=schedule.cron_expr,
            interval_seconds=schedule.interval_seconds,
            previous=previous,
        )
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        cursor = previous = nxt
    return out


def min_gap(slots: list[datetime]) -> timedelta | None:
    """Smallest time between two consecutive slots (None with fewer than two)."""
    gaps = [b - a for a, b in zip(slots, slots[1:], strict=False)]
    return min(gaps) if gaps else None


def max_per_day(slots: list[datetime]) -> int:
    """Most slots that fall inside any rolling 24 h."""
    best = 0
    start = 0
    for end, slot in enumerate(slots):
        while slot - slots[start] >= timedelta(days=1):
            start += 1
        best = max(best, end - start + 1)
    return best
