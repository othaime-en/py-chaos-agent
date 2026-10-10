"""Tests for cgroup detection, budget math, and the resource governor."""

import threading
from pathlib import Path

import pytest

from src import limits
from src.resources import (
    MIB,
    CgroupInfo,
    ResourceGovernor,
    SafetySettings,
    compute_budget,
    read_cgroup,
)

GIB = 1024 * MIB


# ---------------------------------------------------------------------------
# Fake cgroup trees
# ---------------------------------------------------------------------------


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def v2_level(level: Path, cpu_max=None, mem_max=None, mem_cur=None, inactive=None):
    if cpu_max is not None:
        write(level / "cpu.max", cpu_max)
    if mem_max is not None:
        write(level / "memory.max", mem_max)
    if mem_cur is not None:
        write(level / "memory.current", str(mem_cur))
    if inactive is not None:
        write(level / "memory.stat", f"anon 1\ninactive_file {inactive}\nfile 5\n")


@pytest.fixture
def v2_root(tmp_path):
    root = tmp_path / "cg"
    write(root / "cgroup.controllers", "cpu memory")
    return root


@pytest.fixture
def proc(tmp_path):
    def _make(text: str) -> Path:
        path = tmp_path / "proc_cgroup"
        path.write_text(text)
        return path

    return _make


# ---------------------------------------------------------------------------
# cgroup v2
# ---------------------------------------------------------------------------


class TestCgroupV2:
    def test_unlimited(self, v2_root, proc):
        v2_level(v2_root, cpu_max="max 100000", mem_max="max", mem_cur=5 * MIB)
        info = read_cgroup(v2_root, proc("0::/\n"))
        assert info.version == 2
        assert info.cpu_limit_cores is None
        assert info.memory_limit_bytes is None

    @pytest.mark.parametrize(
        "cpu_max,expected",
        [("100000 100000", 1.0), ("150000 100000", 1.5), ("50000 100000", 0.5)],
    )
    def test_cpu_quota(self, v2_root, proc, cpu_max, expected):
        v2_level(v2_root, cpu_max=cpu_max)
        assert read_cgroup(v2_root, proc("0::/\n")).cpu_limit_cores == expected

    def test_memory_working_set_excludes_reclaimable_cache(self, v2_root, proc):
        v2_level(
            v2_root,
            mem_max=str(512 * MIB),
            mem_cur=100 * MIB,
            inactive=20 * MIB,
        )
        info = read_cgroup(v2_root, proc("0::/\n"))
        assert info.memory_limit_bytes == 512 * MIB
        assert info.memory_working_set_bytes == 80 * MIB

    def test_working_set_never_negative(self, v2_root, proc):
        v2_level(v2_root, mem_max=str(GIB), mem_cur=10 * MIB, inactive=50 * MIB)
        assert read_cgroup(v2_root, proc("0::/\n")).memory_working_set_bytes == 0

    def test_tighter_pod_level_limit_wins(self, v2_root, proc):
        """A parent cgroup (the pod) can be tighter than the container."""
        pod = v2_root / "kubepods" / "pod1"
        container = pod / "ctr"
        v2_level(container, cpu_max="200000 100000", mem_max=str(GIB), mem_cur=MIB)
        v2_level(pod, cpu_max="100000 100000", mem_max=str(256 * MIB), mem_cur=MIB)
        info = read_cgroup(v2_root, proc("0::/kubepods/pod1/ctr\n"))
        assert info.cpu_limit_cores == 1.0
        assert info.memory_limit_bytes == 256 * MIB

    def test_level_with_least_headroom_wins_not_smallest_limit(self, v2_root, proc):
        """Limit 400 with 390 used leaves less room than limit 300 with 0 used."""
        parent = v2_root / "a"
        child = parent / "b"
        v2_level(child, mem_max=str(400 * MIB), mem_cur=390 * MIB)
        v2_level(parent, mem_max=str(300 * MIB), mem_cur=0)
        info = read_cgroup(v2_root, proc("0::/a/b\n"))
        assert info.memory_limit_bytes == 400 * MIB

    def test_cgroup_namespace_uses_root_only(self, v2_root, proc):
        """In a container with its own cgroup namespace the relative path from
        /proc/self/cgroup does not exist under the mount; use the root."""
        v2_level(v2_root, cpu_max="100000 100000", mem_max=str(128 * MIB))
        info = read_cgroup(v2_root, proc("0::/kubepods/pod9/ctr\n"))
        assert info.cpu_limit_cores == 1.0
        assert info.memory_limit_bytes == 128 * MIB

    @pytest.mark.parametrize("junk", ["", "garbage", "abc def", "max", "-"])
    def test_garbage_cpu_max_is_ignored(self, v2_root, proc, junk):
        v2_level(v2_root, cpu_max=junk)
        assert read_cgroup(v2_root, proc("0::/\n")).cpu_limit_cores is None

    def test_garbage_memory_max_is_ignored(self, v2_root, proc):
        v2_level(v2_root, mem_max="lots")
        assert read_cgroup(v2_root, proc("0::/\n")).memory_limit_bytes is None

    def test_missing_proc_file_is_fine(self, v2_root, tmp_path):
        v2_level(v2_root, mem_max=str(64 * MIB))
        info = read_cgroup(v2_root, tmp_path / "nope")
        assert info.memory_limit_bytes == 64 * MIB


