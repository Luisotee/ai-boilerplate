"""Scheduling into a group from a private chat (Devflow 6b058203).

The tool side (``manage_scheduled_tasks(group_name=…)``): strict match among the
shared groups, a confirmed admin only (fail closed), the task stored on the
GROUP with the requester as ``created_from_user_id``. The run side
(``services/task_creator.py``): the live re-check before every run.
"""

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.agent.tools.scheduled_tasks import manage_scheduled_tasks
from ai_api.database import ScheduledTask
from ai_api.services import task_creator
from ai_api.whatsapp.client import GroupMembership, SharedGroup

MOD = "ai_api.agent.tools.scheduled_tasks"
PRIVATE = "5511999999999@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
OTHER = "120363000000000002@g.us"
TG_GROUP = "tg:-1001"
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
GROUP_ROW_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _requester(**kw):
    values = dict(
        id=USER_ID,
        whatsapp_jid=PRIVATE,
        whatsapp_lid=None,
        phone="+5511999999999",
        telegram_jid=None,
        name="Ana",
    )
    values.update(kw)
    return SimpleNamespace(**values)


def _ctx(jid=PRIVATE, client_id=None, is_group_admin=None, user=None, group_name="Book Club"):
    ctx = MagicMock()
    deps = ctx.deps
    deps.user_id = str(USER_ID)
    deps.whatsapp_jid = jid
    deps.client_id = client_id
    deps.is_group_admin = is_group_admin
    deps.scheduled_run = False
    deps.user_message_id = None
    rows = {USER_ID: user or _requester()}
    rows[GROUP_ROW_ID] = SimpleNamespace(name=group_name, whatsapp_jid=jid)
    deps.db.get.side_effect = lambda _model, key: rows.get(
        key if isinstance(key, uuid.UUID) else uuid.UUID(str(key))
    )
    return ctx


def _task(**kw):
    values = dict(
        id=uuid.UUID("ab12cd34-0000-0000-0000-000000000000"),
        kind="reminder",
        text="Meeting at 5",
        schedule="every day at 17:00",
        timezone="America/Sao_Paulo",
        next_run_at=datetime(2026, 11, 25, 20, 0),
        status="active",
        user_id=GROUP_ROW_ID,
        address=GROUP,
        client_id=None,
    )
    values.update(kw)
    return ScheduledTask(**values)


def _shared(*groups):
    return patch(f"{MOD}.resolve_shared_groups", AsyncMock(return_value=list(groups)))


def _group_row():
    return patch(f"{MOD}.get_or_create_user", return_value=SimpleNamespace(id=GROUP_ROW_ID))


