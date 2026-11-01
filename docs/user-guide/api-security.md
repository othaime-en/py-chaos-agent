# API Security

The control API can kill processes, burn CPU and memory, and add network
latency. It is locked down by default.

## What is enforced

| Control | Behavior |
| --- | --- |
| Authentication | Every endpoint requires `Authorization: Bearer <token>`, except `/` and `/health` (liveness probes). |
| Fail closed | No credentials configured means protected endpoints return `503`, not an open API. |
| Roles | Per-user tokens carry a role (`viewer`, `operator`, `admin`). A route nobody listed needs `admin`. |
| Lockout | Five failed attempts in a minute lock a client out for five minutes (`429`). |
| Audit log | Every change, every refused request and every injection is recorded with who did it. |
| TLS | Optional in-process HTTPS, with optional client certificates. |
| Loopback default | The API and the metrics endpoint bind to `127.0.0.1` unless you say otherwise. |
| Metrics token | The metrics endpoint needs its own token on any non-loopback address. |
| Startup validation | The server refuses to start without credentials, or with a bad token file or certificate. |
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

## Credentials

There are two ways to authenticate, and you can use both at once.

**One shared token** gives full control. It is the simplest setup and it is what
earlier versions used.

| Variable | Purpose |
| --- | --- |
| `CHAOS_API_TOKEN_FILE` | Path to a file containing the token. Preferred. Re-read on every request, so rotating a mounted Secret needs no restart. |
| `CHAOS_API_TOKEN` | The token itself. Used if no file is set. |

**Per-user tokens with roles** say who did what and let you revoke one person
without touching the others. Set `CHAOS_API_TOKENS_FILE`.

Generate a shared token with `openssl rand -hex 32`. Keep it out of git.

Other settings: `CHAOS_API_HOST` (bind address, default `127.0.0.1`),
`CHAOS_API_AUTH_DISABLED` (`true` disables auth; loopback only; development only)
and `CHAOS_API_ENABLE_DOCS` (`true` serves the interactive docs, which are not
behind the token).

## Per-user tokens and roles

Create a token for a person or a system:

```bash
python -m src.tokens new --name alice --role operator
```

It prints the token once, which you give to the user, and the entry to add to the
tokens file. The file stores the SHA-256 **hash** of each token, never the token,
so a leaked config file or backup does not hand out working credentials.

```yaml
# tokens.yaml
tokens:
  - name: alice
    role: operator
    sha256: 3f1c5a...            # from `python -m src.tokens new`
  - name: grafana
    role: viewer
    sha256: 9ab2d7...
    expires: 2027-06-30          # optional: the token stops working after this date
```

Check a file before deploying it:

```bash
python -m src.tokens check tokens.yaml
```

The file is re-read when it changes, so adding or revoking a token takes effect
without a restart. A file that no longer parses makes the API refuse every
request (`503`) rather than keep serving from a stale copy. Names are unique, and
unknown keys are an error, so a typo does not silently drop a setting.

### What each role can do

Each role includes the ones before it.

| Role | Can |
| --- | --- |
| `viewer` | `GET /status`, `/config`, `/config/failures/{type}`, `/limits`, `/kill-switch/status`, `/metrics/summary` |
| `operator` | Also `POST /agent/start`, `/agent/stop`, `/agent/restart`, `/inject/manual`, `/inject/abort` |
| `admin` | Also `PATCH /config`, `PATCH /config/failures/{type}`, `POST /config/reload`, `POST /kill-switch/reset`, `POST /metrics/reset`, `GET /audit` |

A user without the role gets `403` naming the role needed. That is not a failed
login, so it does not count toward a lockout. The shared token is an `admin`.

A route that is not listed in `ROLE_POLICY` (in `src/auth.py`) needs `admin`, so a
new endpoint is locked down until someone decides. A test fails if a route is
missing from the table.

## Lockout after failed logins

