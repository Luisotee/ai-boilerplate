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
  ``fingerprint`` (source + error type + where it happened, no line numbers),
  and one ``INSERT … ON CONFLICT`` against the partial unique index on open
  fingerprints either adds a row or bumps the open one. A bump replaces the
  whole *sample* (user, chat, job, context, traceback) with the latest
  occurrence, so a row never mixes two users' data; ``created_at`` stays
  "first seen". A resolved or ignored report never absorbs a repeat, so a
  regression shows up as a new row.

Every report snapshots the chat's last few messages (``context``) so an
operator can see what happened. ``scrub_user_reports`` removes everything a
report copied from a chat when the user runs ``/clean`` or a ``/link`` merge
discards their Telegram row. Writers and the scrub both lock the ``users`` row
first, so a report can't snapshot messages that a concurrent ``/clean`` is
deleting. Reports are read through ``/admin/bug-reports``.
"""

import hashlib
import re
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePath

from sqlalchemy import case, func, null, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..database import BugReport, SessionLocal, User, get_conversation_messages
from ..logger import logger
from ..runtime_config import runtime_config

CONTEXT_MESSAGES = 10
CONTEXT_MESSAGE_CHARS = 1000
TITLE_MAX = 200
DESCRIPTION_MAX = 4000
ERROR_DETAIL_MAX = 8000

#: Package directory name used to find "our" frames in a traceback.
_PACKAGE = "ai_api"

#: Title a user/agent report gets once /clean removed what the agent wrote.
SCRUBBED_TITLE = "(removed by /clean)"

#: Columns describing ONE occurrence; a bump replaces all of them together.
_SAMPLE_COLUMNS = (
    "user_id",
    "whatsapp_jid",
    "client_id",
    "conversation_type",
    "job_id",
    "document_id",
    "models",
    "error_detail",
    "context",
)

_NORMALIZERS = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<id>"),
    # A path starts a token: "/data/x.pdf" or "c:\\x", never the "/" in "and/or".
    (re.compile(r"(?<![\w.])(?:[a-z]:)?[\\/][^\s'\"]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+"), "#"),
    (re.compile(r"\s+"), " "),
)


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


def normalize_message(message: str, limit: int = 80) -> str:
    """Error text with ids, paths and numbers replaced, for grouping.

    ``Document 3f2a…: /data/x.pdf has 0 pages`` and the same error for another
    document normalise to the same string.
    """
    out = message.lower()
    for pattern, placeholder in _NORMALIZERS:
        out = pattern.sub(placeholder, out)
    return out.strip()[:limit]


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


def _lock_user(db: Session, user_id: str | None) -> User | None:
    """``SELECT … FOR UPDATE`` on the chat's user row (None if it is gone).

    Report writers take it before snapshotting messages, and the /clean scrub
    takes it too, so the two serialise: either the report waits and snapshots
    after the clean, or the clean waits and then scrubs the new report.
    """
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).with_for_update().first()


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
    """Create a report from the agent tool. Raises ``RateLimited`` over the cap.

    Runs in the agent run's session; the user-row lock is held until the commit.
    """
    _lock_user(db, user_id)
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


def _record(db: Session, fp: str, fields: dict) -> None:
    """Insert a report, or bump the open one with this fingerprint (atomic)."""
    user_id = fields.get("user_id")
    if user_id and _lock_user(db, user_id) is None:
        user_id = None  # the row was deleted (e.g. merged away): nothing to point at
    now = _now()
    values = {
        **fields,
        "id": uuid.uuid4(),
        "status": "open",
        "user_id": user_id,
        "context": snapshot_context(db, user_id),
        "fingerprint": fp,
        "occurrences": 1,
        "created_at": now,
        "last_seen_at": now,
    }
    stmt = insert(BugReport).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[BugReport.fingerprint],
        index_where=text("status = 'open'"),
        set_={
            "occurrences": BugReport.occurrences + 1,
            "last_seen_at": stmt.excluded.last_seen_at,
            **{col: getattr(stmt.excluded, col) for col in _SAMPLE_COLUMNS},
        },
    )
    db.execute(stmt)
    db.commit()


def record_auto_report(
    source: str,
    *,
    title: str,
    exc: BaseException | None = None,
    error_type: str | None = None,
    error_detail: str | None = None,
    location: str | None = None,
    user_id: str | None = None,
    whatsapp_jid: str | None = None,
    client_id: str | None = None,
    conversation_type: str | None = None,
    job_id: str | None = None,
    document_id: str | None = None,
) -> None:
    """Record an automatic report. Never raises — it runs on failure paths.

    Uses its own session: the caller's may be in a failed transaction, and a
    rollback there would discard work the caller still needs. It blocks on the
    database, so async callers run it with ``asyncio.to_thread``.

    ``location`` overrides the traceback frame in the fingerprint (a PDF
    failure has no traceback; it passes its normalised reason instead).
    """
    try:
        if not runtime_config.get("bug_reports_enabled"):
            return
        if exc is not None:
            error_type = error_type or error_type_name(exc)
            error_detail = error_detail or format_error_detail(exc)
            location = location or innermost_location(exc)
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


def scrub_user_reports(db: Session, user_id: str) -> int:
    """Remove what a user's reports copied from the chat (``/clean``, merges).

    Clears the message snapshot, the traceback (a DB error can quote the
    user's text) and the description; a user/agent title is replaced, since
    the agent writes it from the conversation (automatic titles are fixed
    text). What remains is metadata: source, category, status, error type,
    models, counts, timestamps and the chat id (the ``users`` row keeps that
    too). Does not commit — it runs inside the caller's transaction, after
    taking the user-row lock.
    """
    _lock_user(db, user_id)
    return (
        db.query(BugReport)
        .filter(BugReport.user_id == user_id)
        .update(
            {
                BugReport.context: null(),
                BugReport.error_detail: null(),
                BugReport.description: "",
                BugReport.title: case(
                    (BugReport.source.in_(("user", "agent")), SCRUBBED_TITLE),
                    else_=BugReport.title,
                ),
            },
            synchronize_session=False,
        )
    )
