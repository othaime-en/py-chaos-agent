"""
The standalone agent runs the shared engine: injections of different types
overlap and the loop does not wait for them. Real process, real signal.
"""

import signal
import textwrap
import time

import pytest

from tests.process_helpers import wait_exit, wait_for

pytestmark = pytest.mark.slow

CONFIG = textwrap.dedent("""
    agent:
      interval_seconds: 1
    logging:
      level: DEBUG
    failures:
      network:
        enabled: true
        probability: 1.0
        interface: eth0
        delay_ms: 100
        duration_seconds: 60
      memory:
        enabled: true
        probability: 1.0
        mb: 16
        duration_seconds: 60
    kill_switch:
      enabled: false
    """)


def test_standalone_agent_overlaps_faults_and_keeps_ticking(env):
    (env.dir / "config.yaml").write_text(CONFIG)
    proc = env.start_agent()

    # Both 60 second injections are running at once. Under the old sequential
    # loop the second would not start until the first had finished.
    wait_for(lambda: "eth0" in env.rules(), 15, "network injection never started")
    wait_for(
        lambda: "Failure injection start" in env.output()
        and env.output().count("memory") > 0,
        15,
        "memory injection never started while the network one was running",
    )
    assert "eth0" in env.rules(), "network injection ended before memory started"

    # The loop is still ticking while both are running (under the old loop it
    # would be stuck inside the first injection), and leaving the busy types
    # alone is not logged as a warning.
    wait_for(
        lambda: env.output().count("Chaos iteration complete") >= 3,
        15,
        "the loop did not keep ticking while injections were running",
    )
    assert "Injection skipped" not in env.output()

    started = time.time()
    proc.send_signal(signal.SIGTERM)
    assert wait_exit(proc, 15) == 0
    assert time.time() - started < 10, "shutdown waited out the 60s injections"
    assert env.rules() == {}, "tc rule leaked after SIGTERM"
