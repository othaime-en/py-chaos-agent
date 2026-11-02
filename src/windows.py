"""
Allowed time windows: when an experiment may run.

A window set (``ExperimentWindows`` in ``src.schemas``) is a time zone plus a list
of windows, each a set of weekdays with a start and an end time::

    timezone: Africa/Dar_es_Salaam
    allow:
      - days: [mon, tue, wed, thu]
        start: "10:00"
        end: "16:00"

Rules, so the answer to "is it open?" is never a surprise:

* A window is open from its start (inclusive) to its end (exclusive).
* ``days`` are the days the window *starts* on. If ``end`` is not later than
  ``start`` the window runs past midnight into the next day, so ``fri 22:00`` to
  ``02:00`` is open on Saturday at 01:00 and closed on Sunday at 01:00. ``24:00``
  is allowed as an end and means midnight.
* Times are wall-clock times in the window's time zone. Across a daylight-saving
  change the window keeps its wall-clock times, so it can be an hour shorter or
  longer that day. A boundary that falls in the skipped or repeated hour resolves
  the way ``zoneinfo`` resolves it.
* Windows that overlap or touch (a window ending at midnight and one starting at
  midnight) are treated as one continuous window.
* No windows at all means "always allowed". That is the default.

Everything here is pure: pass in the time, get an answer.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order

# Far enough ahead to always find the next opening: windows repeat weekly.
_LOOKAHEAD_DAYS = 9


def utcnow() -> dt.datetime:
    """The current time, timezone-aware. Tests replace this."""
    return dt.datetime.now(dt.timezone.utc)


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _iso(moment: Optional[dt.datetime]) -> Optional[str]:
    if moment is None:
        return None
    utc = moment.astimezone(dt.timezone.utc)
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class WindowStatus:
    """
    Whether the windows are open at a moment.

    When open, ``closes_at`` is when the current continuous window ends. When
    closed, ``opens_at`` is when the next one starts. Both are UTC. With no
    windows configured it is open and both are ``None``.
    """

    open: bool
    closes_at: Optional[dt.datetime] = None
    opens_at: Optional[dt.datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"open": self.open}
        if self.open:
            data["closes_at"] = _iso(self.closes_at)
        else:
            data["opens_at"] = _iso(self.opens_at)
        return data


def _at(day: dt.date, minutes: int, tz: ZoneInfo) -> dt.datetime:
    """Wall-clock ``minutes`` after midnight on ``day`` in ``tz``, as UTC."""
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    return (midnight + dt.timedelta(minutes=minutes)).astimezone(dt.timezone.utc)


def _intervals(
    windows: Mapping[str, Any], now: dt.datetime
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Every window instance around ``now``, merged where they touch."""
    tz = ZoneInfo(windows["timezone"])
    today = now.astimezone(tz).date()
    found: List[Tuple[dt.datetime, dt.datetime]] = []
    for offset in range(-1, _LOOKAHEAD_DAYS):
        day = today + dt.timedelta(days=offset)
        for window in windows["allow"]:
            if DAYS[day.weekday()] not in window["days"]:
                continue
            start = _minutes(window["start"])
            end = _minutes(window["end"])
            # An end that is not after the start means "the next day".
            if end <= start:
                end += 24 * 60
            found.append((_at(day, start, tz), _at(day, end, tz)))
    found.sort()
    merged: List[Tuple[dt.datetime, dt.datetime]] = []
    for start_at, end_at in found:
        if merged and start_at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_at))
        else:
            merged.append((start_at, end_at))
    return merged


def evaluate(windows: Optional[Mapping[str, Any]], now: dt.datetime) -> WindowStatus:
    """Is ``now`` inside an allowed window? ``None`` windows are always open."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not windows:
        return WindowStatus(open=True)
    for start_at, end_at in _intervals(windows, now):
        if start_at <= now < end_at:
            return WindowStatus(open=True, closes_at=end_at)
    upcoming = [s for s, _ in _intervals(windows, now) if s > now]
    return WindowStatus(open=False, opens_at=upcoming[0] if upcoming else None)


def format_local(moment: Optional[dt.datetime], windows: Mapping[str, Any]) -> str:
    """A moment in the window's own time zone, for messages people read."""
    if moment is None:
        return "an unknown time"
    tz = windows["timezone"]
    local = moment.astimezone(ZoneInfo(tz))
    return f"{local:%a %Y-%m-%d %H:%M} ({tz})"
