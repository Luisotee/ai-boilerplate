"""Pure broadcast decisions: routing, audience plan, footer, pacing (services/broadcast.py)."""

import random
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from ai_api.services.broadcast import (
    SKIP_CLOUD_WINDOW,
    Route,
    batch_pause,
    candidate_routes,
    choose_route,
    estimate_baileys_seconds,
    jittered_delay,
    parse_send_window,
    parse_timezone,
    plan_broadcast,
    render_message,
    seconds_until_window,
    typing_seconds,
    whitelist_filter,
)

NOW = datetime(2026, 9, 25, 12, 0, 0)
ALL = ("baileys", "cloud", "telegram")


def user(
    jid="5511999999999@s.whatsapp.net",
    *,
    telegram_jid=None,
    last_client_id=None,
    opt_out=False,
    conversation_type="private",
    phone=None,
    lid=None,
    uid="u1",
):
    return SimpleNamespace(
        id=uid,
        whatsapp_jid=jid,
        whatsapp_lid=lid,
        telegram_jid=telegram_jid,
        last_client_id=last_client_id,
        broadcast_opt_out=opt_out,
        conversation_type=conversation_type,
        phone=phone,
    )


class TestCandidateRoutes:
    def test_unlinked_telegram_row(self):
        assert candidate_routes(user("tg:42")) == [Route("telegram", "tg:42")]

    def test_legacy_whatsapp_row_defaults_to_baileys(self):
        u = user(last_client_id=None)
        assert candidate_routes(u) == [Route("baileys", u.whatsapp_jid)]

    def test_cloud_user(self):
        u = user(last_client_id="cloud")
        assert candidate_routes(u) == [Route("cloud", u.whatsapp_jid)]

    def test_linked_user_prefers_last_used_platform(self):
        wa_last = user(telegram_jid="tg:7", last_client_id="baileys")
        assert [r.platform for r in candidate_routes(wa_last)] == ["baileys", "telegram"]

        tg_last = user(telegram_jid="tg:7", last_client_id="telegram")
        assert candidate_routes(tg_last) == [
            Route("telegram", "tg:7"),
            Route("baileys", tg_last.whatsapp_jid),
        ]


class TestChooseRoute:
    def test_uses_preferred_route(self):
        route, reason = choose_route(user(telegram_jid="tg:7"), ALL, None, NOW)
        assert route.platform == "baileys" and reason is None

    def test_linked_user_falls_back_to_other_selected_identity(self):
        u = user(telegram_jid="tg:7", last_client_id="baileys")
        route, reason = choose_route(u, ["telegram"], None, NOW)
        assert route == Route("telegram", "tg:7") and reason is None

    def test_no_selected_platform(self):
        assert choose_route(user("tg:42"), ["baileys"], None, NOW) == (None, None)

    def test_cloud_inside_window(self):
        u = user(last_client_id="cloud")
        route, reason = choose_route(u, ALL, NOW - timedelta(hours=2), NOW)
        assert route.platform == "cloud" and reason is None

    def test_cloud_outside_window_is_skipped(self):
        u = user(last_client_id="cloud")
        route, reason = choose_route(u, ALL, NOW - timedelta(hours=30), NOW)
        assert route.platform == "cloud" and reason == SKIP_CLOUD_WINDOW

    def test_cloud_never_wrote_is_skipped(self):
        route, reason = choose_route(user(last_client_id="cloud"), ALL, None, NOW)
        assert reason == SKIP_CLOUD_WINDOW

    def test_cloud_outside_window_falls_back_to_telegram(self):
        u = user(last_client_id="cloud", telegram_jid="tg:7")
        route, reason = choose_route(u, ALL, NOW - timedelta(days=3), NOW)
        assert route == Route("telegram", "tg:7") and reason is None


class TestPlanBroadcast:
    def test_counts_every_exclusion(self):
        rows = [
            (user(uid="a"), None),
            (user(uid="b", opt_out=True), None),
            (user(uid="c", jid="tg:5"), None),  # telegram not selected
            (user(uid="d", last_client_id="cloud"), NOW - timedelta(days=2)),
            (user(uid="e", jid="blocked@s.whatsapp.net"), None),
        ]
        plan = plan_broadcast(
            rows,
            ["baileys", "cloud"],
            allowed=lambda u: u.whatsapp_jid != "blocked@s.whatsapp.net",
            now=NOW,
        )
        assert [r.user_id for r in plan.pending()] == ["a"]
        assert plan.opted_out == 1
        assert plan.not_whitelisted == 1
        assert plan.no_selected_platform == 1
        assert plan.count("cloud", skipped=SKIP_CLOUD_WINDOW) == 1
        assert plan.count("baileys") == 1


