"""Per-user keys for the OpenAI-compatible ``/v1`` API.

A key is ``sk-`` + 256 random bits. Only its sha256 is stored (a slow hash adds
nothing for a random token of that size); the key itself is shown once, by the
operator CLI (``python -m ai_api.scripts.api_keys``). Each key belongs to one
``users`` row:

- ``create_key(db, name)`` makes a new private ``web:<uuid hex>`` user for it;
- ``create_key(db, name, jid=...)`` attaches it to an existing private
  WhatsApp/Telegram user (an integration for a known person);
- a ``web:`` user can later ``/link`` to WhatsApp/Telegram: the web row is the
  orphan, and ``move_api_keys`` repoints its keys to the kept row before the
  orphan is deleted. The WhatsApp↔Telegram merges call it too, so a key linked
  to a Telegram orphan survives that merge instead of being cascade-deleted.

Revocation sets ``revoked_at`` and is checked on every request (no cache).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import ApiKey, User, is_group_jid
from ..logger import logger
from ..surfaces import CLI_JID_PREFIX, WEB_JID_PREFIX

KEY_PREFIX = "sk-"
#: Characters of the key kept in clear for listings ("sk-" + 7).
DISPLAY_PREFIX_LEN = 10
#: ``last_used_at`` is written at most this often per key.
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass
class IssuedKey:
    key: str  # the only time the full key exists outside the client
    record: ApiKey
    user: User


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _now() -> datetime:
    # Naive UTC, like every other DateTime column.
    return datetime.utcnow()


def create_key(db: Session, name: str, jid: str | None = None) -> IssuedKey:
    """Issue a key. Raises ``ValueError`` with a user-facing reason on bad input."""
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    if jid is None:
        user = User(
            whatsapp_jid=f"{WEB_JID_PREFIX}{uuid.uuid4().hex}",
            name=name,
            conversation_type="private",
        )
        db.add(user)
        db.flush()
    else:
        user = (
            db.query(User).filter(or_(User.whatsapp_jid == jid, User.telegram_jid == jid)).first()
        )
        if user is None:
            raise ValueError(f"no user with JID {jid}")
        if user.conversation_type != "private" or is_group_jid(user.whatsapp_jid):
            raise ValueError("keys can only belong to a private chat")
        if user.whatsapp_jid.startswith(CLI_JID_PREFIX):
            raise ValueError("terminal-chat users can't have keys")

    key = generate_key()
    record = ApiKey(
        user_id=user.id,
        name=name,
        key_prefix=key[:DISPLAY_PREFIX_LEN],
        key_hash=hash_key(key),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(f"Issued API key {record.id} ({record.key_prefix}…) for user {user.id}")
    return IssuedKey(key=key, record=record, user=user)


def authenticate(db: Session, key: str | None) -> tuple[ApiKey, User] | None:
    """The active key and its user, or None. Bumps ``last_used_at`` (throttled)."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    record = (
        db.query(ApiKey)
        .filter(ApiKey.key_hash == hash_key(key), ApiKey.revoked_at.is_(None))
        .first()
    )
    if record is None:
        return None
    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None:
        return None
    now = _now()
    if record.last_used_at is None or now - record.last_used_at >= LAST_USED_RESOLUTION:
        try:
            record.last_used_at = now
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("Failed to record API key use", exc_info=True)
    return record, user


def list_keys(db: Session) -> list[tuple[ApiKey, User | None]]:
    rows = db.query(ApiKey).order_by(ApiKey.created_at).all()
    users = {u.id: u for u in db.query(User).filter(User.id.in_([r.user_id for r in rows])).all()}
    return [(r, users.get(r.user_id)) for r in rows]


def revoke_key(db: Session, key_ref: str) -> ApiKey:
    """Revoke by key id or by its display prefix. Raises ``ValueError`` if none or
    several match; revoking an already revoked key is a no-op."""
    query = db.query(ApiKey)
    try:
        matches = query.filter(ApiKey.id == uuid.UUID(key_ref)).all()
    except ValueError:
        matches = query.filter(ApiKey.key_prefix == key_ref).all()
    if not matches:
        raise ValueError(f"no key matches {key_ref}")
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} keys share prefix {key_ref}; use the id")
    record = matches[0]
    if record.revoked_at is None:
        record.revoked_at = _now()
        db.commit()
        logger.info(f"Revoked API key {record.id} ({record.key_prefix}…)")
    return record


def move_api_keys(db: Session, from_user_id, to_user_id) -> int:
    """Repoint every key of ``from_user_id`` to ``to_user_id``. Does not commit:
    it runs inside a /link merge, which commits (or rolls back) as one unit."""
    moved = (
        db.query(ApiKey)
        .filter(ApiKey.user_id == from_user_id)
        .update({ApiKey.user_id: to_user_id}, synchronize_session=False)
    )
    if moved:
        logger.info(f"Moved {moved} API key(s) from user {from_user_id} to {to_user_id}")
    return moved