Repeated bad credentials lock the client (by IP address) out. A locked-out client
gets `429` with `Retry-After`, and the credential is not even checked, so a
correct guess cannot be confirmed. A successful login clears the failure count.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHAOS_AUTH_MAX_FAILURES` | `5` | Failures in the window that cause a lockout. `0` turns it off. |
| `CHAOS_AUTH_FAILURE_WINDOW_SECONDS` | `60` | How long a failure is remembered. |
| `CHAOS_AUTH_LOCKOUT_SECONDS` | `300` | How long the lockout lasts. |

A value that is not a number stops startup, instead of silently disabling the
protection. `/health` is never locked, so Kubernetes probes keep working.

Alert on `chaos_auth_lockouts_total` and `chaos_auth_failures_total`.

**Behind a reverse proxy** every request comes from the proxy's address, so one
bad client would lock everyone out. Set `FORWARDED_ALLOW_IPS` to your proxy's
address so uvicorn reads the real client from `X-Forwarded-For`. Without it,
that header is ignored (and cannot be used to dodge the lockout).

## Audit log

Every event is one JSON object: when, who (`principal` and `role`), where from,
and what happened.

```json
{"time": "2026-10-31T09:14:02.511+00:00", "event": "api.request", "principal": "alice",
 "role": "operator", "auth": "token-file", "client": "10.0.0.7", "method": "POST",
 "path": "/inject/manual", "status": 200, "outcome": "ok",
 "detail": {"failure_type": "network", "dry_run": false}}
```

What is recorded:

| Event | When |
| --- | --- |
| `api.request` | Any request that changes something, with its outcome and details (the failure type, the configuration changed, the kill switch choice) |
| `auth.failed` | A wrong, missing or expired token, or a lockout (`detail.reason` says which) |
| `auth.forbidden` | A valid user whose role is too low (`detail.required_role`) |
| `audit.read` | Someone read the audit log |
| `injection.started`, `.completed`, `.failed`, `.refused` | Each injection, with `source` `manual` (and the user) or `scheduler` (the agent's own loop, principal `scheduler`) |

Plain reads (status, config) are not recorded, so the events that matter are not
buried in health-check noise. Tokens and request headers are never recorded, and
any detail key that looks like a secret is masked.

Where it goes, all at once:

- the `chaos.audit` logger, so it is on stdout and in your log pipeline
- a file, if `CHAOS_AUDIT_LOG_FILE` is set. One JSON object per line, created
  owner-only (`0600`), reopened per event so log rotation works. A file that
  cannot be created stops startup; a write that fails later is logged and counted
  in `chaos_audit_write_failures_total` but does not take the API down.
- the latest 500 events in memory, readable by an `admin`:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  "https://agent:9000/audit?principal=alice&event=api.request&limit=20"
```

An audit log kept only on the host being audited is evidence, not proof. Ship the
file somewhere the agent cannot write to.

## TLS

Without TLS an API token crosses the network in clear text. Bind to loopback, put
a TLS-terminating proxy in front, or let the agent serve HTTPS itself:

```bash
python -m src.api_server --host 0.0.0.0 \
  --tls-cert fullchain.pem --tls-key privkey.pem
```

| Variable (or flag) | Purpose |
| --- | --- |
| `CHAOS_TLS_CERT_FILE` (`--tls-cert`) | PEM certificate, with its chain |
| `CHAOS_TLS_KEY_FILE` (`--tls-key`) | PEM private key |
| `CHAOS_TLS_CLIENT_CA_FILE` (`--tls-client-ca`) | Require clients to present a certificate signed by this CA (mutual TLS) |
| `CHAOS_REQUIRE_TLS` | `true`: refuse to listen on a non-loopback address without TLS |

TLS 1.2 is the minimum. A missing file, a malformed PEM, or a key that does not
match the certificate stops startup with a message naming the file. With mutual
TLS, a client without a valid certificate is refused during the handshake, and
still needs a token on top.

Listening on a non-loopback address without TLS logs a warning. If a proxy
terminates TLS in front of the agent that is expected; set `CHAOS_REQUIRE_TLS`
where the agent itself must be reached over TLS.

