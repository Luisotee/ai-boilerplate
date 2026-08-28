"""WHITELIST_PHONES parsing and matching.

The Python mirror of ``src/utils/whitelist.ts`` in each of the three TS clients
(which are byte-identical copies of one another). Keep the two in sync — and
keep this side no more permissive than the TS side: the clients are the primary
gate and this is the defence-in-depth backstop, so a value the gate rejects must
never be accepted here.

An entry is matched as EITHER a phone number OR a verbatim chat id. Under
Baileys v7 a chat is usually LID-addressed, so its jid is an anonymized ``@lid``
whose digits are NOT a phone — a bare-phone entry can only match such a chat via
the resolved E.164 that the client sends alongside it.

Pure by design (no settings, no DB, no runtime_config): ``routes/chat.py`` owns
reading the effective value, and ``routes/admin.py`` validates a proposed one.
"""

import re
from typing import NamedTuple

PHONE_JID_SUFFIX = "@s.whatsapp.net"
GROUP_JID_SUFFIX = "@g.us"

# Only cosmetic separators — never "all non-digits", which would strip the sign
# off "tg:-100…" and leak chat ids into the phone set.
#
# The whitespace half is the ECMAScript ``\s`` set written out literally rather
# than as ``\s``, because the two engines disagree: Python's ``\s`` omits U+FEFF
# and adds U+0085 / U+001C–001F, while ``re.ASCII`` would drop NBSP and
# U+2000–200A. Either way one side accepts an entry the other rejects, so both
# pin the same 25 codepoints instead.
PHONE_PUNCTUATION = re.compile("[\t\n\v\f\r    -     　﻿\\-().]")
# re.ASCII on both: JS "\d" is [0-9] unconditionally, Python's is Unicode-aware.
DEVICE_SUFFIX = re.compile(r":\d+@", re.ASCII)
_PHONE_DIGITS = re.compile(r"\d{5,}", re.ASCII)


class Whitelist(NamedTuple):
    """A whitelist split into the two ways an entry can match."""

    ids: frozenset[str]
    phones: frozenset[str]
    size: int


def phone_digits(value: str) -> str | None:
    """Digits of a phone-shaped value, or None if it isn't one."""
    digits = PHONE_PUNCTUATION.sub("", value)
    # A SINGLE leading "+", matching JS's /^\+/ — str.lstrip("+") would eat a
    # run of them and accept "++49…", which the TS gate rejects.
    if digits.startswith("+"):
        digits = digits[1:]
    # fullmatch, not match(r"^…$"): Python's "$" also matches BEFORE a trailing
    # newline while JS's does not. The punctuation class currently masks that
    # (it strips \n, in both engines), but narrowing the class would make it
    # live — and then the backstop would accept what the gate rejects.
    return digits if _PHONE_DIGITS.fullmatch(digits) else None


def parse_whitelist(raw: str) -> Whitelist:
    """Parse the comma-separated whitelist into ids + phone digits."""
    ids: set[str] = set()
    phones: set[str] = set()
    size = 0

    for entry in raw.split(","):
        trimmed = entry.strip()
        if not trimmed:
            continue
        size += 1
        # Every entry is kept verbatim, so nothing an operator already relies on
        # can stop matching.
        ids.add(trimmed)

        if trimmed.startswith("tg:"):
            continue

        # A device-suffixed entry ("…:50@s.whatsapp.net") would otherwise be
        # dead: matching strips the suffix off the incoming jid, so the entry
        # could never equal it. Normalize the entry the same way — BEFORE the
        # @s.whatsapp.net strip, since the suffix sits in front of the "@".
        # Additive: the verbatim form stays in `ids`. count=1 mirrors JS's
        # non-global String.replace.
        normalized = DEVICE_SUFFIX.sub("@", trimmed, count=1)
        if normalized != trimmed:
            ids.add(normalized)

        local = (
            normalized[: -len(PHONE_JID_SUFFIX)]
            if normalized.endswith(PHONE_JID_SUFFIX)
            else normalized
        )
        # Any residual "@" means a non-phone scheme (@lid, @g.us, future ones).
        if "@" in local:
            continue

        digits = phone_digits(local)
        if digits:
            phones.add(digits)

    return Whitelist(frozenset(ids), frozenset(phones), size)


def is_whitelisted(wl: Whitelist, chat_jid: str, phone: str | None = None) -> bool:
    """Whether a conversation is whitelisted. True if the whitelist is empty.

    ``chat_jid`` is the CONVERSATION's jid and ``phone`` its E.164 number if the
    client could resolve one — never a group participant's.
    """
    if wl.size == 0:
        return True

    jid = DEVICE_SUFFIX.sub("@", chat_jid, count=1)
    if chat_jid in wl.ids or jid in wl.ids:
        return True

    local, sep, _ = jid.partition("@")
    if sep and local:
        # Deliberately namespace-blind, because this IS the pre-split matcher:
        # the whole check used to be `jid.split("@")[0] in whitelist`. It keeps
        # a bare "120363…" admitting its group and a bare LID admitting its
        # chat on existing installs — the same code path for both, since
        # nothing distinguishes a bare group id from a bare phone.
        if local in wl.ids:
            return True
        # Normalized digits, though, are only ever a phone. Consulting them for
        # an @lid / @g.us / @broadcast local part would let a phone entry admit
        # an unrelated namespace that merely shares digits — phone JIDs only.
        if jid.endswith(PHONE_JID_SUFFIX) and local in wl.phones:
            return True

    # The `phone` argument is never consulted for a group: a participant's
    # whitelisted phone must never admit the whole group. Independent of the
    # guard above — this clause reads the caller-supplied E.164, that one reads
    # the jid's own local part.
    if phone and not jid.endswith(GROUP_JID_SUFFIX):
        digits = phone_digits(phone)
        if digits and digits in wl.phones:
            return True

    return False
