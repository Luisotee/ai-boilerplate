"""
Cross-platform identity linking via one-time codes.

A user runs `/link` on one platform (WhatsApp or Telegram) to obtain a 6-digit
code, then `/link <code>` on the other platform within 10 minutes to merge the
two `users` rows into one. After linking, both platforms share the same
conversation history, core memory, and preferences.

Discard-orphan policy: when a merge happens, the orphan row's pre-link
conversation messages, preferences, and core memory are dropped (cascade
delete). Only future messages are shared. This is documented user-facing in
the `/link` reply text.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy.orm import Session

from ..database import User, is_telegram_jid
from ..logger import logger
from ..surfaces import WEB_JID_PREFIX, is_cli_jid
from .api_keys import move_api_keys
from .bug_reports import scrub_user_reports

LINK_CODE_TTL_SECONDS = 600  # 10 minutes
_CODE_KEY = "link:code:{code}"
_USER_KEY = "link:user:{user_id}"

#: "web" is a `web:` row of the OpenAI-compatible API (services/api_keys.py).
#: It is always the orphan of a merge: the WhatsApp/Telegram row is kept and the
#: web row's keys move to it.
Platform = Literal["whatsapp", "telegram", "web"]


@dataclass
class LinkResult:
    """Outcome of `consume_link_code`."""

    success: bool
    error: str | None = None  # Error code (see ERROR_* constants below)
    message: str | None = None  # User-facing message


@dataclass
class UnlinkResult:
    """Outcome of `unlink`. `cleared` is True only after a successful commit;
    `db_error` distinguishes a commit failure from "no link to remove" so the
    caller can render distinct user-facing messages."""

    cleared: bool
    db_error: bool = False


# Error codes
ERROR_INVALID_CODE = "INVALID_CODE"
ERROR_SAME_PLATFORM = "SAME_PLATFORM"
ERROR_ALREADY_LINKED = "ALREADY_LINKED"
ERROR_SAME_USER = "SAME_USER"
ERROR_SOURCE_GONE = "SOURCE_GONE"
ERROR_MERGE_FAILED = "MERGE_FAILED"


def platform_for_jid(jid: str) -> Platform:
    if jid.startswith(WEB_JID_PREFIX):
        return "web"
    return "telegram" if is_telegram_jid(jid) else "whatsapp"


_PLATFORM_NAMES = {"whatsapp": "WhatsApp", "telegram": "Telegram", "web": "the web app"}


def _generate_code() -> str:
    """Cryptographically random 6-digit code (zero-padded)."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def generate_link_code(redis: Redis, user_id: str, platform: Platform) -> str:
    """Create a one-time link code for `user_id`. Returns the 6-digit code.

    Overwrites any existing code for the same user (last-write-wins).
    """
    # Invalidate any prior code for this user so old codes can't be reused.
    prior = await redis.get(_USER_KEY.format(user_id=user_id))
    if prior is not None:
        prior_code = prior.decode() if isinstance(prior, bytes) else prior
        await redis.delete(_CODE_KEY.format(code=prior_code))

    code = _generate_code()
    payload = f"{user_id}|{platform}"
    await redis.setex(_CODE_KEY.format(code=code), LINK_CODE_TTL_SECONDS, payload)
    await redis.setex(_USER_KEY.format(user_id=user_id), LINK_CODE_TTL_SECONDS, code)
    logger.info(f"Generated link code for user {user_id} ({platform})")
    return code


def is_already_linked(user: User) -> bool:
    """A user is already linked when both platform identities are populated."""
    return (
        bool(user.whatsapp_jid)
        and bool(user.telegram_jid)
        and not is_telegram_jid(user.whatsapp_jid)
    )