# ---------------------------------------------------------------------------
# cgroup v1
# ---------------------------------------------------------------------------


@pytest.fixture
def v1_root(tmp_path):
    root = tmp_path / "cg1"
    (root / "memory").mkdir(parents=True)
    (root / "cpu").mkdir(parents=True)
    return root


class TestCgroupV1:
    def test_unlimited_sentinels(self, v1_root, proc):
        write(v1_root / "memory" / "memory.limit_in_bytes", "9223372036854771712")
        write(v1_root / "memory" / "memory.usage_in_bytes", str(10 * MIB))
        write(v1_root / "cpu" / "cpu.cfs_quota_us", "-1")
        write(v1_root / "cpu" / "cpu.cfs_period_us", "100000")
        info = read_cgroup(v1_root, proc("4:memory:/\n1:cpu:/\n"))
        assert info.version == 1
        assert info.cpu_limit_cores is None
        assert info.memory_limit_bytes is None

    def test_limits_and_working_set(self, v1_root, proc):
        write(v1_root / "memory" / "memory.limit_in_bytes", str(512 * MIB))
        write(v1_root / "memory" / "memory.usage_in_bytes", str(200 * MIB))
        write(
            v1_root / "memory" / "memory.stat",
            f"cache 1\ntotal_inactive_file {50 * MIB}\n",
        )
        write(v1_root / "cpu" / "cpu.cfs_quota_us", "50000")
        write(v1_root / "cpu" / "cpu.cfs_period_us", "100000")
        info = read_cgroup(v1_root, proc("4:memory:/\n1:cpu:/\n"))
        assert info.cpu_limit_cores == 0.5
        assert info.memory_limit_bytes == 512 * MIB
        assert info.memory_working_set_bytes == 150 * MIB

    def test_combined_cpu_cpuacct_directory(self, tmp_path, proc):
        root = tmp_path / "cg1b"
        (root / "memory").mkdir(parents=True)
        write(root / "cpu,cpuacct" / "cpu.cfs_quota_us", "200000")
        write(root / "cpu,cpuacct" / "cpu.cfs_period_us", "100000")
        assert read_cgroup(root, proc("1:cpu,cpuacct:/\n")).cpu_limit_cores == 2.0

    def test_nested_v1_parent_limit(self, v1_root, proc):
        child = v1_root / "memory" / "pod" / "ctr"
        write(child / "memory.limit_in_bytes", str(GIB))
        write(child / "memory.usage_in_bytes", "0")
        write(v1_root / "memory" / "pod" / "memory.limit_in_bytes", str(128 * MIB))
        write(v1_root / "memory" / "pod" / "memory.usage_in_bytes", "0")
        info = read_cgroup(v1_root, proc("4:memory:/pod/ctr\n"))
        assert info.memory_limit_bytes == 128 * MIB


class TestNoCgroup:
    def test_nothing_found_means_no_limits(self, tmp_path, proc):
        info = read_cgroup(tmp_path / "empty", proc("0::/\n"))
        assert info == CgroupInfo()

    def test_never_raises_on_odd_inputs(self, tmp_path):
        a_file = tmp_path / "file"
        a_file.write_text("x")
        assert read_cgroup(a_file, a_file) == CgroupInfo()


# ---------------------------------------------------------------------------
# Budget math
# ---------------------------------------------------------------------------

S = SafetySettings()  # cpu 0.8, memory 0.5


