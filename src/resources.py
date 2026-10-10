"""
Resource governor: keeps CPU and memory injections inside the container's
real budget.

The static limits in ``src.limits`` are fixed constants. This module adds the
part that depends on where the agent is actually running. It reads the
container's cgroup limits (v2 and v1), measures current headroom, and answers
two questions before any CPU or memory injection starts:

* CPU: how many workers may run? Requests above the budget are **clamped**
  (fewer workers still produce load, so reducing is harmless).
* Memory: is this allocation safe? Requests above the budget are **refused**
  (a smaller allocation would silently run a different experiment, and an
  oversized one risks an OOM kill).

Concurrent injections (the loop plus manual API calls) share one budget via
reservations, so two safe requests cannot add up to an unsafe one.

What this does NOT do: it bounds the agent's own container. It cannot make an
unlimited container safe. If no cgroup limit exists it falls back to host
CPU count and host available memory, and ``require_cgroup_limits`` can refuse
to inject at all in that situation.

All sizes are MiB (1024 * 1024 bytes), matching how the memory injector
allocates.
"""

import math
import os
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import psutil

from . import limits
from .logging_config import get_logger

logger = get_logger(__name__)

MIB = 1024 * 1024
# cgroup v1 reports ~2**63 when no memory limit is set.
_V1_UNLIMITED_BYTES = 1 << 60

DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")
DEFAULT_PROC_CGROUP = Path("/proc/self/cgroup")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetySettings:
    """Operator-controlled safety knobs. Set from the config file only; the
    API cannot change them, so a remote caller can never loosen them."""

    cpu_fraction: float = limits.DEFAULT_CPU_FRACTION
    memory_fraction: float = limits.DEFAULT_MEMORY_FRACTION
    require_cgroup_limits: bool = False


@dataclass(frozen=True)
class CgroupInfo:
    """What the kernel says this container may use. None means no limit."""

    version: Optional[int] = None  # 1, 2, or None if no cgroup data found
    cpu_limit_cores: Optional[float] = None
    memory_limit_bytes: Optional[int] = None
    # Working set at the level that sets the limit: usage minus reclaimable
    # file cache, the same measure the kubelet uses for eviction.
    memory_working_set_bytes: Optional[int] = None


@dataclass(frozen=True)
class Budget:
    """Effective ceilings for one moment in time."""

    max_cores: int
    max_memory_mb: int
    cpu_capacity: float
    memory_headroom_mb: int
    cpu_limited_by: str  # "cgroup" or "host"
    memory_limited_by: str  # "cgroup" or "host"
    cgroup: CgroupInfo


@dataclass(frozen=True)
class Grant:
    """Outcome of asking the governor for resources."""

    ok: bool
    requested: int
    granted: int
    clamped: bool = False
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# cgroup reading
# ---------------------------------------------------------------------------


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _to_int(text: Optional[str]) -> Optional[int]:
    """Parse an integer; 'max' and garbage both mean 'no usable value'."""
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _stat_value(path: Path, *keys: str) -> int:
    """Read the first matching key from a memory.stat file (0 if absent)."""
    text = _read(path)
    if not text:
        return 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in keys:
            value = _to_int(parts[1])
            if value is not None:
                return value
    return 0


def _levels(base: Path, rel: str) -> Iterator[Path]:
    """
    Yield `base/rel` then each ancestor up to and including `base`.

    A pod-level cgroup can be tighter than the container's own, so every level
    is checked and the tightest limit wins. If the relative path does not exist
    under `base` (common when the container has its own cgroup namespace), only
    `base` itself is used.
    """
    rel_parts = [p for p in rel.strip("/").split("/") if p]
    if rel_parts and (base.joinpath(*rel_parts)).is_dir():
        for depth in range(len(rel_parts), 0, -1):
            yield base.joinpath(*rel_parts[:depth])
    yield base


def _own_cgroup_paths(proc_cgroup: Path) -> Dict[str, str]:
    """
    Parse /proc/self/cgroup into {controller: relative_path}.
    v2 lines look like '0::/path'; v1 like '4:memory:/path'.
    """
    paths: Dict[str, str] = {}
    text = _read(proc_cgroup)
    if not text:
        return paths
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, controllers, path = parts
        if controllers == "":
            paths["v2"] = path
        else:
            for controller in controllers.split(","):
                paths[controller] = path
    return paths


