"""streams/scheduled_runner.py: claim, send-time checks, delivery outcomes, auto-disable."""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ai_api.database import ScheduledTask, TaskRun, User
from ai_api.streams import scheduled_runner as runner
from ai_api.whatsapp import WhatsAppClientError, WhatsAppNotConnectedError

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


class TestChatAllowed:
    def _user(self, **kw):
        return User(whatsapp_jid=kw.get("jid", "5511@s.whatsapp.net"), phone=kw.get("phone"))

    def test_empty_whitelist_allows(self):
        with _settings():
            assert runner._chat_allowed(self._user(), "5511@s.whatsapp.net")

    def test_unlisted_chat_is_blocked(self):
        with _settings(whitelist_phones="4915755945319"):
            assert not runner._chat_allowed(self._user(), "5511999@s.whatsapp.net")

    def test_listed_phone_passes(self):
        with _settings(whitelist_phones="5511999"):
            assert runner._chat_allowed(self._user(phone="+5511999"), "5511999@s.whatsapp.net")

    def test_group_under_jid_gating_must_be_listed(self):
        with _settings(whitelist_phones="4915755945319"):
            assert not runner._chat_allowed(self._user(), "120363@g.us")

    def test_group_under_membership_gating_passes(self):
        with (
            _settings(whitelist_phones="4915755945319"),
            patch.object(runner.settings, "group_gating", "membership"),
        ):
            assert runner._chat_allowed(self._user(), "120363@g.us")


def _prepare_db(run, task, user, sent_today=0):
    db = MagicMock()
    db.get.side_effect = lambda model, _id: {TaskRun: run, ScheduledTask: task, User: user}[model]
    db.query.return_value.filter.return_value.scalar.return_value = sent_today
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
        task = _task()
        user = User(whatsapp_jid=task.address)
        job, run, db = self._go(task, user)
        assert job.address == task.address
        assert job.platform == "baileys"
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
        assert job.platform == "cloud"

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
            task_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            kind="agent",
            text="check the river level",
            schedule="every day at 07:00",
            timezone="America/Rio_Branco",
            platform="baileys",
            client_id=None,
            address="x",
            chat_jid="x",
        )
        prompt = runner.agent_prompt(job, NOW)
        assert "[SILENT]" in prompt
        assert "Never create, change or cancel scheduled tasks" in prompt
        assert prompt.rstrip().endswith("(America/Rio_Branco)")
        assert "Mon 09 Nov 2026 10:00" in prompt


def _job(kind="reminder", text="drink water"):
    return runner._Job(
        run_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind=kind,
        text=text,
        schedule="every day at 09:00",
        timezone="UTC",
        platform="baileys",
        client_id=None,
        address="5511@s.whatsapp.net",
        chat_jid="5511@s.whatsapp.net",
    )


class TestProcess:
    def _patches(self, job, *, claimed=True, send=None, agent=None):
        return (
            patch.object(runner, "_claim", return_value=claimed),
            patch.object(runner, "_prepare", return_value=job),
            patch.object(runner, "_send", new_callable=AsyncMock, side_effect=send),
            patch.object(runner, "_record"),
            patch.object(runner, "_agent_reply", new_callable=AsyncMock, side_effect=agent),
        )

    async def _run(self, job, **kw):
        p_claim, p_prepare, p_send, p_record, p_agent = self._patches(job, **kw)
        with p_claim, p_prepare, p_send as send, p_record as record, p_agent as agent:
            await runner.process_scheduled_run(str(job.run_id if job else uuid.uuid4()))
        return send, record, agent

    async def test_lost_claim_does_nothing(self):
        send, record, _ = await self._run(_job(), claimed=False)
        send.assert_not_awaited()
        record.assert_not_called()

    async def test_reminder_is_sent_and_saved_to_history(self):
        job = _job()
        send, record, agent = await self._run(job)
        send.assert_awaited_once_with(job, "⏰ drink water")
        agent.assert_not_awaited()
        record.assert_called_once_with(job.run_id, "sent", history_text="⏰ drink water")

    async def test_maybe_delivered_is_unknown_and_never_retried(self):
        job = _job()
        _, record, _ = await self._run(job, send=httpx.ReadTimeout("slow"))
        record.assert_called_once_with(job.run_id, "unknown", "unknown", provably_not_sent=False)

    async def test_disconnected_client_counts_as_not_sent(self):
        job = _job()
        _, record, _ = await self._run(job, send=WhatsAppNotConnectedError())
        args = record.call_args
        assert args.args[1] == "failed"
        assert args.kwargs["provably_not_sent"] is True

    async def test_refused_address_is_final(self):
        job = _job()
        _, record, _ = await self._run(job, send=WhatsAppClientError("x", status_code=404))
        record.assert_called_once_with(
            job.run_id, "failed", "not_on_whatsapp", provably_not_sent=False
        )

    async def test_agent_silent_sends_nothing(self):
        job = _job(kind="agent")
        p = self._patches(job)
        with (
            p[0],
            p[1],
            p[2] as send,
            p[3] as record,
            patch.object(runner, "_agent_reply", new_callable=AsyncMock, return_value=None),
        ):
            await runner.process_scheduled_run(str(job.run_id))
        send.assert_not_awaited()
        record.assert_called_once_with(job.run_id, "silent")

    async def test_agent_reply_is_sent(self):
        job = _job(kind="agent")
        p = self._patches(job)
        with (
            p[0],
            p[1],
            p[2] as send,
            p[3] as record,
            patch.object(
                runner, "_agent_reply", new_callable=AsyncMock, return_value="River is rising."
            ),
        ):
            await runner.process_scheduled_run(str(job.run_id))
        send.assert_awaited_once_with(job, "River is rising.")
        record.assert_called_once_with(job.run_id, "sent", history_text="River is rising.")

    async def test_agent_model_error_fails_quietly(self):
        from pydantic_ai.exceptions import ModelAPIError

        job = _job(kind="agent")
        send, record, _ = await self._run(job, agent=ModelAPIError("gemini", "down"))
        send.assert_not_awaited()
        record.assert_called_once_with(job.run_id, "failed", "model_error")

    async def test_never_raises(self):
        with patch.object(runner, "_claim", side_effect=RuntimeError("db down")):
            await runner.process_scheduled_run(str(uuid.uuid4()))
