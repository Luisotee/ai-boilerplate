"""
Shared-group tools: read from, and relay into, groups the user shares with the bot.

Both tools only work in a PRIVATE chat and only ever touch a group that BOTH the
bot and the requesting user are in. The privacy boundary:

  * the requester's identity is read from THEIR DB row (`deps.user_id`), never
    from tool arguments — the model cannot ask about someone else;
  * membership is re-resolved on every call (Baileys: live, server-side via
    `POST /whatsapp/shared-groups`; Telegram: derived from stored authorship,
    plus a live `getChatMember` check right before any send);
  * a group name from the model is only ever matched AGAINST that verified list
    (`_match_shared_group`), so it cannot reach an arbitrary group.

Off by default (`SHARED_GROUP_TOOLS_ENABLED`, hot via /admin): when disabled the
`prepare` hook hides both tools from the model entirely. Group transcripts read
here enter a private conversation's model context — i.e. they are sent to the
LLM provider again, in a new context — which is the trade-off an operator opts
into.
"""

import difflib
from datetime import UTC, datetime, timedelta

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ...database import User, get_conversation_messages, is_group_jid
from ...formatting import markdown_to_whatsapp
from ...logger import logger
from ...rag.conversation import format_conversation_results, format_transcript
from ...rag.conversation import search_conversation_history as search_conversation_fn
from ...runtime_config import runtime_config
from ...services.shared_groups import platform_of, resolve_shared_groups, telegram_identity
from ...whatsapp import WhatsAppNotConnectedError
from ...whatsapp.client import SharedGroup
from ..core import AgentDeps, agent
from ._db import safe_rollback

# Caps only an *explicitly requested* message count, so the agent can't ask for
# an unbounded window. When no limit is passed, get_conversation_messages falls
# back to its own MAX_FLAT_MESSAGES safety cap.
MAX_TRANSCRIPT_LIMIT = 100

CLOUD_UNSUPPORTED = (
    "Group features aren't available on this WhatsApp number: the WhatsApp Cloud API "
    "doesn't give me any group context, so I can't see or post into groups here."
)


