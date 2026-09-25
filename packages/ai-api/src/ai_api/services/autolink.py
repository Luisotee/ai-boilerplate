"""
Phone-based cross-platform auto-linking (Telegram → WhatsApp).

Telegram can ask a user to share their own phone number via a `request_contact`
keyboard button (`/linkphone` in the Telegram client). When the shared number
matches exactly one existing WhatsApp `users` row, the two rows are merged
without the `/link` code dance. Declining the button falls back to `/link`
(see `link.py`), and the merge uses the same discard-orphan policy.

SECURITY — why this is a separate, guarded path rather than a relaxation of
`get_or_create_user`:

`get_or_create_user`'s Step 3 merges by phone with no platform discriminator.
If a `tg:` JID were allowed to reach it, any phone collision would rewrite an
existing WhatsApp row's `whatsapp_jid`, silently handing one person's history,
core memory and (with the shared-group tools) group relay authority to
another. Telegram therefore skips Steps 2 and 3, and every cross-platform merge
goes through an explicit, refusing-by-default check here.

The anti-hijack check is `contact.user_id == ctx.from.id`. A Telegram user can
share ANY contact from their address book, but a shared card carries the
*contacted person's* `user_id` (or none, if they are not on Telegram).
Requiring it to equal the sender's own id is what makes the phone a claim about
the sender. It is the strongest check the Bot API offers; it is not a
cryptographic proof, so the merge is additionally refused whenever the target
is already linked or the phone is ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..database import User, is_group_jid, is_telegram_jid
from ..logger import logger

# Error codes
ERROR_GROUP_JID = "GROUP_JID"
ERROR_NOT_TELEGRAM = "NOT_TELEGRAM"
ERROR_UNVERIFIED_CONTACT = "UNVERIFIED_CONTACT"
ERROR_BAD_PHONE = "BAD_PHONE"
ERROR_NO_MATCH = "NO_MATCH"
ERROR_AMBIGUOUS = "AMBIGUOUS"
ERROR_ALREADY_LINKED = "ALREADY_LINKED"
ERROR_MERGE_FAILED = "MERGE_FAILED"

# A number can only be SHOWN to carry a country code when it is written with a
# leading "+". Telegram's `Contact.phone_number` is the account's international
# number, but clients are inconsistent about the "+", so a bare number is
# accepted too — but only when it is too long to be a national number. That
# matters because prepending "+" to a national number silently yields a
# DIFFERENT, valid-looking international one: the Brazilian national mobile
# "(11) 98765-4321" becomes "+11987654321" (country code +1), and matching that
# could link someone else's account. National numbers run up to 11 digits in
# common numbering plans (BR, CN mobiles), so a bare number needs 12+.
# Trade-off, deliberately on the safe side: a bare 11-digit international number
# (+1 NANP without its "+") is refused and the user is pointed at `/link`.
MIN_PHONE_DIGITS = 8  # ITU minimum, only reachable via an explicit "+"
MIN_BARE_PHONE_DIGITS = 12  # no "+": too long to be a national number
MAX_PHONE_DIGITS = 15  # E.164 maximum


@dataclass
class AutolinkResult:
    """Outcome of `try_autolink`."""

    success: bool
    error: str | None = None
    message: str | None = None
    #: Messages discarded from the Telegram orphan row, so the reply can be
    #: explicit about what the merge threw away.
    discarded_messages: int = 0


def normalize_shared_phone(raw: str | None) -> str | None:
    """Normalize a Telegram-shared phone to the `+E.164` form `users.phone` uses.

    Strips everything non-numeric and re-adds the "+" (`phone_from_jid` produces
    the same shape for WhatsApp rows, which is what makes them comparable).
    Returns None when the number cannot be shown to carry a country code.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    explicit_international = raw.lstrip().startswith("+")
    floor = MIN_PHONE_DIGITS if explicit_international else MIN_BARE_PHONE_DIGITS
    if not (floor <= len(digits) <= MAX_PHONE_DIGITS):
        return None
    return f"+{digits}"


