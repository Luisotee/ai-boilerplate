"""routes/chat.py _is_allowed: a cli: chat only from loopback or an exact whitelist entry."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_api.routes import chat


def _request(host):
    return SimpleNamespace(client=SimpleNamespace(host=host) if host else None)


def _whitelist(raw):
    return patch.object(chat.runtime_config, "get", return_value=raw)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.5"])
def test_loopback_is_allowed_even_under_a_whitelist(host):
    with _whitelist("5511999999999"):
        assert chat._is_allowed(_request(host), "cli:luis")


@pytest.mark.parametrize("host", ["172.18.0.1", "10.0.0.3", "testclient", None])
def test_remote_needs_an_exact_entry(host):
    with _whitelist(""):  # an empty whitelist does NOT admit cli: from elsewhere
        assert not chat._is_allowed(_request(host), "cli:luis")
    with _whitelist("5511999999999, cli:other"):
        assert not chat._is_allowed(_request(host), "cli:luis")
    with _whitelist("5511999999999, cli:luis"):
        assert chat._is_allowed(_request(host), "cli:luis")


def test_other_jids_use_the_normal_whitelist():
    with patch.object(chat, "_is_whitelisted", return_value=False) as wl:
        assert not chat._is_allowed(_request("127.0.0.1"), "5511@s.whatsapp.net", "+5511")
    wl.assert_called_once_with("5511@s.whatsapp.net", "+5511")
