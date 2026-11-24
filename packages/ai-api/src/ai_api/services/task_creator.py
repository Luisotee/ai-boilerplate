"""The live re-check of a task scheduled into a group from a private chat.

Such a task (``scheduled_tasks.created_from_user_id`` set) runs in the group
only while the person who asked for it, in private, is still a member AND an
admin of that group. ``streams/scheduled_runner.py`` (before the LLM) and
``streams/scheduled_sender.py`` (before the send) ask the chat client live
before every run:

- requester row gone → task disabled ``creator_unknown``
- not a member → task disabled ``creator_not_in_group``
- a member, not an admin → task disabled ``creator_not_admin``
- the lookup failed, timed out, or the client didn't say (an older client
  without ``isAdmin`` / ``is_admin``) → this run skipped ``creator_check_failed``;
  the task stays active and the skip never counts toward auto-disable.

Fail closed: nothing here ever lets a run through on a failed lookup.
"""

from __future__ import annotations

import asyncio
import uuid

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
#: Verdicts that disable the task (the others only skip one run).
DISABLING = frozenset({CREATOR_UNKNOWN, CREATOR_NOT_IN_GROUP, CREATOR_NOT_ADMIN})
#: Bound on one live lookup: a slow client must not hold a run (or a send lane).
CHECK_TIMEOUT_SECONDS = 15.0


def _load(task_id: uuid.UUID) -> tuple[bool, User | None, str, str]:
    """``(needs_check, requester, platform, group_jid)``; the requester is detached."""
    db = SessionLocal()
    try:
        task = db.get(ScheduledTask, task_id)
        if task is None or task.created_from_user_id is None:
            return False, None, "", ""
        user = db.get(User, task.created_from_user_id)
        if user is not None:
            db.expunge(user)
        return True, user, task.client_id or "baileys", task.address
    finally:
        db.close()


async def creator_verdict(task_id: uuid.UUID) -> str | None:
    """None = the run may go; else one of the ``CREATOR_*`` codes. Never raises."""
    try:
        needs, user, platform, group_jid = await asyncio.to_thread(_load, task_id)
    except Exception:
        logger.error("Scheduled task %s: creator check could not read the DB", task_id)
        return CREATOR_CHECK_FAILED
    if not needs:
        return None
    if user is None:
        return CREATOR_UNKNOWN
    try:
        async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http:
            client = create_whatsapp_client(
                http_client=http,
                base_url=get_whatsapp_client_url(platform),
                api_key=get_whatsapp_api_key(platform),
            )
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                is_member, is_admin = await requester_group_status(
                    client, platform, user, group_jid
                )
    except Exception:
        logger.warning(
            "Scheduled task %s: could not confirm its creator is still a group admin",
            task_id,
            exc_info=True,
        )
        return CREATOR_CHECK_FAILED
    if not is_member:
        return CREATOR_NOT_IN_GROUP
    if is_admin is None:
        return CREATOR_CHECK_FAILED
    if is_admin is not True:
        return CREATOR_NOT_ADMIN
    return None
