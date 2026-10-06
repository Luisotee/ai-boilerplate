"""services/bug_reports.py: fingerprints, dedup of automatic reports, the
per-user cap of agent reports, and /clean dropping the message snapshots."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError

from ai_api.services import bug_reports as br


def _settings(**overrides):
    values = {
        "bug_reports_enabled": True,
        "bug_reports_per_user_per_hour": 5,
        "gemini_model": "gemini-x",
        "deepseek_model": "deepseek-x",
    }
    values.update(overrides)
    rc = MagicMock()
    rc.get.side_effect = values.__getitem__
    return patch.object(br, "runtime_config", rc)


def _raise_from_here(exc):
    raise exc


def _raised(exc):
    try:
        _raise_from_here(exc)
    except BaseException as e:  # noqa: BLE001
        return e


class TestErrorTypeName:
    def test_plain(self):
        assert br.error_type_name(RuntimeError("x")) == "RuntimeError"

    def test_http_status_is_part_of_the_type(self):
        exc = ModelHTTPError(status_code=404, model_name="m", body=None)
        assert br.error_type_name(exc) == "ModelHTTPError(404)"

    def test_group_members_sorted_and_deduplicated(self):
        a = ModelHTTPError(status_code=429, model_name="a", body=None)
        b = ModelHTTPError(status_code=401, model_name="b", body=None)
        group = FallbackExceptionGroup("all failed", [a, b, a])
        assert br.error_type_name(group) == (
            "FallbackExceptionGroup[ModelHTTPError(401), ModelHTTPError(429)]"
        )


class TestFingerprint:
    def test_location_is_file_and_function_without_line(self):
        loc = br.innermost_location(_raised(RuntimeError("boom")))
        # This test file is not inside the ai_api package, so no frame matches.
        assert loc is None

    def test_location_uses_innermost_package_frame(self):
        frames = []
        for filename, name in [
            ("/app/src/ai_api/streams/processor.py", "outer"),
            ("/venv/site-packages/pydantic_ai/x.py", "lib"),
            ("/app/src/ai_api/agent/model_chain.py", "request_stream"),
            ("/venv/site-packages/httpx/_client.py", "send"),
        ]:
            frame = MagicMock(filename=filename)
            frame.name = name  # MagicMock(name=...) names the mock itself
            frames.append(frame)
        with patch.object(br.traceback, "extract_tb", return_value=frames):
            loc = br.innermost_location(RuntimeError("x"))
        assert loc == "agent/model_chain.py:request_stream"

    def test_same_inputs_same_fingerprint(self):
        assert br.fingerprint("job_crash", "KeyError", "a.py:f") == br.fingerprint(
            "job_crash", "KeyError", "a.py:f"
        )

    @pytest.mark.parametrize(
        "other",
        [
            ("model_error", "KeyError", "a.py:f"),
            ("job_crash", "ValueError", "a.py:f"),
            ("job_crash", "KeyError", "a.py:g"),
            ("job_crash", "KeyError", None),
        ],
    )
    def test_any_difference_changes_it(self, other):
        assert br.fingerprint("job_crash", "KeyError", "a.py:f") != br.fingerprint(*other)


class TestSnapshotContext:
    def test_no_user_no_context(self):
        assert br.snapshot_context(MagicMock(), None) is None

    def test_truncates_and_serialises(self):
        msg = MagicMock(
            role="user",
            sender_name="Ana",
            content="x" * 5000,
            timestamp=datetime(2026, 10, 6, 12, 0),
        )
        with patch.object(br, "get_conversation_messages", return_value=[msg]) as get:
            ctx = br.snapshot_context(MagicMock(), "u1")
        assert get.call_args.kwargs["limit"] == br.CONTEXT_MESSAGES
        assert ctx[0]["role"] == "user"
        assert ctx[0]["sender"] == "Ana"
        assert len(ctx[0]["content"]) == br.CONTEXT_MESSAGE_CHARS
        assert ctx[0]["at"] == "2026-10-06T12:00:00"


def _upsert_sql(db) -> str:
    from sqlalchemy.dialects import postgresql

    stmt = db.execute.call_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


class TestRecordAutoReport:
    def _record(self, db, settings=None, **kwargs):
        kwargs.setdefault("exc", _raised(KeyError("k")))
        kwargs.setdefault("user_id", "u1")
        with (
            _settings(**(settings or {})),
            patch.object(br, "SessionLocal", return_value=db),
            patch.object(br, "snapshot_context", return_value=[{"role": "user"}]) as snap,
        ):
            br.record_auto_report("job_crash", title="Chat job crashed", job_id="job-2", **kwargs)
        return snap

    def test_upserts_on_the_open_fingerprint_index(self):
        db = MagicMock()
        self._record(db)
        sql = _upsert_sql(db)
        assert "INSERT INTO bug_reports" in sql
        assert "ON CONFLICT (fingerprint) WHERE status = 'open' DO UPDATE" in sql
        assert "occurrences = (bug_reports.occurrences + " in sql
        db.commit.assert_called_once()
        db.close.assert_called_once()

    def test_a_bump_replaces_the_whole_sample(self):
        """Every per-occurrence column comes from the same (latest) occurrence,
        so a row never shows one user's chat next to another user's job."""
        db = MagicMock()
        self._record(db)
        set_clause = _upsert_sql(db).split("DO UPDATE SET", 1)[1]
        for col in br._SAMPLE_COLUMNS:
            assert f"{col} = excluded.{col}" in set_clause
        assert "created_at" not in set_clause  # stays "first seen"

    def test_values(self):
        db = MagicMock()
        self._record(db)
        params = db.execute.call_args.args[0].compile().params
        assert params["source"] == "job_crash"
        assert params["error_type"] == "KeyError"
        assert "KeyError" in params["error_detail"]
        assert params["context"] == [{"role": "user"}]
        assert len(params["fingerprint"]) == 64

    def test_locks_the_user_row_before_snapshotting(self):
        db = MagicMock()
        order = []
        db.query.return_value.filter.return_value.with_for_update.side_effect = (
            lambda: order.append("lock") or MagicMock(first=MagicMock(return_value=object()))
        )
        with (
            _settings(),
            patch.object(br, "SessionLocal", return_value=db),
            patch.object(br, "snapshot_context", side_effect=lambda *a: order.append("snap")),
        ):
            br.record_auto_report("job_crash", title="t", exc=_raised(KeyError()), user_id="u1")
        assert order == ["lock", "snap"]

    def test_deleted_user_is_not_referenced(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = None
        with (
            _settings(),
            patch.object(br, "SessionLocal", return_value=db),
            patch.object(br, "get_conversation_messages") as get,
        ):
            br.record_auto_report("job_crash", title="t", exc=_raised(KeyError()), user_id="u1")
        params = db.execute.call_args.args[0].compile().params
        assert params["user_id"] is None
        assert params["context"] is None
        get.assert_not_called()

    def test_explicit_location_changes_the_fingerprint(self):
        fps = []
        for location in ("x|empty result", "x|gemini_api_key not configured"):
            db = MagicMock()
            with _settings(), patch.object(br, "SessionLocal", return_value=db):
                br.record_auto_report(
                    "pdf_failure", title="t", error_type="ValueError", location=location
                )
            fps.append(db.execute.call_args.args[0].compile().params["fingerprint"])
        assert fps[0] != fps[1]

    def test_disabled_does_nothing(self):
        db = MagicMock()
        self._record(db, settings={"bug_reports_enabled": False})
        db.execute.assert_not_called()

    def test_never_raises(self):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("db down")
        self._record(db)  # must not raise
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_without_exception_uses_given_type(self):
        db = MagicMock()
        with _settings(), patch.object(br, "SessionLocal", return_value=db):
            br.record_auto_report(
                "pdf_failure",
                title="PDF failed",
                error_type="ValueError",
                error_detail="non-retriable error: ValueError: bad pdf",
                document_id="doc-1",
            )
        params = db.execute.call_args.args[0].compile().params
        assert params["error_type"] == "ValueError"
        assert params["document_id"] == "doc-1"
        assert params["context"] is None  # no user: no snapshot


class TestNormalizeMessage:
    def test_strips_ids_paths_and_numbers(self):
        a = br.normalize_message(
            "ValueError: Document 3f2a9c1e-1111-2222-3333-444455556666 at /data/a.pdf has 0 pages"
        )
        b = br.normalize_message(
            "ValueError: Document 9b8a7c6d-aaaa-bbbb-cccc-ddddeeeeffff at /data/b.pdf has 12 pages"
        )
        assert a == b
        assert "<id>" in a and "<path>" in a

    def test_different_causes_stay_different(self):
        assert br.normalize_message(
            "ValueError: LlamaParse returned an empty result (no markdown pages)."
        ) != br.normalize_message("ValueError: GEMINI_API_KEY not configured")

    def test_slash_inside_a_word_is_not_a_path(self):
        assert br.normalize_message("and/or") == "and/or"

    def test_truncated(self):
        assert len(br.normalize_message("x" * 500)) == 80


class TestFileReport:
    def _file(self, db, recent=0, **settings):
        db.query.return_value.filter.return_value.scalar.return_value = recent
        with _settings(**settings), patch.object(br, "snapshot_context", return_value=[]):
            return br.file_report(
                db,
                source="user",
                category="user_request",
                title="  Wrong hours  ",
                description="Said 9am, sign says 10am",
                user_id="u1",
                whatsapp_jid="1@s.whatsapp.net",
                client_id="baileys",
                conversation_type="private",
                job_id="job-1",
            )

    def test_creates_report(self):
        db = MagicMock()
        report = self._file(db)
        assert report.title == "Wrong hours"
        assert report.source == "user"
        assert report.models == "gemini-x"
        db.add.assert_called_once_with(report)
        db.commit.assert_called_once()

    def test_over_the_cap_is_refused(self):
        db = MagicMock()
        with pytest.raises(br.RateLimited):
            self._file(db, recent=5)
        db.add.assert_not_called()

    def test_zero_cap_refuses_everything(self):
        with pytest.raises(br.RateLimited):
            self._file(MagicMock(), recent=0, bug_reports_per_user_per_hour=0)


class TestScrubUserReports:
    def test_clears_everything_copied_from_the_chat(self):
        from sqlalchemy.dialects import postgresql

        db = MagicMock()
        br.scrub_user_reports(db, "u1")
        db.query.return_value.filter.return_value.with_for_update.assert_called_once()
        values = db.query.return_value.filter.return_value.update.call_args.args[0]
        # null(), not None: None would store JSON 'null' on a JSONB column.
        assert str(values[br.BugReport.context]) == "NULL"
        assert str(values[br.BugReport.error_detail]) == "NULL"
        assert values[br.BugReport.description] == ""
        title = str(values[br.BugReport.title].compile(dialect=postgresql.dialect()))
        assert "CASE WHEN" in title and "bug_reports.source IN" in title

    def test_clean_command_scrubs_reports(self):
        from ai_api import commands

        db = MagicMock()
        with patch.object(commands, "scrub_user_reports") as scrub:
            commands.handle_clean_command(db, "u1", "1@s.whatsapp.net", level="messages")
        scrub.assert_called_once_with(db, "u1")


def test_short_ref():
    assert br.short_ref("12345678-aaaa-bbbb-cccc-dddddddddddd") == "12345678"
