"""The live re-check of a task scheduled into a group from a private chat.

Such a task (``scheduled_tasks.created_from_user_id`` set) runs in the group
only while the person who asked for it, in private, is still a member AND an
admin of that group. ``streams/scheduled_runner.py`` (before the LLM) and
``streams/scheduled_sender.py`` (before the send) ask the chat client live
before every run:

- requester row gone → task disabled ``creator_unknown``
- not a member → task disabled ``creator_not_in_group``. On Baileys a recurring
  task is disabled only on the second run in a row that misses the group: the
  first miss skips the run ``creator_not_seen`` (transient, like
  ``creator_check_failed``), because the client also leaves a group out when it
  can't resolve a LID-only participant (``getPNForLID`` failing or not mapped
  yet). The task's ``last_error`` carries the miss to the next run (no column);
  a passing check clears it, so a pass in the runner followed by a miss in the
  sender of the same run is a first miss again. A one-shot never escalates:
  every miss is ``creator_not_seen`` and uses its bounded retries, ending
  disabled ``creator_not_seen`` when they run out. Telegram's ``getChatMember``
  answer (left / kicked) is authoritative and disables at once.
- a member, not an admin → task disabled ``creator_not_admin``
- the lookup failed, timed out, or the client didn't say (an older client
  without ``isAdmin`` / ``is_admin``) → this run skipped ``creator_check_failed``;
  the task stays active and the skip never counts toward auto-disable.

Both transient skips re-arm a one-shot with its bounded retry
(``scheduled_runner._one_shot_retry_at``).

Fail closed: nothing here ever lets a run through on a failed lookup.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import httpx

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import ScheduledTask, SessionLocal, User
from ..logger import logger
from ..whatsapp import create_whatsapp_client
from .shared_groups import requester_group_status

CREATOR_UNKNOWN = "creator_unknown"
CREATOR_NOT_IN_GROUP = "creator_not_in_group"
CREATOR_NOT_ADMIN = "creator_not_admin"
CREATOR_CHECK_FAILED = "creator_check_failed"
#: The group was missing from the creator's Baileys shared groups once: maybe a
#: LID the client could not resolve, so only a second miss in a row disables.
CREATOR_NOT_SEEN = "creator_not_seen"
#: Verdicts that disable the task (the others only skip one run).
DISABLING = frozenset({CREATOR_UNKNOWN, CREATOR_NOT_IN_GROUP, CREATOR_NOT_ADMIN})
#: Bound on one live lookup: a slow client must not hold a run (or a send lane).
CHECK_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class _Check:
    needs_check: bool
    #: Detached; None when the requester row is gone.
    requester: User | None = None
    platform: str = ""
    group_jid: str = ""
    #: The task's previous check missed the group (``last_error``).
    missed_before: bool = False
    one_shot: bool = False


def _load(task_id: uuid.UUID) -> _Check:
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None or task.created_from_user_id is None:
            return _Check(False)
        user = db.get(User, task.created_from_user_id)
        if user is not None:
            db.expunge(user)
        return _Check(
            True,
            user,
            task.client_id or "baileys",
            task.address,
            missed_before=task.last_error == CREATOR_NOT_SEEN,
            one_shot=task.run_at is not None,
        )
    finally:
        db.close()


def _clear_miss(task_id: uuid.UUID) -> None:
    """A passing check ends the run of misses (conditional: only the marker)."""
    db = SessionLocal()
    try:
        db.query(ScheduledTask).filter(
            ScheduledTask.id == task_id, ScheduledTask.last_error == CREATOR_NOT_SEEN
        ).update({"last_error": None}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


async def creator_verdict(task_id: uuid.UUID) -> str | None:
    """None = the run may go; else one of the ``CREATOR_*`` codes. Never raises."""
    try:
        check = await asyncio.to_thread(_load, task_id)
    except Exception:
        logger.error("Scheduled task %s: creator check could not read the DB", task_id)
        return CREATOR_CHECK_FAILED
    if not check.needs_check:
        return None
    if check.requester is None:
        return CREATOR_UNKNOWN
    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http:
            client = create_whatsapp_client(
                http_client=http,
                base_url=get_whatsapp_client_url(check.platform),
                api_key=get_whatsapp_api_key(check.platform),
            )
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                is_member, is_admin = await requester_group_status(
                    client, check.platform, check.requester, check.group_jid
                )
    except Exception:
        logger.warning(
            "Scheduled task %s: could not confirm its creator is still a group admin",
            task_id,
            exc_info=True,
        )
        return CREATOR_CHECK_FAILED
    if not is_member:
        if check.platform == "baileys" and (check.one_shot or not check.missed_before):
            return CREATOR_NOT_SEEN
        return CREATOR_NOT_IN_GROUP
    if is_admin is None:
        return CREATOR_CHECK_FAILED
    if is_admin is not True:
        return CREATOR_NOT_ADMIN
    if check.missed_before:
        try:
            await asyncio.to_thread(_clear_miss, task_id)
        except Exception:
            # Safe either way: a kept marker only makes a later miss disable sooner.
            logger.warning("Scheduled task %s: could not clear its miss", task_id, exc_info=True)
    return None
