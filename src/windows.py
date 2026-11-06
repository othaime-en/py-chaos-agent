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
* ``blackouts`` are date ranges (inclusive, whole days in the time zone) when it
  is never allowed, whatever the windows say. Without ``allow`` windows, "any time
  except the blackouts" is allowed.
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


def iso(moment: Optional[dt.datetime]) -> Optional[str]:
    if moment is None:
        return None
    utc = moment.astimezone(dt.timezone.utc)
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class WindowStatus:
    """
    Whether the windows are open at a moment.

    When open, ``closes_at`` is when the current continuous window ends, or
    ``None`` if nothing ends it (no ``allow`` windows and no blackout ahead). When
    closed, ``opens_at`` is when the next one starts, and ``blackout`` says why if
    a blackout period is the reason. Times are UTC. With no windows configured it
    is open and both times are ``None``.
    """

    open: bool
    closes_at: Optional[dt.datetime] = None
    opens_at: Optional[dt.datetime] = None
    in_blackout: bool = False
    blackout: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"open": self.open}
        if self.open:
            data["closes_at"] = iso(self.closes_at)
        else:
            data["opens_at"] = iso(self.opens_at)
            if self.in_blackout:
                data["blackout"] = self.blackout or "blackout period"
        return data


def _at(day: dt.date, minutes: int, tz: ZoneInfo) -> dt.datetime:
    """Wall-clock ``minutes`` after midnight on ``day`` in ``tz``, as UTC."""
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    return (midnight + dt.timedelta(minutes=minutes)).astimezone(dt.timezone.utc)


def _intervals(
    windows: Mapping[str, Any], now: dt.datetime
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Every ``allow`` window instance around ``now``, merged where they touch."""
    tz = ZoneInfo(windows["timezone"])
    today = now.astimezone(tz).date()
    found: List[Tuple[dt.datetime, dt.datetime]] = []
    for offset in range(-1, _LOOKAHEAD_DAYS):
        day = today + dt.timedelta(days=offset)
        for window in windows.get("allow") or []:
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


def _blackouts(
    windows: Mapping[str, Any],
) -> List[Tuple[dt.datetime, dt.datetime, str]]:
    """
    The blackout periods as UTC ranges, merged where they overlap or touch. Dates
    are inclusive and whole days in the windows' time zone: ``from`` starts at
    midnight, ``to`` ends at the midnight after it.
    """
    tz = ZoneInfo(windows["timezone"])
    found = []
    for blackout in windows.get("blackouts") or []:
        first = dt.date.fromisoformat(blackout["from"])
        last = dt.date.fromisoformat(blackout["to"])
        found.append(
            (
                _at(first, 0, tz),
                _at(last + dt.timedelta(days=1), 0, tz),
                blackout.get("reason") or "",
            )
        )
    found.sort(key=lambda b: (b[0], b[1]))
    merged: List[Tuple[dt.datetime, dt.datetime, str]] = []
    for start_at, end_at, reason in found:
        if merged and start_at <= merged[-1][1]:
            prior = merged[-1]
            reasons = [r for r in (prior[2], reason) if r]
            joined = "; ".join(dict.fromkeys(reasons))
            merged[-1] = (prior[0], max(prior[1], end_at), joined)
        else:
            merged.append((start_at, end_at, reason))
    return merged


def evaluate(windows: Optional[Mapping[str, Any]], now: dt.datetime) -> WindowStatus:
    """
    Is ``now`` inside an allowed window and outside every blackout? ``None``
    windows are always open.

    A blackout beats an allowed window. Without ``allow`` windows the experiment
    is allowed at any time except during a blackout.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not windows:
        return WindowStatus(open=True)

    blackouts = _blackouts(windows)
    has_allow = bool(windows.get("allow"))

    def blackout_at(t: dt.datetime) -> Optional[Tuple[dt.datetime, dt.datetime, str]]:
        return next((b for b in blackouts if b[0] <= t < b[1]), None)

    def allowed_until(t: dt.datetime) -> Tuple[bool, Optional[dt.datetime]]:
        if not has_allow:
            return True, None
        for start_at, end_at in _intervals(windows, t):
            if start_at <= t < end_at:
                return True, end_at
        return False, None

    def next_allowed(t: dt.datetime) -> dt.datetime:
        if allowed_until(t)[0]:
            return t
        return min(s for s, _ in _intervals(windows, t) if s > t)

    current = blackout_at(now)
    if current is None:
        is_open, ends = allowed_until(now)
        if is_open:
            ahead = [b[0] for b in blackouts if b[0] > now]
            candidates = [t for t in [ends, min(ahead) if ahead else None] if t]
            return WindowStatus(
                open=True, closes_at=min(candidates) if candidates else None
            )

    # Closed. Find the next moment that is allowed and not in a blackout.
    moment = now
    for _ in range(len(blackouts) + 2):
        moment = next_allowed(moment)
        hit = blackout_at(moment)
        if hit is None:
            break
        moment = hit[1]
    return WindowStatus(
        open=False,
        opens_at=moment,
        in_blackout=current is not None,
        blackout=(current[2] or None) if current else None,
    )


def format_local(moment: Optional[dt.datetime], windows: Mapping[str, Any]) -> str:
    """A moment in the window's own time zone, for messages people read."""
    if moment is None:
        return "an unknown time"
    tz = windows["timezone"]
    local = moment.astimezone(ZoneInfo(tz))
    return f"{local:%a %Y-%m-%d %H:%M} ({tz})"
