"""
Deployment files must agree with the code.

Two real bugs lived in the gap: the Dockerfile, Compose file and Kubernetes
manifest all set JSON_LOGS while the code read LOG_FORMAT (so JSON logging was
never enabled), and the Compose healthcheck called `curl`, which the image does
not contain (so it could never pass).
"""

import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


def env_names_in_dockerfile():
    text = (REPO / "docker" / "Dockerfile").read_text()
    return set(re.findall(r"^ENV\s+([A-Z_][A-Z0-9_]*)=", text, re.M))


def env_names_in_compose():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    names = set()
    for service in compose["services"].values():
        for entry in service.get("environment", []) or []:
            names.add(entry.split("=", 1)[0])
    return names


def env_names_in_kubernetes():
    names = set()
    for doc in yaml.safe_load_all((REPO / "k8s" / "chaos-demo.yaml").read_text()):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env", []) or []:
                names.add(env["name"])
    return names


def source_text():
    return "\n".join(p.read_text() for p in (REPO / "src").rglob("*.py"))


# Variables the platform or the shell reads, not this code
NOT_OURS = {"PATH", "PYTHONUNBUFFERED", "PYTHONPATH", "LANG", "HOME"}


@pytest.mark.parametrize(
    "where,names",
    [
        ("docker/Dockerfile", env_names_in_dockerfile),
        ("docker-compose.yml", env_names_in_compose),
        ("k8s/chaos-demo.yaml", env_names_in_kubernetes),
    ],
)
def test_every_environment_variable_set_is_read_by_the_code(where, names):
    declared = names() - NOT_OURS
    assert declared, f"found no environment variables in {where}; parser broken?"
    code = source_text()
    unread = sorted(
        n for n in declared if f'"{n}"' not in code and f"'{n}'" not in code
    )
    assert unread == [], f"{where} sets variables that nothing reads: {unread}"


def test_the_deployment_files_use_log_format_not_the_legacy_name():
    for names in (
        env_names_in_dockerfile,
        env_names_in_compose,
        env_names_in_kubernetes,
    ):
        found = names()
        assert "LOG_FORMAT" in found
        assert "JSON_LOGS" not in found


# ---------------------------------------------------------------------------
# The Compose healthcheck
# ---------------------------------------------------------------------------


def healthcheck_command():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    return compose["services"]["chaos-agent"]["healthcheck"]["test"]


def test_the_healthcheck_does_not_use_tools_the_image_lacks():
    command = " ".join(healthcheck_command())
    assert "curl" not in command and "wget" not in command
    dockerfile = (REPO / "docker" / "Dockerfile").read_text()
    assert "curl" not in dockerfile, "if curl is added to the image, drop this test"


def test_the_healthcheck_is_an_exec_form_python_command():
    command = healthcheck_command()
    assert command[0] == "CMD" and command[1] == "python" and command[2] == "-c"


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    handler = type("H", (_Handler,), {"status": 200})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def run_healthcheck(port):
    """Run the real healthcheck, pointed at `port`, with this interpreter."""
    command = healthcheck_command()
    code = command[3].replace("localhost:9000", f"127.0.0.1:{port}")
    assert f"127.0.0.1:{port}" in code, "the URL in the healthcheck changed"
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=30
    ).returncode


class TestHealthcheckBehavior:
    def test_passes_when_the_service_is_healthy(self, server):
        handler, port = server
        assert run_healthcheck(port) == 0

    @pytest.mark.parametrize("status", [500, 503, 404])
    def test_fails_on_an_error_response(self, server, status):
        handler, port = server
        handler.status = status
        assert run_healthcheck(port) != 0

    def test_fails_when_nothing_is_listening(self):
        assert run_healthcheck(1) != 0
