"""Integration tests for /admin/* endpoints (management dashboard contract).

Covers the prompt (whole, sections, effective), process heartbeats, settings GET/PATCH/DELETE (hot vs restart-only),
the read-only conversation viewer, and auth enforcement.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient, ConnectError, ReadTimeout

from tests.helpers.factories import make_conversation_message, make_user

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}

# Patches that keep the app lifespan from touching real infra.
LIFESPAN_PATCHES = (
    patch("ai_api.main.init_db"),
    patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
    patch("ai_api.main.cleanup_expired_documents"),
)


def _make_mock_db():
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
    return db


def _app_with_db(mock_db):
    from ai_api.database import get_db
    from ai_api.main import app

    def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _cleanup():
    from ai_api.main import app

    app.dependency_overrides.clear()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _row(content, base, updated_at=None, key=None):
    from ai_api.agent.prompt import section_hash

    return MagicMock(
        key=key,
        content=content,
        base_hash=section_hash(base),
        updated_at=updated_at or datetime(2026, 11, 20, tzinfo=UTC),
    )


async def _call(method, url, overrides=None, **kwargs):
    """Call an /admin route with the stored prompt overrides ``overrides``."""
    app = _app_with_db(_make_mock_db())
    try:
        with (
            patch("ai_api.agent.prompt.get_prompt_overrides", return_value=overrides or {}),
            patch("ai_api.routes.admin.set_prompt_override") as mock_set,
            patch("ai_api.routes.admin.delete_prompt_override") as mock_delete,
        ):
            async with _client(app) as client:
                resp = await client.request(method, url, headers=AUTH_HEADERS, **kwargs)
        return resp, mock_set, mock_delete
    finally:
        _cleanup()


class TestPrompt:
    async def test_get_prompt_default_when_no_override(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        resp, _, _ = await _call("GET", "/admin/prompt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_overridden"] is False
        assert data["content"] == DEFAULT_SYSTEM_PROMPT
        assert data["default_length"] == len(DEFAULT_SYSTEM_PROMPT)
        assert data["stale"] is False
        assert data["sections_overridden"] == 0

    async def test_get_prompt_returns_a_fresh_whole_override(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        rows = {"all": _row("CUSTOM", DEFAULT_SYSTEM_PROMPT)}
        resp, _, _ = await _call("GET", "/admin/prompt", rows)
        assert resp.json()["is_overridden"] is True
        assert resp.json()["content"] == "CUSTOM"

    async def test_a_stale_whole_override_is_ignored_and_flagged(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        rows = {"all": _row("OLD COPY", "the default of May")}
        resp, _, _ = await _call("GET", "/admin/prompt", rows)
        data = resp.json()
        assert data["content"] == DEFAULT_SYSTEM_PROMPT
        assert data["is_overridden"] is False
        assert data["stale"] is True

    async def test_section_overrides_show_in_the_base_prompt(self):
        from ai_api.agent.prompt import SECTIONS_BY_KEY

        rows = {"tone": _row("Tone: always cheerful.", SECTIONS_BY_KEY["tone"].default)}
        resp, _, _ = await _call("GET", "/admin/prompt", rows)
        data = resp.json()
        assert "Tone: always cheerful." in data["content"]
        assert data["is_overridden"] is True
        assert data["sections_overridden"] == 1

    async def test_put_prompt_stores_a_whole_override_on_the_current_default(self):
        from ai_api.agent.prompt import DEFAULT_PROMPT_HASH

        resp, mock_set, _ = await _call("PUT", "/admin/prompt", json={"content": "NEW PROMPT"})
        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args.args[1:] == ("all", "NEW PROMPT", DEFAULT_PROMPT_HASH)

    async def test_saving_the_unchanged_default_stores_nothing(self):
        """The June accident: an unchanged save must not freeze the prompt."""
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        resp, mock_set, _ = await _call(
            "PUT", "/admin/prompt", json={"content": DEFAULT_SYSTEM_PROMPT}
        )
        assert resp.status_code == 200
        mock_set.assert_not_called()

    async def test_an_empty_section_override_omits_it_and_counts_in_both_fields(self):
        """ "" drops a non-empty default section: a real change, so it counts.

        is_overridden and sections_overridden come from the same set (FleetView
        derives "whole override active" from them).
        """
        from ai_api.agent.prompt import SECTIONS_BY_KEY

        rows = {"tone": _row("", SECTIONS_BY_KEY["tone"].default)}
        resp, _, _ = await _call("GET", "/admin/prompt", rows)
        data = resp.json()
        assert SECTIONS_BY_KEY["tone"].default not in data["content"]
        assert data["sections_overridden"] == 1
        assert data["is_overridden"] is True

    async def test_section_overrides_under_a_whole_override_are_not_applied(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT, SECTIONS_BY_KEY

        rows = {
            "all": _row("CUSTOM", DEFAULT_SYSTEM_PROMPT),
            "tone": _row("Tone: cheerful.", SECTIONS_BY_KEY["tone"].default),
        }
        resp, _, _ = await _call("GET", "/admin/prompt", rows)
        assert resp.json()["content"] == "CUSTOM"
        assert resp.json()["sections_overridden"] == 0
        resp, _, _ = await _call("GET", "/admin/prompt/sections", rows)
        tone = next(s for s in resp.json()["sections"] if s["key"] == "tone")
        assert tone["status"] == "override"  # the enum is unchanged
        assert tone["applied"] is False
        assert all(not s["applied"] for s in resp.json()["sections"])

    async def test_putting_the_code_default_over_section_overrides_stores_it(self):
        """With a section override the model gets other text, so the default is a change."""
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT, SECTIONS_BY_KEY

        rows = {"tone": _row("Tone: cheerful.", SECTIONS_BY_KEY["tone"].default)}
        resp, mock_set, _ = await _call(
            "PUT", "/admin/prompt", rows, json={"content": DEFAULT_SYSTEM_PROMPT}
        )
        assert resp.status_code == 200
        assert mock_set.call_args.args[1:3] == ("all", DEFAULT_SYSTEM_PROMPT)

    async def test_resaving_the_current_text_with_crlf_and_trailing_spaces_stores_nothing(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        noisy = DEFAULT_SYSTEM_PROMPT.replace("\n", "  \r\n") + "\n\n"
        resp, mock_set, mock_delete = await _call("PUT", "/admin/prompt", json={"content": noisy})
        assert resp.status_code == 200
        mock_set.assert_not_called()

    async def test_an_unchanged_save_keeps_a_stale_whole_override(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        rows = {"all": _row("OLD COPY", "the default of May")}
        resp, mock_set, mock_delete = await _call(
            "PUT", "/admin/prompt", rows, json={"content": DEFAULT_SYSTEM_PROMPT}
        )
        assert resp.status_code == 200
        mock_set.assert_not_called()
        mock_delete.assert_not_called()

    async def test_an_unchanged_save_clears_a_fresh_whole_override(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        rows = {"all": _row("CUSTOM", DEFAULT_SYSTEM_PROMPT)}
        resp, mock_set, mock_delete = await _call(
            "PUT", "/admin/prompt", rows, json={"content": DEFAULT_SYSTEM_PROMPT}
        )
        assert resp.status_code == 200
        mock_set.assert_not_called()
        assert mock_delete.call_args.args[1] == "all"

    async def test_put_blank_prompt_rejected(self):
        resp, _, _ = await _call("PUT", "/admin/prompt", json={"content": "   "})
        assert resp.status_code == 400

    async def test_delete_prompt_clears_the_whole_override(self):
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        resp, _, mock_delete = await _call("DELETE", "/admin/prompt")
        assert resp.status_code == 200
        assert resp.json()["content"] == DEFAULT_SYSTEM_PROMPT
        assert mock_delete.call_args.args[1] == "all"


class TestPromptSections:
    async def test_lists_every_section_in_prompt_order(self):
        from ai_api.agent.prompt import PROMPT_SECTIONS, section_hash

        resp, _, _ = await _call("GET", "/admin/prompt/sections")
        assert resp.status_code == 200
        data = resp.json()
        assert [s["key"] for s in data["sections"]] == [s.key for s in PROMPT_SECTIONS]
        first = data["sections"][0]
        assert first["status"] == "default"
        assert first["default_hash"] == section_hash(PROMPT_SECTIONS[0].default)
        assert first["effective"] == PROMPT_SECTIONS[0].default
        assert data["whole_override"] == {"active": False, "stale": False, "updated_at": None}

    async def test_a_stale_section_keeps_the_saved_text_but_applies_the_code(self):
        from ai_api.agent.prompt import SECTIONS_BY_KEY

        rows = {"tone": _row("Tone: old edit.", "an older tone default")}
        resp, _, _ = await _call("GET", "/admin/prompt/sections", rows)
        tone = next(s for s in resp.json()["sections"] if s["key"] == "tone")
        assert tone["status"] == "stale"
        assert tone["override"] == "Tone: old edit."
        assert tone["effective"] == SECTIONS_BY_KEY["tone"].default

    async def test_put_section_stores_it_with_the_default_hash(self):
        from ai_api.agent.prompt import SECTIONS_BY_KEY, section_hash

        base = section_hash(SECTIONS_BY_KEY["tone"].default)
        resp, mock_set, _ = await _call(
            "PUT",
            "/admin/prompt/sections/tone",
            json={"content": "Tone: brief.", "base_hash": base},
        )
        assert resp.status_code == 200
        assert mock_set.call_args.args[1:] == ("tone", "Tone: brief.", base)

    async def test_put_section_on_an_outdated_default_is_a_conflict(self):
        resp, mock_set, _ = await _call(
            "PUT", "/admin/prompt/sections/tone", json={"content": "x", "base_hash": "old"}
        )
        assert resp.status_code == 409
        mock_set.assert_not_called()

    async def test_put_section_equal_to_the_default_deletes_the_override(self):
        from ai_api.agent.prompt import SECTIONS_BY_KEY, section_hash

        default = SECTIONS_BY_KEY["tone"].default
        resp, mock_set, mock_delete = await _call(
            "PUT",
            "/admin/prompt/sections/tone",
            json={"content": default, "base_hash": section_hash(default)},
        )
        assert resp.status_code == 200
        mock_set.assert_not_called()
        assert mock_delete.call_args.args[1] == "tone"

    async def test_unknown_section_is_404(self):
        resp, _, _ = await _call("DELETE", "/admin/prompt/sections/nope")
        assert resp.status_code == 404
        resp, _, _ = await _call(
            "PUT", "/admin/prompt/sections/nope", json={"content": "x", "base_hash": "y"}
        )
        assert resp.status_code == 404

    async def test_an_orphan_override_is_listed_and_can_be_deleted(self):
        rows = {"persona": _row("Old persona.", "gone", key="persona")}
        resp, _, _ = await _call("GET", "/admin/prompt/sections", rows)
        orphan = resp.json()["sections"][-1]
        assert orphan["key"] == "persona"
        assert orphan["orphan"] is True and orphan["applied"] is False
        assert orphan["override"] == "Old persona."
        assert all(not s["orphan"] for s in resp.json()["sections"][:-1])
        resp, _, mock_delete = await _call("DELETE", "/admin/prompt/sections/persona", rows)
        assert resp.status_code == 200
        assert mock_delete.call_args.args[1] == "persona"

    async def test_delete_section(self):
        resp, _, mock_delete = await _call("DELETE", "/admin/prompt/sections/operator")
        assert resp.status_code == 200
        assert mock_delete.call_args.args[1] == "operator"


class TestEffectivePrompt:
    async def test_renders_every_hook_for_a_sample_private_chat(self):
        from ai_api.agent.core import INSTRUCTION_HOOKS
        from ai_api.agent.prompt import DEFAULT_SYSTEM_PROMPT

        with patch("ai_api.agent.core.get_core_memory", return_value=None):
            resp, _, _ = await _call("GET", "/admin/prompt/effective")
        assert resp.status_code == 200
        data = resp.json()
        assert data["chat"] == "private" and data["surface"] == "whatsapp"
        assert data["content"].startswith(DEFAULT_SYSTEM_PROMPT)
        assert data["length"] == len(data["content"])
        hook_keys = [b["key"] for b in data["blocks"] if b["kind"] == "hook"]
        assert hook_keys == [f"hook:{h.func.__name__}" for h in INSTRUCTION_HOOKS[1:]]
        assert all(not b["editable"] for b in data["blocks"] if b["kind"] == "hook")
        assert data["prompt_code_hash"]

    async def test_web_group_is_rejected(self):
        resp, _, _ = await _call("GET", "/admin/prompt/effective?chat=group&surface=web")
        assert resp.status_code == 422

    async def test_unknown_user_is_404(self):
        app = _app_with_db(db := _make_mock_db())
        db.get.return_value = None
        try:
            async with _client(app) as client:
                resp = await client.get(
                    "/admin/prompt/effective?user_id=00000000-0000-0000-0000-000000000001",
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 404
        finally:
            _cleanup()


class TestEffectivePromptByJid:
    async def test_renders_the_users_own_chat_by_jid(self):
        user = MagicMock(
            id="11111111-1111-1111-1111-111111111111", whatsapp_jid="5511@s.whatsapp.net"
        )
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin._resolve_user", return_value=user) as resolve,
                patch("ai_api.agent.prompt.get_prompt_overrides", return_value={}),
                patch("ai_api.agent.core.get_core_memory", return_value=MagicMock(content="- Ana")),
            ):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/prompt/effective", params={"jid": "tg:42"}, headers=AUTH_HEADERS
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert resolve.call_args.args[1] == "tg:42"
            assert data["user_id"] == user.id
            assert data["surface"] == "telegram"  # the chat it was looked up by
            assert "- Ana" in data["content"]
        finally:
            _cleanup()

    async def test_unknown_jid_is_404(self):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin._resolve_user", return_value=None):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/prompt/effective", params={"jid": "nobody"}, headers=AUTH_HEADERS
                    )
            assert resp.status_code == 404
        finally:
            _cleanup()

    async def test_a_user_with_the_web_group_sample_selectors_is_not_rejected(self):
        """chat and surface only pick the sample chat; a user's own chat ignores them."""
        user = MagicMock(
            id="11111111-1111-1111-1111-111111111111", whatsapp_jid="5511@s.whatsapp.net"
        )
        app = _app_with_db(db := _make_mock_db())
        db.get.return_value = user
        try:
            with (
                patch("ai_api.agent.prompt.get_prompt_overrides", return_value={}),
                patch("ai_api.agent.core.get_core_memory", return_value=None),
            ):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/prompt/effective",
                        params={"user_id": user.id, "surface": "web", "chat": "group"},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            assert resp.json()["chat"] == "private"
        finally:
            _cleanup()

    async def test_user_id_and_jid_together_is_422(self):
        resp, _, _ = await _call(
            "GET",
            "/admin/prompt/effective",
            params={"jid": "x", "user_id": "11111111-1111-1111-1111-111111111111"},
        )
        assert resp.status_code == 422


