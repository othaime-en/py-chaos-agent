"""
The hardening in the deploy files, the lockfiles and CI stays in place.

These read the files and assert on their content, so a later edit that quietly
drops a protection (root user, writable filesystem, a :latest tag, an unpinned
action) fails here instead of in production.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


def text(*parts):
    return REPO.joinpath(*parts).read_text()


def k8s_docs():
    return [d for d in yaml.safe_load_all(text("k8s", "chaos-demo.yaml")) if d]


def containers():
    deployment = next(d for d in k8s_docs() if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    return pod, {c["name"]: c for c in pod["containers"]}


class TestKubernetes:
    def test_pod_refuses_to_run_as_root(self):
        pod, _ = containers()
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

    @pytest.mark.parametrize(
        "name, uid", [("chaos-agent", 10001), ("target-app", 10002)]
    )
    def test_each_container_is_locked_down(self, name, uid):
        _, by_name = containers()
        sc = by_name[name]["securityContext"]
        assert sc["runAsUser"] == uid
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]

    def test_only_the_agent_gets_capabilities_and_only_two(self):
        _, by_name = containers()
        agent = by_name["chaos-agent"]["securityContext"]["capabilities"]
        assert sorted(agent["add"]) == ["KILL", "NET_ADMIN"]
        assert "add" not in by_name["target-app"]["securityContext"]["capabilities"]

    def test_neither_container_is_privileged(self):
        _, by_name = containers()
        for c in by_name.values():
            assert not c["securityContext"].get("privileged")

    def test_the_agent_can_write_tmp_and_logs(self):
        _, by_name = containers()
        mounts = {m["mountPath"] for m in by_name["chaos-agent"]["volumeMounts"]}
        assert {"/tmp", "/var/log/chaos-agent"} <= mounts

    def test_image_tags_are_pinned(self):
        _, by_name = containers()
        for c in by_name.values():
            tag = c["image"].rpartition(":")[2]
            assert tag not in ("", "latest"), f"{c['image']} is not pinned"
            assert re.fullmatch(r"\d+\.\d+\.\d+", tag), c["image"]

    def test_traffic_is_denied_by_default(self):
        policies = [d for d in k8s_docs() if d["kind"] == "NetworkPolicy"]
        deny = [p for p in policies if p["spec"]["podSelector"] == {}]
        assert deny, "no default-deny NetworkPolicy"
        assert set(deny[0]["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert "ingress" not in deny[0]["spec"] and "egress" not in deny[0]["spec"]

    def test_the_control_api_port_is_not_opened_to_other_pods(self):
        for doc in k8s_docs():
            if doc["kind"] != "NetworkPolicy":
                continue
            for rule in doc["spec"].get("ingress", []):
                for port in rule.get("ports", []):
                    assert port["port"] != 9000


class TestImages:
    def test_agent_image_runs_unprivileged_and_installs_with_hashes(self):
        dockerfile = text("docker", "Dockerfile")
        assert re.search(r"^USER\s+10001", dockerfile, re.M)
        assert "--require-hashes" in dockerfile
        assert "cap_net_admin,cap_kill+ep" in dockerfile

    def test_target_image_runs_unprivileged(self):
        assert re.search(r"^USER\s+10002", text("docker", "Dockerfile.target"), re.M)

    def test_no_shell_wrapper_in_front_of_the_interpreter(self):
        """Under no-new-privileges a wrapper loses the file capabilities."""
        dockerfile = text("docker", "Dockerfile")
        assert "ENTRYPOINT" not in dockerfile
        assert re.search(r'^CMD \["python"', dockerfile, re.M)

    def test_base_images_are_not_latest(self):
        for name in ("Dockerfile", "Dockerfile.target"):
            for line in re.findall(
                r"^(?:ARG PYTHON_IMAGE|FROM)\S*\s+(\S+)", text("docker", name), re.M
            ):
                assert not line.endswith(":latest"), line
        assert "python:3.12" in text("docker", "Dockerfile")

    def test_compose_makes_the_filesystem_read_only(self):
        compose = yaml.safe_load(text("docker-compose.yml"))
        for service in compose["services"].values():
            assert service["read_only"] is True
            assert service["cap_drop"] == ["ALL"]


class TestSupplyChain:
    @pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
    def test_every_pinned_package_has_hashes(self, name):
        entries = re.findall(
            r"^([A-Za-z0-9_.\-]+)==\S+ \\\n((?:    --hash=.*\n?)+)", text(name), re.M
        )
        pins = re.findall(r"^[A-Za-z0-9_.\-]+==", text(name), re.M)
        assert pins and len(entries) == len(pins), "a package is missing its hashes"

    def test_test_tools_are_not_in_the_runtime_set(self):
        runtime = text("requirements.txt").lower()
        for dev_only in ("pytest", "black", "flake8", "mypy", "httpx"):
            assert f"\n{dev_only}==" not in runtime, dev_only

    def test_every_action_is_pinned_to_a_commit(self):
        for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
            for ref in re.findall(
                r"^\s*-?\s*uses:\s*(\S+)", workflow.read_text(), re.M
            ):
                if ref.startswith("./"):
                    continue
                assert re.search(
                    r"@[0-9a-f]{40}$", ref
                ), f"{workflow.name}: {ref} is not pinned to a SHA"

    def test_ci_scans_images_and_audits_dependencies(self):
        ci = text(".github", "workflows", "ci.yml")
        assert "pip-audit" in ci
        assert "aquasecurity/trivy-action" in ci
        assert "k8s_smoke_test.sh" in ci

    def test_python_version_is_consistent(self):
        assert "python_version = 3.12" in text("mypy.ini")
        assert 'python-version: "3.12"' in text(".github", "workflows", "ci.yml")


class TestTerraform:
    def tf(self, name):
        return text("terraform", name)

    def test_state_is_remote_and_locked(self):
        assert 'backend "s3"' in self.tf("backend.tf")
        example = self.tf("backend.hcl.example")
        assert "use_lockfile = true" in example
        assert "encrypt      = true" in example

    def test_the_backend_file_with_real_values_is_not_committed(self):
        assert "backend.hcl" in text(".gitignore").splitlines()

    def test_only_the_providers_that_are_used_remain(self):
        versions = self.tf("versions.tf")
        assert "hashicorp/aws" in versions
        for unused in ("helm", "kubernetes"):
            assert f'source  = "hashicorp/{unused}"' not in versions
            assert f'provider "{unused}"' not in self.tf("provider.tf")
        assert "aws_caller_identity" not in self.tf("provider.tf")

    def test_native_state_locking_needs_terraform_1_10(self):
        assert 'required_version = ">= 1.10"' in self.tf("versions.tf")

    @pytest.mark.parametrize("repo", ["chaos_agent", "target_app"])
    def test_registries_are_immutable_and_scanned(self, repo):
        ecr = self.tf("ecr.tf")
        block = ecr.split(f'resource "aws_ecr_repository" "{repo}"')[1].split(
            "\nresource"
        )[0]
        assert 'image_tag_mutability = "IMMUTABLE"' in block
        assert "scan_on_push = true" in block
        assert "encryption_configuration" in block

    def test_the_cluster_endpoint_can_be_closed(self):
        eks = self.tf("eks.tf")
        for name in ("private_access", "public_access", "public_access_cidrs"):
            assert f"cluster_endpoint_{name}" in eks
            assert f'variable "cluster_endpoint_{name}"' in self.tf("variables.tf")

    def test_an_open_endpoint_is_flagged_on_every_plan(self):
        assert 'check "cluster_endpoint_is_not_open_to_the_internet"' in self.tf(
            "eks.tf"
        )

    def test_ci_validates_terraform(self):
        ci = text(".github", "workflows", "ci.yml")
        assert "terraform fmt -check" in ci and "terraform validate" in ci
