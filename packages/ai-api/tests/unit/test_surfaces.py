"""surfaces.py and the web rendering path (formatting.render_reply)."""

import pytest

from ai_api.formatting import render_reply, strip_split_delimiters
from ai_api.surfaces import (
    delivers_through_chat_client,
    is_app_jid,
    is_cli_jid,
    renders_markdown,
    surface_of,
)


@pytest.mark.parametrize(
    "client_id,jid,expected",
    [
        (None, "5511999999999@s.whatsapp.net", "chat"),
        ("telegram", "tg:42", "chat"),
        ("cloud", "5511999999999@s.whatsapp.net", "chat"),
        ("web", "5511999999999@s.whatsapp.net", "web"),  # key linked to a WhatsApp user
        (None, "web:abc", "web"),
        (None, "cli:luis", "cli"),
        ("cli", "cli:luis", "cli"),
    ],
)
def test_surface_of(client_id, jid, expected):
    assert surface_of(client_id, jid) == expected


def test_only_web_renders_markdown():
    assert renders_markdown("web")
    assert not renders_markdown("cli")
    assert not renders_markdown("chat")


def test_jid_predicates():
    assert is_app_jid("web:x") and is_app_jid("cli:x")
    assert not is_app_jid("tg:1") and not is_app_jid(None) and not is_app_jid("x@g.us")
    assert is_cli_jid("cli:x") and not is_cli_jid("web:x")


def test_chat_client_delivery():
    assert delivers_through_chat_client(None, "5511@s.whatsapp.net")
    assert not delivers_through_chat_client("web", "5511@s.whatsapp.net")
    assert not delivers_through_chat_client(None, "cli:luis")


class TestStripSplitDelimiters:
    def test_drops_burst_lines_and_collapses_blank_runs(self):
        assert strip_split_delimiters("Hi!\n---\nAnswer\n\n---\n\nMore") == "Hi!\n\nAnswer\n\nMore"

    def test_leaves_fenced_code_alone(self):
        text = "Config:\n```yaml\n---\nkey: 1\n```\nDone"
        assert strip_split_delimiters(text) == text

    def test_only_exact_delimiter_lines(self):
        # Same rule as the TS clients' /^---\s*$/: indented or longer rules stay.
        assert strip_split_delimiters("a\n ---\nb\n----\nc") == "a\n ---\nb\n----\nc"


def test_render_reply_by_surface():
    md = "**Bold** [x](https://e.com)\n---\nok"
    assert render_reply(md, markdown=True) == "**Bold** [x](https://e.com)\n\nok"
    assert render_reply(md, markdown=False).startswith("*Bold*")