The certificate is loaded at startup. Rotate it by restarting (a rolling restart
in Kubernetes). When TLS is on it covers the metrics port too, and Compose's
healthcheck and Kubernetes probes then need `https`.

## The metrics endpoint

`/metrics` shows when chaos runs, what it targets, the kill switch state and who
is failing to authenticate. It has the same protections as the API:

- It listens on `127.0.0.1` by default. Set `CHAOS_METRICS_HOST` to change it.
- A scrape token (`CHAOS_METRICS_TOKEN` or `CHAOS_METRICS_TOKEN_FILE`, 32 or more
  characters) is required whenever one is set, on any address. The file is
  re-read on every request.
- A non-loopback address **without** a token is refused at startup. If a
  NetworkPolicy or private network already limits access, set
  `CHAOS_METRICS_AUTH_DISABLED=true` to opt out. It logs a warning.
- The scrape token must differ from the API token. It sits in Prometheus's
  configuration, so it must not be able to start chaos.
- The same lockout applies, with separate counters, so a misconfigured Prometheus
  cannot lock anyone out of the API.
- Only `GET /metrics` is served.

Prometheus configuration:

```yaml
scrape_configs:
  - job_name: chaos-agent
    authorization:
      credentials_file: /etc/prometheus/chaos-metrics-token
    # scheme: https        # when TLS is on
    static_configs:
      - targets: ["chaos-agent:8000"]
```

## Using the API

```bash
export CHAOS_API_TOKEN=...
curl -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/status
```

## Docker Compose

```bash
cp .env.example .env
{
  echo "CHAOS_API_TOKEN=$(openssl rand -hex 32)"
  echo "CHAOS_METRICS_TOKEN=$(openssl rand -hex 32)"
} > .env
docker-compose up --build
```

Compose refuses to start without both tokens. Ports 8000 and 9000 are published
on the host's loopback interface only. The audit log is written to
`/var/log/chaos-agent/audit.log` on the logs volume.

## Kubernetes

```bash
kubectl apply -f k8s/chaos-demo.yaml   # creates the namespace first
kubectl -n chaos-demo create secret generic chaos-api-token \
  --from-literal=token="$(openssl rand -hex 32)" \
  --from-literal=metrics-token="$(openssl rand -hex 32)"
kubectl -n chaos-demo port-forward deploy/resilient-app 9000:9000
```

The pod stays in `ContainerCreating` until the Secret exists. The API is not
exposed through a Service; use `port-forward`. Metrics are on the
`chaos-agent-api` Service, behind the metrics token and the NetworkPolicy.

Rotate a token by updating the Secret. The kubelet refreshes the mounted file
within about a minute and the agent picks it up on the next request.

## Upgrading

- **Metrics moved to loopback by default.** Anything that scraped the agent over
  the network must now set `CHAOS_METRICS_HOST` and a scrape token. In Compose,
  add `CHAOS_METRICS_TOKEN` to `.env`. In Kubernetes, add the `metrics-token` key
  to the existing Secret:

  ```bash
  kubectl -n chaos-demo patch secret chaos-api-token --type merge \
    -p "{\"stringData\":{\"metrics-token\":\"$(openssl rand -hex 32)\"}}"
  ```

  Without it the agent refuses to start and says what to set. Update Prometheus to
  send the token.
- Nothing else needs migrating. The shared API token keeps working and is an
  `admin`.
- Failed logins now lock a client out. A script that retries with a wrong token
  will hit `429`.

## Not covered

- Login and lockout state live in memory: they reset on restart, and each replica
  keeps its own. Run the agent as one replica, which is the design.
- Through `kubectl port-forward` every client reaches the agent as `127.0.0.1`, so
  the lockout and the audit log's `client` field cannot tell those users apart: a
  lockout there applies to everyone using the tunnel. Per-user tokens still say
  who did what.
- The audit log is not tamper-evident. Ship it off the host.
- There is no single sign-on. Tokens are issued and revoked by editing the file.
- The only rate limit is the one on failed logins. There is no general request
  rate limit.
- TLS certificates are not reloaded while running.
