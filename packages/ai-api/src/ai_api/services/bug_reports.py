"""Bug reports: filed by the agent, or captured automatically from failures.

Two ways in:

- ``file_report`` — the ``report_bug`` agent tool. A user asked to report
  something (``source="user"``), or the agent decided to on its own: the user
  complained that an answer was wrong, it noticed its own mistake, a tool kept
  failing (``source="agent"``). Capped per user per hour
  (``bug_reports_per_user_per_hour``), since anyone who can talk to the bot can
  ask it to file one.
- ``record_auto_report`` — the failure paths: both models failing
  (``model_error``), a crashed chat job (``job_crash``), a PDF that permanently
  failed to parse (``pdf_failure``). These are deduplicated: each carries a
  ``fingerprint`` (source + error type + where in our code it was raised, no
  line numbers), and a repeat of an OPEN report only bumps its ``occurrences``
  and ``last_seen_at``. A resolved or ignored report never absorbs a repeat, so
  a regression shows up as a new row.

Every report snapshots the chat's last few messages (``context``) so an
operator can see what happened; ``clear_user_context`` drops them again when
the user runs ``/clean``. Reports are read through ``/admin/bug-reports``.
"""

import hashlib
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import PurePath

from sqlalchemy import func, null
from sqlalchemy.orm import Session

from ..database import BugReport, SessionLocal, get_conversation_messages
from ..logger import logger
from ..runtime_config import runtime_config

CONTEXT_MESSAGES = 10
CONTEXT_MESSAGE_CHARS = 1000
TITLE_MAX = 200
DESCRIPTION_MAX = 4000
ERROR_DETAIL_MAX = 8000

#: Package directory name used to find "our" frames in a traceback.
_PACKAGE = "ai_api"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_type_name(exc: BaseException) -> str:
    """``ModelHTTPError(404)``, ``FallbackExceptionGroup[ModelHTTPError(401), …]``.

    The HTTP status is part of the type: a 401 (bad key) and a 404 (bad model
    name) are different bugs. An exception group lists its members, sorted and
    deduplicated, so the order the models failed in doesn't matter.
    """
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        name = f"{name}({status})"
    if isinstance(exc, BaseExceptionGroup):
        inner = sorted({error_type_name(e) for e in exc.exceptions})
        name = f"{name}[{', '.join(inner)}]"
    return name


def innermost_location(exc: BaseException) -> str | None:
    """``agent/model_chain.py:request_stream`` — the deepest frame in our package.

    File and function only: a line number would change the fingerprint on
    every unrelated edit to the file.
    """
    location = None
    for frame in traceback.extract_tb(exc.__traceback__):
        parts = PurePath(frame.filename).parts
        if _PACKAGE in parts:
            rel = "/".join(parts[len(parts) - parts[::-1].index(_PACKAGE) :])
            location = f"{rel}:{frame.name}"
    return location


def fingerprint(source: str, error_type: str, location: str | None = None) -> str:
    """Stable identity of an automatic report: same bug, same fingerprint."""
    key = "\x1f".join([source, error_type, location or ""])
    return hashlib.sha256(key.encode()).hexdigest()


def format_error_detail(exc: BaseException) -> str:
    """``Type: message`` plus the traceback, truncated (operator-only)."""
    detail = "".join(traceback.format_exception(exc))
    return _truncate(detail, ERROR_DETAIL_MAX)


def current_models() -> str:
    """The model chain configured right now (not necessarily the one that answered)."""
    from ..config import settings

    gemini = runtime_config.get("gemini_model")
    if settings.deepseek_api_key:
        return f"{runtime_config.get('deepseek_model')} → {gemini}"
    return gemini


def snapshot_context(db: Session, user_id: str | None) -> list[dict] | None:
    """The chat's last ``CONTEXT_MESSAGES`` messages, oldest first, each truncated."""
    if not user_id:
        return None
    messages = get_conversation_messages(db, user_id, limit=CONTEXT_MESSAGES)
    return [
        {
            "role": m.role,
            "sender": m.sender_name,
            "content": _truncate(m.content or "", CONTEXT_MESSAGE_CHARS),
            "at": m.timestamp.isoformat() if m.timestamp else None,
        }
        for m in messages
    ]