def try_autolink(
    db: Session,
    telegram_user: User,
    raw_phone: str | None,
    contact_user_id: int | None,
    sender_user_id: int | None,
) -> AutolinkResult:
    """Merge `telegram_user` into the WhatsApp row owning `raw_phone`.

    Refuses unless every one of these holds:
      0. the caller is not a group;
      1. the caller really is an unlinked Telegram orphan;
      2. the contact card carries a `user_id`...
      3. ...and it is the sender's own (anti-hijack);
      4. the phone normalizes to a plausible international number;
      5. exactly one private WhatsApp row matches it;
      6. that row is not already linked to some other Telegram account.

    On success the WhatsApp row is kept (it owns the `phone` column) and the
    Telegram orphan is deleted — the same discard-orphan policy as `/link`.
    """
    # (0) Never a group. A Telegram group JID is also a `tg:` JID, so it would
    # pass (1); merging it would delete the group's transcript and bind the
    # group's id as a person's telegram_jid. The client refuses non-private
    # chats too; this is the server-side half.
    if is_group_jid(telegram_user.whatsapp_jid):
        logger.warning(f"Refused autolink for a group JID: {telegram_user.whatsapp_jid}")
        return AutolinkResult(
            success=False,
            error=ERROR_GROUP_JID,
            message="Account linking only works in a private chat.",
        )

    # (1) A pre-link Telegram orphan stores its tg: JID in whatsapp_jid and has
    # no telegram_jid yet.
    if not is_telegram_jid(telegram_user.whatsapp_jid):
        if telegram_user.telegram_jid:
            # A linked Telegram JID resolves to the kept WhatsApp row.
            return AutolinkResult(
                success=False,
                error=ERROR_ALREADY_LINKED,
                message=(
                    "Your accounts are already linked. Send `/unlink` to link a different one."
                ),
            )
        return AutolinkResult(
            success=False,
            error=ERROR_NOT_TELEGRAM,
            message="Linking by phone number is only available on Telegram.",
        )
    if telegram_user.telegram_jid:
        return AutolinkResult(
            success=False,
            error=ERROR_ALREADY_LINKED,
            message="Your accounts are already linked. Send `/unlink` to link a different one.",
        )

    # (2)+(3) The contact must be verifiably the sender's own.
    if contact_user_id is None or sender_user_id is None or contact_user_id != sender_user_id:
        logger.warning(
            "Rejected autolink: contact does not belong to the sender "
            f"(contact_user_id={contact_user_id}, sender_user_id={sender_user_id})"
        )
        return AutolinkResult(
            success=False,
            error=ERROR_UNVERIFIED_CONTACT,
            message="That contact isn't you. Tap the button to share your own phone number.",
        )

    # (4)
    phone = normalize_shared_phone(raw_phone)
    if phone is None:
        return AutolinkResult(
            success=False,
            error=ERROR_BAD_PHONE,
            message="I couldn't read that phone number. You can link manually with `/link`.",
        )

    # (5) Private WhatsApp rows only — a `tg:` row (or a group) sharing the
    # phone is not a WhatsApp identity and must never be a merge target.
    candidates = [
        u
        for u in db.query(User).filter(User.phone == phone).all()
        if not is_telegram_jid(u.whatsapp_jid)
        and not is_group_jid(u.whatsapp_jid)
        and u.conversation_type == "private"
        and u.id != telegram_user.id
    ]
    if not candidates:
        return AutolinkResult(
            success=False,
            error=ERROR_NO_MATCH,
            message=(
                "I couldn't find a WhatsApp account with that number. If you use a "
                "different number there, link with `/link` instead."
            ),
        )
    if len(candidates) > 1:
        # Should be impossible (phone is effectively per person), but merging
        # into an arbitrary one of several rows is exactly the mistake this
        # module exists to prevent.
        logger.warning(f"Refused autolink: {len(candidates)} WhatsApp rows share phone {phone}")
        return AutolinkResult(
            success=False,
            error=ERROR_AMBIGUOUS,
            message=(
                "More than one account uses that number. To be safe, please link "
                "manually with `/link`."
            ),
        )

    whatsapp_user = candidates[0]

    # (6)
    if whatsapp_user.telegram_jid:
        return AutolinkResult(
            success=False,
            error=ERROR_ALREADY_LINKED,
            message=(
                "That WhatsApp account is already linked to another Telegram account. "
                "Send `/unlink` there first."
            ),
        )

    telegram_jid_value = telegram_user.whatsapp_jid
    discarded = len(telegram_user.messages)

    try:
        whatsapp_user.telegram_jid = telegram_jid_value
        db.delete(telegram_user)  # cascade clears messages/prefs/core_memory
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            f"Failed to autolink telegram={telegram_jid_value} "
            f"to whatsapp={whatsapp_user.whatsapp_jid}"
        )
        return AutolinkResult(
            success=False,
            error=ERROR_MERGE_FAILED,
            message="Sorry, linking failed due to a database error. Please try again.",
        )

    logger.info(
        f"Auto-linked identities by phone: whatsapp={whatsapp_user.whatsapp_jid} "
        f"telegram={telegram_jid_value} (kept row={whatsapp_user.id}, "
        f"discarded {discarded} Telegram messages)"
    )
    note = (
        f"\n\nYour {discarded} earlier message(s) here on Telegram were discarded."
        if discarded
        else ""
    )
    return AutolinkResult(
        success=True,
        message=(
            "Linked successfully. Your WhatsApp and Telegram conversations now "
            f"share the same memory.{note}"
        ),
        discarded_messages=discarded,
    )