class TestSettingsPerProcess:
    async def test_values_per_process_and_restart_pending(self):
        old = datetime(2026, 11, 1, tzinfo=UTC)
        new = datetime(2026, 11, 20, tzinfo=UTC)

        def proc(name, started, timeout):
            return {
                "process": name,
                "instance": f"{name}-host",
                "pid": 1,
                "started_at": started,
                "last_seen_at": new,
                "prompt_code_hash": "h",
                "settings": {"deepseek_timeout_seconds": timeout, "history_limit_private": 10},
            }

        procs = [proc("api", new, 45.0), proc("worker", old, 30.0)]
        app = _app_with_db(db := _make_mock_db())
        db.query.return_value.all.return_value = []
        try:
            with (
                patch("ai_api.routes.admin.read_processes", AsyncMock(return_value=procs)),
                patch("ai_api.routes.admin.get_setting_overrides", return_value={}),
            ):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            items = resp.json()["settings"]
            timeout = _find(items, "deepseek_timeout_seconds")
            assert timeout["restart_pending"] is True
            assert [(p["process"], p["value"]) for p in timeout["processes"]] == [
                ("api", 45.0),
                ("worker", 30.0),
            ]
            history = _find(items, "history_limit_private")
            assert history["restart_pending"] is False  # hot settings never need a restart
            whitelist = _find(items, "whitelist_phones")
            assert whitelist["processes"] == []  # not in these heartbeats
        finally:
            _cleanup()


