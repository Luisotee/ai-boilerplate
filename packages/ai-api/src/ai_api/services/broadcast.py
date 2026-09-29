"""Broadcasts: who receives one, and on which platform.

An operator broadcast (a changelog / announcement, sent from FleetView through
``POST /admin/broadcasts``) goes to every chat the bot has ever talked to that
has not opted out. This module holds the decisions; ``routes/broadcasts.py``
snapshots the recipients and ``streams/broadcast_consumer.py`` delivers them.
The pacing math lives in ``broadcast_pacing.py``.

Routing a user to a platform:

- A ``tg:`` row that was never linked is only reachable on Telegram.
- A WhatsApp row goes through the WhatsApp client it last wrote from
  (``users.whatsapp_client_id``: Baileys or Cloud). NULL (a row from before
  the column existed) means Baileys only when this deployment has no Cloud
  client; with one, the row could be a Cloud user who never saw the Baileys
  number, so it is skipped (``unknown_client``) until the user writes again —
  an unexpected message from an unknown number is exactly what gets reported
  as spam. "Has a Cloud client" is the health probe OR ``cloud_seen`` (some
  row has written through Cloud), so a Cloud client that is slow or
  restarting during one probe doesn't route its users through Baileys. A
  WhatsApp GROUP is never ambiguous: the Cloud API has no groups, and a group
  where the bot is never mentioned keeps a NULL client forever. If the row was merged with a Telegram account (``telegram_jid``)
  it also has a Telegram route; the platform the user last used overall
  (``users.last_client_id``) comes first.
- One person, one message: rows that are the same WhatsApp human (an early
  ``@lid`` row next to the phone-JID row carrying that LID, or private rows
  sharing a phone) are grouped by ``_people`` and only the best row is sent.
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
SKIP_UNKNOWN_CLIENT = "unknown_client"
#: Also stored, when the whitelist changes while a broadcast is running.
NOT_WHITELISTED = "not_whitelisted"

# Why choose_route found no route at all (plan counter, never stored).
NO_PLATFORM = "no_platform"


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
    #: Extra rows of a person who already has a recipient (never sent).
    duplicates: int = 0

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


def is_ambiguous_whatsapp_row(user: User) -> bool:
    """A private WhatsApp row whose client (Baileys vs Cloud) was never recorded."""
    return (
        user.whatsapp_client_id is None
        and not is_telegram_jid(user.whatsapp_jid)
        and not is_group_jid(user.whatsapp_jid)
    )


def choose_route(
    user: User,
    selected: Iterable[str],
    allowed: RouteFilter,
    now: datetime,
    *,
    cloud_deployed: bool = False,
) -> tuple[Route | None, str | None]:
    """Pick the route for ``user`` among the ``selected`` platforms.

    Returns ``(route, None)`` to send, ``(route, SKIP_CLOUD_WINDOW |
    SKIP_UNKNOWN_CLIENT)`` to record the user as skipped, or
    ``(None, NO_PLATFORM | NOT_WHITELISTED)`` when no usable route exists.
    ``now`` is naive UTC, like the DB timestamps. ``cloud_deployed``: this bot
    also runs a Cloud client, so a NULL ``whatsapp_client_id`` is ambiguous.
    """
    selected = set(selected)
    skipped: tuple[Route, str] | None = None
    blocked = False
    for route in candidate_routes(user):
        if route.platform not in selected:
            continue
        if not allowed(user, route):
            blocked = True
            continue
        if route.platform == "cloud" and not in_cloud_window(user.cloud_last_inbound_at, now):
            skipped = skipped or (route, SKIP_CLOUD_WINDOW)
            continue
        if route.platform == "baileys" and cloud_deployed and is_ambiguous_whatsapp_row(user):
            skipped = skipped or (route, SKIP_UNKNOWN_CLIENT)
            continue
        return route, None
    if skipped is not None:
        return skipped
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


def cloud_seen(db: Session) -> bool:
    """Whether any chat has ever written through the Cloud client.

    Evidence of a Cloud deployment that doesn't depend on one health probe.
    """
    return db.query(User.id).filter(User.whatsapp_client_id == "cloud").first() is not None


def _is_lid(jid: str | None) -> bool:
    return bool(jid) and jid.endswith("@lid")


def _mergeable(user: User) -> bool:
    """Only private WhatsApp rows can be the same person; groups and tg: never are."""
    return (
        user.conversation_type == "private"
        and not is_telegram_jid(user.whatsapp_jid)
        and not is_group_jid(user.whatsapp_jid)
    )


def _people(users: Iterable[User]) -> list[list[User]]:
    """Group rows into people, best row first; order follows each person's first row.

    Two private WhatsApp rows are one person when an ``@lid`` row's JID is the
    other row's ``whatsapp_lid`` (the chat was first seen before its LID
    resolved to a phone), or when they share a phone number. The best row has
    a phone JID rather than an ``@lid`` one; ties go to the newest.
    """
    users = list(users)
    parent = list(range(len(users)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[str, int] = {}
    for i, user in enumerate(users):
        if not _mergeable(user):
            continue
        keys = []
        if _is_lid(user.whatsapp_jid):
            keys.append(f"lid:{user.whatsapp_jid}")
        if user.whatsapp_lid:
            keys.append(f"lid:{user.whatsapp_lid}")
        phone = (user.phone or "").strip().lstrip("+")
        if phone:
            keys.append(f"phone:{phone}")
        for key in keys:
            if key in owner:
                parent[find(i)] = find(owner[key])
            else:
                owner[key] = i

    groups: dict[int, list[int]] = {}
    for i in range(len(users)):
        groups.setdefault(find(i), []).append(i)

    def rank(i: int) -> tuple:
        user = users[i]
        created = user.created_at.timestamp() if user.created_at else 0.0
        return (_is_lid(user.whatsapp_jid), -created)

    people = sorted(groups.values(), key=min)
    return [[users[i] for i in sorted(members, key=rank)] for members in people]


def plan_broadcast(
    users: Iterable[User],
    selected: Iterable[str],
    allowed: RouteFilter,
    now: datetime,
    *,
    cloud_deployed: bool = False,
) -> BroadcastPlan:
    """Turn audience rows into the recipient snapshot (pure)."""
    selected = tuple(selected)
    plan = BroadcastPlan()
    for person in _people(users):
        plan.duplicates += len(person) - 1
        if any(row.broadcast_opt_out for row in person):
            plan.opted_out += 1
            continue
        user = person[0]
        route, reason = choose_route(user, selected, allowed, now, cloud_deployed=cloud_deployed)
        if route is None:
            if reason == NOT_WHITELISTED:
                plan.not_whitelisted += 1
            else:
                plan.no_selected_platform += 1
            continue
        plan.recipients.append(RecipientPlan(user.id, route.platform, route.address, reason))
    return plan


def whitelist_filter(raw_whitelist: str) -> RouteFilter:
    """Whitelist predicate per ROUTE, mirroring ``routes/chat.py`` ``_is_whitelisted``.

    A broadcast must never reach a chat the bot would refuse to talk to, and the
    check is on the identity the message actually goes to: a linked user
    whitelisted only as ``tg:…`` gets nothing on WhatsApp, and vice versa.

    Stricter than chat gating for groups: under ``GROUP_GATING=membership`` a
    group is in scope for chat because a member is whitelisted (Baileys) or
    simply because the bot is in it (Telegram), and the bot mostly stays silent
    there. A broadcast into such a group would be unsolicited, so a group
    passes only when it is listed itself.
    """
    from ..whitelist import is_whitelisted, parse_whitelist

    wl = parse_whitelist(raw_whitelist or "")
    if wl.size == 0:
        # Same as chat: a whitelist with no entries (" , ") is no whitelist.
        return lambda _user, _route: True

    def allowed(user: User, route: Route) -> bool:
        if is_group_jid(route.address) or route.platform == "telegram":
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
