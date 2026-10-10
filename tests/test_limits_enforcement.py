"""
The injectors enforce hard upper limits themselves, so even a direct caller
that bypasses config and API validation cannot spawn unbounded load.
"""

import pytest

from src import limits
from src.failures import cpu, memory, network
from src.metrics import INJECTIONS_TOTAL


def failed(kind):
    return INJECTIONS_TOTAL.labels(failure_type=kind, status="failed")._value.get()


class TestCpuLimits:
    @pytest.mark.parametrize(
        "config",
        [
            {"cores": limits.MAX_CORES + 1, "duration_seconds": 1},
            {"cores": 10**6, "duration_seconds": 1},
            {"cores": 1, "duration_seconds": limits.MAX_DURATION_SECONDS + 1},
            {"cores": True, "duration_seconds": 1},
            {"cores": "4", "duration_seconds": 1},
        ],
    )
    def test_rejected_without_spawning_workers(self, config, monkeypatch):
        monkeypatch.setattr(
            cpu, "_cpu_hog", lambda *a: pytest.fail("workers must not start")
        )
        cpu.inject_cpu(config)
        assert failed("cpu") == 1

    def test_rejected_even_in_dry_run(self):
        cpu.inject_cpu({"cores": 10**6, "duration_seconds": 1}, dry_run=True)
        assert failed("cpu") == 1

    def test_values_at_the_limit_are_allowed(self, monkeypatch, big_machine):
        calls = []
        monkeypatch.setattr(cpu, "_cpu_hog", lambda c, d: calls.append((c, d)))
        cpu.inject_cpu(
            {
                "cores": limits.MAX_CORES,
                "duration_seconds": limits.MAX_DURATION_SECONDS,
            }
        )
        assert calls == [(limits.MAX_CORES, limits.MAX_DURATION_SECONDS)]


class TestMemoryLimits:
    @pytest.mark.parametrize(
        "config",
        [
            {"mb": limits.MAX_MEMORY_MB + 1, "duration_seconds": 1},
            {"mb": 10**9, "duration_seconds": 1},
            {"mb": 10, "duration_seconds": limits.MAX_DURATION_SECONDS + 1},
            {"mb": None, "duration_seconds": 1},
        ],
    )
    def test_rejected_without_allocating(self, config, monkeypatch):
        started = []
        monkeypatch.setattr(
            memory.threading.Thread,
            "start",
            lambda self: started.append(1),
        )
        memory.inject_memory(config)
        assert started == []
        assert failed("memory") == 1


class TestNetworkLimits:
    def test_duration_over_limit_rejected_before_any_tc_call(self, monkeypatch):
        monkeypatch.setattr(
            network.subprocess,
            "run",
            lambda *a, **k: pytest.fail("tc must not run"),
        )
        network.inject_network(
            {
                "interface": "eth0",
                "delay_ms": 100,
                "duration_seconds": limits.MAX_DURATION_SECONDS + 1,
            }
        )
        assert failed("network") == 1
