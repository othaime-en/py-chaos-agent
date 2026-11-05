"""
Selection: may this agent run this experiment right now, and if not, why not?

One function, :func:`evaluate`, answers that by checking the gates in a fixed order
and reporting every one, so a refusal, a dry run and the preview endpoint all give
the same explanation:

1. **eligibility**: the agent-wide opt-in (``agent.eligibility`` in the config).
   A pod that never got the label is not eligible, so forgetting it fails safe.
2. **selector**: the experiment's label selector.
3. **window**: the experiment's time windows, and whether its chaos fits in the
   time left.
4. **sample**: whether this agent is in the experiment's ``sample_percent``.
5. **kill_switch**: whether the kill switch is holding chaos back.
6. **idle**: whether anything is already running.

The last two are state the caller knows about, so it passes them in. Gates that do
not apply (no selector, no windows, ...) are left out of the report.

Sampling is deterministic. Each agent hashes its own identity, the experiment name
and a *sample key* into a number from 0 to 100 and is in the sample if that is
below ``sample_percent``. The same agents are therefore picked every time, with no
coordination between agents, until the key changes (pass a new one, such as the
date, to draw a new sample). Across a fleet the share picked is close to
``sample_percent`` for large fleets and only approximately so for small ones: it is
a dice roll per agent, not a count.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from . import targets, windows as time_windows

GATE_ORDER = ("eligibility", "selector", "window", "sample", "kill_switch", "idle")


@dataclass(frozen=True)
class Gate:
    """One check: its name, whether it passed, and why."""

    name: str
    ok: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"gate": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class Selection:
    """The result of every applicable gate, in order."""

    gates: List[Gate] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return all(g.ok for g in self.gates)

    @property
    def failed(self) -> List[Gate]:
        return [g for g in self.gates if not g.ok]

    @property
    def summary(self) -> str:
        if self.eligible:
            return "selected"
        return "; ".join(f"{g.name}: {g.detail}" for g in self.failed)

    def gate(self, name: str) -> Optional[Gate]:
        return next((g for g in self.gates if g.name == name), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"eligible": self.eligible, "gates": [g.to_dict() for g in self.gates]}


def chaos_budget_seconds(experiment: Mapping[str, Any]) -> float:
    """
    The longest the chaos phase of ``experiment`` can last, worst case.

    That is the scheduled duration (or, with ``max_injections``, the time until
    the last allowed injection starts) plus the longest single fault, which may
    still be running when the schedule ends. The baseline and recovery checks
    are not chaos and are not counted.
    """
    schedule = experiment["schedule"]
    active = float(schedule["duration_seconds"])
    cap = schedule.get("max_injections")
    if cap is not None:
        active = min(active, (cap - 1) * float(schedule["interval_seconds"]))
    longest = max(
        (f.get("duration_seconds", 0) for f in experiment["faults"].values()),
        default=0,
    )
    return active + longest


def sample_bucket(agent_id: str, experiment: str, sample_key: str = "") -> float:
    """This agent's place for this experiment, from 0 (inclusive) to 100."""
    digest = hashlib.sha256(
        "\0".join((agent_id, experiment, sample_key)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 * 100


def eligibility_problem(
    eligibility: Optional[Mapping[str, Any]], labels: Mapping[str, str]
) -> Optional[str]:
    """Why this agent is not eligible for chaos, or ``None`` if it is."""
    if not eligibility:
        return None
    result = targets.match(eligibility, labels)
    return None if result.matches else result.summary


def window_gate(experiment: Mapping[str, Any], now: dt.datetime) -> Optional[Gate]:
    """The window gate for ``experiment``, or ``None`` if it has no windows."""
    spec = experiment.get("windows")
    if not spec:
        return None
    status = time_windows.evaluate(spec, now)
    if not status.open:
        return Gate(
            "window",
            False,
            "Outside the allowed time window; the next one opens "
            + time_windows.format_local(status.opens_at, spec),
        )
    assert status.closes_at is not None
    needed = chaos_budget_seconds(experiment)
    remaining = (status.closes_at - now).total_seconds()
    closes = time_windows.format_local(status.closes_at, spec)
    if needed > remaining:
        return Gate(
            "window",
            False,
            f"Not enough time left in the allowed window: it closes {closes}, in "
            f"{int(remaining)}s, and this experiment needs up to {int(needed)}s "
            "of chaos",
        )
    return Gate("window", True, f"open until {closes}")


def evaluate(
    experiment: Mapping[str, Any],
    *,
    labels: Mapping[str, str],
    now: dt.datetime,
    eligibility: Optional[Mapping[str, Any]] = None,
    agent_id: str = "",
    sample_key: str = "",
    kill_switch_reason: Optional[str] = None,
    busy_reason: Optional[str] = None,
    report_state: bool = False,
) -> Selection:
    """
    Run every applicable gate for ``experiment`` and report each one.

    ``kill_switch_reason`` and ``busy_reason`` are why chaos is held back or
    something is already running, or ``None``; the caller supplies them because it
    owns that state. Those two gates are listed when they fail, or always with
    ``report_state=True`` (what the preview does).
    """
    gates: List[Gate] = []

    if eligibility:
        problem = eligibility_problem(eligibility, labels)
        gates.append(
            Gate(
                "eligibility",
                problem is None,
                problem or "labels satisfy this agent's eligibility rule",
            )
        )

    selector = experiment.get("selector")
    if selector and (selector.get("match_labels") or selector.get("match_expressions")):
        result = targets.match(selector, labels)
        gates.append(
            Gate(
                "selector",
                result.matches,
                "labels match the selector" if result.matches else result.summary,
            )
        )

    window = window_gate(experiment, now)
    if window is not None:
        gates.append(window)

    percent = (selector or {}).get("sample_percent")
    if percent is not None:
        bucket = sample_bucket(agent_id, experiment["name"], sample_key)
        picked = bucket < percent
        shown = f"{percent:g}%"
        gates.append(
            Gate(
                "sample",
                picked,
                (
                    f"this agent is in the {shown} sample (position {bucket:.1f})"
                    if picked
                    else f"this agent is not in the {shown} sample "
                    f"(position {bucket:.1f}, needs to be below {percent:g})"
                ),
            )
        )

    if kill_switch_reason is not None:
        gates.append(Gate("kill_switch", False, f"Chaos paused: {kill_switch_reason}"))
    elif report_state:
        gates.append(
            Gate("kill_switch", True, "the kill switch is not holding chaos back")
        )
    if busy_reason is not None:
        gates.append(Gate("idle", False, busy_reason))
    elif report_state:
        gates.append(Gate("idle", True, "nothing else is running"))

    return Selection(gates)