def short_ref(report_id) -> str:
    """The reference the user is given (first 8 hex chars of the id)."""
    return str(report_id).replace("-", "")[:8]


def reports_in_last_hour(db: Session, user_id: str) -> int:
    since = _now() - timedelta(hours=1)
    return (
        db.query(func.count(BugReport.id))
        .filter(
            BugReport.user_id == user_id,
            BugReport.source.in_(("user", "agent")),
            BugReport.created_at >= since,
        )
        .scalar()
        or 0
    )


class RateLimited(Exception):
    """The user already filed ``bug_reports_per_user_per_hour`` reports."""


def file_report(
    db: Session,
    *,
    source: str,
    category: str,
    title: str,
    description: str,
    user_id: str,
    whatsapp_jid: str | None,
    client_id: str | None,
    conversation_type: str | None,
    job_id: str | None,
) -> BugReport:
    """Create a report from the agent tool. Raises ``RateLimited`` over the cap."""
    cap = int(runtime_config.get("bug_reports_per_user_per_hour"))
    if reports_in_last_hour(db, user_id) >= cap:
        raise RateLimited
    report = BugReport(
        source=source,
        category=category,
        title=_truncate(title.strip() or "Untitled report", TITLE_MAX),
        description=_truncate(description.strip(), DESCRIPTION_MAX),
        user_id=user_id,
        whatsapp_jid=whatsapp_jid,
        client_id=client_id,
        conversation_type=conversation_type,
        job_id=job_id,
        models=current_models(),
        context=snapshot_context(db, user_id),
    )
    db.add(report)
    db.commit()
    return report


def _record(db: Session, fp: str, fields: dict) -> BugReport | None:
    """Bump an open report with this fingerprint, or insert a new one."""
    now = _now()
    bumped = (
        db.query(BugReport)
        .filter(BugReport.fingerprint == fp, BugReport.status == "open")
        .update(
            {
                BugReport.occurrences: BugReport.occurrences + 1,
                BugReport.last_seen_at: now,
                BugReport.job_id: fields.get("job_id"),
            },
            synchronize_session=False,
        )
    )
    if bumped:
        db.commit()
        return None
    report = BugReport(
        **fields,
        fingerprint=fp,
        context=snapshot_context(db, fields.get("user_id")),
        created_at=now,
        last_seen_at=now,
    )
    db.add(report)
    db.commit()
    return report


def record_auto_report(
    source: str,
    *,
    title: str,
    exc: BaseException | None = None,
    error_type: str | None = None,
    error_detail: str | None = None,
    user_id: str | None = None,
    whatsapp_jid: str | None = None,
    client_id: str | None = None,
    conversation_type: str | None = None,
    job_id: str | None = None,
    document_id: str | None = None,
) -> None:
    """Record an automatic report. Never raises — it runs on failure paths.

    Uses its own session: the caller's may be in a failed transaction, and a
    rollback there would discard work the caller still needs.
    """
    try:
        if not runtime_config.get("bug_reports_enabled"):
            return
        location = None
        if exc is not None:
            error_type = error_type or error_type_name(exc)
            error_detail = error_detail or format_error_detail(exc)
            location = innermost_location(exc)
        error_type = error_type or "unknown"
        fp = fingerprint(source, error_type, location)
        fields = {
            "source": source,
            "title": _truncate(title, TITLE_MAX),
            "description": "",
            "error_type": _truncate(error_type, 255),
            "error_detail": _truncate(error_detail, ERROR_DETAIL_MAX),
            "user_id": user_id,
            "whatsapp_jid": whatsapp_jid,
            "client_id": client_id,
            "conversation_type": conversation_type,
            "job_id": job_id,
            "document_id": document_id,
            "models": current_models() if source == "model_error" else None,
        }
        db = SessionLocal()
        try:
            _record(db, fp, fields)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception:
        logger.error(f"Failed to record {source} bug report", exc_info=True)


def clear_user_context(db: Session, user_id: str) -> int:
    """Drop the message snapshots of a user's reports (``/clean``).

    The report itself stays; only the copied messages go. Does not commit —
    it runs inside the caller's ``/clean`` transaction.
    """
    return (
        db.query(BugReport)
        .filter(BugReport.user_id == user_id, BugReport.context.isnot(None))
        .update({BugReport.context: null()}, synchronize_session=False)
    )
