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
    DAR = windows(win("10:00", "16:00"), timezone="Africa/Dar_es_Salaam")  # UTC+3

    def test_times_are_local(self):
        assert evaluate(self.DAR, at("2026-11-02 07:00")).open  # 10:00 local
        assert not evaluate(self.DAR, at("2026-11-02 06:59")).open
        assert not evaluate(self.DAR, at("2026-11-02 13:00")).open  # 16:00 local

    def test_the_local_day_decides_the_weekday(self):
        # Sunday 22:00 UTC is already Monday 01:00 in Dar es Salaam.
        monday = windows(
            win("00:00", "06:00", ("mon",)), timezone="Africa/Dar_es_Salaam"
        )
        assert evaluate(monday, at("2026-11-01 22:00")).open

    def test_results_are_utc(self):
        status = evaluate(self.DAR, at("2026-11-02 08:00"))
        assert status.closes_at.utcoffset() == dt.timedelta(0)
        assert status.closes_at == at("2026-11-02 13:00")

    def test_format_local(self):
        text = format_local(at("2026-11-02 07:00"), self.DAR)
        assert text == "Mon 2026-11-02 10:00 (Africa/Dar_es_Salaam)"

    def test_format_local_without_a_moment(self):
        assert format_local(None, self.DAR) == "an unknown time"


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