def _read_v2(root: Path, proc_cgroup: Path) -> CgroupInfo:
    rel = _own_cgroup_paths(proc_cgroup).get("v2", "")

    cpu_limit: Optional[float] = None
    mem_limit: Optional[int] = None
    mem_ws: Optional[int] = None
    best_headroom: Optional[int] = None

    for level in _levels(root, rel):
        cpu_text = _read(level / "cpu.max")
        if cpu_text:
            parts = cpu_text.split()
            quota = _to_int(parts[0]) if parts else None
            period = _to_int(parts[1]) if len(parts) > 1 else None
            if quota is not None and period:
                cores = quota / period
                if cpu_limit is None or cores < cpu_limit:
                    cpu_limit = cores

        limit = _to_int(_read(level / "memory.max"))
        if limit is not None:
            current = _to_int(_read(level / "memory.current")) or 0
            inactive = _stat_value(level / "memory.stat", "inactive_file")
            working_set = max(current - inactive, 0)
            headroom = limit - working_set
            if best_headroom is None or headroom < best_headroom:
                best_headroom, mem_limit, mem_ws = headroom, limit, working_set

    return CgroupInfo(2, cpu_limit, mem_limit, mem_ws)


def _read_v1(root: Path, proc_cgroup: Path) -> CgroupInfo:
    own = _own_cgroup_paths(proc_cgroup)

    cpu_limit: Optional[float] = None
    for controller_dir in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
        base = root / controller_dir
        if not base.is_dir():
            continue
        rel = own.get("cpu", "")
        for level in _levels(base, rel):
            quota = _to_int(_read(level / "cpu.cfs_quota_us"))
            period = _to_int(_read(level / "cpu.cfs_period_us"))
            if quota is not None and quota > 0 and period:
                cores = quota / period
                if cpu_limit is None or cores < cpu_limit:
                    cpu_limit = cores
        break

    mem_limit: Optional[int] = None
    mem_ws: Optional[int] = None
    best_headroom: Optional[int] = None
    base = root / "memory"
    if base.is_dir():
        for level in _levels(base, own.get("memory", "")):
            limit = _to_int(_read(level / "memory.limit_in_bytes"))
            if limit is None or limit >= _V1_UNLIMITED_BYTES:
                continue
            usage = _to_int(_read(level / "memory.usage_in_bytes")) or 0
            inactive = _stat_value(
                level / "memory.stat", "total_inactive_file", "inactive_file"
            )
            working_set = max(usage - inactive, 0)
            headroom = limit - working_set
            if best_headroom is None or headroom < best_headroom:
                best_headroom, mem_limit, mem_ws = headroom, limit, working_set

    return CgroupInfo(1, cpu_limit, mem_limit, mem_ws)


def read_cgroup(
    root: Path = DEFAULT_CGROUP_ROOT, proc_cgroup: Path = DEFAULT_PROC_CGROUP
) -> CgroupInfo:
    """
    Read this container's cgroup limits. Never raises: anything unreadable is
    reported as "no limit", and the caller falls back to host resources.
    """
    try:
        if (root / "cgroup.controllers").exists():
            return _read_v2(root, proc_cgroup)
        if (root / "memory").is_dir() or (root / "cpu").is_dir():
            return _read_v1(root, proc_cgroup)
    except Exception as e:  # defensive: never let detection crash an injection
        logger.warning("Could not read cgroup limits", extra={"error": str(e)})
    return CgroupInfo()


# ---------------------------------------------------------------------------
# Host facts
# ---------------------------------------------------------------------------


def host_cpus() -> int:
    """CPUs this process may run on (honors cpusets, unlike os.cpu_count)."""
    try:
        return max(len(os.sched_getaffinity(0)), 1)
    except (AttributeError, OSError):
        return max(os.cpu_count() or 1, 1)


def host_available_bytes() -> int:
    return int(psutil.virtual_memory().available)


# ---------------------------------------------------------------------------
# Budget math (pure, so it is easy to test)
# ---------------------------------------------------------------------------