class TestComputeBudget:
    @pytest.mark.parametrize(
        "limit,expected_cores",
        [(0.5, 1), (1.0, 1), (1.5, 1), (2.0, 1), (2.5, 2), (4.0, 3), (10.0, 8)],
    )
    def test_cpu_from_cgroup_limit(self, limit, expected_cores):
        b = compute_budget(CgroupInfo(2, limit), 32, 8 * GIB, S)
        assert b.max_cores == expected_cores
        assert b.cpu_limited_by == "cgroup"

    def test_cpu_falls_back_to_host(self):
        b = compute_budget(CgroupInfo(), 8, 8 * GIB, S)
        assert b.max_cores == 6
        assert b.cpu_limited_by == "host"

    def test_cgroup_limit_above_host_cpus_uses_host(self):
        b = compute_budget(CgroupInfo(2, 16.0), 2, 8 * GIB, S)
        assert b.cpu_limited_by == "host"
        assert b.max_cores == 1

    def test_at_least_one_core_always(self):
        assert compute_budget(CgroupInfo(2, 0.1), 1, GIB, S).max_cores == 1

    def test_hard_ceiling_applies(self):
        b = compute_budget(CgroupInfo(), 512, 8 * GIB, S)
        assert b.max_cores == limits.MAX_CORES

    def test_memory_from_cgroup_headroom(self):
        info = CgroupInfo(2, None, 512 * MIB, 112 * MIB)  # 400 MiB headroom
        b = compute_budget(info, 4, 16 * GIB, S)
        assert b.memory_headroom_mb == 400
        assert b.max_memory_mb == 200
        assert b.memory_limited_by == "cgroup"

    def test_host_available_wins_when_tighter(self):
        info = CgroupInfo(2, None, 4 * GIB, 0)
        b = compute_budget(info, 4, 200 * MIB, S)
        assert b.memory_limited_by == "host"
        assert b.max_memory_mb == 100

    def test_no_cgroup_uses_host_available(self):
        b = compute_budget(CgroupInfo(), 4, 1000 * MIB, S)
        assert b.max_memory_mb == 500
        assert b.memory_limited_by == "host"

    def test_over_limit_usage_gives_zero_budget(self):
        info = CgroupInfo(2, None, 100 * MIB, 150 * MIB)
        assert compute_budget(info, 4, 8 * GIB, S).max_memory_mb == 0

    def test_memory_hard_ceiling_applies(self):
        b = compute_budget(CgroupInfo(), 4, 512 * GIB, S)
        assert b.max_memory_mb == limits.MAX_MEMORY_MB

    def test_fractions_scale_the_budget(self):
        info = CgroupInfo(2, 4.0, 1000 * MIB, 0)
        tight = compute_budget(info, 8, 8 * GIB, SafetySettings(0.25, 0.1))
        loose = compute_budget(info, 8, 8 * GIB, SafetySettings(0.9, 0.8))
        assert tight.max_cores == 1 and loose.max_cores == 3
        assert tight.max_memory_mb == 100 and loose.max_memory_mb == 800


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------


def make_governor(
    cpu_limit=None, mem_limit_mb=None, ws_mb=0, cpus=8, avail_mb=16 * 1024, **settings
):
    info = CgroupInfo(
        2,
        cpu_limit,
        None if mem_limit_mb is None else mem_limit_mb * MIB,
        None if mem_limit_mb is None else ws_mb * MIB,
    )
    return ResourceGovernor(
        SafetySettings(**settings),
        cgroup_reader=lambda: info,
        cpu_reader=lambda: cpus,
        memory_reader=lambda: avail_mb * MIB,
    )


