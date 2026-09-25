"""Broadcasts: who receives one, on which platform, and how fast.

An operator broadcast (a changelog / announcement, sent from FleetView through
``POST /admin/broadcasts``) goes to every chat the bot has ever talked to that
has not opted out. This module holds the decisions; ``routes/broadcasts.py``
snapshots the recipients and ``streams/broadcast_consumer.py`` delivers them.

Routing a user to a platform:

- A ``tg:`` row that was never linked is only reachable on Telegram.
- A WhatsApp row goes through the client it last wrote from
  (``users.last_client_id``: Baileys or Cloud; NULL on old rows means Baileys),
  and, if it was merged with a Telegram account (``telegram_jid``), also has
  Telegram as a second route. The platform the user last used comes first.
- Only platforms selected for the broadcast count. A user with no selected
  route is left out of the snapshot entirely.
- The Cloud API can only send free-form text within 24h of the user's last
  message (anything else needs a template, which is not implemented), so a
  Cloud route older than ``CLOUD_WINDOW`` is skipped — unless the user has
  another selected route.

The pacing helpers are pure so the anti-ban schedule is testable.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_whatsapp_client_url
from ..database import ConversationMessage, User, is_group_jid, is_telegram_jid
from ..logger import logger

Platform = Literal["baileys", "cloud", "telegram"]
Audience = Literal["private", "groups", "all"]

PLATFORMS: tuple[Platform, ...] = ("baileys", "cloud", "telegram")
AUDIENCES: tuple[Audience, ...] = ("private", "groups", "all")

#: Meta allows free-form messages for 24h after the user's last message. Keep an
#: hour of slack: a Cloud recipient is re-checked right before sending, but the
#: lane may still be minutes behind.
CLOUD_WINDOW = timedelta(hours=23)

# Skip reasons stored in broadcast_recipients.error_code.
SKIP_CLOUD_WINDOW = "cloud_window"
SKIP_OPTED_OUT = "opted_out"
SKIP_USER_DELETED = "user_deleted"


@dataclass(frozen=True)
class Route:
    platform: Platform
    address: str


@dataclass(frozen=True)
class RecipientPlan:
    """One snapshot row: pending with a route, or skipped with a reason."""

    user_id: object
    platform: Platform
    address: str
    skip_reason: str | None = None


@dataclass
class BroadcastPlan:
    """Result of resolving an audience; also what the preview endpoint reports."""

    recipients: list[RecipientPlan] = field(default_factory=list)
    opted_out: int = 0
    not_whitelisted: int = 0
    no_selected_platform: int = 0

    def pending(self) -> list[RecipientPlan]:
        return [r for r in self.recipients if r.skip_reason is None]

    def count(self, platform: Platform, *, skipped: str | None = None) -> int:
        return sum(
            1 for r in self.recipients if r.platform == platform and r.skip_reason == skipped
        )


# --- Routing ----------------------------------------------------------------


def _whatsapp_platform(user: User) -> Platform:
    return "cloud" if user.last_client_id == "cloud" else "baileys"


def candidate_routes(user: User) -> list[Route]:
    """Every route that reaches ``user``, most-recently-used platform first."""
    if is_telegram_jid(user.whatsapp_jid):
        # Unlinked Telegram row: its tg: JID lives in whatsapp_jid.
        return [Route("telegram", user.whatsapp_jid)]

    whatsapp = Route(_whatsapp_platform(user), user.whatsapp_jid)
    if not user.telegram_jid:
        return [whatsapp]
    telegram = Route("telegram", user.telegram_jid)
    if user.last_client_id == "telegram":
        return [telegram, whatsapp]
    return [whatsapp, telegram]


def choose_route(
    user: User,
    selected: Iterable[str],
    last_inbound_at: datetime | None,
    now: datetime,
) -> tuple[Route | None, str | None]:
    """Pick the route for ``user`` among the ``selected`` platforms.

    Returns ``(route, None)`` to send, ``(route, reason)`` to record the user as
    skipped, or ``(None, None)`` when no selected platform reaches them.
    ``last_inbound_at`` and ``now`` are naive UTC, like the DB timestamps.
    """
    selected = set(selected)
    skipped: tuple[Route | None, str | None] = (None, None)
    for route in candidate_routes(user):
        if route.platform not in selected:
            continue
        if route.platform == "cloud" and not in_cloud_window(last_inbound_at, now):
            skipped = skipped if skipped[0] else (route, SKIP_CLOUD_WINDOW)
            continue
        return route, None
    return skipped


def in_cloud_window(last_inbound_at: datetime | None, now: datetime) -> bool:
    return last_inbound_at is not None and now - last_inbound_at < CLOUD_WINDOW


# --- Audience ---------------------------------------------------------------


def last_inbound_subquery(db: Session):
    """``user_id -> max(timestamp)`` of messages the user (not the bot) sent."""
    return (
        db.query(
            ConversationMessage.user_id.label("user_id"),
            func.max(ConversationMessage.timestamp).label("last_inbound_at"),
        )
        .filter(ConversationMessage.role == "user")
        .group_by(ConversationMessage.user_id)
        .subquery()
    )


def audience_rows(db: Session, audience: Audience) -> list[tuple[User, datetime | None]]:
    """Every chat in ``audience`` (opted-out ones included) with its last inbound time."""
    last = last_inbound_subquery(db)
    query = db.query(User, last.c.last_inbound_at).outerjoin(last, last.c.user_id == User.id)
    if audience == "private":
        query = query.filter(User.conversation_type == "private")
    elif audience == "groups":
        query = query.filter(User.conversation_type == "group")
    return query.order_by(User.created_at).all()


def plan_broadcast(
    rows: Iterable[tuple[User, datetime | None]],
    selected: Iterable[str],
    allowed: Callable[[User], bool],
    now: datetime,
) -> BroadcastPlan:
    """Turn audience rows into the recipient snapshot (pure)."""
    selected = tuple(selected)
    plan = BroadcastPlan()
    for user, last_inbound_at in rows:
        if user.broadcast_opt_out:
            plan.opted_out += 1
            continue
        if not allowed(user):
            plan.not_whitelisted += 1
            continue
        route, reason = choose_route(user, selected, last_inbound_at, now)
        if route is None:
            plan.no_selected_platform += 1
            continue
        plan.recipients.append(RecipientPlan(user.id, route.platform, route.address, reason))
    return plan


def whitelist_filter(raw_whitelist: str, group_gating: str) -> Callable[[User], bool]:
    """Whitelist predicate mirroring ``routes/chat.py`` ``_is_whitelisted``.

    A broadcast must never reach a chat the bot would refuse to talk to. A row
    passes if ANY of its identities is whitelisted (a linked user may be listed
    under either platform).
    """
    from ..whitelist import is_whitelisted, parse_whitelist

    if not raw_whitelist:
        return lambda _user: True
    wl = parse_whitelist(raw_whitelist)

    def allowed(user: User) -> bool:
        if group_gating == "membership" and is_group_jid(user.whatsapp_jid):
            return True
        if is_whitelisted(wl, user.whatsapp_jid, user.phone):
            return True
        if user.whatsapp_lid and is_whitelisted(wl, user.whatsapp_lid, user.phone):
            return True
        return bool(user.telegram_jid) and is_whitelisted(wl, user.telegram_jid)

    return allowed


# --- Message ----------------------------------------------------------------


def render_message(text: str, footer: str | None) -> str:
    """The text every recipient gets: the operator's text, then the opt-out footer."""
    text = text.strip()
    footer = (footer or "").strip()
    return f"{text}\n\n{footer}" if footer else text


# --- Platform availability --------------------------------------------------


async def probe_platforms(timeout: float = 3.0) -> dict[Platform, bool]:
    """Which chat clients answer at all (``GET /health``; any HTTP status counts).

    Client URLs have localhost defaults, so "configured" can't be read from the
    settings: a Cloud-only bot still has a Baileys URL. A client that doesn't
    answer isn't deployed (or is down) and is left out of a default selection.
    """

    async def reachable(http: httpx.AsyncClient, platform: Platform) -> bool:
        try:
            await http.get(f"{get_whatsapp_client_url(platform).rstrip('/')}/health")
            return True
        except httpx.HTTPError as e:
            logger.info("Broadcast: %s client not reachable (%s)", platform, type(e).__name__)
            return False

    async with httpx.AsyncClient(timeout=timeout) as http:
        results = await asyncio.gather(*(reachable(http, p) for p in PLATFORMS))
    return dict(zip(PLATFORMS, results, strict=True))


# --- Baileys pacing (anti-ban) ----------------------------------------------

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


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
    """0 inside the window, else seconds until it next opens (wraps midnight)."""
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
    return (opens - local_now).total_seconds()


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