def compute_budget(
    cgroup: CgroupInfo,
    cpus: int,
    host_available: int,
    settings: SafetySettings,
) -> Budget:
    # CPU: the tighter of the cgroup quota and the CPUs we may schedule on.
    if cgroup.cpu_limit_cores is not None and cgroup.cpu_limit_cores < cpus:
        capacity, cpu_by = cgroup.cpu_limit_cores, "cgroup"
    else:
        capacity, cpu_by = float(cpus), "host"
    # Always allow at least one worker; never exceed the absolute ceiling.
    max_cores = min(
        limits.MAX_CORES, max(1, math.floor(capacity * settings.cpu_fraction))
    )

    # Memory: headroom is what is left under the cgroup limit, further capped
    # by what the host reports as available.
    cgroup_headroom: Optional[int] = None
    if cgroup.memory_limit_bytes is not None:
        cgroup_headroom = cgroup.memory_limit_bytes - (
            cgroup.memory_working_set_bytes or 0
        )
    if cgroup_headroom is not None and cgroup_headroom <= host_available:
        headroom, mem_by = cgroup_headroom, "cgroup"
    else:
        headroom, mem_by = host_available, "host"
    headroom = max(headroom, 0)
    max_mb = min(
        limits.MAX_MEMORY_MB,
        int(headroom * settings.memory_fraction) // MIB,
    )

    return Budget(
        max_cores=max_cores,
        max_memory_mb=max(max_mb, 0),
        cpu_capacity=round(capacity, 3),
        memory_headroom_mb=headroom // MIB,
        cpu_limited_by=cpu_by,
        memory_limited_by=mem_by,
        cgroup=cgroup,
    )


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------