class TestGovernorCpu:
    def test_within_budget_is_granted_unchanged(self):
        g = make_governor(cpu_limit=4.0)  # budget 3
        grant = g.acquire_cores(2)
        assert (grant.ok, grant.granted, grant.clamped) == (True, 2, False)

    def test_over_budget_is_clamped(self):
        g = make_governor(cpu_limit=4.0)
        grant = g.acquire_cores(10)
        assert grant.ok and grant.granted == 3 and grant.clamped
        assert "clamped from 10 to 3" in grant.reason

    def test_reservations_share_one_budget(self):
        g = make_governor(cpu_limit=4.0)  # budget 3
        assert g.acquire_cores(2).granted == 2
        second = g.acquire_cores(2)
        assert second.ok and second.granted == 1 and second.clamped

    def test_exhausted_budget_is_refused(self):
        g = make_governor(cpu_limit=4.0)
        g.acquire_cores(3)
        grant = g.acquire_cores(1)
        assert not grant.ok and grant.granted == 0
        assert "exhausted" in grant.reason

    def test_release_frees_budget(self):
        g = make_governor(cpu_limit=4.0)
        g.acquire_cores(3)
        g.release_cores(3)
        assert g.acquire_cores(3).granted == 3

    def test_release_never_goes_negative(self):
        g = make_governor(cpu_limit=4.0)
        g.release_cores(99)
        assert g.snapshot()["reserved"]["cores"] == 0

    def test_preview_does_not_reserve(self):
        g = make_governor(cpu_limit=4.0)
        g.preview_cores(3)
        assert g.snapshot()["reserved"]["cores"] == 0
        assert g.acquire_cores(3).granted == 3

    def test_zero_request_is_a_noop(self):
        g = make_governor(cpu_limit=4.0)
        assert g.acquire_cores(0).ok
        assert g.snapshot()["reserved"]["cores"] == 0

    def test_require_limits_refuses_without_cpu_limit(self):
        g = make_governor(cpu_limit=None, require_cgroup_limits=True)
        grant = g.acquire_cores(1)
        assert not grant.ok and "no CPU limit" in grant.reason

    def test_require_limits_allows_with_cpu_limit(self):
        g = make_governor(cpu_limit=2.0, require_cgroup_limits=True)
        assert g.acquire_cores(1).ok

    def test_concurrent_acquires_never_exceed_budget(self):
        g = make_governor(cpu_limit=10.0, cpus=16)  # budget floor(10*0.8)=8
        results = []

        def worker():
            results.append(g.acquire_cores(1))

        threads = [threading.Thread(target=worker) for _ in range(60)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(r.granted for r in results if r.ok) == 8
        assert sum(1 for r in results if not r.ok) == 52


class TestGovernorMemory:
    def test_within_budget_is_granted(self):
        g = make_governor(mem_limit_mb=1000)  # headroom 1000, budget 500
        assert g.acquire_memory(400).ok

    def test_over_budget_is_refused_with_reason(self):
        g = make_governor(mem_limit_mb=1000)
        grant = g.acquire_memory(501)
        assert not grant.ok and grant.granted == 0
        assert "501 MB requested" in grant.reason
        assert "500 MB fits" in grant.reason
        assert "cgroup" in grant.reason

    def test_boundary_is_inclusive(self):
        g = make_governor(mem_limit_mb=1000)
        assert g.acquire_memory(500).ok

    def test_reservations_reduce_allowance(self):
        g = make_governor(mem_limit_mb=1000)
        assert g.acquire_memory(300).ok
        grant = g.acquire_memory(300)
        assert not grant.ok and "300 MB already reserved" in grant.reason

    def test_release_restores_allowance(self):
        g = make_governor(mem_limit_mb=1000)
        g.acquire_memory(300)
        g.release_memory(300)
        assert g.acquire_memory(500).ok

    def test_existing_usage_shrinks_budget(self):
        g = make_governor(mem_limit_mb=1000, ws_mb=600)  # headroom 400 -> 200
        assert not g.acquire_memory(201).ok
        assert g.acquire_memory(200).ok

    def test_preview_does_not_reserve(self):
        g = make_governor(mem_limit_mb=1000)
        assert g.preview_memory(500).ok
        assert g.snapshot()["reserved"]["memory_mb"] == 0

    def test_zero_request_is_a_noop(self):
        g = make_governor(mem_limit_mb=1000)
        assert g.acquire_memory(0).ok

    def test_require_limits_refuses_without_memory_limit(self):
        g = make_governor(mem_limit_mb=None, require_cgroup_limits=True)
        grant = g.acquire_memory(10)
        assert not grant.ok and "no memory limit" in grant.reason

    def test_host_available_bounds_when_no_cgroup_limit(self):
        g = make_governor(mem_limit_mb=None, avail_mb=200)  # budget 100
        assert g.acquire_memory(100).ok
        assert not make_governor(mem_limit_mb=None, avail_mb=200).acquire_memory(101).ok


class TestGovernorAdmin:
    def test_configure_changes_settings(self):
        g = make_governor(cpu_limit=4.0)
        assert g.preview_cores(10).granted == 3
        g.configure(SafetySettings(cpu_fraction=0.25))
        assert g.preview_cores(10).granted == 1

    def test_reset_clears_reservations_and_settings(self):
        g = make_governor(cpu_limit=4.0)
        g.configure(SafetySettings(cpu_fraction=0.25))
        g.acquire_cores(1)
        g.reset()
        assert g.settings == SafetySettings()
        assert g.snapshot()["reserved"] == {"cores": 0, "memory_mb": 0}

    def test_snapshot_shape(self):
        snap = make_governor(cpu_limit=4.0, mem_limit_mb=1000).snapshot()
        assert set(snap) == {"safety", "cgroup", "budget", "reserved", "warnings"}
        assert snap["cgroup"]["cpu_limit_cores"] == 4.0
        assert snap["cgroup"]["memory_limit_mb"] == 1000
        assert snap["budget"]["max_cores"] == 3
        assert snap["budget"]["max_memory_mb"] == 500
        assert snap["warnings"] == []

    def test_snapshot_warns_when_unlimited(self):
        warnings = make_governor().snapshot()["warnings"]
        assert len(warnings) == 2
        assert any("CPU limit" in w for w in warnings)
        assert any("memory limit" in w for w in warnings)
