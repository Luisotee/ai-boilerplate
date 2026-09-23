"""Shared DB-session helpers for agent tools."""

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def safe_rollback(db: Session) -> None:
    """Roll back the run's shared session after a failed tool call, never raising.

    All tools in one agent run share a single session: a failed flush/commit leaves
    it in a failed transaction, and every later tool call would then hit
    ``PendingRollbackError``. The rollback itself can fail too (e.g. the connection
    is gone) — that must not replace the tool's generic error reply with a crash.
    """
    try:
        db.rollback()
    except Exception:
        logger.warning("Session rollback after tool error failed", exc_info=True)
