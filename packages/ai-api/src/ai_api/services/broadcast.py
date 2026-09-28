"""Broadcasts: who receives one, and on which platform.

An operator broadcast (a changelog / announcement, sent from FleetView through
``POST /admin/broadcasts``) goes to every chat the bot has ever talked to that
has not opted out. This module holds the decisions; ``routes/broadcasts.py``
snapshots the recipients and ``streams/broadcast_consumer.py`` delivers them.
The pacing math lives in ``broadcast_pacing.py``.

Routing a user to a platform:

- A ``tg:`` row that was never linked is only reachable on Telegram.
- A WhatsApp row goes through the WhatsApp client it last wrote from
  (``users.whatsapp_client_id``: Baileys or Cloud; NULL on old rows means
  Baileys). If it was merged with a Telegram account (``telegram_jid``) it also
  has a Telegram route; the platform the user last used overall
  (``users.last_client_id``) comes first.
- Only selected platforms count, and only routes whose own identity passes the
  whitelist. A user with no usable route is left out of the snapshot.
- The Cloud API can only send free-form text within 24h of the user's last
  Cloud message (anything else needs a template, which is not implemented), so
  a Cloud route whose ``users.cloud_last_inbound_at`` is older than
  ``CLOUD_WINDOW`` is skipped — unless the user has another selected route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from ..config import get_whatsapp_client_url
from ..database import User, is_group_jid, is_telegram_jid
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

# Why choose_route found no route at all (plan counters, never stored).
NO_PLATFORM = "no_platform"
NOT_WHITELISTED = "not_whitelisted"


@dataclass(frozen=True)
class Route:
    platform: Platform
    address: str


RouteFilter = Callable[[User, Route], bool]


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


def candidate_routes(user: User) -> list[Route]:
    """Every route that reaches ``user``, most-recently-used platform first."""
    if is_telegram_jid(user.whatsapp_jid):
        # Unlinked Telegram row: its tg: JID lives in whatsapp_jid.
        return [Route("telegram", user.whatsapp_jid)]

    platform: Platform = "cloud" if user.whatsapp_client_id == "cloud" else "baileys"
    whatsapp = Route(platform, user.whatsapp_jid)
    if not user.telegram_jid:
        return [whatsapp]
    telegram = Route("telegram", user.telegram_jid)
    if user.last_client_id == "telegram":
        return [telegram, whatsapp]
    return [whatsapp, telegram]


def in_cloud_window(last_cloud_inbound_at: datetime | None, now: datetime) -> bool:
    """Naive-UTC check that a Cloud chat is still inside Meta's 24h window."""
    return last_cloud_inbound_at is not None and now - last_cloud_inbound_at < CLOUD_WINDOW


def choose_route(
    user: User,
    selected: Iterable[str],
    allowed: RouteFilter,
    now: datetime,
) -> tuple[Route | None, str | None]:
    """Pick the route for ``user`` among the ``selected`` platforms.

    Returns ``(route, None)`` to send, ``(route, SKIP_CLOUD_WINDOW)`` to record
    the user as skipped, or ``(None, NO_PLATFORM | NOT_WHITELISTED)`` when no
    usable route exists. ``now`` is naive UTC, like the DB timestamps.
    """
    selected = set(selected)
    skipped: Route | None = None
    blocked = False
    for route in candidate_routes(user):
        if route.platform not in selected:
            continue
        if not allowed(user, route):
            blocked = True
            continue
        if route.platform == "cloud" and not in_cloud_window(user.cloud_last_inbound_at, now):
            skipped = skipped or route
            continue
        return route, None
    if skipped is not None:
        return skipped, SKIP_CLOUD_WINDOW
    return None, NOT_WHITELISTED if blocked else NO_PLATFORM


# --- Audience ---------------------------------------------------------------


def audience_users(db: Session, audience: Audience) -> list[User]:
    """Every chat in ``audience``, opted-out ones included (the preview counts them)."""
    query = db.query(User)
    if audience == "private":
        query = query.filter(User.conversation_type == "private")
    elif audience == "groups":
        query = query.filter(User.conversation_type == "group")
    return query.order_by(User.created_at).all()


def plan_broadcast(
    users: Iterable[User],
    selected: Iterable[str],
    allowed: RouteFilter,
    now: datetime,
) -> BroadcastPlan:
    """Turn audience rows into the recipient snapshot (pure)."""
    selected = tuple(selected)
    plan = BroadcastPlan()
    for user in users:
        if user.broadcast_opt_out:
            plan.opted_out += 1
            continue
        route, reason = choose_route(user, selected, allowed, now)
        if route is None:
            if reason == NOT_WHITELISTED:
                plan.not_whitelisted += 1
            else:
                plan.no_selected_platform += 1
            continue
        plan.recipients.append(RecipientPlan(user.id, route.platform, route.address, reason))
    return plan


def whitelist_filter(raw_whitelist: str, group_gating: str) -> RouteFilter:
    """Whitelist predicate per ROUTE, mirroring ``routes/chat.py`` ``_is_whitelisted``.

    A broadcast must never reach a chat the bot would refuse to talk to, and the
    check is on the identity the message actually goes to: a linked user
    whitelisted only as ``tg:…`` gets nothing on WhatsApp, and vice versa.
    """
    from ..whitelist import is_whitelisted, parse_whitelist

    if not raw_whitelist:
        return lambda _user, _route: True
    wl = parse_whitelist(raw_whitelist)

    def allowed(user: User, route: Route) -> bool:
        if group_gating == "membership" and is_group_jid(route.address):
            return True
        if route.platform == "telegram":
            return is_whitelisted(wl, route.address)
        if is_whitelisted(wl, user.whatsapp_jid, user.phone):
            return True
        return bool(user.whatsapp_lid) and is_whitelisted(wl, user.whatsapp_lid, user.phone)

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
