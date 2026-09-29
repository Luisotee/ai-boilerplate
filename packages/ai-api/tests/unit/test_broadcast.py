"""Pure broadcast decisions: routing, audience plan, footer (services/broadcast.py)
and the anti-ban pacing math (broadcast_pacing.py)."""

import random
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ai_api.broadcast_pacing import (
    PacingSettings,
    batch_pause,
    estimate_baileys_seconds,
    jittered_delay,
    parse_send_window,
    parse_timezone,
    resume_pacing,
    seconds_until_window,
    typing_seconds,
)
from ai_api.services.broadcast import (
    NO_PLATFORM,
    NOT_WHITELISTED,
    SKIP_CLOUD_WINDOW,
    SKIP_UNKNOWN_CLIENT,
    Route,
    _people,
    candidate_routes,
    choose_route,
    plan_broadcast,
    render_message,
    whitelist_filter,
)

NOW = datetime(2026, 9, 25, 12, 0, 0)
ALL = ("baileys", "cloud", "telegram")


def allow_all(_user, _route):
    return True


def user(
    jid="5511999999999@s.whatsapp.net",
    *,
    telegram_jid=None,
    last_client_id=None,
    whatsapp_client_id=None,
    cloud_last_inbound_at=None,
    opt_out=False,
    conversation_type="private",
    phone=None,
    lid=None,
    uid="u1",
    created_at=None,
):
    return SimpleNamespace(
        id=uid,
        whatsapp_jid=jid,
        whatsapp_lid=lid,
        telegram_jid=telegram_jid,
        last_client_id=last_client_id,
        whatsapp_client_id=whatsapp_client_id,
        cloud_last_inbound_at=cloud_last_inbound_at,
        broadcast_opt_out=opt_out,
        conversation_type=conversation_type,
        phone=phone,
        created_at=created_at,
    )


def cloud_user(**kw):
    kw.setdefault("whatsapp_client_id", "cloud")
    kw.setdefault("last_client_id", "cloud")
    return user(**kw)


class TestCandidateRoutes:
    def test_unlinked_telegram_row(self):
        assert candidate_routes(user("tg:42")) == [Route("telegram", "tg:42")]

    def test_legacy_whatsapp_row_defaults_to_baileys(self):
        u = user()
        assert candidate_routes(u) == [Route("baileys", u.whatsapp_jid)]

    def test_cloud_user(self):
        u = cloud_user()
        assert candidate_routes(u) == [Route("cloud", u.whatsapp_jid)]

    def test_linked_user_prefers_last_used_platform(self):
        wa_last = user(telegram_jid="tg:7", last_client_id="baileys")
        assert [r.platform for r in candidate_routes(wa_last)] == ["baileys", "telegram"]

        tg_last = user(telegram_jid="tg:7", last_client_id="telegram")
        assert candidate_routes(tg_last) == [
            Route("telegram", "tg:7"),
            Route("baileys", tg_last.whatsapp_jid),
        ]

    def test_linked_cloud_user_who_last_wrote_on_telegram_stays_on_cloud(self):
        """Review finding: this user used to be routed to Baileys."""
        u = user(telegram_jid="tg:7", last_client_id="telegram", whatsapp_client_id="cloud")
        assert candidate_routes(u) == [Route("telegram", "tg:7"), Route("cloud", u.whatsapp_jid)]