async def shared_group_tools_enabled(
    ctx: RunContext[AgentDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """`prepare` hook: hide both tools unless SHARED_GROUP_TOOLS_ENABLED is on."""
    return tool_def if runtime_config.get("shared_group_tools_enabled") else None


def _match_shared_group(
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


def _requester(deps: AgentDeps) -> User | None:
    """The requesting user's own row — the ONLY source of their identity."""
    return deps.db.query(User).filter(User.id == deps.user_id).first()


def _group_row(db, group_jid: str) -> User | None:
    """A group's conversation row, looked up WITHOUT creating one.

    A read must have no side effects, and a group with no row simply has no
    stored messages.
    """
    return db.query(User).filter(User.whatsapp_jid == group_jid).first()


@agent.tool(prepare=shared_group_tools_enabled)
async def get_group_context(
    ctx: RunContext[AgentDeps],
    group_name: str | None = None,
    search_query: str | None = None,
    limit: int | None = None,
    since_hours: int | None = None,
) -> str:
    """
    Read recent activity from groups you and the user share.

    ONLY works in private (1:1) chats. You can ONLY see groups where BOTH you and
    this user are members — never reveal or imply knowledge of any other group.

    On Telegram the list is INCOMPLETE by nature: the Telegram Bot API cannot
    enumerate a group's members, so a shared group only becomes known once the
    user has posted in it while you were present. If the user names a group you
    cannot find, say you have no record of it rather than asserting it does not
    exist or that they are not in it.

    Usage:
    - No group_name: list the groups you share with the user.
    - group_name: read a recent transcript of that group.
    - group_name AND search_query: semantic search within that group's history.

    Transcript mode is chronological. Narrow it with `limit` (last N messages)
    and/or `since_hours` (only messages from the last N hours, e.g. 24 = last
    day); the two combine. Use search_query when looking for a topic.

    Args:
        ctx: Run context with db, user_id, whatsapp_jid and whatsapp_client.
        group_name: Group name/subject to read. Omit to list shared groups.
        search_query: Optional topic to semantically search within the group.
        limit: Optional cap on how many recent messages to return (transcript mode).
        since_hours: Optional window — only messages from the last N hours.

    Returns:
        A list of shared groups, a group transcript, search results, or an
        explanatory message.
    """
    deps = ctx.deps
    logger.info(
        f"TOOL CALLED: get_group_context group_name={group_name!r} "
        f"search_query={search_query!r} jid={deps.whatsapp_jid}"
    )

    # In a group the bot already has that group's context; this is for 1:1 only.
    if is_group_jid(deps.whatsapp_jid):
        return "This only works in a private chat with me, not inside a group."
    if platform_of(deps.client_id) == "cloud":
        return CLOUD_UNSUPPORTED

    try:
        user = _requester(deps)
        if not user:
            return "I couldn't identify your account."

        groups = await resolve_shared_groups(deps, user)
        if not groups:
            return (
                "I couldn't find any group that you and I are both in. "
                "I can only tell you about groups we share."
            )

        if not group_name:
            names = "\n".join(f"- {g.subject}" for g in groups)
            return f"Groups we share:\n{names}\n\nWhich one would you like to know about?"

        match = _match_shared_group(groups, group_name)
        if not match:
            available = ", ".join(g.subject for g in groups)
            return (
                f'I couldn\'t find a group we share called "{group_name}". '
                f"The groups we share are: {available}."
            )

        group_user = _group_row(deps.db, match.group_jid)
        if not group_user:
            return f'I don\'t have any stored messages from "{match.subject}" yet.'

        if search_query:
            # Don't silently substitute a recent transcript for a topic search —
            # the agent would summarize it as if the search had run.
            if not deps.embedding_service:
                return (
                    "Topic search isn't available right now. I can show you the recent "
                    f'messages from "{match.subject}" instead, if you like.'
                )
            query_embedding = await deps.embedding_service.generate(
                search_query, task_type="RETRIEVAL_QUERY"
            )
            if not query_embedding:
                return "Failed to generate a search embedding. Please try again."

            results = await search_conversation_fn(
                db=deps.db,
                query_embedding=query_embedding,
                user_id=str(group_user.id),
                query_text=search_query,
                exclude_message_ids=[],
            )
            if not results:
                return f'I found nothing about "{search_query}" in "{match.subject}".'
            formatted = format_conversation_results(results)
            return f'In "{match.subject}", about "{search_query}":\n\n{formatted}'

        effective_limit = None
        if limit is not None:
            effective_limit = max(1, min(limit, MAX_TRANSCRIPT_LIMIT))
        since = None
        if since_hours is not None and since_hours > 0:
            # The timestamp column is naive UTC.
            since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=since_hours)

        messages = get_conversation_messages(
            deps.db, str(group_user.id), limit=effective_limit, since=since
        )
        if not messages:
            if since is not None:
                return f'There are no messages from "{match.subject}" in the last {since_hours}h.'
            return f'I don\'t have any stored messages from "{match.subject}" yet.'

        transcript = format_transcript(
            messages,
            assistant_label=runtime_config.get("bot_name"),
            with_timestamps=since is not None,
        )
        logger.info(f"get_group_context: {len(messages)} messages from '{match.subject}'")
        return f'Recent activity in "{match.subject}":\n\n{transcript}'

    except WhatsAppNotConnectedError:
        # Needs re-pairing, not a retry. Still fail-closed: no group data.
        logger.warning("get_group_context: WhatsApp client not connected", exc_info=True)
        return (
            "I can't reach the groups right now because WhatsApp is disconnected. "
            "Please let the bot's operator know."
        )
    except Exception:
        # Shared DB session: roll back so later tools don't hit PendingRollbackError.
        safe_rollback(deps.db)
        logger.error("get_group_context failed", exc_info=True)
        return "I couldn't look up the groups right now. Please try again shortly."


@agent.tool(prepare=shared_group_tools_enabled)
async def send_group_message(
    ctx: RunContext[AgentDeps],
    group_name: str,
    message: str,
    sender_name: str | None = None,
) -> str:
    """
    Send a message on the user's behalf into a group you both share.

    ONLY works in private (1:1) chats, and can ONLY target a group where BOTH you
    and this user are members. The sent message is ALWAYS prefixed with an
    attribution naming the requester (name and/or phone), so the group knows you
    are relaying for someone — never anonymously; if the requester can't be
    identified the tool refuses and asks for a name.

    Group matching is strict: the name must match exactly or be an unambiguous
    partial. It won't guess between similar names or fix typos — if the user's
    reference is partial or ambiguous, list the shared groups (get_group_context)
    and confirm the EXACT name first.

    Before calling this tool you MUST confirm with the user the exact target group
    and the exact message text, and wait for their explicit yes — a group message
    cannot be unsent.

    Args:
        ctx: Run context with db, user_id, whatsapp_jid and whatsapp_client.
        group_name: Name/subject of the shared group to send to.
        message: The message text to relay into the group.
        sender_name: The requester's name for the attribution line (from memory or
            this conversation). If you don't know it, ask before sending.

    Returns:
        A confirmation that the message was sent, or an explanatory message.
    """
    deps = ctx.deps
    logger.info(
        f"TOOL CALLED: send_group_message group_name={group_name!r} jid={deps.whatsapp_jid}"
    )

    if is_group_jid(deps.whatsapp_jid):
        return "I can only relay a message into a group from a private chat with you."
    if platform_of(deps.client_id) == "cloud":
        return CLOUD_UNSUPPORTED
    if not deps.whatsapp_client:
        return "I can't send to groups right now (chat client unavailable)."
    if not message.strip():
        return "I can't send an empty message. What should I send?"
    if not group_name.strip():
        return "Which group should I send it to?"

    try:
        user = _requester(deps)
        if not user:
            return "I couldn't identify your account."

        groups = await resolve_shared_groups(deps, user)
        if not groups:
            return (
                "I couldn't find any group that you and I are both in. "
                "I can only send to groups we share."
            )

        # Only ever a group from the verified list, matched strictly: a wrong
        # send can't be undone.
        match = _match_shared_group(groups, group_name, strict=True)
        if not match:
            available = ", ".join(g.subject for g in groups)
            return (
                f'I\'m not sure which group you mean by "{group_name}". '
                f"The groups we share are: {available}. Which one?"
            )

        # Telegram: re-verify membership live before an irreversible send. The
        # candidate list is derived from stored messages, which never expire, so
        # a user removed from a group would otherwise keep posting into it.
        # FAIL-CLOSED: any lookup error refuses the send.
        if platform_of(deps.client_id) == "telegram":
            tg_identity = telegram_identity(user)
            if not tg_identity:
                return "I couldn't confirm your Telegram identity, so I won't send this."
            try:
                still_member = await deps.whatsapp_client.is_group_member(
                    match.group_jid, tg_identity
                )
            except Exception:
                logger.error(
                    f"send_group_message: membership check failed for '{match.subject}'",
                    exc_info=True,
                )
                return (
                    "I couldn't confirm that you're still in that group, so I didn't "
                    "send anything. Please try again shortly."
                )
            if not still_member:
                logger.warning(
                    f"send_group_message: refused — requester no longer in '{match.subject}'"
                )
                return (
                    f'You\'re no longer a member of "{match.subject}", '
                    "so I can't send messages there for you."
                )

        # Attribution is mandatory: the group must see who asked. The name is
        # cosmetic (model- or DB-supplied); the phone is server-derived from the
        # requester's row and may be empty. Neither known -> refuse.
        name = (sender_name or user.name or "").strip()
        phone = (user.phone or "").strip()
        if name and phone:
            label = f"{name} ({phone})"
        elif name or phone:
            label = name or phone
        else:
            return "I need to know who the message is from before I send it. What's your name?"

        body = markdown_to_whatsapp(message)
        text = f"📩 {label} asked me to send this message:\n\n{body}"

        result = await deps.whatsapp_client.send_text(phone_number=match.group_jid, text=text)
        logger.info(f"send_group_message: sent to '{match.subject}' (ID: {result.message_id})")
        return f'I sent your message to "{match.subject}".'

    except WhatsAppNotConnectedError:
        logger.warning("send_group_message: WhatsApp client not connected", exc_info=True)
        return (
            "I can't send right now because WhatsApp is disconnected. "
            "Please let the bot's operator know."
        )
    except Exception:
        safe_rollback(deps.db)
        logger.error("send_group_message failed", exc_info=True)
        return "I couldn't send the message to the group right now. Please try again shortly."