class TestProcesses:
    async def test_lists_live_processes_and_flags_a_prompt_code_mismatch(self):
        now = datetime(2026, 11, 20, tzinfo=UTC)
        procs = [
            {
                "process": "api",
                "instance": "a1",
                "pid": 1,
                "started_at": now,
                "last_seen_at": now,
                "prompt_code_hash": "aaa",
                "settings": {},
            },
            {
                "process": "worker",
                "instance": "w1",
                "pid": 1,
                "started_at": now,
                "last_seen_at": now,
                "prompt_code_hash": "bbb",
                "settings": {},
            },
        ]
        with patch("ai_api.routes.admin.read_processes", AsyncMock(return_value=procs)):
            resp, _, _ = await _call("GET", "/admin/processes")
        assert resp.status_code == 200
        data = resp.json()
        assert [p["process"] for p in data["processes"]] == ["api", "worker"]
        assert data["prompt_code_mismatch"] is True

    async def test_redis_down_gives_an_empty_list(self):
        with patch(
            "ai_api.routes.admin.read_processes", AsyncMock(side_effect=ConnectionError("down"))
        ):
            resp, _, _ = await _call("GET", "/admin/processes")
        assert resp.status_code == 200
        assert resp.json() == {"processes": [], "prompt_code_mismatch": False}


