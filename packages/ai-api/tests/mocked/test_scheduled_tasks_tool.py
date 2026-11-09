"""The manage_scheduled_tasks tool and services/scheduled_tasks.py (limits, scope)."""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.core import SCHEDULED_RUN_TOOLS, scheduled_run_tools, scheduled_tasks_guidance
from ai_api.agent.tools.scheduled_tasks import manage_scheduled_tasks, scheduled_tasks_enabled
from ai_api.database import ConversationMessage, ScheduledTask
from ai_api.services import scheduled_tasks as svc

NOW = datetime(2026, 11, 9, 15, 0)  # Monday 10:00 in Rio Branco
GROUP = "120363012345678@g.us"
PRIVATE = "5511999999999@s.whatsapp.net"

SETTINGS = {
    "bot_timezone": "America/Rio_Branco",
    "broadcast_timezone": "UTC",
    "scheduled_tasks_max_per_chat": 10,
    "scheduled_tasks_min_interval_minutes": 60,
    "scheduled_tasks_enabled": True,
}


@pytest.fixture(autouse=True)
def settings_values():
    values = dict(SETTINGS)
    with (
        patch.object(svc.runtime_config, "get", side_effect=lambda k: values[k]),
        patch.object(svc, "utcnow", return_value=NOW),
    ):
        yield values


def _db(active=0):
    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = active
    return db


def _create(db=None, **kw):
    args = dict(
        user_id=uuid.uuid4(),
        address=PRIVATE,
        client_id=None,
        kind="reminder",
        text="call Ana",
        schedule_text="tomorrow at 9:00",
        now=NOW,
    )
    args.update(kw)
    return svc.create_task(db or _db(), **args)


class TestCreateTask:
    def test_one_shot(self):
        db = _db()
        task = _create(db)
        assert task.run_at == datetime(2026, 11, 10, 14, 0)
        assert task.next_run_at == task.run_at
        assert task.timezone == "America/Rio_Branco"
        assert task.status == "active"
        assert task.address == PRIVATE
        db.commit.assert_called_once()

    def test_recurring_arms_the_first_slot(self):
        task = _create(schedule_text="every weekday at 8:30")
        assert task.cron_expr == "30 8 * * 1,2,3,4,5"
        assert task.next_run_at == datetime(2026, 11, 10, 13, 30)

    def test_empty_bot_timezone_falls_back_to_broadcast_timezone(self, settings_values):
        settings_values["bot_timezone"] = ""
        settings_values["broadcast_timezone"] = "America/Manaus"
        task = _create()
        assert task.timezone == "America/Manaus"

    def test_too_many_tasks(self):
        with pytest.raises(svc.TaskError, match="10 active"):
            _create(_db(active=10))

    def test_too_frequent(self):
        with pytest.raises(svc.TaskError, match="60 minutes"):
            _create(schedule_text="every 30m")

    def test_too_frequent_cron(self):
        with pytest.raises(svc.TaskError, match="too often"):
            _create(schedule_text="*/15 * * * *")

    def test_agent_task_fire_cap(self, settings_values):
        settings_values["scheduled_tasks_min_interval_minutes"] = 60
        with pytest.raises(svc.TaskError, match="4 times a day"):
            _create(kind="agent", schedule_text="every 2h")
        assert _create(kind="agent", schedule_text="every 6h").interval_seconds == 21600

    def test_bad_schedule_is_a_task_error(self):
        with pytest.raises(svc.TaskError, match="past"):
            _create(schedule_text="2020-01-01 09:00")

    @pytest.mark.parametrize("cron", ["* * 1 * *", "*/5 * 20 * *", "* 9 * 12 *"])
    def test_frequency_is_measured_from_the_first_slot(self, cron):
        # None of these fires in the next 8 days, but each fires every few
        # minutes once it starts: the limit must still apply.
        with pytest.raises(svc.TaskError, match="too often"):
            _create(schedule_text=cron)

    def test_agent_fire_cap_from_the_first_slot(self, settings_values):
        settings_values["scheduled_tasks_min_interval_minutes"] = 60
        with pytest.raises(svc.TaskError, match="4 times a day"):
            _create(kind="agent", schedule_text="0 * 1 * *")

    def test_a_cron_that_never_fires_is_refused(self):
        with pytest.raises(svc.TaskError, match="never fires"):
            _create(schedule_text="0 9 30 2 *")

    def test_reminder_markdown_is_stored_clean(self):
        assert _create(text="**Call Ana**").text == "*Call Ana*"

    def test_agent_prompt_is_stored_as_given(self):
        task = _create(kind="agent", text="**check** the river", schedule_text="every 6h")
        assert task.text == "**check** the river"

    def test_creator_is_recorded(self):
        task = _create(created_by_jid="5511888@s.whatsapp.net", created_by_name="Ana")
        assert (task.created_by_jid, task.created_by_name) == ("5511888@s.whatsapp.net", "Ana")

    def test_text_limits(self):
        with pytest.raises(svc.TaskError):
            _create(text="   ")
        with pytest.raises(svc.TaskError, match="1000"):
            _create(text="x" * 1001)


