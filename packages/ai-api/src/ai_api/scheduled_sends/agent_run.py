"""Write a prompt send's text with the chat agent, unattended.

The same isolation as a scheduled task's agent run (``streams/scheduled_runner.py``),
through the same flag: ``AgentDeps(scheduled_run=True)`` keeps only the
read-only ``SCHEDULED_RUN_TOOLS`` (``agent/core.py``), and the run has no
history, no core memory (a placeholder ``user_id``) and a synthetic JID, so a
preview or a run never creates a ``users`` row nor sends anything itself. The
operator's prompt is wrapped in ``FIXED_RULES`` (code, not editable), and
``[SILENT]`` as the whole answer means "send nothing". An over-long text gets
one rewrite.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from ..config import settings
from ..database import SessionLocal
from ..formatting import markdown_to_whatsapp
from ..logger import logger
from ..streams.scheduled_runner import SILENT_TOKEN, join_bursts

MAX_PROMPT_CHARS = 2000
MAX_TEXT_CHARS = 3000
#: Bound on one generation (both attempts): no user waits, but a run holds a slot.
RUN_TIMEOUT_SECONDS = 180.0
# The WHOLE stripped output, optionally bracketed or bolded: "silent" inside a
# sentence is ordinary text, not the sentinel.
_SENTINEL_RE = re.compile(r"[\[*_\s]*SILENT[\]*_\s.]*", re.IGNORECASE)
_NO_USER_ID = "00000000-0000-0000-0000-000000000000"

FIXED_RULES = f"""[AUTOMATIC TASK: SCHEDULED MESSAGE]
This is not a message from a person: it is a message the bot's operators scheduled. The text
you answer is sent to the chat exactly as it is.

Fixed rules (they take precedence over the instructions below):
1. Numbers, dates and facts come only from your tools in this run, or from calculate on
   them. Never estimate or invent them.
2. If you can't write the message safely (the data you need failed or isn't available),
   answer exactly {SILENT_TOKEN} and nothing else.
3. Answer only with the message text, no comments about the task, in at most about
   1500 characters, in the language the instructions are written in.
4. Chat markup only: *bold* with one asterisk, _italic_, lists with "- ". No Markdown
   headings, tables or [text](url) links."""

_AUDIENCE_RULES = {
    "group": "Audience: a group chat. Address the group, not one person.",
    "private": "Audience: a private chat with one person.",
}


@dataclass
class GeneratedText:
    text: str | None = None
    error: str | None = None
    prompt: str = ""


def is_no_send(text: str) -> bool:
    return bool(_SENTINEL_RE.fullmatch((text or "").strip()))


def build_prompt(*, content: str, chat_type: str) -> str:
    """FIXED_RULES + the audience rule + the operator's prompt."""
    body = content.strip()[:MAX_PROMPT_CHARS]
    return "\n\n".join(
        [FIXED_RULES, _AUDIENCE_RULES.get(chat_type, _AUDIENCE_RULES["group"]), body]
    )


def problems(text: str) -> list[str]:
    if len(text) > MAX_TEXT_CHARS:
        return [f"too long ({len(text)} characters, at most {MAX_TEXT_CHARS})"]
    return []


async def _answer(prompt: str, deps) -> str:
    from ..agent import get_ai_response

    reply = ""
    async for token in get_ai_response(prompt, None, agent_deps=deps):
        reply += token
    return join_bursts(markdown_to_whatsapp(reply)).strip()


async def generate_text(
    *, content: str, chat_type: str, http_client: httpx.AsyncClient
) -> GeneratedText:
    """One unattended agent run. Never raises: a failure is ``GeneratedText.error``."""
    # Lazy: the tool modules must be imported for their @agent.tool decorators.
    from ..agent import tools as _tools  # noqa: F401
    from ..agent.core import AgentDeps
    from ..embeddings import create_embedding_service

    prompt = build_prompt(content=content, chat_type=chat_type)
    result = GeneratedText(prompt=prompt)
    db = SessionLocal()  # its own Session: runs execute concurrently
    try:
        deps = AgentDeps(
            db=db,
            user_id=_NO_USER_ID,
            whatsapp_jid=f"scheduled-send:{chat_type}",
            recent_message_ids=[],
            embedding_service=create_embedding_service(settings.gemini_api_key),
            http_client=http_client,
            is_group_admin=None,  # no sender: group-changing tools fail closed
            scheduled_run=True,
        )

        async def attempts() -> None:
            attempt_prompt = prompt
            for attempt in (1, 2):
                text = await _answer(attempt_prompt, deps)
                if not text:
                    result.error = "empty_output"
                    return
                if is_no_send(text):
                    result.error = "no_send"
                    return
                found = problems(text)
                if not found:
                    result.text = text
                    return
                logger.warning("Scheduled send text rejected (attempt %d): %s", attempt, found)
                attempt_prompt = (
                    prompt
                    + "\n\nThe previous version was rejected: "
                    + "; ".join(found)
                    + ". Write it again without that."
                )
            result.error = "guardrails"

        await asyncio.wait_for(attempts(), timeout=RUN_TIMEOUT_SECONDS)
        return result
    except TimeoutError:
        result.text, result.error = None, "timeout"
        logger.warning("Scheduled send agent run timed out after %.0fs", RUN_TIMEOUT_SECONDS)
        return result
    except Exception as exc:
        result.text, result.error = None, f"agent_error:{type(exc).__name__}"[:64]
        logger.error("Scheduled send agent run failed", exc_info=True)
        return result
    finally:
        db.close()