class TestCreateFromPrivate:
    async def test_an_admin_schedules_into_the_group(self):
        ctx = _ctx()
        with (
            _shared(SharedGroup(GROUP, "Book Club", is_admin=True)),
            _group_row(),
            patch(f"{MOD}.create_task", return_value=_task()) as create,
        ):
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="every day at 17:00", text="Meeting at 5",
                group_name="book club",
            )  # fmt: skip
        kw = create.call_args.kwargs
        assert kw["user_id"] == GROUP_ROW_ID and kw["address"] == GROUP
        assert kw["created_from_user_id"] == USER_ID
        assert kw["created_by_jid"] == PRIVATE and kw["created_by_name"] == "Ana"
        assert 'the group "Book Club"' in reply

    async def test_a_junk_task_id_on_create_is_ignored(self):
        """task_id is cancel-only: a stray "new" never reaches the cancel lookups."""
        ctx = _ctx()
        with (
            _shared(SharedGroup(GROUP, "Book Club", is_admin=True)),
            _group_row(),
            patch(f"{MOD}.create_task", return_value=_task()) as create,
            patch(f"{MOD}.cancel_task") as cancel,
            patch(f"{MOD}.find_task") as find,
        ):
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", task_id="new", group_name="Book Club"
            )
        cancel.assert_not_called()
        find.assert_not_called()
        assert create.call_args.kwargs["address"] == GROUP and "Scheduled #" in reply

    @pytest.mark.parametrize("is_admin", [False, None])
    async def test_a_non_admin_or_an_unknown_role_is_refused(self, is_admin):
        """None = an older client without isAdmin: fail closed."""
        ctx = _ctx()
        with (
            _shared(SharedGroup(GROUP, "Book Club", is_admin=is_admin)),
            patch(f"{MOD}.create_task") as create,
        ):
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        create.assert_not_called()
        assert "Only an admin" in reply

    async def test_an_ambiguous_name_lists_the_shared_groups(self):
        ctx = _ctx()
        with (
            _shared(
                SharedGroup(GROUP, "Book Club North", is_admin=True),
                SharedGroup(OTHER, "Book Club South", is_admin=True),
            ),
            patch(f"{MOD}.create_task") as create,
        ):
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="book club"
            )
        create.assert_not_called()
        assert "Book Club North" in reply and "Book Club South" in reply

    async def test_no_shared_group_saves_nothing(self):
        ctx = _ctx()
        with _shared(), patch(f"{MOD}.create_task") as create:
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        create.assert_not_called()
        assert "couldn't find any group" in reply

    async def test_the_cloud_api_has_no_groups(self):
        ctx = _ctx(client_id="cloud")
        with (
            _shared(SharedGroup(GROUP, "Book Club", is_admin=True)),
            patch(f"{MOD}.create_task") as create,
        ):
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        create.assert_not_called()
        assert "WhatsApp or Telegram" in reply

    async def test_telegram_asks_the_admin_role_live(self):
        user = _requester(whatsapp_jid="tg:55", phone=None)
        ctx = _ctx(jid="tg:55", client_id="telegram", user=user)
        ctx.deps.whatsapp_client.group_membership = AsyncMock(
            return_value=GroupMembership(is_member=True, is_admin=True)
        )
        with (
            _shared(SharedGroup(TG_GROUP, "Book Club")),  # derived list: role unknown
            _group_row(),
            patch(f"{MOD}.create_task", return_value=_task(address=TG_GROUP)) as create,
        ):
            await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        ctx.deps.whatsapp_client.group_membership.assert_awaited_once_with(TG_GROUP, "tg:55")
        assert create.call_args.kwargs["created_by_jid"] == "tg:55"

    @pytest.mark.parametrize(
        "membership", [GroupMembership(False, False), GroupMembership(True, False)]
    )
    async def test_telegram_left_or_not_admin_is_refused(self, membership):
        user = _requester(whatsapp_jid="tg:55", phone=None)
        ctx = _ctx(jid="tg:55", client_id="telegram", user=user)
        ctx.deps.whatsapp_client.group_membership = AsyncMock(return_value=membership)
        with _shared(SharedGroup(TG_GROUP, "Book Club")), patch(f"{MOD}.create_task") as create:
            await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        create.assert_not_called()

    async def test_a_telegram_lookup_error_fails_closed(self):
        user = _requester(whatsapp_jid="tg:55", phone=None)
        ctx = _ctx(jid="tg:55", client_id="telegram", user=user)
        ctx.deps.whatsapp_client.group_membership = AsyncMock(side_effect=RuntimeError("down"))
        with _shared(SharedGroup(TG_GROUP, "Book Club")), patch(f"{MOD}.create_task") as create:
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        create.assert_not_called()
        assert "down" not in reply


class TestInAGroup:
    async def test_naming_this_same_group_is_the_usual_group_path(self):
        ctx = _ctx(jid=GROUP, is_group_admin=True, group_name="Book Club")
        ctx.deps.user_id = str(GROUP_ROW_ID)
        with patch(f"{MOD}.create_task", return_value=_task()) as create:
            await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Book Club"
            )
        assert create.call_args.kwargs["address"] == GROUP
        assert "created_from_user_id" not in create.call_args.kwargs

    async def test_naming_another_group_is_refused(self):
        ctx = _ctx(jid=GROUP, is_group_admin=True, group_name="Book Club")
        ctx.deps.user_id = str(GROUP_ROW_ID)
        with patch(f"{MOD}.create_task") as create:
            reply = await manage_scheduled_tasks(
                ctx, "create", schedule="in 1h", text="x", group_name="Hiking"
            )
        create.assert_not_called()
        assert "private chat" in reply


class TestCancelFromPrivate:
    def _patches(self, task):
        from ai_api.services.scheduled_tasks import TaskNotFound

        return (
            patch(f"{MOD}.cancel_task", side_effect=TaskNotFound("none here")),
            patch(f"{MOD}.find_task", return_value=task),
        )

    async def test_an_admin_cancels_a_task_they_scheduled_into_a_group(self):
        task = _task(client_id="telegram", address=TG_GROUP)
        ctx = _ctx()
        status = AsyncMock(return_value=(True, True))
        own, remote = self._patches(task)
        with own, remote, patch(f"{MOD}._admin_status", status):
            reply = await manage_scheduled_tasks(ctx, "cancel", task_id="ab12cd34")
        assert task.status == "cancelled" and "in Book Club" in reply
        # The task's own client (Telegram), not this chat's (Baileys): linked accounts.
        assert status.await_args.args[1] == "telegram"

    @pytest.mark.parametrize("answer", [(True, False), (False, False), (True, None)])
    async def test_a_non_admin_cannot_cancel(self, answer):
        task = _task()
        own, remote = self._patches(task)
        with own, remote, patch(f"{MOD}._admin_status", AsyncMock(return_value=answer)):
            reply = await manage_scheduled_tasks(_ctx(), "cancel", task_id="ab12cd34")
        assert task.status == "active" and "Only an admin" in reply

    async def test_a_failed_lookup_cancels_nothing(self):
        task = _task()
        own, remote = self._patches(task)
        with own, remote, patch(f"{MOD}._admin_status", AsyncMock(side_effect=OSError("x"))):
            reply = await manage_scheduled_tasks(_ctx(), "cancel", task_id="ab12cd34")
        assert task.status == "active" and "couldn't confirm" in reply

    async def test_an_ambiguous_id_never_falls_through_to_a_group_task(self):
        """Two private reminders and one group task share the prefix: refuse, cancel nothing."""
        from ai_api.services.scheduled_tasks import TaskError

        task = _task()
        with (
            patch(f"{MOD}.cancel_task", side_effect=TaskError("Several tasks start with #ab12")),
            patch(f"{MOD}.find_task", return_value=task) as find,
            patch(f"{MOD}._admin_status", AsyncMock(return_value=(True, True))),
        ):
            reply = await manage_scheduled_tasks(_ctx(), "cancel", task_id="ab12")
        find.assert_not_called()
        assert task.status == "active" and "Several tasks" in reply

    async def test_list_shows_the_group_tasks_with_their_group(self):
        ctx = _ctx()
        with (
            patch(f"{MOD}.list_tasks", return_value=[]),
            patch(f"{MOD}.list_tasks_created_from", return_value=[_task()]),
        ):
            reply = await manage_scheduled_tasks(ctx, "list")
        assert "[in Book Club] #ab12cd34" in reply