class TestFindTask:
    def _db_with(self, *tasks):
        db = MagicMock()
        db.query.return_value.filter.return_value.limit.return_value.all.return_value = list(tasks)
        return db

    def test_prefix_match(self):
        task = ScheduledTask(id=uuid.UUID("ab12cd34-0000-0000-0000-000000000000"), status="active")
        db = self._db_with(task)
        assert svc.find_task(db, "u", "#ab12cd34") is task
        # Filtered in SQL (chat, status, id prefix), at most two rows read.
        assert len(db.query.return_value.filter.call_args.args) == 3
        db.query.return_value.filter.return_value.limit.assert_called_once_with(2)

    def test_unknown_or_ambiguous(self):
        a = ScheduledTask(id=uuid.UUID("ab12cd34-0000-0000-0000-000000000000"))
        b = ScheduledTask(id=uuid.UUID("ab12ffff-0000-0000-0000-000000000000"))
        with pytest.raises(svc.TaskError, match="No scheduled task"):
            svc.find_task(self._db_with(), "u", "ffff0000")
        with pytest.raises(svc.TaskError, match="Several"):
            svc.find_task(self._db_with(a, b), "u", "ab12")

    def test_rejects_garbage(self):
        with pytest.raises(svc.TaskError):
            svc.find_task(self._db_with(), "u", "x'; drop")

    def test_cancel(self):
        task = ScheduledTask(
            id=uuid.UUID("ab12cd34-0000-0000-0000-000000000000"),
            status="active",
            next_run_at=NOW,
        )
        svc.cancel_task(self._db_with(task), "u", "ab12cd34")
        assert task.status == "cancelled"
        assert task.next_run_at is None


def _ctx(jid=PRIVATE, is_group_admin=None, client_id=None, scheduled_run=False):
    ctx = MagicMock()
    ctx.deps.user_id = "user-1"
    ctx.deps.whatsapp_jid = jid
    ctx.deps.is_group_admin = is_group_admin
    ctx.deps.client_id = client_id
    ctx.deps.scheduled_run = scheduled_run
    ctx.deps.user_message_id = None
    return ctx


def _created(**kw):
    defaults = dict(
        id=uuid.UUID("ab12cd34-0000-0000-0000-000000000000"),
        kind="reminder",
        text="call Ana",
        schedule="once at Tue 10 Nov 2026 09:00",
        timezone="America/Rio_Branco",
        next_run_at=datetime(2026, 11, 10, 14, 0),
        status="active",
    )
    defaults.update(kw)
    return ScheduledTask(**defaults)


class TestTool:
    async def test_create_targets_this_chat_and_restates_it(self):
        ctx = _ctx()
        with patch(
            "ai_api.agent.tools.scheduled_tasks.create_task", return_value=_created()
        ) as create:
            result = await manage_scheduled_tasks(
                ctx, "create", schedule="tomorrow at 9:00", text="call Ana"
            )
        kwargs = create.call_args.kwargs
        assert kwargs["address"] == PRIVATE
        assert kwargs["user_id"] == "user-1"
        assert "#ab12cd34" in result
        assert "Tue 10 Nov 2026 09:00" in result
        assert '"call Ana"' in result

    @pytest.mark.parametrize("admin", [None, False])
    @pytest.mark.parametrize("action", ["create", "cancel"])
    async def test_group_non_admin_is_refused(self, admin, action):
        ctx = _ctx(jid=GROUP, is_group_admin=admin)
        with (
            patch("ai_api.agent.tools.scheduled_tasks.create_task") as create,
            patch("ai_api.agent.tools.scheduled_tasks.cancel_task") as cancel,
        ):
            result = await manage_scheduled_tasks(
                ctx, action, schedule="in 5m", text="x", task_id="ab12cd34"
            )
        assert "Only a group admin" in result
        create.assert_not_called()
        cancel.assert_not_called()

    async def test_group_admin_creates_for_the_group(self):
        ctx = _ctx(jid=GROUP, is_group_admin=True)
        with patch(
            "ai_api.agent.tools.scheduled_tasks.create_task", return_value=_created()
        ) as create:
            result = await manage_scheduled_tasks(ctx, "create", schedule="in 5m", text="x")
        assert create.call_args.kwargs["address"] == GROUP
        assert "this group" in result

    async def test_creator_comes_from_the_message_being_answered(self):
        ctx = _ctx(jid=GROUP, is_group_admin=True)
        msg_id = uuid.uuid4()
        ctx.deps.user_message_id = str(msg_id)
        ctx.deps.db.get.return_value = ConversationMessage(
            sender_jid="5511888@s.whatsapp.net", sender_name="Ana"
        )
        with patch(
            "ai_api.agent.tools.scheduled_tasks.create_task", return_value=_created()
        ) as create:
            await manage_scheduled_tasks(ctx, "create", schedule="in 5m", text="x")
        ctx.deps.db.get.assert_called_once_with(ConversationMessage, msg_id)
        assert create.call_args.kwargs["created_by_jid"] == "5511888@s.whatsapp.net"
        assert create.call_args.kwargs["created_by_name"] == "Ana"

    async def test_any_member_can_list(self):
        ctx = _ctx(jid=GROUP, is_group_admin=None)
        with patch("ai_api.agent.tools.scheduled_tasks.list_tasks", return_value=[_created()]):
            result = await manage_scheduled_tasks(ctx, "list")
        assert "#ab12cd34" in result

    async def test_task_error_is_relayed(self):
        ctx = _ctx()
        with patch(
            "ai_api.agent.tools.scheduled_tasks.create_task",
            side_effect=svc.TaskError("That time is already in the past."),
        ):
            result = await manage_scheduled_tasks(ctx, "create", schedule="x", text="y")
        assert result == "That time is already in the past."
        ctx.deps.db.rollback.assert_called_once()

    async def test_cloud_chat_gets_the_window_note(self):
        ctx = _ctx(client_id="cloud")
        with patch("ai_api.agent.tools.scheduled_tasks.create_task", return_value=_created()):
            result = await manage_scheduled_tasks(ctx, "create", schedule="in 5m", text="x")
        assert "24 hours" in result

    async def test_cancel_commits(self):
        ctx = _ctx()
        with patch("ai_api.agent.tools.scheduled_tasks.cancel_task", return_value=_created()):
            result = await manage_scheduled_tasks(ctx, "cancel", task_id="ab12cd34")
        ctx.deps.db.commit.assert_called_once()
        assert "Cancelled #ab12cd34" in result


