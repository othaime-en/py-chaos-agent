"""Delaying only some destinations: validation, tc commands, cleanup, real tc."""

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import limits
from src.failures import network
from src.lifecycle import lifecycle
from src.metrics import INJECTIONS_TOTAL, metric_value
from src.schemas import ConfigValidationError, validate_failure_config


def cfg(*destinations, **extra):
    return {
        "enabled": True,
        "probability": 0.1,
        "duration_seconds": 5,
        "destinations": list(destinations),
        **extra,
    }


def validate(*destinations):
    return validate_failure_config("network", cfg(*destinations))


def rejected(destination):
    with pytest.raises(ConfigValidationError) as exc:
        validate(destination)
    return str(exc.value)


class TestValidation:
    def test_none_means_the_whole_interface(self):
        assert validate()["destinations"] == []
        data = validate_failure_config("network", {"duration_seconds": 5})
        assert data["destinations"] == []

    @pytest.mark.parametrize(
        "given, cidr",
        [
            ("10.0.4.7", "10.0.4.7/32"),
            ("10.0.4.0/24", "10.0.4.0/24"),
            ("10.0.0.0/16", "10.0.0.0/16"),
            ("192.168.1.10/32", "192.168.1.10/32"),
            ("2001:db8::/48", "2001:db8::/48"),
            ("2001:db8::1", "2001:db8::1/128"),
        ],
    )
    def test_addresses_and_networks(self, given, cidr):
        assert validate({"cidr": given})["destinations"][0]["cidr"] == cidr

    def test_a_port_defaults_to_tcp(self):
        d = validate({"cidr": "10.0.4.7", "port": 5432})["destinations"][0]
        assert d == {"cidr": "10.0.4.7/32", "port": 5432, "protocol": "tcp"}

    def test_udp_can_be_asked_for(self):
        d = validate({"cidr": "10.0.4.7", "port": 53, "protocol": "udp"})[
            "destinations"
        ][0]
        assert d["protocol"] == "udp"

    def test_a_protocol_without_a_port(self):
        d = validate({"cidr": "10.0.4.7", "protocol": "udp"})["destinations"][0]
        assert d["port"] is None and d["protocol"] == "udp"

    def test_no_port_and_no_protocol_means_everything_to_that_host(self):
        d = validate({"cidr": "10.0.4.7"})["destinations"][0]
        assert d["port"] is None and d["protocol"] is None

    @pytest.mark.parametrize(
        "bad",
        [
            "127.0.0.1",
            "127.0.0.0/24",
            "169.254.169.254",
            "169.254.0.0/20",
            "224.0.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "ff02::1",
            "::",
        ],
    )
    def test_unsafe_addresses_are_refused(self, bad):
        assert "loopback, link-local, multicast" in rejected({"cidr": bad})

    @pytest.mark.parametrize(
        "bad", ["0.0.0.0/0", "10.0.0.0/8", "10.0.0.0/15", "2001:db8::/32", "::/0"]
    )
    def test_networks_that_are_too_wide_are_refused(self, bad):
        message = rejected({"cidr": bad})
        assert "too wide" in message or "loopback" in message

    def test_the_prefix_limits_are_inclusive(self):
        validate({"cidr": f"10.0.0.0/{limits.MIN_DESTINATION_PREFIX_V4}"})
        validate({"cidr": f"2001:db8::/{limits.MIN_DESTINATION_PREFIX_V6}"})

    @pytest.mark.parametrize(
        "bad",
        [
            "db.internal",
            "",
            "10.0.4",
            "10.0.4.7/24",
            "10.0.4.7/33",
            "1.2.3.4; rm -rf /",
            "10.0.4.0/24,10.0.5.0/24",
        ],
    )
    def test_things_that_are_not_networks(self, bad):
        assert "cidr must be an IP address or a network" in rejected({"cidr": bad})

    @pytest.mark.parametrize("port", [0, 65536, -1, "5432", True])
    def test_bad_ports(self, port):
        with pytest.raises(ConfigValidationError):
            validate({"cidr": "10.0.4.7", "port": port})

    @pytest.mark.parametrize("protocol", ["icmp", "TCP", "any", 6])
    def test_bad_protocols(self, protocol):
        with pytest.raises(ConfigValidationError):
            validate({"cidr": "10.0.4.7", "protocol": protocol})

    def test_unknown_keys(self):
        with pytest.raises(ConfigValidationError):
            validate({"cidr": "10.0.4.7", "host": "db"})

    def test_duplicates_are_refused(self):
        with pytest.raises(ConfigValidationError, match="duplicate destination"):
            validate(
                {"cidr": "10.0.4.7", "port": 5432},
                {"cidr": "10.0.4.7/32", "port": 5432},
            )

    def test_the_same_host_on_different_ports_is_fine(self):
        validate({"cidr": "10.0.4.7", "port": 5432}, {"cidr": "10.0.4.7", "port": 6379})

    def test_the_number_of_destinations_is_capped(self):
        many = [
            {"cidr": f"10.0.4.{i}"} for i in range(limits.MAX_NETWORK_DESTINATIONS + 1)
        ]
        with pytest.raises(ConfigValidationError):
            validate(*many)


