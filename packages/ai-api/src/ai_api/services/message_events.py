"""Apply platform revoke ("delete for everyone") and edit events to saved history.

A saved user message is found by its conversation plus the platform's own
message id (`conversation_messages.whatsapp_message_id`). Deleting or updating
that row is enough: every history reader (`get_conversation_history`,
`get_chat_history`, `search_conversation_history`, `get_group_context`) reads
the same table, and the embedding goes with the row.

Who may change a row:
- private chat: the contact is the only other party, so any event in that
  conversation applies to its user messages;
- group revoke: the row's own sender, or a confirmed group admin (WhatsApp
  admins may delete others' messages). Fails closed: anything but
  `is_group_admin is True` is not an admin;
- group edit: the row's own sender only (nobody can edit someone else's message).

Only `role="user"` rows carry a platform id, so assistant replies are never hit.
"""

from sqlalchemy.orm import Session

from ..database import ConversationMessage, find_user, is_group_jid
from ..logger import logger


def may_change(
    row_sender_jid: str | None,
    actor_jid: str | None,
    *,
    is_group: bool,
    is_group_admin: bool | None = None,
    admin_may_act: bool = False,
) -> bool:
    """Whether the actor of a revoke/edit may change a row sent by ``row_sender_jid``."""
    if not is_group:
        return True
    if actor_jid and row_sender_jid and actor_jid == row_sender_jid:
        return True
    return admin_may_act and is_group_admin is True


def _matching_rows(db: Session, whatsapp_jid: str, message_id: str, phone: str | None):
    user = find_user(db, whatsapp_jid, phone)
    if not user:
        return []
    return (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.user_id == user.id,
            ConversationMessage.whatsapp_message_id == message_id,
            ConversationMessage.role == "user",
        )
        .all()
    )


def revoke_message(
    db: Session,
    whatsapp_jid: str,
    message_id: str,
    *,
    sender_jid: str | None = None,
    is_group_admin: bool | None = None,
    phone: str | None = None,
) -> int:
    """Delete the saved copy of a message deleted for everyone. Returns rows deleted."""
    is_group = is_group_jid(whatsapp_jid)
    deleted = 0
    for row in _matching_rows(db, whatsapp_jid, message_id, phone):
        if not may_change(
            row.sender_jid,
            sender_jid,
            is_group=is_group,
            is_group_admin=is_group_admin,
            admin_may_act=True,
        ):
            logger.info(
                f"Revoke of {message_id} in {whatsapp_jid} ignored: not the sender or an admin"
            )
            continue
        db.delete(row)
        deleted += 1
    if deleted:
        db.commit()
        logger.info(f"Deleted {deleted} revoked message(s) {message_id} in {whatsapp_jid}")
    return deleted


def edit_message(
    db: Session,
    whatsapp_jid: str,
    message_id: str,
    new_text: str,
    *,
    sender_jid: str | None = None,
    phone: str | None = None,
) -> list[ConversationMessage]:
    """Rows an edit may change, with their content already rewritten (not committed).

    The caller re-embeds each `row.content` and commits. Group rows keep their
    `"<sender_name>: "` prefix, as `/chat/save` and `/chat/enqueue` wrote them.
    """
    is_group = is_group_jid(whatsapp_jid)
    changed = []
    for row in _matching_rows(db, whatsapp_jid, message_id, phone):
        if not may_change(row.sender_jid, sender_jid, is_group=is_group):
            logger.info(f"Edit of {message_id} in {whatsapp_jid} ignored: not the sender")
            continue
        row.content = f"{row.sender_name}: {new_text}" if row.sender_name else new_text
        changed.append(row)
    return changed