class TestRecursionGuard:
    async def test_tool_hidden_in_scheduled_runs(self, settings_values):
        tool_def = MagicMock()
        with patch("ai_api.agent.tools.scheduled_tasks.runtime_config.get", return_value=True):
            assert await scheduled_tasks_enabled(_ctx(scheduled_run=True), tool_def) is None
            assert await scheduled_tasks_enabled(_ctx(), tool_def) is tool_def

    async def test_prepare_survives_missing_deps(self):
        ctx = MagicMock()
        ctx.deps = None
        with patch("ai_api.agent.tools.scheduled_tasks.runtime_config.get", return_value=True):
            assert await scheduled_tasks_enabled(ctx, "td") == "td"

    async def test_tool_hidden_when_disabled(self):
        with patch("ai_api.agent.tools.scheduled_tasks.runtime_config.get", return_value=False):
            assert await scheduled_tasks_enabled(_ctx(), MagicMock()) is None

    async def test_scheduled_runs_only_see_read_only_tools(self):
        from ai_api.agent.core import agent

        names = list(agent._function_toolset.tools)
        defs = [MagicMock() for _ in names]
        for d, n in zip(defs, names, strict=True):
            d.name = n
        kept = await scheduled_run_tools(_ctx(scheduled_run=True), defs)
        kept_names = {d.name for d in kept}
        assert kept_names <= SCHEDULED_RUN_TOOLS
        assert "manage_scheduled_tasks" not in kept_names
        assert "send_whatsapp_message" not in kept_names
        assert "get_weather" in kept_names
        assert await scheduled_run_tools(_ctx(), defs) == defs

    def test_allow_list_names_real_tools(self):
        from ai_api.agent.core import agent

        assert set(SCHEDULED_RUN_TOOLS) <= set(agent._function_toolset.tools)

    def test_agent_uses_the_filter(self):
        from ai_api.agent.core import agent

        assert agent._prepare_tools is scheduled_run_tools


class TestGuidance:
    """The tool's guidance is an instructions hook, so a bot_prompt override keeps it."""

    async def test_present_when_enabled(self):
        with patch("ai_api.agent.core.runtime_config.get", return_value=True):
            text = await scheduled_tasks_guidance(_ctx())
        assert "manage_scheduled_tasks" in text
        assert "Never compute dates yourself" in text

    async def test_absent_when_disabled_or_in_a_scheduled_run(self):
        with patch("ai_api.agent.core.runtime_config.get", return_value=False):
            assert await scheduled_tasks_guidance(_ctx()) == ""
        with patch("ai_api.agent.core.runtime_config.get", return_value=True):
            assert await scheduled_tasks_guidance(_ctx(scheduled_run=True)) == ""

    def test_not_in_the_default_prompt(self):
        from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT

        assert "manage_scheduled_tasks" not in DEFAULT_SYSTEM_PROMPT