class TestChooseRoute:
    def test_uses_preferred_route(self):
        route, reason = choose_route(user(telegram_jid="tg:7"), ALL, allow_all, NOW)
        assert route.platform == "baileys" and reason is None

    def test_linked_user_falls_back_to_other_selected_identity(self):
        u = user(telegram_jid="tg:7", last_client_id="baileys")
        route, reason = choose_route(u, ["telegram"], allow_all, NOW)
        assert route == Route("telegram", "tg:7") and reason is None

    def test_no_selected_platform(self):
        assert choose_route(user("tg:42"), ["baileys"], allow_all, NOW) == (None, NO_PLATFORM)

    def test_cloud_window_uses_last_cloud_message(self):
        inside = cloud_user(cloud_last_inbound_at=NOW - timedelta(hours=2))
        assert choose_route(inside, ALL, allow_all, NOW)[1] is None

        outside = cloud_user(cloud_last_inbound_at=NOW - timedelta(hours=30))
        route, reason = choose_route(outside, ALL, allow_all, NOW)
        assert route.platform == "cloud" and reason == SKIP_CLOUD_WINDOW

    def test_cloud_never_wrote_is_skipped(self):
        assert choose_route(cloud_user(), ALL, allow_all, NOW)[1] == SKIP_CLOUD_WINDOW

    def test_cloud_outside_window_falls_back_to_telegram(self):
        u = cloud_user(telegram_jid="tg:7", cloud_last_inbound_at=NOW - timedelta(days=3))
        route, reason = choose_route(u, ALL, allow_all, NOW)
        assert route == Route("telegram", "tg:7") and reason is None

    def test_whitelist_is_checked_per_route(self):
        """A user whitelisted only as tg:7 must not be messaged on WhatsApp."""
        u = user(telegram_jid="tg:7", last_client_id="baileys")
        allowed = whitelist_filter("tg:7")
        assert choose_route(u, ALL, allowed, NOW) == (Route("telegram", "tg:7"), None)
        assert choose_route(u, ["baileys"], allowed, NOW) == (None, NOT_WHITELISTED)


class TestUnknownWhatsAppClient:
    """Rows from before client tracking: Baileys only if no Cloud client exists."""

    def test_no_cloud_deployed_means_baileys(self):
        route, reason = choose_route(user(), ALL, allow_all, NOW, cloud_deployed=False)
        assert route.platform == "baileys" and reason is None

    def test_cloud_deployed_skips_the_ambiguous_row(self):
        """Review finding: an old Cloud-only user would get a message from the
        Baileys number they never talked to — a spam-report risk."""
        route, reason = choose_route(user(), ALL, allow_all, NOW, cloud_deployed=True)
        assert route.platform == "baileys" and reason == SKIP_UNKNOWN_CLIENT

    def test_known_baileys_user_is_unaffected(self):
        u = user(whatsapp_client_id="baileys")
        assert choose_route(u, ALL, allow_all, NOW, cloud_deployed=True)[1] is None

    @pytest.mark.parametrize("jid", ["120363012345678@g.us"])
    def test_a_whatsapp_group_is_never_ambiguous(self, jid):
        """Review finding: the Cloud API has no groups, and a group where the bot
        is never mentioned keeps a NULL client forever, so it was never reached."""
        group = user(jid, conversation_type="group")
        route, reason = choose_route(group, ALL, allow_all, NOW, cloud_deployed=True)
        assert route == Route("baileys", jid) and reason is None

    def test_ambiguous_linked_user_falls_back_to_telegram(self):
        u = user(telegram_jid="tg:7")
        route, reason = choose_route(u, ALL, allow_all, NOW, cloud_deployed=True)
        assert route == Route("telegram", "tg:7") and reason is None


class TestPlanBroadcast:
    def test_counts_every_exclusion(self):
        users = [
            user(uid="a"),
            user(uid="b", opt_out=True),
            user(uid="c", jid="tg:5"),  # telegram not selected
            cloud_user(uid="d", cloud_last_inbound_at=NOW - timedelta(days=2)),
            user(uid="e", jid="blocked@s.whatsapp.net"),
        ]
        plan = plan_broadcast(
            users,
            ["baileys", "cloud"],
            allowed=lambda u, _r: u.whatsapp_jid != "blocked@s.whatsapp.net",
            now=NOW,
        )
        assert [r.user_id for r in plan.pending()] == ["a"]
        assert plan.opted_out == 1
        assert plan.not_whitelisted == 1
        assert plan.no_selected_platform == 1
        assert plan.count("cloud", skipped=SKIP_CLOUD_WINDOW) == 1
        assert plan.count("baileys") == 1


