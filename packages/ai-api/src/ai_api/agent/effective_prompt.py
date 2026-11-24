"""Render the instructions the model gets, by the agent's own hooks (GET /admin/prompt/effective).

Nothing here re-describes the prompt: it calls every function in
``INSTRUCTION_HOOKS`` with a real ``RunContext`` for a sample (or real) chat and
joins the outputs the way Pydantic AI does, so a hook added in code shows up in
FleetView with no change there. ``tests/mocked/test_effective_prompt.py`` checks
the result against the instructions a real agent run sends.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from .. import surfaces as surfaces_module
from ..database import is_group_jid
from ..services import shared_groups as shared_groups_module
from ..surfaces import CLI_CLIENT_ID, CLI_JID_PREFIX, WEB_CLIENT_ID, WEB_JID_PREFIX
from . import prompt as prompt_module
from .core import INSTRUCTION_HOOKS, AgentDeps, base_system_prompt
from .prompt import load_resolved_prompt

Chat = Literal["private", "group"]
Surface = Literal["whatsapp", "telegram", "web"]

#: Never a real user, so the per-chat hooks find no memory.
PREVIEW_USER_ID = "00000000-0000-0000-0000-000000000000"

_PREVIEW_JIDS: dict[tuple[Surface, Chat], str] = {
    ("whatsapp", "private"): "5500000000000@s.whatsapp.net",
    ("whatsapp", "group"): "120363000000000000@g.us",
    ("telegram", "private"): "tg:1",
    ("telegram", "group"): "tg:-1",
    ("web", "private"): f"{WEB_JID_PREFIX}preview",
}
_CLIENT_IDS: dict[Surface, str] = {
    "whatsapp": "baileys",
    "telegram": "telegram",
    "web": WEB_CLIENT_ID,
}


@dataclass(frozen=True)
class Block:
    key: str
    kind: Literal["section", "hook"]
    title: str
    content: str
    source: Literal["code", "override"]
    editable: bool
    stale_override: bool


@dataclass(frozen=True)
class EffectivePrompt:
    content: str
    blocks: list[Block]
    chat: Chat
    surface: Surface


def surface_of_jid(jid: str, whatsapp_client_id: str | None = None) -> tuple[Surface, str]:
    """(surface, client_id) a real user's chat runs on.

    A ``cli:`` user renders WhatsApp markup like a chat, and a Cloud API user is
    on WhatsApp too, so both are reported as ``whatsapp`` (FleetView's surface
    enum is closed); the client id keeps the hooks' per-client output right.
    """
    if jid.startswith("tg:"):
        return "telegram", "telegram"
    if jid.startswith(WEB_JID_PREFIX):
        return "web", WEB_CLIENT_ID
    if jid.startswith(CLI_JID_PREFIX):
        return "whatsapp", CLI_CLIENT_ID
    if whatsapp_client_id == "cloud" and not is_group_jid(jid):
        return "whatsapp", "cloud"
    return "whatsapp", "baileys"


def preview_deps(
    db, chat: Chat, surface: Surface, user=None, chat_jid: str | None = None
) -> AgentDeps:
    """Deps for a sample chat, or for ``user``'s own chat (its core memory).

    ``chat_jid`` is the chat the user was looked up by (a linked user's
    ``tg:`` chat runs with that JID, like a chat job); default: the primary JID.
    """
    if user is not None:
        jid = chat_jid or user.whatsapp_jid
        _, client_id = surface_of_jid(jid, getattr(user, "whatsapp_client_id", None))
        user_id = str(user.id)
    else:
        if (surface, chat) not in _PREVIEW_JIDS:
            raise ValueError(f"no {chat} chat on {surface}")
        jid = _PREVIEW_JIDS[(surface, chat)]
        client_id = _CLIENT_IDS[surface]
        user_id = PREVIEW_USER_ID
    return AgentDeps(
        db=db, user_id=user_id, whatsapp_jid=jid, recent_message_ids=[], client_id=client_id
    )


def join_instructions(parts: list[str]) -> str:
    """Pydantic AI's own join (agent/__init__.py ``get_instructions``)."""
    return "\n\n".join(p for p in parts if p).strip()


async def build_effective_prompt(deps: AgentDeps) -> EffectivePrompt:
    """Call every instructions hook for ``deps`` and return the blocks and the whole text."""
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    outputs: list[str] = []
    blocks: list[Block] = []
    for hook in INSTRUCTION_HOOKS:
        text = await hook.func(ctx) or ""
        outputs.append(text)
        if hook.func is base_system_prompt:
            blocks.extend(_base_blocks(deps.db))
        else:
            blocks.append(
                Block(f"hook:{hook.func.__name__}", "hook", hook.title, text, "code", False, False)
            )
    surface, _ = surface_of_jid(deps.whatsapp_jid)
    chat: Chat = "group" if is_group_jid(deps.whatsapp_jid) else "private"
    return EffectivePrompt(join_instructions(outputs), blocks, chat, surface)


def _base_blocks(db) -> list[Block]:
    resolved = load_resolved_prompt(db)
    if resolved.whole.active:
        return [
            Block(
                "whole_override",
                "section",
                "Whole prompt (override)",
                resolved.text,
                "override",
                True,
                False,
            )
        ]
    return [
        Block(
            f"section:{s.section.key}",
            "section",
            s.section.title,
            s.text,
            "override" if s.status == "override" else "code",
            True,
            s.status == "stale",
        )
        for s in resolved.sections
    ]


#: Modules whose code changes what a hook writes (keep in step with agent/core.py's imports).
_HOOK_HELPERS = (surfaces_module, shared_groups_module)


@lru_cache(maxsize=1)
def prompt_code_hash() -> str:
    """Fingerprint of the code that builds the instructions in this process.

    The source files of the prompt sections, of every hook's module and of the
    helpers the hooks shape text with (``_HOOK_HELPERS``): two processes
    reporting different values run different prompt code (one image rebuilt
    without the other). A tool's own docstring is not covered.
    """
    files = {inspect.getsourcefile(m) for m in (prompt_module, *_HOOK_HELPERS)}
    files |= {inspect.getsourcefile(h.func) for h in INSTRUCTION_HOOKS}
    digest = hashlib.sha256()
    for path in sorted(f for f in files if f):
        digest.update(Path(path).read_bytes())
    return digest.hexdigest()[:16]