class TestCommands:
    def test_no_destinations_is_the_original_single_command(self):
        assert network.build_delay_commands("eth0", 100) == [
            ["tc", "qdisc", "add", "dev", "eth0", "root", "netem", "delay", "100ms"]
        ]

    def test_prio_netem_and_one_filter_per_destination(self):
        commands = network.build_delay_commands(
            "eth0", 250, [{"cidr": "10.0.4.7/32", "port": 5432, "protocol": "tcp"}]
        )
        assert [" ".join(c) for c in commands] == [
            "tc qdisc add dev eth0 root handle 1: prio bands 3 priomap "
            + " ".join(["1"] * 16),
            "tc qdisc add dev eth0 parent 1:3 handle 30: netem delay 250ms",
            "tc filter add dev eth0 protocol ip parent 1:0 prio 1 u32 match ip dst "
            "10.0.4.7/32 match ip protocol 6 0xff match ip dport 5432 0xffff flowid 1:3",
        ]

    def test_ordinary_traffic_is_mapped_to_the_untouched_band(self):
        prio = network.build_delay_commands("eth0", 100, [{"cidr": "10.0.4.7/32"}])[0]
        assert (
            prio[prio.index("priomap") + 1 :] == ["1"] * 16
        ), "traffic must default to band 2, not the delayed band 3"

    def test_udp_and_a_whole_host(self):
        commands = network.build_delay_commands(
            "eth0",
            100,
            [
                {"cidr": "10.0.4.7/32", "port": 53, "protocol": "udp"},
                {"cidr": "10.0.5.0/24"},
            ],
        )
        assert "match ip protocol 17 0xff match ip dport 53 0xffff" in " ".join(
            commands[2]
        )
        assert " ".join(commands[3]).endswith("match ip dst 10.0.5.0/24 flowid 1:3")

    def test_ipv6_uses_the_ip6_matches(self):
        commands = network.build_delay_commands(
            "eth0", 100, [{"cidr": "2001:db8::/64", "port": 443, "protocol": "tcp"}]
        )
        line = " ".join(commands[2])
        assert "protocol ipv6" in line and "match ip6 dst 2001:db8::/64" in line
        assert "match ip6 protocol 6 0xff match ip6 dport 443 0xffff" in line

    def test_each_filter_gets_its_own_priority(self):
        commands = network.build_delay_commands(
            "eth0",
            100,
            [{"cidr": "10.0.4.7"}, {"cidr": "2001:db8::1"}, {"cidr": "10.0.5.7"}],
        )
        priorities = [c[c.index("prio") + 1] for c in commands[2:]]
        assert priorities == ["1", "2", "3"]

    def test_arguments_are_separate_list_items_never_a_shell_string(self):
        for command in network.build_delay_commands(
            "eth0", 100, [{"cidr": "10.0.4.7", "port": 80}]
        ):
            assert all(isinstance(a, str) and " " not in a for a in command)

    @pytest.mark.parametrize(
        "bad", ["not-a-network", "10.0.4.7/24", "1.2.3.4; reboot", ""]
    )
    def test_an_invalid_destination_raises_before_anything_runs(self, bad):
        with pytest.raises(ValueError):
            network.build_delay_commands("eth0", 100, [{"cidr": bad}])


