"""Per-send opt-out (``/broadcast off <number>``, the ``settings`` tool, the footer).

The global ``users.broadcast_opt_out`` (``/broadcast off``) still stops every
operator broadcast and every scheduled send; this only stops one send.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..database import ScheduledSend, ScheduledSendOptOut, User
from ..logger import logger


def find_send(db: Session, number: int) -> ScheduledSend | None:
    """A send people can still stop or restart (not cancelled)."""
    return (
        db.query(ScheduledSend)
        .filter(ScheduledSend.number == number, ScheduledSend.status != "cancelled")
        .first()
    )


def can_reach(send: ScheduledSend, user: User) -> bool:
    """Whether ``send``'s audience can include this chat (bare ``/broadcast`` lists only those)."""
    is_group = user.conversation_type == "group"
    if send.audience == "private" and is_group:
        return False
    if send.audience == "groups" and not is_group:
        return False
    if send.audience == "chats":
        jids = set(send.chat_jids or [])
        return bool({user.whatsapp_jid, user.telegram_jid} & jids)
    return True


def listed_sends(db: Session, user: User) -> list[ScheduledSend]:
    """Active or paused recurring sends whose audience can include this chat."""
    sends = (
        db.query(ScheduledSend)
        .filter(
            ScheduledSend.status.in_(("active", "paused")),
            ScheduledSend.cron_expr.isnot(None),
        )
        .order_by(ScheduledSend.number)
        .all()
    )
    return [s for s in sends if can_reach(s, user)]


def opted_out_numbers(db: Session, user: User) -> set[int]:
    return {
        number
        for (number,) in db.query(ScheduledSend.number)
        .join(ScheduledSendOptOut, ScheduledSendOptOut.scheduled_send_id == ScheduledSend.id)
        .filter(ScheduledSendOptOut.user_id == user.id)
    }


def set_send_opt_out(db: Session, send: ScheduledSend, user: User, *, opt_out: bool) -> None:
    """Stop (or restart) one send for this chat; commits."""
    row = db.get(ScheduledSendOptOut, (send.id, user.id))
    if opt_out and row is None:
        db.add(ScheduledSendOptOut(scheduled_send_id=send.id, user_id=user.id))
    elif not opt_out and row is not None:
        db.delete(row)
    else:
        return
    db.commit()
    logger.info("Scheduled send %s opt-out set to %s for user %s", send.number, opt_out, user.id)


def describe_subscriptions(db: Session, user: User) -> str:
    """Lines for bare ``/broadcast``: each listed send and this chat's state."""
    sends = listed_sends(db, user)
    if not sends:
        return ""
    stopped = opted_out_numbers(db, user)
    lines = ["Scheduled messages:"]
    for send in sends:
        state = "off" if send.number in stopped else "on"
        lines.append(f"- {send.number}: {send.name} ({state})")
    lines.append("Send /broadcast off <number> or /broadcast on <number> for just one of them.")
    return "\n".join(lines)


def move_opt_outs(db: Session, from_user_id, to_user_id) -> None:
    """A /link merge: the kept row inherits the orphan's per-send opt-outs (no commit).

    Like ``broadcast_opt_out``, which the merges OR: a stop on either account stays.
    """
    have = {
        send_id
        for (send_id,) in db.query(ScheduledSendOptOut.scheduled_send_id).filter(
            ScheduledSendOptOut.user_id == to_user_id
        )
    }
    for row in (
        db.query(ScheduledSendOptOut).filter(ScheduledSendOptOut.user_id == from_user_id).all()
    ):
        if row.scheduled_send_id not in have:
            db.add(ScheduledSendOptOut(scheduled_send_id=row.scheduled_send_id, user_id=to_user_id))
        db.delete(row)