# --- The live re-check before every run ----------------------------------------------


def _loaded(user, platform="baileys", missed_before=False, one_shot=False):
    check = task_creator._Check(True, user, platform, GROUP, missed_before, one_shot)
    return patch.object(task_creator, "_load", return_value=check)


class TestCreatorVerdict:
    async def test_an_ordinary_task_is_not_checked(self):
        with patch.object(task_creator, "_load", return_value=task_creator._Check(False)):
            assert await task_creator.creator_verdict(uuid.uuid4()) is None

    async def test_a_deleted_creator_disables(self):
        with _loaded(None):
            verdict = await task_creator.creator_verdict(uuid.uuid4())
        assert verdict == task_creator.CREATOR_UNKNOWN

    @pytest.mark.parametrize(
        "answer,expected",
        [
            ((True, True), None),
            ((False, False), task_creator.CREATOR_NOT_SEEN),  # a first Baileys miss
            ((True, False), task_creator.CREATOR_NOT_ADMIN),
            ((True, None), task_creator.CREATOR_CHECK_FAILED),  # older client: no field
        ],
    )
    async def test_the_live_answer_decides(self, answer, expected):
        with (
            _loaded(_requester()),
            patch.object(task_creator, "requester_group_status", AsyncMock(return_value=answer)),
        ):
            assert await task_creator.creator_verdict(uuid.uuid4()) == expected

    @pytest.mark.parametrize(
        "platform,missed_before,one_shot,expected",
        [
            # Baileys omits a group it can't resolve a LID for: one miss is no proof.
            ("baileys", False, False, task_creator.CREATOR_NOT_SEEN),
            ("baileys", True, False, task_creator.CREATOR_NOT_IN_GROUP),
            # A one-shot never escalates: its bounded retries end it.
            ("baileys", True, True, task_creator.CREATOR_NOT_SEEN),
            # Telegram's getChatMember left/kicked is authoritative.
            ("telegram", False, False, task_creator.CREATOR_NOT_IN_GROUP),
        ],
    )
    async def test_a_missing_group(self, platform, missed_before, one_shot, expected):
        with (
            _loaded(_requester(), platform, missed_before, one_shot),
            patch.object(
                task_creator, "requester_group_status", AsyncMock(return_value=(False, False))
            ),
        ):
            assert await task_creator.creator_verdict(uuid.uuid4()) == expected
        assert task_creator.CREATOR_NOT_SEEN not in task_creator.DISABLING

    async def test_a_lookup_error_only_skips_the_run(self):
        with (
            _loaded(_requester()),
            patch.object(
                task_creator, "requester_group_status", AsyncMock(side_effect=RuntimeError("x"))
            ),
        ):
            verdict = await task_creator.creator_verdict(uuid.uuid4())
        assert verdict == task_creator.CREATOR_CHECK_FAILED
        assert verdict not in task_creator.DISABLING


class TestRequesterGroupStatus:
    async def test_baileys_reads_the_shared_list(self):
        from ai_api.services.shared_groups import requester_group_status

        client = MagicMock()
        client.get_shared_groups = AsyncMock(
            return_value=[SharedGroup(GROUP, "Book Club", is_admin=True)]
        )
        assert await requester_group_status(client, "baileys", _requester(), GROUP) == (True, True)
        assert await requester_group_status(client, "baileys", _requester(), OTHER) == (
            False,
            False,
        )

    async def test_the_cloud_api_has_no_answer(self):
        from ai_api.services.shared_groups import requester_group_status

        with pytest.raises(ValueError):
            await requester_group_status(MagicMock(), "cloud", _requester(), GROUP)