class FakeTc:
    """A small model of one interface's qdisc tree and filters."""

    def __init__(self):
        self.calls = []
        self.qdiscs = {}  # handle-or-"root" -> description
        self.filters = []
        self.lock = threading.Lock()
        self.fail_on = None

    def __call__(self, args):
        import subprocess as sp

        with self.lock:
            self.calls.append(list(args))
            line = " ".join(args)
            if self.fail_on and self.fail_on in line:
                return sp.CompletedProcess(
                    args, 2, "", "RTNETLINK answers: Invalid argument"
                )
            kind, verb = args[1], args[2]
            if kind == "qdisc" and verb == "show":
                return sp.CompletedProcess(args, 0, self.show(), "")
            if kind == "qdisc" and verb == "add":
                if "root" in args:
                    if "root" in self.qdiscs:
                        return sp.CompletedProcess(args, 2, "", "File exists")
                    handle = (
                        args[args.index("handle") + 1] if "handle" in args else "8001:"
                    )
                    name = args[args.index("root") + (3 if "handle" in args else 1)]
                    self.qdiscs["root"] = f"qdisc {name} {handle} root refcnt 2"
                else:
                    parent = args[args.index("parent") + 1]
                    if "root" not in self.qdiscs:
                        return sp.CompletedProcess(
                            args, 2, "", "Parent Qdisc doesn't exists"
                        )
                    handle = args[args.index("handle") + 1]
                    self.qdiscs[handle] = (
                        f"qdisc netem {handle} parent {parent} limit 1000"
                    )
                return sp.CompletedProcess(args, 0, "", "")
            if kind == "filter" and verb == "add":
                if "root" not in self.qdiscs:
                    return sp.CompletedProcess(
                        args, 2, "", "Cannot find specified qdisc on specified device"
                    )
                self.filters.append(line)
                return sp.CompletedProcess(args, 0, "", "")
            if kind == "qdisc" and verb == "del":
                if "root" not in self.qdiscs:
                    return sp.CompletedProcess(
                        args, 2, "", "Cannot delete qdisc with handle of zero."
                    )
                self.qdiscs.clear()
                self.filters.clear()
                return sp.CompletedProcess(args, 0, "", "")
            return sp.CompletedProcess(args, 0, "", "")

    def show(self):
        return "\n".join(self.qdiscs.values()) or "qdisc noqueue 0: root"


@pytest.fixture
def tc(monkeypatch):
    fake = FakeTc()
    monkeypatch.setattr(network, "_run_cmd", fake)
    monkeypatch.setattr(network, "verify_interface_exists", lambda i: (True, None))
    network._applied.clear()
    yield fake
    network._applied.clear()


def count(status):
    return metric_value(INJECTIONS_TOTAL, failure_type="network", status=status)


