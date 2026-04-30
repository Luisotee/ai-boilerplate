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

LINK_CODE_TTL_SECONDS = 600  # 10 minutes
_CODE_KEY = "link:code:{code}"
_USER_KEY = "link:user:{user_id}"

Platform = Literal["whatsapp", "telegram"]


@dataclass
class LinkResult:
    """Outcome of `consume_link_code`."""

    success: bool
    error: str | None = None  # Error code (see ERROR_* constants below)
    message: str | None = None  # User-facing message


# Error codes
ERROR_INVALID_CODE = "INVALID_CODE"
ERROR_SAME_PLATFORM = "SAME_PLATFORM"
ERROR_ALREADY_LINKED = "ALREADY_LINKED"
ERROR_SAME_USER = "SAME_USER"
ERROR_SOURCE_GONE = "SOURCE_GONE"
ERROR_MERGE_FAILED = "MERGE_FAILED"


def platform_for_jid(jid: str) -> Platform:
    return "telegram" if is_telegram_jid(jid) else "whatsapp"


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


def _is_already_linked(user: User) -> bool:
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
            "(one WhatsApp, one Telegram).",
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

    if _is_already_linked(source) or _is_already_linked(target):
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


def unlink(db: Session, user_id: str) -> bool:
    """Clear `telegram_jid` on `user_id`'s row. Returns True if a link was cleared.

    Returns False on commit failure (after rolling back) — callers already treat
    False as "no link to remove", which is the safe outcome on a failed write.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.telegram_jid:
        return False
    prior_jid = user.telegram_jid
    user.telegram_jid = None
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(f"Failed to unlink user {user_id}: {exc}")
        return False
    logger.info(f"Unlinked user {user.id}: dropped telegram_jid={prior_jid}")
    return True
