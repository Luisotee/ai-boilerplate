"""scheduled_sends/schedule.py, and agent_run's and planning's pure parts."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from ai_api.scheduled_sends import agent_run
from ai_api.scheduled_sends.agent_run import FIXED_RULES, build_prompt, is_no_send, problems
from ai_api.scheduled_sends.planning import broadcast_audience
from ai_api.scheduled_sends.schedule import (
    ScheduleInputError,
    compile_schedule,
    first_run,
    next_runs,
    period_of,
)

SP = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 20, 12, 0)  # a Friday, 09:00 in São Paulo


class TestCompile:
    def test_weekly_compiles_to_cron_in_the_send_zone(self):
        c = compile_schedule({"kind": "weekly", "time": "08:00", "weekdays": [5, 1]}, SP, NOW)
        assert c.cron_expr == "0 8 * * 1,5"
        assert c.description == "every monday,friday at 08:00"
        # Monday 08:00 in São Paulo (UTC-3) is 11:00 UTC.
        assert first_run(c, SP, NOW) == datetime(2026, 11, 23, 11, 0)

    def test_every_weekday_and_every_day(self):
        weekdays = {"kind": "weekly", "time": "08:00", "weekdays": [1, 2, 3, 4, 5]}
        assert compile_schedule(weekdays, SP, NOW).description == "every weekday at 08:00"
        all_days = {"kind": "weekly", "time": "08:00", "weekdays": list(range(7))}
        assert compile_schedule(all_days, SP, NOW).description == "every day at 08:00"

    def test_daily_and_monthly(self):
        daily = compile_schedule({"kind": "daily", "time": "7:05"}, SP, NOW)
        assert daily.cron_expr == "5 7 * * *" and daily.description == "every day at 07:05"
        monthly = compile_schedule({"kind": "monthly", "time": "09:00", "day_of_month": 5}, SP, NOW)
        assert monthly.cron_expr == "0 9 5 * *"
        assert monthly.description == "every month on day 5 at 09:00"
        assert period_of(monthly.cron_expr, SP, datetime(2026, 12, 5, 12, 0)) == timedelta(days=31)

    def test_once_is_naive_local_time(self):
        c = compile_schedule({"kind": "once", "run_at": "2026-11-21T08:00"}, SP, NOW)
        assert c.run_at == datetime(2026, 11, 21, 11, 0) and c.cron_expr is None
        assert c.description == "once, Sat 21 Nov 2026 08:00"
        assert next_runs(run_at=c.run_at, cron_expr=None, tz=SP, now=NOW) == [c.run_at]

    @pytest.mark.parametrize(
        "schedule",
        [
            {"kind": "once", "run_at": "2026-11-20T08:00"},  # past
            {"kind": "once", "run_at": "2028-01-01T08:00"},  # > 1 year
            {"kind": "once", "run_at": "tomorrow"},
            {"kind": "daily", "time": "24:00"},
            {"kind": "weekly", "time": "08:00", "weekdays": []},
            {"kind": "weekly", "time": "08:00", "weekdays": [7]},
            {"kind": "weekly", "time": "08:00", "weekdays": [1, 1]},
            {"kind": "weekly", "time": "08:00", "weekdays": [True]},
            {"kind": "monthly", "time": "08:00", "day_of_month": 29},
            {"kind": "hourly", "time": "08:00"},
        ],
    )
    def test_rejects(self, schedule):
        with pytest.raises(ScheduleInputError):
            compile_schedule(schedule, SP, NOW)

    def test_next_runs_lists_upcoming_slots(self):
        slots = next_runs(run_at=None, cron_expr="0 8 * * 1", tz=SP, now=NOW, count=3)
        assert [s - slots[0] for s in slots] == [timedelta(0), timedelta(7), timedelta(14)]


class TestPrompt:
    def test_fixed_rules_come_first_then_the_audience_then_the_prompt(self):
        prompt = build_prompt(content="Weekly news {x}", chat_type="group")
        assert prompt.startswith(FIXED_RULES)
        assert "a group chat" in prompt and prompt.endswith("Weekly news {x}")

    def test_the_prompt_is_capped(self):
        assert build_prompt(content="x" * 5000, chat_type="private").count("x") <= 2000 + 50

    def test_sentinel_and_near_misses(self):
        assert is_no_send("[SILENT]") and is_no_send("silent") and not is_no_send("ok")
        assert is_no_send("*[SILENT]*") and is_no_send(" SILENT. ")

    def test_the_word_inside_a_message_is_not_the_sentinel(self):
        assert not is_no_send("The office stays silent on Sundays.")
        assert not is_no_send("[SILENT] but here is the summary")

    def test_only_an_overlong_text_is_rejected(self):
        assert problems("x" * 3001)
        assert problems("Average price 12.00 this week, see https://example.com") == []


class TestGenerate:
    async def _generate(self, replies):
        answers = iter(replies)

        async def answer(prompt, deps):
            answer.prompts.append(prompt)
            answer.deps = deps
            return next(answers)

        answer.prompts = []
        with (
            patch.object(agent_run, "_answer", answer),
            patch.object(agent_run, "SessionLocal", MagicMock()),
            patch("ai_api.embeddings.create_embedding_service", return_value=None),
        ):
            result = await agent_run.generate_text(
                content="News", chat_type="group", http_client=AsyncMock()
            )
        return result, answer

    async def test_a_text_comes_back_from_an_isolated_run(self):
        result, answer = await self._generate(["Hello group"])
        assert (result.text, result.error) == ("Hello group", None)
        deps = answer.deps
        # Read-only tools, no memory (placeholder user), no sender (fail closed).
        assert deps.scheduled_run is True and deps.is_group_admin is None
        assert deps.user_id == agent_run._NO_USER_ID

    async def test_silent_sends_nothing(self):
        result, _ = await self._generate(["[SILENT]"])
        assert (result.text, result.error) == (None, "no_send")

    async def test_an_overlong_text_gets_one_rewrite(self):
        result, answer = await self._generate(["x" * 3001, "short"])
        assert result.text == "short"
        assert "rejected" in answer.prompts[1]

    async def test_two_overlong_texts_fail(self):
        result, _ = await self._generate(["x" * 3001, "y" * 3001])
        assert (result.text, result.error) == (None, "guardrails")

    async def test_an_agent_error_never_raises(self):
        async def boom(prompt, deps):
            raise RuntimeError("model down")

        with (
            patch.object(agent_run, "_answer", boom),
            patch.object(agent_run, "SessionLocal", MagicMock()),
            patch("ai_api.embeddings.create_embedding_service", return_value=None),
        ):
            result = await agent_run.generate_text(
                content="News", chat_type="private", http_client=AsyncMock()
            )
        assert result.error == "agent_error:RuntimeError"


def test_chats_audience_maps_to_a_broadcast_enum():
    def user(kind):
        return SimpleNamespace(conversation_type=kind)

    assert broadcast_audience("chats", [user("private")]) == "private"
    assert broadcast_audience("chats", [user("group")]) == "groups"
    assert broadcast_audience("chats", [user("group"), user("private")]) == "all"
    assert broadcast_audience("groups", []) == "groups"