class TestOnePersonOneMessage:
    """Review finding: an early @lid row next to the phone-JID row carrying that LID
    (or two rows sharing a phone) got the broadcast twice."""

    def test_lid_row_joins_the_phone_row_that_carries_its_lid(self):
        lid_row = user("99887766@lid", uid="lid", created_at=NOW)
        phone_row = user(
            "5511999999999@s.whatsapp.net",
            uid="phone",
            lid="99887766@lid",
            created_at=NOW - timedelta(days=30),
        )
        plan = plan_broadcast([lid_row, phone_row], ALL, allow_all, NOW)
        assert [r.user_id for r in plan.pending()] == ["phone"]
        assert plan.duplicates == 1

    def test_rows_sharing_a_phone_send_to_the_phone_jid_row(self):
        rows = [
            user("111@lid", uid="lid", phone="+5511999999999"),
            user("5511999999999@s.whatsapp.net", uid="phone", phone="5511999999999"),
        ]
        plan = plan_broadcast(rows, ALL, allow_all, NOW)
        assert [r.user_id for r in plan.pending()] == ["phone"]

    def test_ties_go_to_the_newest_row(self):
        # e.g. the BR mobile number with and without the extra 9
        rows = [
            user("5511999999999@s.whatsapp.net", uid="old", phone="+5511999999999", created_at=NOW),
            user(
                "551199999999@s.whatsapp.net",
                uid="new",
                phone="5511999999999",
                created_at=NOW + timedelta(days=1),
            ),
        ]
        assert [u.id for u in _people(rows)[0]] == ["new", "old"]

    @pytest.mark.parametrize("opted_out", ["lid", "phone"])
    def test_an_opt_out_on_either_row_wins(self, opted_out):
        rows = [
            user("99887766@lid", uid="lid", opt_out=opted_out == "lid"),
            user(
                "5511999999999@s.whatsapp.net",
                uid="phone",
                lid="99887766@lid",
                opt_out=opted_out == "phone",
            ),
        ]
        plan = plan_broadcast(rows, ALL, allow_all, NOW)
        assert plan.pending() == []
        assert plan.opted_out == 1

    def test_groups_and_telegram_rows_never_merge(self):
        rows = [
            user("120363@g.us", uid="g1", conversation_type="group", phone="1"),
            user("120364@g.us", uid="g2", conversation_type="group", phone="1"),
            user("tg:5", uid="t", phone="1"),
            user("5511999999999@s.whatsapp.net", uid="w", phone="1"),
        ]
        plan = plan_broadcast(rows, ALL, allow_all, NOW)
        assert sorted(r.user_id for r in plan.pending()) == ["g1", "g2", "t", "w"]
        assert plan.duplicates == 0

    def test_keeps_the_audience_order(self):
        rows = [user(f"55119000000{n}@s.whatsapp.net", uid=str(n)) for n in range(3)]
        assert [r.user_id for r in plan_broadcast(rows, ALL, allow_all, NOW).pending()] == [
            "0",
            "1",
            "2",
        ]


class TestWhitelistFilter:
    def test_empty_whitelist_allows_everyone(self):
        u = user()
        assert whitelist_filter("")(u, candidate_routes(u)[0]) is True

    @pytest.mark.parametrize("raw", [" , ", ",", "  "])
    def test_a_whitelist_with_no_entries_is_no_whitelist(self, raw):
        """Same as chat: it used to block every recipient instead."""
        u = user()
        assert whitelist_filter(raw)(u, candidate_routes(u)[0]) is True

    def test_whatsapp_route_matches_phone_or_lid(self):
        allowed = whitelist_filter("5511999999999")
        assert allowed(user(), Route("baileys", "5511999999999@s.whatsapp.net")) is True
        lid_user = user("123@lid", phone="+5511999999999")
        assert allowed(lid_user, Route("baileys", "123@lid")) is True
        stranger = user("5500000000000@s.whatsapp.net")
        assert allowed(stranger, Route("baileys", stranger.whatsapp_jid)) is False

    def test_telegram_route_only_matches_its_own_id(self):
        allowed = whitelist_filter("5511999999999")
        linked = user(telegram_jid="tg:7")
        assert allowed(linked, Route("telegram", "tg:7")) is False

    def test_a_group_must_be_listed_itself(self):
        """Even under GROUP_GATING=membership, where chat admits such groups: a
        broadcast into a group the bot is mostly silent in would be unsolicited."""
        group = user("120363@g.us", conversation_type="group")
        route = Route("baileys", "120363@g.us")
        assert whitelist_filter("5511999999999")(group, route) is False
        assert whitelist_filter("5511999999999,120363@g.us")(group, route) is True

    def test_a_telegram_group_must_be_listed_itself(self):
        group = user("tg:-1001", conversation_type="group")
        route = Route("telegram", "tg:-1001")
        assert whitelist_filter("tg:7")(group, route) is False
        assert whitelist_filter("tg:-1001")(group, route) is True


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
        utc = ZoneInfo("UTC")
        assert seconds_until_window(window, datetime(2026, 1, 1, 12, tzinfo=utc)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 8, tzinfo=utc)) == 3600
        # 22:00 → opens tomorrow at 09:00
        assert seconds_until_window(window, datetime(2026, 1, 1, 22, tzinfo=utc)) == 11 * 3600
        assert seconds_until_window(None, datetime(2026, 1, 1, 3, tzinfo=utc)) == 0

    def test_wrapping_window(self):
        window = (time(22), time(6))
        utc = ZoneInfo("UTC")
        assert seconds_until_window(window, datetime(2026, 1, 1, 23, tzinfo=utc)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 5, tzinfo=utc)) == 0
        assert seconds_until_window(window, datetime(2026, 1, 1, 12, tzinfo=utc)) == 10 * 3600

    def test_real_seconds_across_spring_forward(self):
        """Review finding: same-zone subtraction gave wall-clock time (11h here)."""
        ny = ZoneInfo("America/New_York")
        # 2026-03-08 02:00 EST → 03:00 EDT. 22:00 EST (03:00 UTC) → 09:00 EDT (13:00 UTC).
        wait = seconds_until_window((time(9), time(21)), datetime(2026, 3, 7, 22, tzinfo=ny))
        assert wait == 10 * 3600

    def test_real_seconds_across_fall_back(self):
        ny = ZoneInfo("America/New_York")
        # 2026-11-01 02:00 EDT → 01:00 EST: the night is an hour longer.
        wait = seconds_until_window((time(9), time(21)), datetime(2026, 10, 31, 22, tzinfo=ny))
        assert wait == 12 * 3600

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


