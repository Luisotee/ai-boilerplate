"""streams/scheduled_runner.py: run rules (checks, outcomes, auto-disable) and the agent phase."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.database import ScheduledTask, TaskRun, User
from ai_api.streams import scheduled_runner as runner

NOW = datetime(2026, 11, 9, 15, 0)


def _task(**kw) -> ScheduledTask:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        address="5511999999999@s.whatsapp.net",
        kind="reminder",
        text="drink water",
        schedule="every day at 09:00",
        cron_expr="0 9 * * *",
        timezone="America/Rio_Branco",
        status="active",
        consecutive_failures=0,
        run_count=0,
        next_run_at=NOW + timedelta(days=1),
    )
    defaults.update(kw)
    return ScheduledTask(**defaults)


def _settings(**overrides):
    values = {
        "whitelist_phones": "",
        "scheduled_tasks_max_runs_per_day": 24,
        "scheduled_tasks_enabled": True,
    }
    values.update(overrides)
    return patch.object(runner.runtime_config, "get", side_effect=lambda k: values[k])


class TestApplyOutcome:
    def test_sent_resets_failures(self):
        task = _task(consecutive_failures=2)
        runner.apply_outcome(task, "sent", None, NOW)
        assert task.consecutive_failures == 0
        assert task.run_count == 1
        assert task.status == "active"

    def test_third_consecutive_failure_disables(self):
        task = _task(consecutive_failures=2)
        runner.apply_outcome(task, "failed", "blocked", NOW)
        assert task.status == "disabled"
        assert task.disabled_reason == "consecutive_failures"
        assert task.next_run_at is None

    @pytest.mark.parametrize(
        "code", ["cloud_window", "daily_limit", "not_whitelisted", "missed", "user_deleted"]
    )
    def test_skips_never_disable_a_recurring_task(self, code):
        task = _task(consecutive_failures=2)
        for _ in range(3):
            runner.apply_outcome(task, "skipped", code, NOW)
        assert task.status == "active"
        assert task.consecutive_failures == 2
        assert (task.last_status, task.last_error) == ("skipped", code)

    def test_unknown_counts_as_a_failure(self):
        task = _task(consecutive_failures=2)
        runner.apply_outcome(task, "unknown", "unknown", NOW)
        assert task.status == "disabled"

    def test_skipped_one_shot_is_still_disabled(self):
        task = _task(cron_expr=None, run_at=NOW, next_run_at=None)
        runner.apply_outcome(task, "skipped", "cloud_window", NOW)
        assert (task.status, task.disabled_reason) == ("disabled", "cloud_window")

    def test_cancelled_skip_is_not_a_failure(self):
        task = _task(status="cancelled")
        runner.apply_outcome(task, "skipped", "cancelled", NOW)
        assert task.consecutive_failures == 0

    def test_delivered_one_shot_completes(self):
        task = _task(cron_expr=None, run_at=NOW, next_run_at=None)
        runner.apply_outcome(task, "sent", None, NOW)
        assert task.status == "completed"

    def test_failed_one_shot_is_disabled_for_inspection(self):
        task = _task(cron_expr=None, run_at=NOW, next_run_at=None)
        runner.apply_outcome(task, "unknown", "unknown", NOW)
        assert task.status == "disabled"
        assert task.disabled_reason == "unknown"

    def test_one_shot_retry_rearms(self):
        task = _task(cron_expr=None, run_at=NOW, next_run_at=None)
        retry = NOW + timedelta(minutes=2)
        runner.apply_outcome(task, "failed", "transport_error", NOW, retry_at=retry)
        assert task.status == "active"
        assert task.next_run_at == retry


class TestRunAllowed:
    def _user(self, **kw):
        return User(whatsapp_jid=kw.get("jid", "5511@s.whatsapp.net"), phone=kw.get("phone"))

    def _allowed(self, task, user=None, gating="jid", **settings):
        with _settings(**settings), patch.object(runner.settings, "group_gating", gating):
            return runner.run_allowed(user or self._user(), task)

    def test_empty_whitelist_allows(self):
        assert self._allowed(_task(address="5511@s.whatsapp.net"))

    def test_unlisted_chat_is_blocked(self):
        task = _task(address="5511999@s.whatsapp.net")
        assert not self._allowed(task, whitelist_phones="4915755945319")

    def test_listed_phone_passes(self):
        task = _task(address="5511999@s.whatsapp.net")
        assert self._allowed(task, self._user(phone="+5511999"), whitelist_phones="5511999")

    def test_group_under_jid_gating_must_be_listed(self):
        task = _task(address="120363@g.us", created_by_jid="4915755945319@s.whatsapp.net")
        assert not self._allowed(task, whitelist_phones="4915755945319")
        assert self._allowed(task, whitelist_phones="120363@g.us")

    def test_group_under_membership_gating_needs_its_creator_whitelisted(self):
        task = _task(address="120363@g.us", created_by_jid="4915755945319@s.whatsapp.net")
        assert self._allowed(task, gating="membership", whitelist_phones="4915755945319")
        assert not self._allowed(task, gating="membership", whitelist_phones="5511888")

    def test_group_under_membership_gating_without_a_creator_fails_closed(self):
        task = _task(address="120363@g.us", created_by_jid=None)
        assert not self._allowed(task, gating="membership", whitelist_phones="4915755945319")

    def test_group_creator_left_the_whitelist(self):
        task = _task(address="120363@g.us", created_by_jid="5511888@s.whatsapp.net")
        assert not self._allowed(task, gating="membership", whitelist_phones="4915755945319")

    def test_telegram_group_creator(self):
        task = _task(address="tg:-1001", created_by_jid="tg:42")
        assert self._allowed(task, gating="membership", whitelist_phones="tg:42")


def _prepare_db(run, task, user, sent_today=0):
    db = MagicMock()
    db.get.side_effect = lambda model, _id: {TaskRun: run, ScheduledTask: task, User: user}[model]
    db.query.return_value.filter.return_value.scalar.return_value = sent_today
    db.query.return_value.filter.return_value.filter.return_value.scalar.return_value = 0
    return db


class TestPrepare:
    def _run(self, task):
        return TaskRun(id=uuid.uuid4(), task_id=task.id, status="running")

    def _go(self, task, user, **settings):
        run = self._run(task)
        db = _prepare_db(run, task, user, settings.pop("sent_today", 0))
        with _settings(**settings), patch.object(runner, "SessionLocal", return_value=db):
            job = runner._prepare(run.id, NOW)
        return job, run, db

    def test_eligible_run_becomes_a_job(self):
        task = _task(kind="agent")
        user = User(whatsapp_jid=task.address)
        job, run, db = self._go(task, user)
        assert job.text == task.text
        assert job.chat_jid == task.address
        db.commit.assert_not_called()

    @pytest.mark.parametrize(
        "change,code",
        [
            ({"status": "cancelled"}, "cancelled"),
        ],
    )
    def test_inactive_task_is_skipped(self, change, code):
        task = _task(**change)
        job, run, db = self._go(task, User(whatsapp_jid=task.address))
        assert job is None
        assert (run.status, run.error_code) == ("skipped", code)
        db.commit.assert_called_once()

    def test_deleted_chat_is_skipped(self):
        job, run, _ = self._go(_task(), None)
        assert job is None
        assert run.error_code == "user_deleted"

    def test_whitelist_removal_stops_it(self):
        task = _task()
        job, run, _ = self._go(task, User(whatsapp_jid=task.address), whitelist_phones="4915")
        assert job is None
        assert run.error_code == "not_whitelisted"

    def test_cloud_chat_outside_the_window_is_skipped(self):
        task = _task(client_id="cloud")
        user = User(whatsapp_jid=task.address, cloud_last_inbound_at=NOW - timedelta(hours=30))
        job, run, _ = self._go(task, user)
        assert job is None
        assert run.error_code == "cloud_window"

    def test_cloud_chat_inside_the_window_goes(self):
        task = _task(client_id="cloud")
        user = User(whatsapp_jid=task.address, cloud_last_inbound_at=NOW - timedelta(hours=2))
        job, _, _ = self._go(task, user)
        assert job.client_id == "cloud"

    def test_daily_cap(self):
        task = _task()
        job, run, _ = self._go(
            task,
            User(whatsapp_jid=task.address),
            scheduled_tasks_max_runs_per_day=2,
            sent_today=2,
        )
        assert job is None
        assert run.error_code == "daily_limit"


class TestKillSwitchAndCap:
    def test_turning_scheduling_off_stops_runs_already_fired(self):
        task = _task()
        db = _prepare_db(None, task, None)
        with _settings(scheduled_tasks_enabled=False):
            assert runner.skip_reason(db, task, User(), NOW) == "disabled"

    def test_disabled_skip_never_calls_the_model_or_counts(self):
        task = _task(kind="agent", consecutive_failures=2)
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="running")
        db = _prepare_db(run, task, User(whatsapp_jid=task.address))
        with (
            _settings(scheduled_tasks_enabled=False),
            patch.object(runner, "SessionLocal", return_value=db),
        ):
            assert runner._prepare(run.id, NOW) is None
        assert (run.status, run.error_code) == ("skipped", "disabled")
        assert task.status == "active" and task.consecutive_failures == 2

    def test_agent_phase_counts_runs_already_on_their_way(self):
        # Cap 1, nothing sent yet, but one reminder is 'ready': no LLM call.
        task = _task(kind="agent")
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="running")
        db = _prepare_db(run, task, User(whatsapp_jid=task.address))
        db.query.return_value.filter.return_value.filter.return_value.scalar.return_value = 1
        with (
            _settings(scheduled_tasks_max_runs_per_day=1),
            patch.object(runner, "SessionLocal", return_value=db),
        ):
            assert runner._prepare(run.id, NOW) is None
        assert run.error_code == "daily_limit"

    def test_in_flight_filter_shape(self):
        """Agent phase: every ready/sending run counts; send phase: only older ready ones."""
        task = _task()
        captured = []

        def fake_query(*_a):
            q = MagicMock()
            q.filter.side_effect = lambda *c: captured.append(c) or q
            q.scalar.return_value = 0
            return q

        db = MagicMock()
        db.query.side_effect = fake_query
        running = TaskRun(id=uuid.uuid4(), status="running", created_at=NOW)
        runner._counted_today(db, task.user_id, NOW, running)
        agent_sql = str(captured[1][1])
        captured.clear()
        sending = TaskRun(id=uuid.uuid4(), status="sending", created_at=NOW)
        runner._counted_today(db, task.user_id, NOW, sending)
        send_sql = str(captured[1][1])
        assert "created_at <" not in agent_sql
        assert "created_at <" in send_sql
        assert "task_runs.id !=" in str(captured[2][0])


class TestText:
    @pytest.mark.parametrize(
        "text", ["[SILENT]", "  [silent] ", "Nothing new today. [SILENT]", "", "   "]
    )
    def test_silent(self, text):
        assert runner.is_silent(text)

    def test_not_silent(self):
        assert not runner.is_silent("Rain expected tomorrow.")

    def test_bursts_are_joined(self):
        assert runner.join_bursts("Hi!\n---\nIt will rain.\n") == "Hi!\n\nIt will rain."

    def test_agent_prompt_puts_the_time_last_and_guards_recursion(self):
        job = runner._Job(
            run_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            text="check the river level",
            schedule="every day at 07:00",
            timezone="America/Rio_Branco",
            client_id=None,
            chat_jid="x",
        )
        prompt = runner.agent_prompt(job, NOW)
        assert "[SILENT]" in prompt
        assert "Never create, change or cancel scheduled tasks" in prompt
        assert prompt.rstrip().endswith("(America/Rio_Branco)")
        assert "Mon 09 Nov 2026 10:00" in prompt


def _job(text="check the river"):
    return runner._Job(
        run_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        text=text,
        schedule="every day at 09:00",
        timezone="UTC",
        client_id=None,
        chat_jid="5511@s.whatsapp.net",
    )


class TestRecord:
    def _db(self, run, task):
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.one_or_none.return_value = run
        db.get.return_value = task
        return db

    def test_settles_a_run_still_in_the_expected_state(self):
        task = _task()
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, status="sending")
        db = self._db(run, task)
        with patch.object(runner, "SessionLocal", return_value=db):
            assert runner.record(run.id, "sending", "sent", history_text="⏰ hi")
        assert run.status == "sent"
        assert task.run_count == 1
        db.add.assert_called_once()
        db.commit.assert_called_once()

    def test_a_run_the_sweep_settled_is_not_rewritten(self):
        db = self._db(None, None)
        with patch.object(runner, "SessionLocal", return_value=db):
            assert not runner.record(uuid.uuid4(), "sending", "sent", history_text="⏰ hi")
        db.add.assert_not_called()
        db.commit.assert_not_called()


class TestProcess:
    """The agent phase: claim, checks, agent, then ready / silent / failed. Never sends."""

    async def _run(self, job, *, claimed=True, reply=None, agent_error=None, ready=True):
        agent = AsyncMock(return_value=reply, side_effect=agent_error)
        with (
            patch.object(runner, "_claim", return_value=claimed),
            patch.object(runner, "_prepare", return_value=job),
            patch.object(runner, "_agent_reply", agent),
            patch.object(runner, "record") as record,
            patch.object(runner, "_mark_ready", return_value=ready) as mark_ready,
        ):
            await runner.process_scheduled_run(str(job.run_id if job else uuid.uuid4()))
        return agent, record, mark_ready

    async def test_lost_claim_does_nothing(self):
        agent, record, mark_ready = await self._run(_job(), claimed=False)
        agent.assert_not_awaited()
        record.assert_not_called()
        mark_ready.assert_not_called()

    async def test_skipped_run_never_calls_the_model(self):
        agent, record, mark_ready = await self._run(None)
        agent.assert_not_awaited()
        mark_ready.assert_not_called()

    async def test_agent_reply_is_left_ready_for_the_sender(self):
        job = _job()
        _, record, mark_ready = await self._run(job, reply="River is rising.")
        mark_ready.assert_called_once_with(job.run_id, "River is rising.")
        record.assert_not_called()

    async def test_agent_silent_sends_nothing(self):
        job = _job()
        _, record, mark_ready = await self._run(job, reply=None)
        mark_ready.assert_not_called()
        record.assert_called_once_with(job.run_id, "running", "silent")

    async def test_agent_model_error_fails_quietly(self):
        from pydantic_ai.exceptions import ModelAPIError

        job = _job()
        _, record, mark_ready = await self._run(job, agent_error=ModelAPIError("gemini", "down"))
        mark_ready.assert_not_called()
        record.assert_called_once_with(job.run_id, "running", "failed", "model_error")

    async def test_other_agent_error_is_a_run_error(self):
        job = _job()
        _, record, _ = await self._run(job, agent_error=RuntimeError("boom"))
        record.assert_called_once_with(job.run_id, "running", "failed", "run_error")

    async def test_never_raises(self):
        with patch.object(runner, "_claim", side_effect=RuntimeError("db down")):
            await runner.process_scheduled_run(str(uuid.uuid4()))
