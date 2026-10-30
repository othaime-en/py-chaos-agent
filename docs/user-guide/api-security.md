# API Security

The control API can kill processes, burn CPU and memory, and add network
latency. It is locked down by default.

## What is enforced

| Control | Behavior |
| --- | --- |
| Authentication | Every endpoint requires `Authorization: Bearer <token>`, except `/` and `/health` (liveness probes). |
| Fail closed | No token configured means protected endpoints return `503`, not an open API. |
| Loopback default | `python -m src.api_server` binds to `127.0.0.1` unless you set `--host` or `CHAOS_API_HOST`. |
| Startup validation | The server refuses to start without a token (at least 32 characters). |
| No auth on public binds | `--insecure-no-auth` is rejected unless the host is loopback. |
| Docs off | `/docs`, `/redoc` and `/openapi.json` return 404 unless `CHAOS_API_ENABLE_DOCS=true`. |
| Least privilege | Containers drop all capabilities except `NET_ADMIN` and `KILL`. No `privileged`. |
| Non-root | The images run as unprivileged users (UID 10001 for the agent, 10002 for the target app). |
| Read-only filesystem | The root filesystem is read-only in Compose and Kubernetes. Only `/tmp` and the log directory are writable. |
| Network policy | Kubernetes denies all traffic by default and allows only the target port, metrics from a `monitoring` namespace, and DNS. |

## How the agent gets its capabilities

The agent does not run as root. A non-root process gets no capabilities from a
container runtime, even with `--cap-add`, so the image grants them as file
capabilities instead:

- the interpreter (`/opt/venv/bin/python`) carries `NET_ADMIN` and `KILL`
- `tc` carries `NET_ADMIN`
- every other process in the container has none

The container must also be granted both capabilities (`cap_add` in Compose,
`securityContext.capabilities.add` in Kubernetes). That grant is the upper bound.
If it is missing the container fails to start with `operation not permitted`.

Run the interpreter directly. A shell wrapper in front of it loses the
capabilities when `no-new-privileges` is on.

The Kubernetes pod needs `NET_ADMIN`, so it cannot satisfy the `restricted` Pod
Security Standard. It fits `baseline` only if your cluster allows that capability.

## Configuring the token

Generate one and keep it out of git:

```bash
openssl rand -hex 32
```

| Variable | Purpose |
| --- | --- |
| `CHAOS_API_TOKEN_FILE` | Path to a file containing the token. Preferred. Re-read on every request, so rotating a mounted Secret needs no restart. |
| `CHAOS_API_TOKEN` | The token itself. Used if no file is set. |
| `CHAOS_API_HOST` | Bind address. Default `127.0.0.1`. |
| `CHAOS_API_AUTH_DISABLED` | `true` disables auth. Loopback only. Development only. |
| `CHAOS_API_ENABLE_DOCS` | `true` serves the interactive docs. They are not behind the token. |

## Using the API

```bash
export CHAOS_API_TOKEN=...
curl -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/status
```

## Docker Compose

```bash
cp .env.example .env
echo "CHAOS_API_TOKEN=$(openssl rand -hex 32)" > .env
docker-compose up --build
```

Ports 8000 and 9000 are published on the host's loopback interface only.

## Kubernetes

```bash
kubectl apply -f k8s/chaos-demo.yaml   # creates the namespace first
kubectl -n chaos-demo create secret generic chaos-api-token \
  --from-literal=token="$(openssl rand -hex 32)"
kubectl -n chaos-demo port-forward deploy/resilient-app 9000:9000
```

The pod stays in `ContainerCreating` until the Secret exists. The API is not
exposed through a Service; use `port-forward`.

Rotate the token by updating the Secret. The kubelet refreshes the mounted file
within about a minute and the agent picks it up on the next request.

## Not covered yet

- No rate limiting or lockout on failed attempts. Use a long random token and
  keep the API off untrusted networks.
- No per-user identity or roles. One shared token grants full control.
- No TLS. Put it behind a TLS-terminating proxy, or use `port-forward`.
- The Prometheus metrics port (8000) is still unauthenticated and listens on
  all interfaces.
- Add a Kubernetes `NetworkPolicy` to restrict which pods can reach the agent.