async def consume_link_code(
    db: Session,
    redis: Redis,
    code: str,
    current_user_id: str,
    current_platform: Platform,
) -> LinkResult:
    """Validate `code`, merge the source row into the target row, delete code.

    `current_user_id` is the row that just ran `/link <code>`. The code's owner
    (set by `generate_link_code`) is the source. The kept row is whichever
    side is WhatsApp-primary (it carries the `phone` column). The Telegram
    side's `tg:<chat_id>` JID moves to the kept row's `telegram_jid` column,
    and the orphan row is deleted (cascade clears its messages/prefs/memory).
    """
    raw = await redis.get(_CODE_KEY.format(code=code))
    if raw is None:
        return LinkResult(
            success=False,
            error=ERROR_INVALID_CODE,
            message="That link code is invalid or has expired. "
            "Run `/link` on the other platform to get a fresh code.",
        )

    payload = raw.decode() if isinstance(raw, bytes) else raw
    try:
        source_user_id, source_platform = payload.split("|", 1)
    except ValueError:
        # Corrupt payload — treat as invalid.
        await redis.delete(_CODE_KEY.format(code=code))
        return LinkResult(
            success=False,
            error=ERROR_INVALID_CODE,
            message="That link code is malformed. Try generating a new one.",
        )

    if source_user_id == current_user_id:
        return LinkResult(
            success=False,
            error=ERROR_SAME_USER,
            message="You already used this code on the same platform. "
            "Run `/link` on the OTHER platform to get a code, "
            "then enter it here.",
        )

    if source_platform == current_platform:
        return LinkResult(
            success=False,
            error=ERROR_SAME_PLATFORM,
            message="Both sides of a link must be on different platforms "
            "(WhatsApp, Telegram or the web app).",
        )

    source = db.query(User).filter(User.id == source_user_id).first()
    target = db.query(User).filter(User.id == current_user_id).first()
    if source is None or target is None:
        # Source/target deleted between code issue and consumption.
        await redis.delete(_CODE_KEY.format(code=code))
        await redis.delete(_USER_KEY.format(user_id=source_user_id))
        return LinkResult(
            success=False,
            error=ERROR_SOURCE_GONE,
            message="The user that generated this code no longer exists. "
            "Generate a new code and try again.",
        )

    if "web" in (source_platform, current_platform):
        web_user, kept = (source, target) if source_platform == "web" else (target, source)
        result = _merge_web_user(db, web_user, kept)
        if result.success:
            await redis.delete(_CODE_KEY.format(code=code))
            await redis.delete(_USER_KEY.format(user_id=source_user_id))
        return result

    if is_already_linked(source) or is_already_linked(target):
        return LinkResult(
            success=False,
            error=ERROR_ALREADY_LINKED,
            message="One of these accounts is already linked. "
            "Run `/unlink` on it first, then re-link.",
        )

    # Identify whatsapp side and telegram side.
    if source_platform == "whatsapp":
        whatsapp_user, telegram_user = source, target
    else:
        whatsapp_user, telegram_user = target, source

    # The Telegram orphan row currently stores its tg: JID in whatsapp_jid.
    telegram_jid_value = telegram_user.whatsapp_jid

    # Merge: keep the WhatsApp row, attach Telegram identity, drop the orphan.
    # Rollback on commit failure (e.g. unique-constraint race on telegram_jid)
    # keeps the route-scoped session clean for any downstream work.
    try:
        whatsapp_user.telegram_jid = telegram_jid_value
        # An opt-out on either side survives the merge: /broadcast off on
        # Telegram followed by /link must not re-subscribe the person.
        whatsapp_user.broadcast_opt_out = bool(
            whatsapp_user.broadcast_opt_out or telegram_user.broadcast_opt_out
        )
        # The orphan's messages are discarded, so are its reports' copies of them.
        scrub_user_reports(db, telegram_user.id)
        # A web key linked to the Telegram row follows it (else the FK cascade
        # would delete the key with the orphan).
        move_api_keys(db, telegram_user.id, whatsapp_user.id)
        db.delete(telegram_user)  # cascade clears messages/prefs/core_memory
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(
            f"Failed to merge link for source={source_user_id} target={current_user_id}: {exc}"
        )
        return LinkResult(
            success=False,
            error=ERROR_MERGE_FAILED,
            message="Sorry, linking failed due to a database error. Please try again.",
        )

    # Clean up Redis state.
    await redis.delete(_CODE_KEY.format(code=code))
    await redis.delete(_USER_KEY.format(user_id=source_user_id))

    logger.info(
        f"Linked identities: whatsapp={whatsapp_user.whatsapp_jid} "
        f"telegram={telegram_jid_value} (kept row={whatsapp_user.id})"
    )
    return LinkResult(
        success=True,
        message="Linked successfully. Your WhatsApp and Telegram conversations now share the same memory.",
    )


