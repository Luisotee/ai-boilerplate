"""services/schedule.py: the schedule grammar and next-fire math (pure)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_api.services.schedule import (
    CATCHUP_MAX,
    CATCHUP_MIN,
    ONE_SHOT_GRACE,
    ScheduleError,
    catchup_window,
    format_local,
    max_per_day,
    min_gap,
    next_fire,
    parse_schedule,
    period,
    upcoming,
)

ACRE = ZoneInfo("America/Rio_Branco")  # UTC-5, no DST
BERLIN = ZoneInfo("Europe/Berlin")
# Monday 2026-11-09 15:00 UTC = 10:00 in Rio Branco.
NOW = datetime(2026, 11, 9, 15, 0)


def first(text, tz=ACRE, now=NOW):
    s = parse_schedule(text, tz, now)
    nxt = s.run_at or next_fire(
        tz=tz, after=now, cron_expr=s.cron_expr, interval_seconds=s.interval_seconds
    )
    return s, format_local(nxt, tz)


class TestOneShot:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("in 30m", "Mon 09 Nov 2026 10:30"),
            ("in 1h30m", "Mon 09 Nov 2026 11:30"),
            ("in 1h30", "Mon 09 Nov 2026 11:30"),
            ("in 2 hours", "Mon 09 Nov 2026 12:00"),
            ("in 1d", "Tue 10 Nov 2026 10:00"),
            ("at 18:30", "Mon 09 Nov 2026 18:30"),
            ("18:30", "Mon 09 Nov 2026 18:30"),
            ("at 9:00", "Tue 10 Nov 2026 09:00"),  # already past today → tomorrow
            ("tomorrow at 9am", "Tue 10 Nov 2026 09:00"),
            ("today at 9pm", "Mon 09 Nov 2026 21:00"),
            ("monday at 9", "Mon 16 Nov 2026 09:00"),  # today's 9:00 has passed
            ("monday at 11:00", "Mon 09 Nov 2026 11:00"),  # today's hasn't
            ("next friday at 21h", "Fri 13 Nov 2026 21:00"),
            ("2026-11-20 09:00", "Fri 20 Nov 2026 09:00"),
            ("2026-11-20T09:00-03:00", "Fri 20 Nov 2026 07:00"),
        ],
    )
    def test_resolves_in_the_bot_timezone(self, text, expected):
        s, when = first(text)
        assert when == expected
        assert not s.recurring
        assert s.description.startswith("once at ")

    def test_run_at_is_naive_utc(self):
        s = parse_schedule("tomorrow at 9:00", ACRE, NOW)
        assert s.run_at == datetime(2026, 11, 10, 14, 0)
        assert s.run_at.tzinfo is None

    @pytest.mark.parametrize(
        "text",
        [
            "in 0m",
            "2020-01-01 10:00",
            "at 25:00",
            "at 13pm",
            "in 1m30",
            "in 2 years",
            "blah",
            "",
            "funday at 9",
        ],
    )
    def test_rejects(self, text):
        with pytest.raises(ScheduleError):
            parse_schedule(text, ACRE, NOW)

    def test_next_weekday_said_on_that_day_is_a_week_out(self):
        # Monday 08:00 local: "next monday at 9:00" is not in an hour.
        monday_8 = datetime(2026, 11, 9, 13, 0)
        s = parse_schedule("next monday at 9:00", ACRE, monday_8)
        assert format_local(s.run_at, ACRE) == "Mon 16 Nov 2026 09:00"
        s = parse_schedule("monday at 9:00", ACRE, monday_8)
        assert format_local(s.run_at, ACRE) == "Mon 09 Nov 2026 09:00"

    def test_more_than_a_year_ahead_is_refused(self):
        with pytest.raises(ScheduleError, match="year"):
            parse_schedule("in 400d", ACRE, NOW)

    def test_local_time_across_a_dst_change_keeps_its_wall_clock(self):
        # 2026-03-29 is the spring-forward day in Berlin; Saturday 28th 12:00 UTC.
        now = datetime(2026, 3, 28, 12, 0)
        s = parse_schedule("tomorrow at 9:00", BERLIN, now)
        assert format_local(s.run_at, BERLIN) == "Sun 29 Mar 2026 09:00"
        assert s.run_at == datetime(2026, 3, 29, 7, 0)  # CEST, UTC+2


class TestRecurring:
    @pytest.mark.parametrize(
        "text,cron,description,expected",
        [
            ("every day at 9:00", "0 9 * * *", "every day at 09:00", "Tue 10 Nov 2026 09:00"),
            ("daily at 7", "0 7 * * *", "every day at 07:00", "Tue 10 Nov 2026 07:00"),
            ("every weekday at 8:30", "30 8 * * 1,2,3,4,5", "every weekday at 08:30", None),
            (
                "every weekend at 10",
                "0 10 * * 0,6",
                "every weekend at 10:00",
                "Sat 14 Nov 2026 10:00",
            ),
            ("every monday,friday at 9:00", "0 9 * * 1,5", None, "Fri 13 Nov 2026 09:00"),
            ("every monday and friday at 9pm", "0 21 * * 1,5", None, "Mon 09 Nov 2026 21:00"),
            ("every mondays at 9", "0 9 * * 1", "every monday at 09:00", "Mon 16 Nov 2026 09:00"),
            ("0 9 * * 1", "0 9 * * 1", "cron 0 9 * * 1", "Mon 16 Nov 2026 09:00"),
        ],
    )
    def test_named_forms_compile_to_cron(self, text, cron, description, expected):
        s, when = first(text)
        assert s.recurring
        assert s.cron_expr == cron
        if description:
            assert s.description == description
        if expected:
            assert when == expected

    def test_sunday_is_cron_zero(self):
        # The weekday-numbering trap (APScheduler Monday=0 vs cron Sunday=0).
        s, when = first("every sunday at 9:00")
        assert s.cron_expr == "0 9 * * 0"
        assert when == "Sun 15 Nov 2026 09:00"

    def test_interval(self):
        s = parse_schedule("every 2h", ACRE, NOW)
        assert s.interval_seconds == 7200
        assert s.description == "every 2h"

    @pytest.mark.parametrize(
        "text", ["every 5 bananas", "every 12:00 day", "61 9 * * *", "* * * * * *", "@daily"]
    )
    def test_rejects(self, text):
        with pytest.raises(ScheduleError):
            parse_schedule(text, ACRE, NOW)

    def test_cron_that_never_fires_is_refused(self):
        with pytest.raises(ScheduleError, match="never fires"):
            parse_schedule("0 9 30 2 *", ACRE, NOW)

    def test_dst_fall_back_fires_once(self):
        ny = ZoneInfo("America/New_York")
        # 2026-11-01 01:30 happens twice in New York (EDT, then EST).
        s = parse_schedule("30 1 * * *", ny, datetime(2026, 10, 31, 12, 0))
        slots = upcoming(s, ny, datetime(2026, 10, 31, 12, 0), timedelta(days=3))
        assert [format_local(x, ny) for x in slots] == [
            "Sun 01 Nov 2026 01:30",
            "Mon 02 Nov 2026 01:30",
            "Tue 03 Nov 2026 01:30",
        ]
        first = slots[0]
        assert next_fire(tz=ny, after=first, cron_expr=s.cron_expr, previous=first) == slots[1]

    @pytest.mark.parametrize(
        "cron,expected_utc",
        [
            # Hourly: 01:00 EST is a real hour after 01:00 EDT, so it fires.
            ("0 * * * *", ["04:00", "05:00", "06:00", "07:00", "08:00"]),
            # Every 15 min keeps firing through the repeated hour.
            ("*/15 * * * *", ["04:45", "05:00", "05:15", "05:30", "05:45", "06:00", "06:15"]),
        ],
    )
    def test_dst_fall_back_keeps_sub_daily_slots(self, cron, expected_utc):
        ny = ZoneInfo("America/New_York")
        start = datetime(2026, 11, 1, 4, 30) if "*/15" in cron else datetime(2026, 11, 1, 3, 30)
        s = parse_schedule(cron, ny, start)
        slots = upcoming(s, ny, start, timedelta(hours=5))
        assert [x.strftime("%H:%M") for x in slots[: len(expected_utc)]] == expected_utc

    def test_cron_follows_dst(self):
        before = datetime(2026, 3, 28, 12, 0)
        nxt = next_fire(tz=BERLIN, after=before, cron_expr="0 9 * * *")
        assert nxt == datetime(2026, 3, 29, 7, 0)  # 09:00 CEST


class TestNextFire:
    def test_interval_is_anchored_at_the_previous_slot(self):
        prev = datetime(2026, 11, 9, 12, 0)
        assert next_fire(
            tz=ACRE, after=datetime(2026, 11, 9, 12, 0, 5), interval_seconds=3600, previous=prev
        ) == datetime(2026, 11, 9, 13, 0)

    def test_missed_interval_slots_collapse_to_the_first_after_now(self):
        prev = datetime(2026, 11, 9, 0, 0)
        assert next_fire(
            tz=ACRE, after=datetime(2026, 11, 9, 10, 30), interval_seconds=3600, previous=prev
        ) == datetime(2026, 11, 9, 11, 0)

    def test_cron_after_downtime_is_the_first_future_slot(self):
        nxt = next_fire(tz=ACRE, after=datetime(2026, 11, 12, 20, 0), cron_expr="0 9 * * *")
        assert format_local(nxt, ACRE) == "Fri 13 Nov 2026 09:00"

    def test_one_shot_has_no_next(self):
        assert next_fire(tz=ACRE, after=NOW, run_at=NOW - timedelta(minutes=1)) is None


class TestCatchup:
    def test_half_the_period_clamped(self):
        assert catchup_window(timedelta(hours=1)) == timedelta(minutes=30)
        assert catchup_window(timedelta(minutes=2)) == CATCHUP_MIN
        assert catchup_window(timedelta(days=7)) == CATCHUP_MAX

    def test_one_shot_grace(self):
        assert catchup_window(None) == ONE_SHOT_GRACE

    def test_period_of_a_cron(self):
        slot = datetime(2026, 11, 10, 14, 0)  # Tue 09:00 local
        assert period(tz=ACRE, slot=slot, cron_expr="0 9 * * *", interval_seconds=None) == (
            timedelta(days=1)
        )


class TestFrequency:
    def test_min_gap_and_max_per_day(self):
        s = parse_schedule("every monday,friday at 9:00", ACRE, NOW)
        slots = upcoming(s, ACRE, NOW, timedelta(days=8))
        assert len(slots) == 2
        assert min_gap(slots) == timedelta(days=3)
        assert max_per_day(slots) == 1

    def test_dense_cron(self):
        s = parse_schedule("*/10 9 * * *", ACRE, NOW)
        slots = upcoming(s, ACRE, NOW, timedelta(days=8))
        assert min_gap(slots) == timedelta(minutes=10)
        assert max_per_day(slots) == 6

    def test_interval_per_day(self):
        s = parse_schedule("every 4h", ACRE, NOW)
        assert max_per_day(upcoming(s, ACRE, NOW, timedelta(days=2))) == 6
