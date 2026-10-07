"""File a bug report for the bot's operators (``services/bug_reports.py``)."""

from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ...database import is_group_jid
from ...logger import logger
from ...runtime_config import runtime_config
from ...services.bug_reports import RateLimited, file_report, short_ref
from ..core import AgentDeps, agent
from ._db import safe_rollback

BugCategory = Literal["user_request", "wrong_answer", "user_complaint", "tool_failure", "other"]


async def bug_reports_enabled(
    ctx: RunContext[AgentDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """`prepare` hook: hide the tool unless BUG_REPORTS_ENABLED is on."""
    return tool_def if runtime_config.get("bug_reports_enabled") else None


@agent.tool(prepare=bug_reports_enabled)
async def report_bug(
    ctx: RunContext[AgentDeps],
    title: str,
    description: str,
    category: BugCategory,
) -> str:
    """
    File a bug report for the bot's operators, with this chat's recent messages attached.

    Use when the user asks you to report a problem, when the user clearly says
    you got something wrong or is frustrated with how you answered, when you
    realise you gave wrong information, or when a tool keeps failing. Do not
    use it for ordinary disagreement or matters of taste. File one report per
    issue.

    Args:
        ctx: Run context with database and user info
        title: One-line summary of the problem (e.g. "Gave the wrong opening hours")
        description: What was asked, what you answered or did, and what was
            expected instead. Factual; never include passwords or other secrets.
        category: "user_request" if the user asked you to report it;
            "wrong_answer" if you realised you were wrong; "user_complaint" if
            the user complained; "tool_failure" if a tool kept failing; else "other"

    Returns:
        The report's reference, or why it was not filed
    """
    logger.info(f"🐞 TOOL CALLED: report_bug (category={category})")
    deps = ctx.deps
    try:
        report = file_report(
            deps.db,
            source="user" if category == "user_request" else "agent",
            category=category,
            title=title,
            description=description,
            # Identity comes from the run's own deps, never from arguments.
            user_id=deps.user_id,
            whatsapp_jid=deps.whatsapp_jid,
            client_id=deps.client_id,
            conversation_type="group" if is_group_jid(deps.whatsapp_jid) else "private",
            job_id=deps.job_id,
        )
        return (
            f"Bug report filed (reference #{short_ref(report.id)}). The operators will review it."
        )
    except RateLimited:
        return (
            "Not filed: this chat has already sent several reports in the last hour. "
            "Tell the user to try again later."
        )
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error filing bug report: {e}", exc_info=True)
        return "Failed to file the bug report. Please try again."
