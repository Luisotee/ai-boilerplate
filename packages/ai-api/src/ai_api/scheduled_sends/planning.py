"""Who a scheduled send reaches, and which text each chat gets (DB reads + pure rules).

The audience and routing rules are the broadcast ones (``services/broadcast.py``
``plan_broadcast``: one message per person, whitelist per route with groups
listed themselves, Cloud 23 h window, ``unknown_client``, global opt-out), plus
this send's own opt-outs. The template has no region concept, so a prompt is
written once per chat type (group, private), never per chat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import ScheduledSendOptOut, User, is_group_jid
from ..services.broadcast import (
    BroadcastPlan,
    audience_users,
    cloud_seen,
    plan_broadcast,
    whitelist_filter,
)

#: Recipient ``error_code`` values this feature adds (open strings in FleetView).
SKIP_OPTED_OUT_CAMPAIGN = "opted_out_campaign"
SKIP_GENERATION_FAILED = "generation_failed"


def broadcast_audience(audience: str, users: Iterable[User]) -> str:
    """The ``broadcasts.audience`` value (a closed FleetView enum) for a run's broadcast."""
    if audience != "chats":
        return audience
    kinds = {u.conversation_type for u in users}
    if kinds == {"private"}:
        return "private"
    if kinds == {"group"}:
        return "groups"
    return "all"


@dataclass
class SendPlan:
    plan: BroadcastPlan
    users: list[User] = field(default_factory=list)
    unknown_chats: int = 0


def candidate_users(db: Session, audience: str, chat_jids: list[str] | None) -> tuple[list, int]:
    """Audience rows (opted-out ones included) and how many listed JIDs have no row."""
    if audience == "chats":
        jids = sorted(set(chat_jids or []))
        users = db.query(User).filter(User.whatsapp_jid.in_(jids)).all() if jids else []
        return users, len(jids) - len({u.whatsapp_jid for u in users})
    return audience_users(db, audience), 0


def campaign_opt_outs(db: Session, scheduled_send_id) -> set:
    if scheduled_send_id is None:
        return set()
    return {
        user_id
        for (user_id,) in db.query(ScheduledSendOptOut.user_id).filter(
            ScheduledSendOptOut.scheduled_send_id == scheduled_send_id
        )
    }


def plan_send(
    db: Session,
    *,
    scheduled_send_id,
    audience: str,
    chat_jids: list[str] | None,
    platforms: list[str],
    whitelist: str,
    now: datetime,
    cloud_deployed: bool,
) -> SendPlan:
    """The broadcast snapshot of one run, before texts."""
    users, unknown = candidate_users(db, audience, chat_jids)
    plan = plan_broadcast(
        users,
        platforms,
        whitelist_filter(whitelist),
        now,
        cloud_deployed=cloud_deployed or cloud_seen(db),
        campaign_opt_outs=campaign_opt_outs(db, scheduled_send_id),
    )
    return SendPlan(plan=plan, users=users, unknown_chats=unknown)


def chat_type_of(address: str) -> str:
    return "group" if is_group_jid(address) else "private"