def wait_until(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


DB = {"cidr": "10.0.4.7/32", "port": 5432, "protocol": "tcp"}
NET_CFG = {
    "interface": "eth0",
    "delay_ms": 100,
    "duration_seconds": 30,
    "destinations": [DB],
}


class TestInjection:
    def test_only_the_selected_traffic_is_set_up(self, tc):
        t = threading.Thread(target=network.inject_network, args=(NET_CFG,))
        t.start()
        wait_until(lambda: len(tc.filters) == 1)
        assert tc.qdiscs["root"].startswith("qdisc prio 1: root")
        assert "30:" in tc.qdiscs and "parent 1:3" in tc.qdiscs["30:"]
        assert "match ip dst 10.0.4.7/32" in tc.filters[0]
        lifecycle.abort_active("test")
        t.join(5)

    def test_it_is_all_removed_afterwards(self, tc):
        network.inject_network({**NET_CFG, "duration_seconds": 0.05})
        assert tc.qdiscs == {} and tc.filters == []
        assert count("success") == 1
        assert not network._applied

    def test_it_is_all_removed_after_an_abort(self, tc):
        t = threading.Thread(target=network.inject_network, args=(NET_CFG,))
        t.start()
        wait_until(lambda: len(tc.filters) == 1)
        lifecycle.abort_active("test")
        t.join(5)
        assert tc.qdiscs == {} and tc.filters == []
        assert count("aborted") == 1

    def test_a_failure_halfway_leaves_nothing_behind(self, tc):
        tc.fail_on = "filter add"
        network.inject_network(NET_CFG)
        assert count("failed") == 1
        assert tc.qdiscs == {} and tc.filters == []
        assert not network._applied

    def test_without_destinations_it_is_still_one_root_netem(self, tc):
        network.inject_network(
            {**NET_CFG, "destinations": [], "duration_seconds": 0.05}
        )
        adds = [c for c in tc.calls if c[2] == "add"]
        assert len(adds) == 1 and "netem" in adds[0] and "prio" not in adds[0]

    def test_an_invalid_destination_fails_before_touching_tc(self, tc):
        network.inject_network({**NET_CFG, "destinations": [{"cidr": "not-a-network"}]})
        assert count("failed") == 1
        assert tc.calls == [] and tc.qdiscs == {}

    def test_a_dry_run_changes_nothing(self, tc):
        network.inject_network(NET_CFG, dry_run=True)
        assert tc.calls == []
        assert count("skipped") == 1


class TestStaleRuleRepair:
    def test_our_prio_netem_layout_is_repaired(self, tc):
        tc.qdiscs.update(
            {
                "root": "qdisc prio 1: root refcnt 2 bands 3",
                "30:": "qdisc netem 30: parent 1:3 limit 1000 delay 100ms",
            }
        )
        assert network.reconcile_stale_rules(["eth0"]) == ["eth0"]
        assert tc.qdiscs == {}

    def test_someone_elses_prio_qdisc_is_left_alone(self, tc):
        tc.qdiscs["root"] = "qdisc prio 1: root refcnt 2 bands 3"
        assert network.reconcile_stale_rules(["eth0"]) == []
        assert "root" in tc.qdiscs

    def test_a_prio_with_another_handle_is_left_alone(self, tc):
        tc.qdiscs.update(
            {
                "root": "qdisc prio 5: root refcnt 2 bands 3",
                "30:": "qdisc netem 30: parent 5:3 limit 1000",
            }
        )
        assert network.reconcile_stale_rules(["eth0"]) == []

    def test_a_plain_root_netem_is_still_repaired(self, tc):
        tc.qdiscs["root"] = "qdisc netem 8001: root refcnt 2 limit 1000 delay 100ms"
        assert network.reconcile_stale_rules(["eth0"]) == ["eth0"]


# ---------------------------------------------------------------------------
# Against real tc and real traffic (skipped where that is not possible)
# ---------------------------------------------------------------------------


def _real_tc_available():
    if not (shutil.which("tc") and shutil.which("ip") and shutil.which("unshare")):
        return False
    probe = subprocess.run(
        [
            "unshare",
            "-n",
            "sh",
            "-c",
            "ip link set lo up && tc qdisc add dev lo root handle 1: htb default 2",
        ],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(
    not _real_tc_available(), reason="needs tc and network namespaces (root)"
)
def test_the_filters_classify_exactly_the_selected_destinations():
    script = Path(__file__).with_name("tc_classification_check.py")
    result = subprocess.run(
        ["unshare", "-n", sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all classified correctly" in result.stdout


class TestThroughTheApi:
    """A manual injection override and PATCH /config go through the same rules."""

    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        from src.api import agent_state, app
        from src.config import Config
        from src.schemas import validate_config_dict

        monkeypatch.setenv("CHAOS_API_TOKEN", "t" * 40)
        agent_state.config = Config(
            validate_config_dict(
                {
                    "agent": {},
                    "failures": {
                        "network": {"duration_seconds": 5, "interface": "eth0"}
                    },
                }
            )
        )
        return TestClient(app, headers={"Authorization": "Bearer " + "t" * 40})

    def test_a_valid_override_is_accepted(self, client):
        response = client.post(
            "/inject/manual",
            json={
                "failure_type": "network",
                "dry_run": True,
                "config": {"destinations": [{"cidr": "10.0.4.7", "port": 5432}]},
            },
        )
        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        "cidr", ["127.0.0.1", "0.0.0.0/0", "169.254.169.254", "db.internal"]
    )
    def test_a_dangerous_override_is_refused(self, client, cidr):
        response = client.post(
            "/inject/manual",
            json={
                "failure_type": "network",
                "dry_run": True,
                "config": {"destinations": [{"cidr": cidr}]},
            },
        )
        assert response.status_code == 422

    def test_config_patch_validates_destinations_too(self, client):
        bad = client.patch(
            "/config/failures/network", json={"destinations": [{"cidr": "10.0.0.0/8"}]}
        )
        assert bad.status_code == 422
