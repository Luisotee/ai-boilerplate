"""The report_bug agent tool: who it files as, the cap, and the on/off switch."""

from unittest.mock import MagicMock, patch

from ai_api.agent.core import agent
from ai_api.agent.tools import bug_report as tool_module
from ai_api.agent.tools.bug_report import bug_reports_enabled, report_bug
from ai_api.services.bug_reports import RateLimited

SECRET = "postgresql://admin:hunter2@db.internal:5432/prod"


def _ctx(jid="123@s.whatsapp.net"):
    ctx = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = jid
    ctx.deps.client_id = "telegram"
    ctx.deps.job_id = "job-9"
    return ctx


def _report():
    report = MagicMock()
    report.id = "abcdef12-0000-0000-0000-000000000000"
    return report


class TestReportBug:
    async def test_user_request_files_as_user(self):
        ctx = _ctx()
        with patch.object(tool_module, "file_report", return_value=_report()) as file:
            result = await report_bug(ctx, "Wrong hours", "Said 9, is 10", "user_request")
        kwargs = file.call_args.kwargs
        assert kwargs["source"] == "user"
        assert kwargs["category"] == "user_request"
        assert kwargs["user_id"] == "user-123"
        assert kwargs["client_id"] == "telegram"
        assert kwargs["job_id"] == "job-9"
        assert kwargs["conversation_type"] == "private"
        assert "#abcdef12" in result

    async def test_agent_judgement_files_as_agent(self):
        with patch.object(tool_module, "file_report", return_value=_report()) as file:
            await report_bug(_ctx(), "t", "d", "wrong_answer")
        assert file.call_args.kwargs["source"] == "agent"

    async def test_group_chat(self):
        with patch.object(tool_module, "file_report", return_value=_report()) as file:
            await report_bug(_ctx("120363012345678@g.us"), "t", "d", "user_complaint")
        assert file.call_args.kwargs["conversation_type"] == "group"

    async def test_rate_limited(self):
        ctx = _ctx()
        with patch.object(tool_module, "file_report", side_effect=RateLimited):
            result = await report_bug(ctx, "t", "d", "user_request")
        assert "Not filed" in result
        ctx.deps.db.rollback.assert_not_called()

    async def test_failure_rolls_back_without_leaking(self):
        ctx = _ctx()
        with patch.object(tool_module, "file_report", side_effect=RuntimeError(SECRET)):
            result = await report_bug(ctx, "t", "d", "other")
        ctx.deps.db.rollback.assert_called_once()
        assert SECRET not in result
        assert "Failed" in result


class TestPrepareHook:
    def test_registered_with_hook(self):
        assert agent._function_toolset.tools["report_bug"].prepare is bug_reports_enabled

    async def test_hidden_when_disabled(self):
        tool_def = MagicMock()
        with patch.object(tool_module.runtime_config, "get", return_value=False):
            assert await bug_reports_enabled(MagicMock(), tool_def) is None
        with patch.object(tool_module.runtime_config, "get", return_value=True):
            assert await bug_reports_enabled(MagicMock(), tool_def) is tool_def


class TestGuidance:
    async def test_follows_the_switch(self):
        from ai_api.agent import core

        with patch.object(core.runtime_config, "get", return_value=True):
            assert "report_bug" in await core.bug_report_guidance(MagicMock())
        with patch.object(core.runtime_config, "get", return_value=False):
            assert await core.bug_report_guidance(MagicMock()) == ""