def _merge_web_user(db: Session, web_user: User, kept: User) -> LinkResult:
    """Fold a `web:` row into a WhatsApp/Telegram row (discard-orphan policy).

    The kept row may already be a WhatsApp↔Telegram pair: a web key only points
    at a row, it adds no identity column, so "already linked" doesn't apply.
    The web row's keys move to the kept row, then the orphan is deleted with its
    messages, preferences and core memory.
    """
    if not web_user.whatsapp_jid.startswith(WEB_JID_PREFIX) or kept.whatsapp_jid.startswith(
        WEB_JID_PREFIX
    ):
        return LinkResult(
            success=False,
            error=ERROR_SAME_PLATFORM,
            message="Both sides of a link must be on different platforms "
            "(WhatsApp, Telegram or the web app).",
        )
    try:
        kept.broadcast_opt_out = bool(kept.broadcast_opt_out or web_user.broadcast_opt_out)
        scrub_user_reports(db, web_user.id)
        move_api_keys(db, web_user.id, kept.id)
        db.delete(web_user)  # cascade clears messages/prefs/core_memory
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(f"Failed to merge web user {web_user.id} into {kept.id}: {exc}")
        return LinkResult(
            success=False,
            error=ERROR_MERGE_FAILED,
            message="Sorry, linking failed due to a database error. Please try again.",
        )
    logger.info(f"Linked web user {web_user.whatsapp_jid} into row {kept.id}")
    other = _PLATFORM_NAMES[platform_for_jid(kept.whatsapp_jid)]
    return LinkResult(
        success=True,
        message=f"Linked successfully. The web app now shares your {other} "
        "conversation and memory.",
    )


async def link_command_reply(
    db: Session,
    open_redis,
    user: User,
    whatsapp_jid: str,
    conversation_type: str,
    parts: list[str],
) -> str:
    """Reply text for `/link`, `/link <code>` or `/unlink` (``parts[0]``).

    Shared by `/chat/enqueue` and the `/v1` API. ``open_redis`` is a callable
    returning an async context manager that yields a Redis client (the caller's
    `get_redis_client`); it is only opened by `/link`.
    """
    command = parts[0].lower()
    if conversation_type == "group":
        return "Linking is only available in private chats."
    if is_cli_jid(whatsapp_jid):
        return "Linking isn't available in the terminal chat."

    if command == "/unlink":
        unlink_result = unlink(db, str(user.id))
        if unlink_result.db_error:
            return "Sorry, unlinking failed due to a database error. Please try again."
        if unlink_result.cleared:
            return (
                "Unlinked. Future messages on the other platform will start a fresh conversation."
            )
        return "No active link to remove."

    platform = platform_for_jid(whatsapp_jid)
    async with open_redis() as redis:
        if len(parts) >= 2:
            code = parts[1].strip()
            result = await consume_link_code(db, redis, code, str(user.id), platform)
            return result.message or ("Linked successfully." if result.success else "Link failed.")
        if is_already_linked(user):
            # Short-circuit: a fully-linked user generating a fresh code
            # produces a code that can't be redeemed (both JIDs resolve
            # to the same row, so consume_link_code hits ERROR_SAME_USER
            # with a misleading message). Tell them directly instead.
            return (
                "Your accounts are already linked. "
                "Run `/unlink` first if you want to re-link. To add the web app, "
                "send `/link` there and enter its code here."
            )
        code = await generate_link_code(redis, str(user.id), platform)
    if platform == "web":
        where = "On WhatsApp or Telegram"
        note = (
            "Note: this web conversation's history will be discarded; the app then "
            "shares your WhatsApp/Telegram conversation and memory."
        )
    else:
        other = "Telegram" if platform == "whatsapp" else "WhatsApp"
        where = f"On {other} (or in the web app)"
        note = "Note: any prior conversation history on the other platform will be discarded."
    response_text = (
        f"Your link code is `{code}`.\n\n{where}, send `/link {code}` within 10 minutes.\n\n{note}"
    )
    if platform == "telegram":
        response_text += (
            "\n\nTip: on Telegram you can also send `/linkphone` and share "
            "your number instead of using a code."
        )
    return response_text


def unlink(db: Session, user_id: str) -> UnlinkResult:
    """Clear `telegram_jid` on `user_id`'s row.

    Returns `UnlinkResult(cleared=True)` after a successful commit,
    `UnlinkResult(cleared=False)` when there's no link to remove,
    and `UnlinkResult(cleared=False, db_error=True)` when the commit failed.
    Distinguishing the last case lets the caller surface a real error instead
    of a misleading "no link to remove".
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.telegram_jid:
        return UnlinkResult(cleared=False)
    prior_jid = user.telegram_jid
    user.telegram_jid = None
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(f"Failed to unlink user {user_id}: {exc}")
        return UnlinkResult(cleared=False, db_error=True)
    logger.info(f"Unlinked user {user.id}: dropped telegram_jid={prior_jid}")
    return UnlinkResult(cleared=True)
