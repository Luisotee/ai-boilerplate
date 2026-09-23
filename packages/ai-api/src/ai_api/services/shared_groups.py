"""
Resolve the groups a requesting user shares with the bot, per platform.

WhatsApp (Baileys) answers this authoritatively: the client lists every group
the bot is in (`groupFetchAllParticipating`) and matches the participants by
phone JID / LID, so membership is verified server-side on every call.

**Telegram cannot.** The Bot API has no member-enumeration method at all —
`getChatMember` needs a user id you already have, `getChatAdministrators`
returns only admins, and `getChat` carries no member list. So there is no way
to *discover* which groups a given user belongs to.

It is derived from stored data instead: every message of a group the bot is in
is saved with the participant's `sender_jid`, so a group where the user has
posted is a group they were provably in. That keeps the privacy boundary (a
group the user has no demonstrated presence in is never surfaced) at the cost
of completeness:

  * a group where the user has never spoken is invisible, and
  * only history from after the bot joined counts.

And the opposite failure: stored messages never expire, so a user who LEFT a
group still looks like a member. Reads accept that; the irreversible
`send_group_message` re-checks live with `getChatMember` before sending.

**WhatsApp Cloud API** webhooks carry no group context at all, so there are no
shared groups there.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import ConversationMessage, User, is_telegram_jid
from ..logger import logger
from ..whatsapp.client import SharedGroup

#: Bound on the derived list, mirroring the bound applied to the Baileys lookup —
#: an unbounded list would grow into the agent's context window.
MAX_DERIVED_GROUPS = 50


def telegram_identity(user: User) -> str | None:
    """The `tg:<chat_id>` this user is known by, or None if they have no Telegram.

    A linked user carries it in `telegram_jid`; an unlinked Telegram user stores
    it in `whatsapp_jid` (the NOT NULL primary identity column).
    """
    if user.telegram_jid:
        return user.telegram_jid
    if is_telegram_jid(user.whatsapp_jid):
        return user.whatsapp_jid
    return None


def derive_telegram_shared_groups(db: Session, user: User) -> list[SharedGroup]:
    """Telegram groups the user has posted in, derived from message authorship."""
    identity = telegram_identity(user)
    if not identity:
        return []

    rows = db.execute(
        select(User.whatsapp_jid, User.name)
        .where(
            User.conversation_type == "group",
            User.whatsapp_jid.like("tg:%"),
            select(ConversationMessage.id)
            .where(
                ConversationMessage.user_id == User.id,
                ConversationMessage.sender_jid == identity,
            )
            .exists(),
        )
        .order_by(User.whatsapp_jid)
        .limit(MAX_DERIVED_GROUPS)
    ).all()

    groups = [SharedGroup(group_jid=jid, subject=name or jid) for jid, name in rows]
    if len(groups) == MAX_DERIVED_GROUPS:
        logger.warning(
            f"Derived Telegram shared-group list hit the {MAX_DERIVED_GROUPS}-group cap "
            f"for {identity}; some groups are omitted"
        )
    return groups


def platform_of(client_id: str | None) -> str:
    """Normalize `AgentDeps.client_id`: None means the default Baileys client."""
    if client_id in ("cloud", "telegram"):
        return client_id
    return "baileys"


async def resolve_shared_groups(deps, user: User) -> list[SharedGroup]:
    """Shared groups for `user` (the requester behind `deps`), by platform.

    `user` MUST be the row loaded from `deps.user_id` — identity always comes
    from the DB, never from tool arguments. That is what enforces the privacy
    boundary.

    Raises the `WhatsAppClientError` family on the Baileys path; callers treat
    any error as "cannot check", never as an empty (or full) list.
    """
    platform = platform_of(deps.client_id)
    if platform == "cloud":
        return []
    if platform == "telegram":
        return derive_telegram_shared_groups(deps.db, user)
    if not deps.whatsapp_client:
        return []
    # A Baileys user's whatsapp_jid may be a phone JID or a LID; the client
    # matches each identifier within its own namespace.
    return await deps.whatsapp_client.get_shared_groups(
        jid=user.whatsapp_jid,
        lid=user.whatsapp_lid,
        phone=user.phone,
    )
