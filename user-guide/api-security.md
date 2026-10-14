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