class TestResumePacing:
    """A restarted Baileys lane must pick up the schedule, not start fresh."""

    SETTINGS = PacingSettings(
        min_delay_seconds=20, max_delay_seconds=60, batch_size=3, batch_pause_seconds=600
    )

    def sends(self, *seconds_ago):
        return [NOW - timedelta(seconds=s) for s in seconds_ago]

    def test_no_history_sends_now(self):
        assert resume_pacing([], NOW, random.Random(1), self.SETTINGS) == (0, 0.0)

    def test_mid_batch_waits_out_the_jittered_gap(self):
        count, wait = resume_pacing(self.sends(5, 40), NOW, random.Random(1), self.SETTINGS)
        assert count == 2
        assert 15 <= wait <= 55  # a 20-60s gap, 5s of it already elapsed

    def test_full_batch_waits_out_the_batch_pause(self):
        count, wait = resume_pacing(self.sends(5, 40, 80), NOW, random.Random(1), self.SETTINGS)
        assert count == 0
        assert 415 <= wait <= 775  # 600s ±30%, minus 5s elapsed

    def test_long_gap_before_counts_as_a_finished_batch(self):
        # The previous send was 900s before the latest: a batch pause happened.
        count, _ = resume_pacing(self.sends(5, 905, 940), NOW, random.Random(1), self.SETTINGS)
        assert count == 1

    def test_idle_long_enough_starts_fresh(self):
        assert resume_pacing(self.sends(500), NOW, random.Random(1), self.SETTINGS) == (0, 0.0)

    def test_gap_already_elapsed_sends_now(self):
        _, wait = resume_pacing(self.sends(70), NOW, random.Random(1), self.SETTINGS)
        assert wait == 0.0


class TestBootValidation:
    """A bad value in .env must stop the process, not silently disable the window."""

    @pytest.mark.parametrize(
        "env",
        [
            {"BROADCAST_SEND_WINDOW": "9am-5pm"},
            {"BROADCAST_TIMEZONE": "Mars/Olympus"},
            {"BROADCAST_MIN_DELAY_SECONDS": "90", "BROADCAST_MAX_DELAY_SECONDS": "30"},
            {"BROADCAST_FOOTER": "x" * 501},
            {"BROADCAST_MAX_DELAY_SECONDS": "3601"},
            {"BROADCAST_BATCH_SIZE": "1001"},
            {"BROADCAST_BATCH_PAUSE_SECONDS": "86401"},
            {"BROADCAST_DAILY_LIMIT": "100001"},
        ],
    )
    def test_rejects_bad_env(self, monkeypatch, env):
        from ai_api.config import Settings

        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with pytest.raises(ValidationError):
            Settings()

    def test_accepts_defaults(self):
        from ai_api.config import Settings

        assert Settings().broadcast_timezone == "UTC"
