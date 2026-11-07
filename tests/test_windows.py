"""Time window evaluation: boundaries, midnight, zones, daylight saving."""

import datetime as dt

import pytest

from src.windows import evaluate, format_local

UTC = dt.timezone.utc


def at(text):
    """'2026-11-05 10:00' -> aware UTC datetime."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def windows(*allow, timezone="UTC"):
    return {"timezone": timezone, "allow": list(allow)}


def win(start, end, days=("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
    return {"days": list(days), "start": start, "end": end}


# 2026-11-02 is a Monday.
WORKDAYS = windows(win("10:00", "16:00", ("mon", "tue", "wed", "thu", "fri")))


class TestBasics:
    def test_no_windows_is_always_open(self):
        status = evaluate(None, at("2026-11-02 03:00"))
        assert status.open and status.closes_at is None and status.opens_at is None

    def test_inside_the_window(self):
        status = evaluate(WORKDAYS, at("2026-11-02 12:00"))
        assert status.open
        assert status.closes_at == at("2026-11-02 16:00")

    def test_start_is_inclusive_and_end_is_exclusive(self):
        assert evaluate(WORKDAYS, at("2026-11-02 10:00")).open
        assert evaluate(WORKDAYS, at("2026-11-02 15:59")).open
        assert not evaluate(WORKDAYS, at("2026-11-02 16:00")).open
        assert not evaluate(WORKDAYS, at("2026-11-02 09:59")).open

    def test_closed_says_when_it_opens_next(self):
        assert evaluate(WORKDAYS, at("2026-11-02 08:00")).opens_at == at(
            "2026-11-02 10:00"
        )
        assert evaluate(WORKDAYS, at("2026-11-02 17:00")).opens_at == at(
            "2026-11-03 10:00"
        )

    def test_days_are_respected(self):
        # Friday evening: the next opening is Monday.
        status = evaluate(WORKDAYS, at("2026-11-06 17:00"))
        assert not status.open and status.opens_at == at("2026-11-09 10:00")
        assert not evaluate(WORKDAYS, at("2026-11-07 12:00")).open  # Saturday

    def test_a_single_day_window_opens_a_week_later(self):
        only_monday = windows(win("10:00", "11:00", ("mon",)))
        status = evaluate(only_monday, at("2026-11-02 11:00"))
        assert status.opens_at == at("2026-11-09 10:00")

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate(WORKDAYS, dt.datetime(2026, 11, 2, 12, 0))

    def test_serializes_to_utc_strings(self):
        assert evaluate(WORKDAYS, at("2026-11-02 12:00")).to_dict() == {
            "open": True,
            "closes_at": "2026-11-02T16:00:00Z",
        }
        assert evaluate(WORKDAYS, at("2026-11-02 08:00")).to_dict() == {
            "open": False,
            "opens_at": "2026-11-02T10:00:00Z",
        }


class TestMultipleWindows:
    def test_either_window_opens_it(self):
        both = windows(win("08:00", "09:00"), win("20:00", "21:00"))
        assert evaluate(both, at("2026-11-02 08:30")).open
        assert evaluate(both, at("2026-11-02 20:30")).open
        assert not evaluate(both, at("2026-11-02 12:00")).open

    def test_the_next_opening_is_the_earliest_one(self):
        both = windows(win("20:00", "21:00"), win("08:00", "09:00"))
        assert evaluate(both, at("2026-11-02 12:00")).opens_at == at("2026-11-02 20:00")

    def test_overlapping_windows_are_one(self):
        overlap = windows(win("09:00", "12:00"), win("11:00", "14:00"))
        assert evaluate(overlap, at("2026-11-02 10:00")).closes_at == at(
            "2026-11-02 14:00"
        )

    def test_touching_windows_are_one(self):
        touching = windows(win("09:00", "12:00"), win("12:00", "14:00"))
        status = evaluate(touching, at("2026-11-02 10:00"))
        assert status.closes_at == at("2026-11-02 14:00")


class TestMidnight:
    NIGHT = windows(win("22:00", "02:00", ("fri",)))

    def test_it_runs_into_the_next_day(self):
        assert evaluate(self.NIGHT, at("2026-11-06 23:00")).open  # Friday
        status = evaluate(self.NIGHT, at("2026-11-07 01:00"))  # Saturday
        assert status.open and status.closes_at == at("2026-11-07 02:00")

    def test_days_are_the_day_it_starts(self):
        assert not evaluate(self.NIGHT, at("2026-11-07 03:00")).open
        assert not evaluate(self.NIGHT, at("2026-11-08 01:00")).open  # Sunday
        assert not evaluate(self.NIGHT, at("2026-11-05 23:00")).open  # Thursday

    def test_across_a_week_boundary(self):
        sunday = windows(win("23:00", "01:00", ("sun",)))
        status = evaluate(sunday, at("2026-11-09 00:30"))  # Monday
        assert status.open and status.closes_at == at("2026-11-09 01:00")

    def test_a_midnight_end_stays_in_the_day(self):
        evening = windows(win("18:00", "24:00", ("mon",)))
        assert evaluate(evening, at("2026-11-02 23:59")).open
        assert not evaluate(evening, at("2026-11-03 00:00")).open

    def test_a_whole_day(self):
        whole = windows(win("00:00", "24:00", ("sat",)))
        assert evaluate(whole, at("2026-11-07 00:00")).open
        assert evaluate(whole, at("2026-11-07 23:59")).open
        assert not evaluate(whole, at("2026-11-08 00:00")).open

    def test_consecutive_whole_days_merge(self):
        weekend = windows(win("00:00", "24:00", ("sat", "sun")))
        status = evaluate(weekend, at("2026-11-07 12:00"))
        assert status.closes_at == at("2026-11-09 00:00")

    def test_a_window_ending_at_midnight_joins_the_next_morning(self):
        split = windows(win("22:00", "24:00"), win("00:00", "06:00"))
        status = evaluate(split, at("2026-11-02 23:00"))
        assert status.closes_at == at("2026-11-03 06:00")


class TestTimeZones:
    HNL = windows(win("10:00", "16:00"), timezone="Pacific/Honolulu")  # UTC-10, no DST

    def test_times_are_local(self):
        assert evaluate(self.HNL, at("2026-11-02 20:00")).open  # 10:00 local
        assert not evaluate(self.HNL, at("2026-11-02 19:59")).open
        assert not evaluate(self.HNL, at("2026-11-03 02:00")).open  # 16:00 local

    def test_the_local_day_decides_the_weekday(self):
        # Monday 05:00 UTC is still Sunday 19:00 in Honolulu.
        monday = windows(win("00:00", "06:00", ("mon",)), timezone="Pacific/Honolulu")
        assert not evaluate(monday, at("2026-11-02 05:00")).open
        assert evaluate(monday, at("2026-11-02 12:00")).open

    def test_results_are_utc(self):
        status = evaluate(self.HNL, at("2026-11-02 21:00"))
        assert status.closes_at.utcoffset() == dt.timedelta(0)
        assert status.closes_at == at("2026-11-03 02:00")

    def test_format_local(self):
        text = format_local(at("2026-11-02 20:00"), self.HNL)
        assert text == "Mon 2026-11-02 10:00 (Pacific/Honolulu)"

    def test_format_local_without_a_moment(self):
        assert format_local(None, self.HNL) == "an unknown time"


class TestDaylightSaving:
    NY = windows(win("01:00", "04:00"), timezone="America/New_York")

    def test_spring_forward_shortens_the_window(self):
        # 2026-03-08: 02:00 -> 03:00 in New York. 01:00-04:00 local is 2 hours.
        status = evaluate(self.NY, at("2026-03-08 06:30"))  # 01:30 EST
        assert status.open
        assert status.closes_at == at("2026-03-08 08:00")  # 04:00 EDT

    def test_fall_back_lengthens_the_window(self):
        # 2026-11-01: 02:00 -> 01:00. 01:00-04:00 local is 4 hours.
        status = evaluate(self.NY, at("2026-11-01 05:30"))  # 01:30 EDT
        assert status.open
        assert status.closes_at == at("2026-11-01 09:00")  # 04:00 EST

    def test_the_wall_clock_time_is_kept_across_the_change(self):
        before = evaluate(self.NY, at("2026-03-07 07:00"))  # 02:00 EST
        after = evaluate(self.NY, at("2026-03-09 06:00"))  # 02:00 EDT
        assert before.open and after.open


def blackout(first, last, reason=""):
    return {"from": first, "to": last, "reason": reason}


def freeze(*blackouts, allow=None, timezone="UTC"):
    spec = {"timezone": timezone, "allow": allow or [], "blackouts": list(blackouts)}
    return spec


# 2026-11-02 is a Monday.
FREEZE = freeze(blackout("2026-12-20", "2027-01-03", "holiday freeze"))


class TestBlackoutsWithoutAllowWindows:
    def test_any_time_is_open_outside_a_blackout(self):
        status = evaluate(FREEZE, at("2026-11-02 12:00"))
        assert status.open
        assert status.closes_at == at(
            "2026-12-20 00:00"
        ), "it closes when the freeze starts"

    def test_with_no_blackout_ahead_nothing_ends_it(self):
        status = evaluate(FREEZE, at("2027-02-01 12:00"))
        assert status.open and status.closes_at is None
        assert status.to_dict() == {"open": True, "closes_at": None}

    def test_inside_a_blackout_it_is_closed_until_the_day_after(self):
        status = evaluate(FREEZE, at("2026-12-25 12:00"))
        assert not status.open and status.in_blackout
        assert status.blackout == "holiday freeze"
        assert status.opens_at == at("2027-01-04 00:00")

    def test_both_dates_are_inclusive_whole_days(self):
        assert not evaluate(FREEZE, at("2026-12-20 00:00")).open
        assert evaluate(FREEZE, at("2026-12-19 23:59")).open
        assert not evaluate(FREEZE, at("2027-01-03 23:59")).open
        assert evaluate(FREEZE, at("2027-01-04 00:00")).open

    def test_a_single_day(self):
        one = freeze(blackout("2026-12-24", "2026-12-24"))
        assert not evaluate(one, at("2026-12-24 15:00")).open
        assert evaluate(one, at("2026-12-25 00:00")).open

    def test_the_reason_is_optional(self):
        spec = freeze(blackout("2026-12-24", "2026-12-24"))
        status = evaluate(spec, at("2026-12-24 15:00"))
        assert status.blackout is None
        assert status.to_dict()["blackout"] == "blackout period"

    def test_serializes_with_the_reason(self):
        assert evaluate(FREEZE, at("2026-12-25 12:00")).to_dict() == {
            "open": False,
            "opens_at": "2027-01-04T00:00:00Z",
            "blackout": "holiday freeze",
        }


class TestBlackoutsBeatAllowWindows:
    MON_FRI = [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "10:00", "end": "16:00"}
    ]

    def test_a_window_inside_a_blackout_is_closed(self):
        spec = freeze(blackout("2026-11-02", "2026-11-03"), allow=self.MON_FRI)
        status = evaluate(spec, at("2026-11-02 12:00"))  # Monday, inside the window
        assert not status.open and status.in_blackout

    def test_it_reopens_at_the_next_window_not_at_the_blackout_end(self):
        spec = freeze(blackout("2026-11-02", "2026-11-03"), allow=self.MON_FRI)
        # The blackout ends Wednesday 00:00, but the window opens at 10:00.
        status = evaluate(spec, at("2026-11-02 12:00"))
        assert status.opens_at == at("2026-11-04 10:00")

    def test_a_window_is_cut_short_by_a_blackout_starting_later(self):
        spec = freeze(
            blackout("2026-11-03", "2026-11-03"),
            allow=[
                {"days": ["mon"], "start": "20:00", "end": "24:00"},
                {"days": ["tue"], "start": "00:00", "end": "06:00"},
            ],
        )
        # Mon 20:00 -> Tue 06:00 is one window, but Tuesday is blacked out.
        status = evaluate(spec, at("2026-11-02 21:00"))
        assert status.open and status.closes_at == at("2026-11-03 00:00")

    def test_the_next_opening_skips_consecutive_blackouts(self):
        spec = freeze(
            blackout("2026-11-03", "2026-11-04"),
            blackout("2026-11-05", "2026-11-06"),
            allow=self.MON_FRI,
        )
        # Tue-Fri are all blacked out, so the next opening is Monday.
        status = evaluate(spec, at("2026-11-03 12:00"))
        assert status.opens_at == at("2026-11-09 10:00")

    def test_outside_a_window_and_with_a_blackout_ahead(self):
        spec = freeze(blackout("2026-11-03", "2026-11-03"), allow=self.MON_FRI)
        status = evaluate(spec, at("2026-11-02 18:00"))  # Monday evening
        assert not status.open and not status.in_blackout
        assert status.opens_at == at("2026-11-04 10:00"), "Tuesday is blacked out"

    def test_no_blackouts_ahead_leaves_a_window_as_before(self):
        spec = freeze(blackout("2030-01-01", "2030-01-02"), allow=self.MON_FRI)
        assert evaluate(spec, at("2026-11-02 12:00")).closes_at == at(
            "2026-11-02 16:00"
        )


class TestBlackoutMerging:
    def test_overlapping_blackouts_are_one_and_keep_both_reasons(self):
        spec = freeze(
            blackout("2026-12-20", "2026-12-26", "holidays"),
            blackout("2026-12-24", "2026-12-31", "freeze"),
        )
        status = evaluate(spec, at("2026-12-25 12:00"))
        assert status.opens_at == at("2027-01-01 00:00")
        assert status.blackout == "holidays; freeze"

    def test_touching_blackouts_are_one(self):
        spec = freeze(
            blackout("2026-12-20", "2026-12-21"), blackout("2026-12-22", "2026-12-23")
        )
        assert evaluate(spec, at("2026-12-21 12:00")).opens_at == at("2026-12-24 00:00")

    def test_unsorted_blackouts(self):
        spec = freeze(
            blackout("2027-01-10", "2027-01-12"), blackout("2026-12-20", "2026-12-21")
        )
        assert evaluate(spec, at("2026-12-01 12:00")).closes_at == at(
            "2026-12-20 00:00"
        )

    def test_a_distant_blackout_does_not_affect_today(self):
        spec = freeze(blackout("2099-01-01", "2099-01-05"))
        assert evaluate(spec, at("2026-11-02 12:00")).closes_at == at(
            "2099-01-01 00:00"
        )


class TestBlackoutTimeZones:
    HNL = freeze(
        blackout("2026-12-24", "2026-12-24"), timezone="Pacific/Honolulu"
    )  # UTC-10, no DST

    def test_the_dates_are_local_days(self):
        # 2026-12-24 local runs from 2026-12-24 10:00 UTC to 2026-12-25 10:00 UTC.
        assert not evaluate(self.HNL, at("2026-12-24 10:00")).open
        assert evaluate(self.HNL, at("2026-12-24 09:59")).open
        assert not evaluate(self.HNL, at("2026-12-25 09:59")).open
        assert evaluate(self.HNL, at("2026-12-25 10:00")).open

    def test_the_message_names_the_local_time(self):
        status = evaluate(self.HNL, at("2026-12-24 15:00"))  # 05:00 local
        assert (
            format_local(status.opens_at, self.HNL)
            == "Fri 2026-12-25 00:00 (Pacific/Honolulu)"
        )

    def test_a_dst_zone_keeps_whole_local_days(self):
        ny = freeze(blackout("2026-03-08", "2026-03-08"), timezone="America/New_York")
        status = evaluate(ny, at("2026-03-08 12:00"))
        assert not status.open
        # The spring-forward day is 23 hours long: midnight EST to midnight EDT.
        assert status.opens_at == at("2026-03-09 04:00")
