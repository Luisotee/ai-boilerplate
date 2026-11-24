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

import difflib

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import ConversationMessage, User, is_group_jid, is_telegram_jid
from ..logger import logger
from ..runtime_config import runtime_config
from ..surfaces import delivers_through_chat_client
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


def shared_group_tools_available(deps) -> bool:
    """Whether the shared-group tools can work in this run's chat.

    SHARED_GROUP_TOOLS_ENABLED is on, the chat is private (inside a group both
    tools refuse), the client isn't the Cloud API (no group context) and the run
    isn't on an app surface (``/v1`` or the terminal chat: no chat client). One
    predicate for the tools' `prepare` hook and the SHARED GROUPS instructions,
    so the prompt never describes tools the model wasn't given.
    """
    if not runtime_config.get("shared_group_tools_enabled"):
        return False
    return (
        not is_group_jid(deps.whatsapp_jid)
        and platform_of(deps.client_id) != "cloud"
        and delivers_through_chat_client(deps.client_id, deps.whatsapp_jid)
    )


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


def match_shared_group(
    groups: list[SharedGroup], group_name: str, *, strict: bool = False
) -> SharedGroup | None:
    """Resolve a free-text group name to one of the user's shared groups.

    Matches exact -> substring -> fuzzy (difflib, 0.6 cutoff) against the shared
    groups' subjects. Only ever returns a group from the provided (already
    authorized) list, so it can't surface or target a group the user doesn't
    share with the bot. Returns None when nothing matches or the name is empty.

    ``strict`` (for irreversible actions like sending): no fuzzy matching, and a
    substring that matches more than one group counts as no match — so the
    caller asks the user to disambiguate instead of guessing the wrong group.
    """
    # An empty target makes ``target in subject`` true for every group, which
    # would otherwise silently pick the first one — never match on it.
    target = group_name.strip().lower()
    if not target:
        return None

    exact = [g for g in groups if g.subject.lower() == target]
    if exact:
        # Two groups with the very same name are ambiguous for a send, too.
        if strict and len(exact) > 1:
            return None
        return exact[0]

    subs = [g for g in groups if target in g.subject.lower()]
    if subs:
        if strict:
            return subs[0] if len(subs) == 1 else None
        return subs[0]

    if not strict:
        close = difflib.get_close_matches(
            target, [g.subject.lower() for g in groups], n=1, cutoff=0.6
        )
        if close:
            return next(g for g in groups if g.subject.lower() == close[0])

    return None


async def requester_group_status(
    client, platform: str, user: User, group_jid: str
) -> tuple[bool, bool | None]:
    """``(is_member, is_admin)`` of ``user`` in ``group_jid``, asked live of the chat client.

    Baileys: the shared-groups lookup (membership resolved server-side) and
    its ``isAdmin``. Telegram: ``getChatMember`` through ``group_membership``.
    ``is_admin`` is None when the client didn't say (an older client): callers
    treat that as "can't confirm", never as admin. Raises on a lookup error,
    and ``ValueError`` where no live answer exists (Cloud API, no Telegram id).
    """
    if platform == "baileys":
        groups = await client.get_shared_groups(
            jid=user.whatsapp_jid, lid=user.whatsapp_lid, phone=user.phone
        )
        match = next((g for g in groups if g.group_jid == group_jid), None)
        if match is None:
            return False, False
        return True, match.is_admin
    if platform == "telegram":
        identity = telegram_identity(user)
        if not identity:
            raise ValueError("no Telegram identity for this user")
        membership = await client.group_membership(group_jid, identity)
        if not membership.is_member:
            return False, False
        return True, membership.is_admin
    raise ValueError(f"no live group membership on {platform}")