# ---------------------------------------------------------------------------
# Settings


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _find(settings_list, key):
    return next(item for item in settings_list if item["key"] == key)


class TestSettings:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_settings_shape(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.get_setting_overrides", return_value={}):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            items = resp.json()["settings"]
            wl = _find(items, "whitelist_phones")
            assert wl["hot"] is True
            assert wl["source"] == "default"
            db_url = _find(items, "database_url")
            assert db_url["hot"] is False
            assert db_url["secret"] is True
            # secret value is masked, never the real connection string
            assert db_url["value"] in ("********", None)
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_settings_reflects_override(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch(
                "ai_api.routes.admin.get_setting_overrides",
                return_value={"tts_default_voice": '"Puck"'},
            ):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            voice = _find(resp.json()["settings"], "tts_default_voice")
            assert voice["value"] == "Puck"
            assert voice["source"] == "override"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_hot_setting(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"tts_default_voice": '"Puck"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"tts_default_voice": "Puck"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            # set_setting_overrides_batch(db, {key: json_value, ...}) — called once
            mock_batch.assert_called_once()
            assert mock_batch.call_args.args[1] == {"tts_default_voice": '"Puck"'}
            assert _find(resp.json()["settings"], "tts_default_voice")["value"] == "Puck"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_restart_only_setting_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"database_url": "postgresql://x"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "restart" in resp.json()["detail"].lower()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_unknown_setting_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"not_a_setting": 1}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "Unknown setting" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_wrong_type_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"history_limit_private": "abc"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_bad_choice_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"pdf_parser": "bogus"}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_gemini_model_accepted(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"gemini_model": '"gemini-3.1-pro"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"gemini_model": "gemini-3.1-pro"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            mock_batch.assert_called_once()
            assert mock_batch.call_args.args[1] == {"gemini_model": '"gemini-3.1-pro"'}
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_gemini_model_empty_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"gemini_model": "   "}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "non-empty" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_gemini_model_too_long_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"gemini_model": "x" * 201}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "too long" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_bot_name_invalid_rejected(self, *_):
        cases = [
            ("", "non-empty"),
            ("   ", "non-empty"),
            ("x" * 101, "too long"),
            ("Bot\nUser: hi", "single line"),
        ]
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                for value, detail in cases:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"bot_name": value}},
                        headers=AUTH_HEADERS,
                    )
                    assert resp.status_code == 400, value
                    assert detail in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_bot_name_accepted(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"bot_name": '"Jarvis"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"bot_name": "Jarvis"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            assert mock_batch.call_args.args[1] == {"bot_name": '"Jarvis"'}
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_deepseek_model_accepted(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"deepseek_model": '"deepseek-v4-pro"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"deepseek_model": "deepseek-v4-pro"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            assert mock_batch.call_args.args[1] == {"deepseek_model": '"deepseek-v4-pro"'}
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_deepseek_model_empty_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"deepseek_model": "   "}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert resp.json()["detail"] == "deepseek_model must be a non-empty string"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_deepseek_model_too_long_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"deepseek_model": "x" * 201}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert resp.json()["detail"] == "deepseek_model is too long (max 200 characters)"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_empty_overrides_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings", json={"overrides": {}}, headers=AUTH_HEADERS
                )
            assert resp.status_code == 400
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_setting_override(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.delete_setting_override") as mock_del,
                patch("ai_api.routes.admin.get_setting_overrides", return_value={}),
            ):
                async with _client(app) as client:
                    resp = await client.delete(
                        "/admin/settings/tts_default_voice", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 200
            mock_del.assert_called_once()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_unknown_setting_404(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.delete("/admin/settings/not_a_setting", headers=AUTH_HEADERS)
            assert resp.status_code == 404
        finally:
            _cleanup()


class TestSettingsCrossConstraints:
    """Coverage for _validate_cross_constraints — the PATCH-time safety guards."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_llamaparse_timeout_too_high_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                # default kb_processing_timeout_seconds is 360
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"llamaparse_timeout_seconds": 9999}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "kb_processing_timeout_seconds" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_llamaparse_timeout_below_cap_accepted(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch"),
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"llamaparse_timeout_seconds": "5"},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"llamaparse_timeout_seconds": 5}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            assert _find(resp.json()["settings"], "llamaparse_timeout_seconds")["value"] == 5
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_stt_groq_without_key_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.settings.groq_api_key", None):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"stt_provider": "groq"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 400
            assert "GROQ_API_KEY" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_stt_whisper_without_url_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            # No URL in this request and none effective → reject.
            with (
                patch("ai_api.routes.admin.settings.whisper_base_url", None),
                patch("ai_api.routes.admin.runtime_config.get", return_value=None),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"stt_provider": "whisper"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 400
            assert "whisper_base_url" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_stt_whisper_with_url_same_request_accepted(self, *_):
        """Batch-aware: setting the URL and provider in one PATCH must pass."""
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch"),
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={
                        "whisper_base_url": '"http://whisper:8000"',
                        "stt_provider": '"whisper"',
                    },
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={
                            "overrides": {
                                "whisper_base_url": "http://whisper:8000",
                                "stt_provider": "whisper",
                            }
                        },
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            assert _find(resp.json()["settings"], "stt_provider")["value"] == "whisper"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_is_atomic_on_partial_failure(self, *_):
        """A bad second key rejects the whole batch — nothing is written."""
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch:
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={
                            "overrides": {
                                "tts_default_voice": "Puck",
                                "history_limit_private": "abc",  # wrong type
                            }
                        },
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 400
            mock_batch.assert_not_called()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_db_write_failure_rolls_back(self, *_):
        """If the batch upsert fails, the route rolls back and never commits.

        Guards against per-row commits that would leave the batch half-persisted.
        """
        mock_db = _make_mock_db()
        app = _app_with_db(mock_db)
        try:
            with patch(
                "ai_api.routes.admin.set_setting_overrides_batch",
                side_effect=RuntimeError("simulated DB write failure"),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={
                            "overrides": {
                                "tts_default_voice": "Puck",
                                "history_limit_private": 10,
                            }
                        },
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 500
            mock_db.commit.assert_not_called()
            mock_db.rollback.assert_called()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_settings_handles_malformed_override(self, *_):
        """A corrupt JSON value in runtime_settings falls back to the env default
        with source='default' (the silent-fallback path in _settings_payload)."""
        from ai_api.config import settings

        app = _app_with_db(_make_mock_db())
        try:
            with patch(
                "ai_api.routes.admin.get_setting_overrides",
                return_value={"tts_default_voice": "not-valid-json"},
            ):
                async with _client(app) as client:
                    resp = await client.get("/admin/settings", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            voice = _find(resp.json()["settings"], "tts_default_voice")
            assert voice["source"] == "default"
            assert voice["value"] == settings.tts_default_voice
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Conversation viewer
# ---------------------------------------------------------------------------


class TestConversationViewer:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_list_users(self, *_):
        mock_db = _make_mock_db()
        # A LID-addressed row whose phone was already resolved at ingestion: the
        # dashboard must receive the phone, not just the opaque LID.
        user = make_user(
            "109994229891095@lid",
            name="Alice",
            phone="+5511999999999",
            whatsapp_lid="109994229891095@lid",
        )
        last = datetime(2026, 5, 25, tzinfo=UTC)
        mock_db.query.return_value.count.return_value = 1
        chain = mock_db.query.return_value.outerjoin.return_value.group_by.return_value
        chain.order_by.return_value.limit.return_value.offset.return_value.all.return_value = [
            (user, 5, last)
        ]
        app = _app_with_db(mock_db)
        try:
            async with _client(app) as client:
                resp = await client.get("/admin/users", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["users"][0]["name"] == "Alice"
            assert data["users"][0]["message_count"] == 5
            assert data["users"][0]["phone"] == "+5511999999999"
            assert data["users"][0]["whatsapp_lid"] == "109994229891095@lid"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_user_messages(self, *_):
        mock_db = _make_mock_db()
        user = make_user("5511999999999@s.whatsapp.net")
        msgs = [
            make_conversation_message("user", "hi"),
            make_conversation_message("assistant", "hello"),
        ]
        filtered = mock_db.query.return_value.filter.return_value
        filtered.count.return_value = 2
        filtered.order_by.return_value.limit.return_value.offset.return_value.all.return_value = (
            msgs
        )
        app = _app_with_db(mock_db)
        try:
            with patch("ai_api.routes.admin._resolve_user", return_value=user):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/users/5511999999999@s.whatsapp.net/messages",
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 2
            assert len(data["messages"]) == 2
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_get_user_messages_404(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin._resolve_user", return_value=None):
                async with _client(app) as client:
                    resp = await client.get(
                        "/admin/users/unknown@s.whatsapp.net/messages", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 404
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_overview(self, *_):
        mock_db = _make_mock_db()
        mock_db.query.return_value.count.side_effect = [3, 10, 2]
        mock_db.query.return_value.filter.return_value.count.return_value = 4
        app = _app_with_db(mock_db)
        try:
            async with _client(app) as client:
                resp = await client.get("/admin/overview", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data == {
                "users": 3,
                "messages": 10,
                "knowledge_base_documents": 2,
                "open_bug_reports": 4,
                "disabled_scheduled_tasks": 4,
            }
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# WhatsApp pairing QR
# ---------------------------------------------------------------------------


def _patch_wa_client(status_payload=None, raises=None):
    """Patch create_whatsapp_client to return a client whose get_whatsapp_status
    returns status_payload (or raises)."""
    client = MagicMock()
    client.get_whatsapp_status = AsyncMock(return_value=status_payload, side_effect=raises)
    return patch("ai_api.routes.admin.create_whatsapp_client", return_value=client)


def _patch_wa_client_logout(result=None, raises=None):
    """Patch create_whatsapp_client to return a client whose logout_whatsapp
    returns result (or raises)."""
    client = MagicMock()
    client.logout_whatsapp = AsyncMock(return_value=result, side_effect=raises)
    return patch("ai_api.routes.admin.create_whatsapp_client", return_value=client)


class TestWhatsAppQr:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_returns_qr_while_unpaired(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client(
                {"status": "qr", "qr": "2@abc", "qrGeneratedAt": "2026-05-26T14:00:00Z"}
            ):
                async with _client(app) as client:
                    resp = await client.get("/admin/whatsapp/qr", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "qr"
            assert data["connected"] is False
            assert data["qr"] == "2@abc"
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_connected_has_no_qr(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client({"status": "connected", "qr": None, "qrGeneratedAt": None}):
                async with _client(app) as client:
                    resp = await client.get("/admin/whatsapp/qr", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "connected"
            assert data["connected"] is True
            assert data["qr"] is None
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_unreachable_client_reports_unavailable(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client(raises=ConnectError("connection refused")):
                async with _client(app) as client:
                    resp = await client.get("/admin/whatsapp/qr", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unavailable"
            assert data["connected"] is False
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_client_error_reports_unavailable(self, *_):
        # The client returned a non-2xx (WhatsAppClientError) — the other arm of
        # the proxy's except tuple. Should still degrade to "unavailable", not 500.
        from ai_api.whatsapp import WhatsAppClientError

        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client(raises=WhatsAppClientError("boom", status_code=500)):
                async with _client(app) as client:
                    resp = await client.get("/admin/whatsapp/qr", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unavailable"
            assert data["connected"] is False
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_read_timeout_reports_unavailable(self, *_):
        # ReadTimeout is in httpx.HTTPError's hierarchy, so the except tuple
        # already catches it — this test makes the 5s timeout ceiling intentional.
        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client(raises=ReadTimeout("read timeout")):
                async with _client(app) as client:
                    resp = await client.get("/admin/whatsapp/qr", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "unavailable"
            assert data["connected"] is False
        finally:
            _cleanup()


class TestWhatsAppLogout:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_success(self, *_):
        from ai_api.whatsapp.client import SuccessResponse

        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client_logout(SuccessResponse(success=True)):
                async with _client(app) as client:
                    resp = await client.post("/admin/whatsapp/logout", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert "detail" in data
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_unreachable_client_returns_503(self, *_):
        # Unlike the passive QR poll (which degrades to a 200 "unavailable" body),
        # logout is an explicit action: an unreachable Baileys client must surface 503.
        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client_logout(raises=ConnectError("connection refused")):
                async with _client(app) as client:
                    resp = await client.post("/admin/whatsapp/logout", headers=AUTH_HEADERS)
            assert resp.status_code == 503
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_client_error_returns_502(self, *_):
        # Reachable but the Node client returned an error status (WhatsAppClientError)
        # — distinct from an unreachable transport error. Surfaces as 502, not 503, so
        # an operator can tell an upstream failure from a networking one.
        from ai_api.whatsapp import WhatsAppClientError

        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client_logout(raises=WhatsAppClientError("boom", status_code=500)):
                async with _client(app) as client:
                    resp = await client.post("/admin/whatsapp/logout", headers=AUTH_HEADERS)
            assert resp.status_code == 502
            assert "boom" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_reported_failure_returns_502(self, *_):
        # Reachable and 2xx, but the client reports success=False: don't pretend it
        # worked with a 200 — surface it as a bad-gateway.
        from ai_api.whatsapp.client import SuccessResponse

        app = _app_with_db(_make_mock_db())
        try:
            with _patch_wa_client_logout(SuccessResponse(success=False)):
                async with _client(app) as client:
                    resp = await client.post("/admin/whatsapp/logout", headers=AUTH_HEADERS)
            assert resp.status_code == 502
        finally:
            _cleanup()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_admin_requires_auth(*_):
    from ai_api.main import app

    async with _client(app) as client:
        resp = await client.get("/admin/prompt")
    assert resp.status_code == 401


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_whatsapp_qr_requires_auth(*_):
    from ai_api.main import app

    async with _client(app) as client:
        resp = await client.get("/admin/whatsapp/qr")
    assert resp.status_code == 401


@patch("ai_api.main.init_db")
@patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
@patch("ai_api.main.cleanup_expired_documents")
async def test_whatsapp_logout_requires_auth(*_):
    from ai_api.main import app

    async with _client(app) as client:
        resp = await client.post("/admin/whatsapp/logout")
    assert resp.status_code == 401

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_whitelist_empty_rejected(self, *_):
        """An empty whitelist means "allow everyone", and the stored override
        shadows the env value across restarts — so clearing a dashboard field to
        "revert to default" would silently disable the gate for good."""
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                for value in ("", "   ", ",", " , , "):
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"whitelist_phones": value}},
                        headers=AUTH_HEADERS,
                    )
                    assert resp.status_code == 400, value
                    assert "DELETE /admin/settings/whitelist_phones" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_whitelist_too_long_rejected(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                resp = await client.patch(
                    "/admin/settings",
                    json={"overrides": {"whitelist_phones": "4915755945319," * 400}},
                    headers=AUTH_HEADERS,
                )
            assert resp.status_code == 400
            assert "too long" in resp.json()["detail"]
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_patch_whitelist_accepted(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with (
                patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_batch,
                patch(
                    "ai_api.routes.admin.get_setting_overrides",
                    return_value={"whitelist_phones": '"4915755945319"'},
                ),
            ):
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={"overrides": {"whitelist_phones": "4915755945319"}},
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            mock_batch.assert_called_once()
        finally:
            _cleanup()


class TestBroadcastSettingsValidation:
    """The broadcast worker reads these per send; PATCH rejects unusable values."""

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_rejects_bad_values(self, *_):
        bad = [
            {"broadcast_send_window": "9am-5pm"},
            {"broadcast_timezone": "Mars/Olympus"},
            {"broadcast_batch_size": 0},
            {"broadcast_daily_limit": -1},
            {"broadcast_min_delay_seconds": 90, "broadcast_max_delay_seconds": 30},
            {"broadcast_footer": "x" * 501},
            {"broadcast_max_delay_seconds": 3601},
            {"broadcast_batch_size": 1001},
            {"broadcast_batch_pause_seconds": 86401},
            {"broadcast_daily_limit": 100001},
        ]
        app = _app_with_db(_make_mock_db())
        try:
            async with _client(app) as client:
                for overrides in bad:
                    resp = await client.patch(
                        "/admin/settings", json={"overrides": overrides}, headers=AUTH_HEADERS
                    )
                    assert resp.status_code == 400, overrides
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_accepts_valid_values(self, *_):
        app = _app_with_db(_make_mock_db())
        try:
            with patch("ai_api.routes.admin.set_setting_overrides_batch") as mock_set:
                async with _client(app) as client:
                    resp = await client.patch(
                        "/admin/settings",
                        json={
                            "overrides": {
                                "broadcast_send_window": "22:00-06:00",
                                "broadcast_timezone": "America/Sao_Paulo",
                                "broadcast_min_delay_seconds": 5,
                                "broadcast_max_delay_seconds": 10,
                                "broadcast_footer": "",
                            }
                        },
                        headers=AUTH_HEADERS,
                    )
            assert resp.status_code == 200
            mock_set.assert_called_once()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_that_would_leave_min_above_max_is_400(self, *_):
        """Env min=20; an override max=10 (with min=5) is valid, but reverting min
        alone would leave 20 > 10 — PATCH checks the pair, DELETE must too."""
        app = _app_with_db(_make_mock_db())
        effective = {"broadcast_min_delay_seconds": 5, "broadcast_max_delay_seconds": 10}
        try:
            with (
                patch("ai_api.routes.admin.delete_setting_override") as mock_del,
                patch("ai_api.routes.admin.runtime_config.get", side_effect=effective.get),
            ):
                async with _client(app) as client:
                    resp = await client.delete(
                        "/admin/settings/broadcast_min_delay_seconds", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 400
            assert "above broadcast_max_delay_seconds" in resp.json()["detail"]
            mock_del.assert_not_called()
        finally:
            _cleanup()

    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_delete_that_keeps_min_below_max_is_allowed(self, *_):
        app = _app_with_db(_make_mock_db())
        effective = {"broadcast_min_delay_seconds": 5, "broadcast_max_delay_seconds": 100}
        try:
            with (
                patch("ai_api.routes.admin.delete_setting_override") as mock_del,
                patch("ai_api.routes.admin.get_setting_overrides", return_value={}),
                patch("ai_api.routes.admin.runtime_config.get", side_effect=effective.get),
            ):
                async with _client(app) as client:
                    resp = await client.delete(
                        "/admin/settings/broadcast_min_delay_seconds", headers=AUTH_HEADERS
                    )
            assert resp.status_code == 200
            mock_del.assert_called_once()
        finally:
            _cleanup()