class TestWhitelistFilter:
    def test_empty_whitelist_allows_everyone(self):
        assert whitelist_filter("", "jid")(user()) is True

    def test_matches_phone_lid_or_telegram_identity(self):
        allowed = whitelist_filter("5511999999999,tg:7", "jid")
        assert allowed(user()) is True
        assert allowed(user("123@lid", phone="+5511999999999")) is True
        assert allowed(user("5500000000000@s.whatsapp.net", telegram_jid="tg:7")) is True
        assert allowed(user("5500000000000@s.whatsapp.net")) is False

    def test_membership_mode_admits_groups(self):
        group = user("120363@g.us", conversation_type="group")
        assert whitelist_filter("5511999999999", "membership")(group) is True
        assert whitelist_filter("5511999999999", "jid")(group) is False


class TestRenderMessage:
    def test_appends_footer_after_blank_line(self):
        assert render_message(" New feature! ", "_opt out_") == "New feature!\n\n_opt out_"

    def test_no_footer(self):
        assert render_message("Hi", "") == "Hi"
        assert render_message("Hi", None) == "Hi"


class TestSendWindow:
    def test_parse(self):
        assert parse_send_window("09:00-21:00") == (time(9), time(21))
        assert parse_send_window(" 22:30 - 06:00 ") == (time(22, 30), time(6))
        assert parse_send_window("") is None

    @pytest.mark.parametrize("bad", ["9-21", "25:00-26:00", "10:00-10:00", "abc"])
    def test_parse_rejects(self, bad):
        with pytest.raises(ValueError):
            parse_send_window(bad)

    def test_inside_and_outside(self):
        window = (time(9), time(21))
        assert seconds_until_window(window, datetime(2026, 1, 1, 12)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 8)) == 3600
        # 22:00 → opens tomorrow at 09:00
        assert seconds_until_window(window, datetime(2026, 1, 1, 22)) == 11 * 3600
        assert seconds_until_window(None, datetime(2026, 1, 1, 3)) == 0

    def test_wrapping_window(self):
        window = (time(22), time(6))
        assert seconds_until_window(window, datetime(2026, 1, 1, 23)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 5)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 12)) == 10 * 3600

    def test_timezone(self):
        assert parse_timezone("America/Sao_Paulo").key == "America/Sao_Paulo"
        with pytest.raises(ValueError):
            parse_timezone("Mars/Olympus")


class TestPacing:
    def test_jittered_delay_within_bounds(self):
        rng = random.Random(1)
        delays = [jittered_delay(rng, 20, 60) for _ in range(200)]
        assert all(20 <= d <= 60 for d in delays)
        assert len({round(d, 3) for d in delays}) > 100  # actually random

    def test_jittered_delay_tolerates_swapped_bounds(self):
        assert 5 <= jittered_delay(random.Random(2), 10, 5) <= 10

    def test_batch_pause_jitter(self):
        rng = random.Random(3)
        pauses = [batch_pause(rng, 600) for _ in range(100)]
        assert all(420 <= p <= 780 for p in pauses)

    def test_typing_seconds_clamped(self):
        assert typing_seconds("hi") == 2.0
        assert typing_seconds("x" * 100) == 3.0
        assert typing_seconds("x" * 10_000) == 8.0

    def test_estimate_respects_daily_cap(self):
        common = dict(
            min_delay=20, max_delay=60, batch_size=15, batch_pause_seconds=600, window=None
        )
        assert estimate_baileys_seconds(0, daily_limit=150, **common) == 0
        small = estimate_baileys_seconds(10, daily_limit=150, **common)
        assert 0 < small < 3600
        # 1000 chats at 150/day need at least 6 more days.
        assert estimate_baileys_seconds(1000, daily_limit=150, **common) >= 6 * 86400

    def test_estimate_stretches_for_send_window(self):
        common = dict(min_delay=20, max_delay=60, batch_size=15, batch_pause_seconds=600)
        always = estimate_baileys_seconds(100, daily_limit=0, window=None, **common)
        half = estimate_baileys_seconds(100, daily_limit=0, window=(time(8), time(20)), **common)
        assert half == pytest.approx(always * 2, rel=0.01)
