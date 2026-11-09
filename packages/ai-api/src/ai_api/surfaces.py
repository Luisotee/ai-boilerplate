"""Where a message comes from and how its reply is rendered.

Most chats come through a chat client (Baileys, Cloud API, Telegram), which
delivers the reply on a platform that renders WhatsApp markup. Two surfaces
don't:

- ``web``: the OpenAI-compatible ``/v1`` API (``routes/openai_compat.py``),
  used by Open WebUI, LibreChat, Chatbox and similar apps. They render Markdown.
  A web-only user's JID is ``web:<id>``; a key linked to a WhatsApp/Telegram user
  runs on that user's JID with ``client_id="web"``.
- ``cli``: the terminal chat (``scripts/chat.py``), JID ``cli:<name>``.

Neither is a WhatsApp or Telegram address, so nothing may be sent to them
through a chat client (WhatsApp tools, broadcasts, scheduled deliveries).
Pure: no settings, no DB.
"""

from typing import Literal

Surface = Literal["chat", "web", "cli"]

WEB_JID_PREFIX = "web:"
CLI_JID_PREFIX = "cli:"
WEB_CLIENT_ID = "web"
CLI_CLIENT_ID = "cli"


def _starts(jid: object, prefix: str) -> bool:
    return isinstance(jid, str) and jid.startswith(prefix)


def is_app_jid(jid: str | None) -> bool:
    """A JID that belongs to an app surface, not to a WhatsApp/Telegram chat."""
    return _starts(jid, WEB_JID_PREFIX) or _starts(jid, CLI_JID_PREFIX)


def is_cli_jid(jid: str | None) -> bool:
    return _starts(jid, CLI_JID_PREFIX)


def surface_of(client_id: str | None, jid: str | None) -> Surface:
    """The surface a job's reply goes to. ``client_id`` wins over the JID, because
    a web key linked to a WhatsApp user runs on that user's WhatsApp JID."""
    if client_id == WEB_CLIENT_ID or _starts(jid, WEB_JID_PREFIX):
        return "web"
    if client_id == CLI_CLIENT_ID or _starts(jid, CLI_JID_PREFIX):
        return "cli"
    return "chat"


def renders_markdown(surface: Surface) -> bool:
    """Only the web apps render Markdown; WhatsApp, Telegram (via the client's
    conversion) and the terminal chat get WhatsApp markup."""
    return surface == "web"


def delivers_through_chat_client(client_id: str | None, jid: str | None) -> bool:
    """Whether tools that send through a chat client (reactions, messages,
    locations, group posts, later deliveries) may run for this job."""
    return surface_of(client_id, jid) == "chat"
