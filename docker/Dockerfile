# syntax=docker/dockerfile:1

# Base image. Dependabot keeps the tag current; to pin by digest, append
# @sha256:<digest> (docker buildx imagetools inspect python:3.12-slim-bookworm).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# Build stage: install the hash-pinned dependencies into a virtualenv.
# --copies makes bin/python a real file (not a symlink to the system
# interpreter), so the runtime stage can give it file capabilities without
# touching the system Python.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build
RUN python -m venv --copies /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# tc comes from iproute2. libcap2-bin provides setcap (and getcap, used below to
# check the result). Do not purge libcap2-bin afterwards: iproute2 depends on it,
# so apt would remove iproute2 and tc along with it.
# Debian package versions are not pinned (a pin breaks the build at every
# security update); the image is scanned with Trivy in CI instead.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 procps libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user. The fixed UID/GID lets Kubernetes verify runAsNonRoot.
RUN groupadd --system --gid 10001 chaos \
    && useradd --system --uid 10001 --gid chaos --no-create-home \
        --shell /usr/sbin/nologin chaos

COPY --from=build /opt/venv /opt/venv

# Run as a normal user and keep only the two capabilities the agent needs.
#
# Container runtimes give a non-root process no capabilities, even with
# --cap-add. File capabilities are how they get handed over:
#   * /opt/venv/bin/python is a private copy of the interpreter (see the build
#     stage) and gets NET_ADMIN and KILL. It needs NET_ADMIN as well as KILL
#     because a child process can only receive a capability its parent holds.
#   * tc gets NET_ADMIN, so the netem commands work.
# Every other process, and every other child of the agent, has no capabilities.
#
# Both capabilities must also be granted to the container (--cap-add, or
# securityContext.capabilities.add). That is what bounds the file capabilities:
# without them the kernel refuses to start the interpreter ("operation not
# permitted"). The container must run the interpreter directly, with no shell
# wrapper in front: under no-new-privileges a wrapper process loses the
# capabilities before it can pass them on.
#
# Setuid and setgid bits are stripped first so there is no other route to
# privilege.
# pipefail, so a failing getcap in the pipes below fails the build.
# These checks only read file metadata. Do not run tc or the capable python here:
# the build does not grant NET_ADMIN or KILL, and the kernel refuses to execute a
# binary whose file capabilities exceed what the process may hold (EPERM, exit 126).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + \
    && setcap cap_net_admin,cap_kill+ep /opt/venv/bin/python \
    && setcap cap_net_admin+ep "$(readlink -f "$(command -v tc)")" \
    && getcap /opt/venv/bin/python | grep -q cap_net_admin \
    && getcap /opt/venv/bin/python | grep -q cap_kill \
    && getcap "$(readlink -f "$(command -v tc)")" | grep -q cap_net_admin \
    && dpkg -s iproute2 > /dev/null

WORKDIR /app
COPY src/ ./src/
COPY config.yaml .
RUN mkdir -p /var/log/chaos-agent && chown chaos:chaos /var/log/chaos-agent

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json

USER 10001:10001

# API port (9000) and metrics port (8000)
EXPOSE 9000 8000

# Run the API server by default. It binds to 127.0.0.1 and requires an API token
# (CHAOS_API_TOKEN or CHAOS_API_TOKEN_FILE). To make it reachable, set
# CHAOS_API_HOST=0.0.0.0 in the runtime environment alongside the token; it
# refuses to start otherwise.
# To run the standalone agent instead: docker run <image> python -m src.agent
CMD ["python", "-u", "-m", "src.api_server", "--port", "9000"]
