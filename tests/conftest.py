"""Shared fixtures and configuration for all tests."""

import pytest
import logging
from src import auth
from src.audit import audit
from src.metrics import (
    EXPERIMENTS_TOTAL,
    INJECTIONS_CLAMPED,
    INJECTIONS_TOTAL,
    INJECTION_ACTIVE,
)
from src.lifecycle import lifecycle
from src.resources import CgroupInfo, governor


@pytest.fixture(autouse=True)
def reset_metrics():
    """
    Reset Prometheus metrics before each test.

    CRITICAL: We must explicitly set counters to zero, not just clear the dict.
    Clearing only removes the internal storage, but Prometheus counters are
    lazy-initialized and may retain state across clears in some scenarios.
    """
    # Clear the internal metrics dictionaries
    INJECTIONS_TOTAL.clear()
    INJECTION_ACTIVE.clear()
    INJECTIONS_CLAMPED.clear()
    EXPERIMENTS_TOTAL.clear()
    governor.reset()
    lifecycle.reset()
    auth.reset_throttle()
    audit.reset()
    audit.configure_file(None)

    # Explicitly initialize all label combinations to zero
    # This ensures a clean slate for every test
    failure_types = ["cpu", "memory", "process", "network"]
    statuses = ["success", "failed", "skipped"]

    for failure_type in failure_types:
        # Initialize counter labels
        for status in statuses:
            INJECTIONS_TOTAL.labels(failure_type=failure_type, status=status).inc(0)

        # Initialize gauge labels
        INJECTION_ACTIVE.labels(failure_type=failure_type).set(0)

    yield

    # Optional: Clean up after test (helps with test isolation)
    INJECTIONS_TOTAL.clear()
    INJECTION_ACTIVE.clear()
    INJECTIONS_CLAMPED.clear()
    EXPERIMENTS_TOTAL.clear()
    governor.reset()
    lifecycle.reset()
    auth.reset_throttle()
    audit.reset()
    audit.configure_file(None)


@pytest.fixture
def caplog_setup(caplog):
    """Configure logging capture for tests."""
    caplog.set_level(logging.INFO)
    return caplog


@pytest.fixture(autouse=True)
def setup_logging():
    """Setup basic logging for all tests."""
    # Configure root logger to output to console for test capture
    logging.basicConfig(
        level=logging.DEBUG, format="%(levelname)s - %(message)s", force=True
    )
    yield
    # Clean up handlers after test
    logging.getLogger().handlers.clear()


@pytest.fixture
def big_machine(monkeypatch):
    """
    Pretend the host has plenty of CPU and memory and no container limits, so
    tests about static limits are not affected by the machine running them.
    """
    monkeypatch.setattr(governor, "_cpu_reader", lambda: 64)
    monkeypatch.setattr(governor, "_memory_reader", lambda: 64 * 1024**3)
    monkeypatch.setattr(governor, "_cgroup_reader", lambda: CgroupInfo())
    return governor


@pytest.fixture
def env(tmp_path):
    """A working directory with a config and stand-ins for tc and ip, for tests
    that run the agent or API server as real processes."""
    from tests.process_helpers import Env

    e = Env(tmp_path)
    yield e
    e.cleanup()


@pytest.fixture(scope="session")
def tls_certs(tmp_path_factory):
    """
    Throwaway certificates for TLS tests: a CA, a server certificate for
    127.0.0.1 and localhost signed by it, a client certificate signed by it, and
    an unrelated certificate and key (to test mismatches). Needs the openssl CLI.
    """
    import shutil
    import subprocess
    from types import SimpleNamespace

    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    d = tmp_path_factory.mktemp("certs")

    def run(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)

    def issue(name, cn, ext):
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key",
            "-subj", f"/CN={cn}", "-out", f"{name}.csr")  # fmt: skip
        (d / f"{name}.ext").write_text(ext + "\n")
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", f"{name}.crt", "-days", "2",
            "-extfile", f"{name}.ext")  # fmt: skip

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
        "-subj", "/CN=test-ca", "-days", "2", "-out", "ca.crt")  # fmt: skip
    issue("server", "localhost", "subjectAltName=IP:127.0.0.1,DNS:localhost")
    issue("client", "test-client", "extendedKeyUsage=clientAuth")
    # Unrelated self-signed pair, for "key does not match certificate".
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "other.key",
        "-subj", "/CN=other", "-days", "2", "-out", "other.crt")  # fmt: skip
    return SimpleNamespace(
        dir=d,
        ca=str(d / "ca.crt"),
        cert=str(d / "server.crt"),
        key=str(d / "server.key"),
        client_cert=str(d / "client.crt"),
        client_key=str(d / "client.key"),
        other_cert=str(d / "other.crt"),
        other_key=str(d / "other.key"),
    )
