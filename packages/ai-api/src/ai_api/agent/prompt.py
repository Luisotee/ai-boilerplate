"""The base system prompt, as named sections an operator can override one at a time.

The model gets ``base_system_prompt`` (``agent/core.py``) first: these sections,
joined by a blank line, then the other instructions hooks. The code default of
each section is the source of truth. An override (``bot_prompt_overrides``)
stores the sha256 of the default it was written against, and applies only while
that default is unchanged: once a commit changes the section, the code text is
used again and the override is kept, flagged ``stale``, for the operator to
re-apply or discard in FleetView. So a code change can never be hidden by a DB
copy, which is what the old whole-prompt ``bot_prompt`` row did: a saved copy of
the default silently shadowed every later prompt change.

The ``operator`` section is empty in code, so it never goes stale: it is the
place for an operator's own extra instructions.

``"all"`` is the legacy whole-prompt override (``PUT /admin/prompt``), under the
same rule: it replaces every section while the composed default is unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ..database import get_prompt_overrides
from ..logger import logger


@dataclass(frozen=True)
class PromptSection:
    key: str
    title: str
    default: str


PROMPT_SECTIONS: tuple[PromptSection, ...] = (
    PromptSection(
        "identity",
        "Identity",
        "You are a helpful AI assistant chatting on WhatsApp and Telegram.",
    ),
    PromptSection(
        "tone",
        "Tone",
        """Be concise, friendly and helpful; keep replies brief and to the point.
If you don't know something, say so clearly.""",
    ),
    PromptSection(
        "tools",
        "Tool rules",
        """Your tools are described in their own definitions. Rules for choosing between them:
- Asking to stop or start audio/voice replies, to change their language or the
  transcription language, or to stop or resume update messages is a settings change:
  call the settings tool. Deleting data: clean_user_data. You cannot do either by
  promising it; never say it is done without calling the tool.
- clean_user_data with level "data" or "all": ALWAYS say what will be deleted and wait
  for the user's yes first, however explicit the request. "messages": confirm unless
  the request is explicit.
- Recall: use what is already in the conversation first. For "what did we talk about
  yesterday" or "the last 50 messages", use get_chat_history (this chat, in order). For
  a topic mentioned some time ago, use search_conversation_history. For questions about
  uploaded documents, use search_knowledge_base and cite document name, page and section.
- Current facts (news, prices, today's events): web_search. Stable facts and
  definitions: your own knowledge or wikipedia_lookup. A link the user sent: fetch_website.
- Arithmetic: calculate. Units: convert_units. Weather: get_weather.
- No tools for greetings, chitchat, or questions the conversation already answers.
- send_whatsapp_message is only for an out-of-band note before a slow tool finishes
  ("one moment, checking..."); split a normal reply with --- instead. The send tools
  only ever reach the current chat.""",
    ),
    PromptSection(
        "bursts",
        "Message bursts",
        """Natural message bursts:
Multi-part replies (intro + answer, answer + follow-up question, distinct thoughts)
feel more human when split. Put --- on its own line between parts and each part is
sent as a separate message. Aim for 2-3 bursts. Keep lists, code blocks and
single-thought answers as one message; never put --- inside a list or code block.""",
    ),
    PromptSection("operator", "Operator instructions", ""),
)

SECTIONS_BY_KEY: dict[str, PromptSection] = {s.key: s for s in PROMPT_SECTIONS}

#: Key of the legacy whole-prompt override (``PUT /admin/prompt``).
WHOLE_OVERRIDE_KEY = "all"

SectionStatus = Literal["default", "override", "stale"]