class ResourceGovernor:
    """
    Grants CPU cores and memory to injections within the live budget, and
    tracks what running injections have reserved.
    """

    def __init__(
        self,
        settings: Optional[SafetySettings] = None,
        cgroup_reader: Optional[Callable[[], CgroupInfo]] = None,
        cpu_reader: Optional[Callable[[], int]] = None,
        memory_reader: Optional[Callable[[], int]] = None,
    ):
        self._lock = threading.Lock()
        self._settings = settings or SafetySettings()
        self._cgroup_reader = cgroup_reader or read_cgroup
        self._cpu_reader = cpu_reader or host_cpus
        self._memory_reader = memory_reader or host_available_bytes
        self._reserved_cores = 0
        self._reserved_mb = 0

    # -- configuration -----------------------------------------------------

    @property
    def settings(self) -> SafetySettings:
        return self._settings

    def configure(self, settings: SafetySettings) -> None:
        with self._lock:
            self._settings = settings

    def reset(self) -> None:
        """Drop reservations and restore default settings (used by tests)."""
        with self._lock:
            self._settings = SafetySettings()
            self._reserved_cores = 0
            self._reserved_mb = 0

    # -- budget ------------------------------------------------------------

    def _budget_unlocked(self) -> Budget:
        return compute_budget(
            self._cgroup_reader(),
            self._cpu_reader(),
            self._memory_reader(),
            self._settings,
        )

    def budget(self) -> Budget:
        with self._lock:
            return self._budget_unlocked()

    # -- CPU ---------------------------------------------------------------

    def _decide_cores(self, requested: int, reserve: bool) -> Grant:
        with self._lock:
            budget = self._budget_unlocked()
            if requested <= 0:
                return Grant(True, requested, requested)

            if (
                self._settings.require_cgroup_limits
                and budget.cgroup.cpu_limit_cores is None
            ):
                return Grant(
                    False,
                    requested,
                    0,
                    reason=(
                        "no CPU limit is set on this container and "
                        "safety.require_cgroup_limits is true"
                    ),
                )

            available = budget.max_cores - self._reserved_cores
            if available < 1:
                return Grant(
                    False,
                    requested,
                    0,
                    reason=(
                        f"CPU budget exhausted: {self._reserved_cores} of "
                        f"{budget.max_cores} allowed cores are already in use "
                        f"by running injections"
                    ),
                )

            granted = min(requested, available)
            if reserve:
                self._reserved_cores += granted
            return Grant(
                True,
                requested,
                granted,
                clamped=granted < requested,
                reason=(
                    f"clamped from {requested} to {granted} cores "
                    f"(budget {budget.max_cores}, limited by {budget.cpu_limited_by}, "
                    f"capacity {budget.cpu_capacity} CPUs)"
                    if granted < requested
                    else None
                ),
            )

    def preview_cores(self, requested: int) -> Grant:
        """What would be granted, without reserving anything."""
        return self._decide_cores(requested, reserve=False)

    def acquire_cores(self, requested: int) -> Grant:
        """Grant (and reserve) cores. Callers must release_cores(granted)."""
        return self._decide_cores(requested, reserve=True)

    def release_cores(self, cores: int) -> None:
        with self._lock:
            self._reserved_cores = max(self._reserved_cores - cores, 0)

    # -- Memory ------------------------------------------------------------

    def _decide_memory(self, requested_mb: int, reserve: bool) -> Grant:
        with self._lock:
            budget = self._budget_unlocked()
            if requested_mb <= 0:
                return Grant(True, requested_mb, requested_mb)

            if (
                self._settings.require_cgroup_limits
                and budget.cgroup.memory_limit_bytes is None
            ):
                return Grant(
                    False,
                    requested_mb,
                    0,
                    reason=(
                        "no memory limit is set on this container and "
                        "safety.require_cgroup_limits is true"
                    ),
                )

            # Reservations from running injections may not be visible in the
            # measured usage yet (pages are touched gradually), so they are
            # subtracted explicitly. Once they are visible this double-counts,
            # which errs on the side of refusing.
            allowed = budget.max_memory_mb - self._reserved_mb
            if requested_mb > allowed:
                return Grant(
                    False,
                    requested_mb,
                    0,
                    reason=(
                        f"{requested_mb} MB requested but only {max(allowed, 0)} MB "
                        f"fits the safety budget ({self._settings.memory_fraction:.0%} "
                        f"of {budget.memory_headroom_mb} MB headroom, limited by "
                        f"{budget.memory_limited_by}; {self._reserved_mb} MB already "
                        f"reserved by running injections)"
                    ),
                )

            if reserve:
                self._reserved_mb += requested_mb
            return Grant(True, requested_mb, requested_mb)

    def preview_memory(self, requested_mb: int) -> Grant:
        return self._decide_memory(requested_mb, reserve=False)

    def acquire_memory(self, requested_mb: int) -> Grant:
        """Grant (and reserve) memory. Callers must release_memory(mb)."""
        return self._decide_memory(requested_mb, reserve=True)

    def release_memory(self, mb: int) -> None:
        with self._lock:
            self._reserved_mb = max(self._reserved_mb - mb, 0)

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Current environment, budget, and reservations, for the API and logs."""
        with self._lock:
            budget = self._budget_unlocked()
            reserved_cores, reserved_mb = self._reserved_cores, self._reserved_mb
            settings = asdict(self._settings)

        cg = budget.cgroup
        warnings: List[str] = []
        if cg.cpu_limit_cores is None:
            warnings.append(
                "No CPU limit detected for this container; CPU injections are "
                "bounded by host CPU count only."
            )
        if cg.memory_limit_bytes is None:
            warnings.append(
                "No memory limit detected for this container; memory injections "
                "are bounded by host available memory only."
            )
        return {
            "safety": settings,
            "cgroup": {
                "version": cg.version,
                "cpu_limit_cores": cg.cpu_limit_cores,
                "memory_limit_mb": (
                    None
                    if cg.memory_limit_bytes is None
                    else cg.memory_limit_bytes // MIB
                ),
                "memory_working_set_mb": (
                    None
                    if cg.memory_working_set_bytes is None
                    else cg.memory_working_set_bytes // MIB
                ),
            },
            "budget": {
                "max_cores": budget.max_cores,
                "max_memory_mb": budget.max_memory_mb,
                "cpu_capacity": budget.cpu_capacity,
                "memory_headroom_mb": budget.memory_headroom_mb,
                "cpu_limited_by": budget.cpu_limited_by,
                "memory_limited_by": budget.memory_limited_by,
            },
            "reserved": {"cores": reserved_cores, "memory_mb": reserved_mb},
            "warnings": warnings,
        }


# Process-wide instance used by the injectors.
governor = ResourceGovernor()


def apply_settings_and_report(settings: SafetySettings) -> None:
    """
    Apply safety settings and log the resulting budget. Call at startup and
    after every config reload so operators can see what the agent will allow
    and whether it is running without container limits.
    """
    governor.configure(settings)
    snap = governor.snapshot()
    logger.info(
        "Resource budget",
        extra={
            "budget": snap["budget"],
            "cgroup": snap["cgroup"],
            "safety": snap["safety"],
        },
    )
    for warning in snap["warnings"]:
        logger.warning(warning)
