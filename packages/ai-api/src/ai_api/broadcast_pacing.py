"""Pure pacing math for broadcasts: the Baileys anti-ban schedule.

Imports nothing from the app, so ``config.py`` can validate the broadcast
settings at boot with the same parsers the worker uses at send time.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Upper bounds for the numeric broadcast settings, shared by the env
#: validation (config.py) and PATCH /admin/settings. Generous: they only catch
#: typos like an extra zero, never a deliberate slow schedule.
MAX_DELAY_SECONDS = 3600
MAX_BATCH_SIZE = 1000
MAX_BATCH_PAUSE_SECONDS = 86400
MAX_DAILY_LIMIT = 100000
SETTING_MAXIMA = {
    "broadcast_min_delay_seconds": MAX_DELAY_SECONDS,
    "broadcast_max_delay_seconds": MAX_DELAY_SECONDS,
    "broadcast_batch_size": MAX_BATCH_SIZE,
    "broadcast_batch_pause_seconds": MAX_BATCH_PAUSE_SECONDS,
    "broadcast_daily_limit": MAX_DAILY_LIMIT,
}

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class PacingSettings:
    """The hot ``broadcast_*`` settings the Baileys lane paces itself with."""

    min_delay_seconds: float
    max_delay_seconds: float
    batch_size: int
    batch_pause_seconds: float


def parse_send_window(value: str | None) -> tuple[time, time] | None:
    """Parse ``"HH:MM-HH:MM"``; empty means no window. Raises ``ValueError``."""
    if not value or not value.strip():
        return None
    match = _WINDOW_RE.match(value)
    if not match:
        raise ValueError("broadcast_send_window must look like 'HH:MM-HH:MM' (e.g. 09:00-21:00)")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError as e:
        raise ValueError(f"broadcast_send_window has an invalid time: {e}") from e
    if start == end:
        raise ValueError("broadcast_send_window start and end must differ")
    return start, end


def parse_timezone(value: str) -> ZoneInfo:
    """Resolve an IANA zone name. Raises ``ValueError`` for an unknown one."""
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"Unknown time zone '{value}' (use an IANA name like UTC)") from e


def seconds_until_window(window: tuple[time, time] | None, local_now: datetime) -> float:
    """0 inside the window, else real seconds until it next opens (wraps midnight).

    ``local_now`` is an aware datetime in the window's zone. The difference is
    taken in UTC: subtracting two datetimes that share a ZoneInfo gives the
    WALL-CLOCK difference, which is an hour off across a DST change.
    """
    if window is None:
        return 0.0
    start, end = window
    now_t = local_now.time()
    inside = start <= now_t < end if start < end else (now_t >= start or now_t < end)
    if inside:
        return 0.0
    opens = local_now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if opens <= local_now:
        opens += timedelta(days=1)
    return (opens.astimezone(UTC) - local_now.astimezone(UTC)).total_seconds()


def jittered_delay(rng: random.Random, min_seconds: float, max_seconds: float) -> float:
    """Uniform pause between two messages; a misordered pair is swapped."""
    low, high = sorted((max(0.0, min_seconds), max(0.0, max_seconds)))
    return rng.uniform(low, high)


def batch_pause(rng: random.Random, base_seconds: float) -> float:
    """Long pause after a batch, ±30% so batches don't form a regular pattern."""
    return max(0.0, base_seconds) * rng.uniform(0.7, 1.3)


def typing_seconds(text: str) -> float:
    """How long to show "typing…" before a message: ~30 ms/char, 2-8 s."""
    return min(8.0, max(2.0, len(text) * 0.03))


def resume_pacing(
    recent_sent_desc: Sequence[datetime],
    now: datetime,
    rng: random.Random,
    settings: PacingSettings,
) -> tuple[int, float]:
    """Where a (re)starting Baileys lane is in the schedule, from past sends.

    The lane's own counters die with it, so without this every resume, lease
    handover or worker restart would send immediately and start a fresh batch.
    ``recent_sent_desc`` are the latest Baileys ``sent_at`` values (newest
    first, across all broadcasts, at most ``batch_size`` of them); ``now`` is
    naive UTC like them.

    Returns ``(sent_in_batch, wait_seconds)``. A gap of at least 70% of the
    batch pause (its jitter floor) counts as a completed batch pause.
    """
    if not recent_sent_desc:
        return 0, 0.0
    batch_size = max(1, settings.batch_size)
    reset_gap = 0.7 * max(0.0, settings.batch_pause_seconds)
    elapsed = (now - recent_sent_desc[0]).total_seconds()
    if reset_gap > 0 and elapsed >= reset_gap:
        return 0, 0.0

    count = 1
    for newer, older in zip(recent_sent_desc, recent_sent_desc[1:], strict=False):
        if reset_gap > 0 and (newer - older).total_seconds() >= reset_gap:
            break
        count += 1

    if count >= batch_size:
        return 0, max(0.0, batch_pause(rng, settings.batch_pause_seconds) - elapsed)
    delay = jittered_delay(rng, settings.min_delay_seconds, settings.max_delay_seconds)
    return count, max(0.0, delay - elapsed)


def estimate_baileys_seconds(
    count: int,
    *,
    min_delay: float,
    max_delay: float,
    batch_size: int,
    batch_pause_seconds: float,
    daily_limit: int,
    window: tuple[time, time] | None,
    text_length: int = 280,
) -> int:
    """Rough wall-clock estimate for ``count`` Baileys sends under the pacing.

    Ignores sends already made today and retries; FleetView shows it as "about".
    """
    if count <= 0:
        return 0
    per_message = (max(min_delay, 0) + max(max_delay, 0)) / 2 + typing_seconds("x" * text_length)
    active = count * per_message + ((count - 1) // max(batch_size, 1)) * batch_pause_seconds
    if window is not None:
        start, end = window
        minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        minutes = minutes if minutes > 0 else minutes + 24 * 60
        active *= (24 * 60) / minutes
    if daily_limit > 0 and count > daily_limit:
        active = max(active, (math.ceil(count / daily_limit) - 1) * 86400)
    return int(active)