def section_hash(text: str) -> str:
    """sha256 hex of a code default: what an override records it was based on."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose(texts: Iterable[str]) -> str:
    """Join section texts the way the model gets them; an empty section is left out."""
    return "\n\n".join(t for t in texts if t)


#: The composed code default (every section's default).
DEFAULT_SYSTEM_PROMPT = compose(s.default for s in PROMPT_SECTIONS)
DEFAULT_PROMPT_HASH = section_hash(DEFAULT_SYSTEM_PROMPT)


@dataclass(frozen=True)
class ResolvedSection:
    section: PromptSection
    #: What the model gets for this section now.
    text: str
    status: SectionStatus
    override: Any | None = None  # the BotPromptOverride row, kept even when stale

    @property
    def default_hash(self) -> str:
        return section_hash(self.section.default)


@dataclass(frozen=True)
class WholeOverride:
    active: bool = False
    stale: bool = False
    content: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ResolvedPrompt:
    sections: tuple[ResolvedSection, ...]
    whole: WholeOverride
    #: Stored rows for keys the code no longer has: never applied, listed so they can be deleted.
    orphans: tuple[Any, ...] = ()

    @property
    def sections_text(self) -> str:
        """The sections composed, ignoring any whole-prompt override."""
        return compose(s.text for s in self.sections)

    @property
    def text(self) -> str:
        """The base prompt the model gets."""
        if self.whole.active and self.whole.content is not None:
            return self.whole.content
        return self.sections_text

    @property
    def sections_applied(self) -> bool:
        """False while a fresh whole override replaces every section."""
        return not self.whole.active

    @property
    def applied_sections(self) -> list[ResolvedSection]:
        """Section overrides the model actually gets (none under a fresh whole override).

        ``/admin/prompt`` derives both ``is_overridden`` and ``sections_overridden``
        from this one list, so the two can never disagree.
        """
        if not self.sections_applied:
            return []
        return [s for s in self.sections if s.status == "override"]

    @property
    def stale_keys(self) -> list[str]:
        keys = [s.section.key for s in self.sections if s.status == "stale"]
        return keys + ([WHOLE_OVERRIDE_KEY] if self.whole.stale else [])


def resolve_prompt(overrides: Mapping[str, Any]) -> ResolvedPrompt:
    """Apply stored overrides (rows with ``content``/``base_hash``/``updated_at``) to the code.

    Pure. An override applies only while its ``base_hash`` matches the current
    code default; otherwise the code default applies and the section is ``stale``.
    Rows for keys the code no longer has are ignored (kept in ``orphans``).
    """
    sections = []
    for section in PROMPT_SECTIONS:
        row = overrides.get(section.key)
        if row is None:
            sections.append(ResolvedSection(section, section.default, "default"))
        elif row.base_hash == section_hash(section.default):
            sections.append(ResolvedSection(section, row.content or "", "override", row))
        else:
            sections.append(ResolvedSection(section, section.default, "stale", row))

    whole = WholeOverride()
    row = overrides.get(WHOLE_OVERRIDE_KEY)
    if row is not None:
        fresh = row.base_hash == DEFAULT_PROMPT_HASH and bool(row.content)
        whole = WholeOverride(
            active=fresh, stale=not fresh, content=row.content, updated_at=row.updated_at
        )
    orphans = tuple(
        row
        for key, row in overrides.items()
        if key not in SECTIONS_BY_KEY and key != WHOLE_OVERRIDE_KEY
    )
    return ResolvedPrompt(tuple(sections), whole, orphans)


#: (key, base_hash) pairs already warned about in this process.
_warned_stale: set[tuple[str, str]] = set()


def warn_on_stale(resolved: ResolvedPrompt, overrides: Mapping[str, Any]) -> None:
    """Log each stale override once per process: it is paused, the code text applies."""
    for key in resolved.stale_keys:
        row = overrides.get(key)
        marker = (key, getattr(row, "base_hash", ""))
        if marker in _warned_stale:
            continue
        _warned_stale.add(marker)
        logger.warning(
            f"⚠️ Prompt override '{key}' is stale: the code default changed after it was "
            "saved, so the code text applies. Re-apply or discard it in FleetView "
            "(PUT/DELETE /admin/prompt/sections/{key})."
        )


def load_resolved_prompt(db) -> ResolvedPrompt:
    """The base prompt for this run: code defaults with the fresh overrides applied."""
    overrides = get_prompt_overrides(db)
    resolved = resolve_prompt(overrides)
    if resolved.stale_keys:
        warn_on_stale(resolved, overrides)
    return resolved
