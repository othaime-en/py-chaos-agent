# API Reference

The control API listens on port 9000 (loopback by default). Every route except `/` and `/health` needs `Authorization: Bearer <token>`.

The **Role** column is the minimum role a per-user token needs. Roles are `viewer`, `operator` and `admin`, and each includes the ones before it. The shared `CHAOS_API_TOKEN` is an `admin`. See [API Security](../user-guide/api-security.md#per-user-tokens-and-roles) for how tokens and roles are set up.

!!! tip "Interactive docs"
    Set `CHAOS_API_ENABLE_DOCS=true` to serve Swagger UI at `/docs`, ReDoc at `/redoc` and the schema at `/openapi.json`. They are off by default and need the `viewer` role.

## Agent

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| `GET` | `/` | none | API root |
| `GET` | `/health` | none | Liveness check |
| `GET` | `/status` | viewer | Agent state: enabled, active injections, kill switch pause, active experiment, eligibility |
| `POST` | `/agent/start` | operator | Start the injection loop. Optional `enable_kill_switch` query parameter |
| `POST` | `/agent/stop` | operator | Stop the injection loop |
| `POST` | `/agent/restart` | operator | Restart the injection loop |

## Injections

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| `POST` | `/inject/manual` | operator | Run one injection now, ignoring probability |
| `POST` | `/inject/abort` | operator | Cut short every running injection and remove its effects. Does not stop the loop |

`/inject/manual` takes a JSON body:

| Field | Description |
| --- | --- |
| `failure_type` | `cpu`, `memory`, `process` or `network` |
| `dry_run` | Optional. Log what would happen without injecting |
| `config` | Optional overrides, merged onto the stored config and validated. `{"duration_seconds": 3}` is enough |

## Experiments

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| `GET` | `/target` | viewer | The labels this agent has, which experiment selectors match against |
| `GET` | `/experiments` | viewer | Experiments defined in the config |
| `GET` | `/experiments/{name}` | viewer | One experiment in full, with its window and selector status |
| `GET` | `/experiments/{name}/selection` | viewer | Would this agent run the experiment now, and if not, why |
| `POST` | `/experiments/{name}/run` | operator | Start a run. Returns `202` at once; poll the run for the outcome |
| `GET` | `/experiment-runs` | viewer | Recent runs, newest first. `limit` 1 to 50, default 20 |
| `GET` | `/experiment-runs/{run_id}` | viewer | One run: status, verdict and steady-state results |
| `POST` | `/experiment-runs/abort` | operator | End the running experiment now. It finishes as `aborted` with an inconclusive verdict |

See [Experiments](../user-guide/experiments.md) for the experiment format.

## Configuration

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| `GET` | `/config` | viewer | Current configuration |
| `GET` | `/config/failures/{failure_type}` | viewer | Configuration for one failure type |
| `PATCH` | `/config` | admin | Change agent settings at runtime |
| `PATCH` | `/config/failures/{failure_type}` | admin | Change one failure type's settings at runtime |
| `POST` | `/config/reload` | admin | Reload `config.yaml` |
| `GET` | `/limits` | viewer | Live resource budget: detected cgroup limits, what injections may use now, and warnings if the container has none |

## Kill Switch, Metrics and Audit

| Method | Path | Role | Description |
| --- | --- | --- | --- |
| `GET` | `/kill-switch/status` | viewer | State, last health check and trip history |
| `POST` | `/kill-switch/reset` | admin | Close the kill switch and clear any latch |
| `GET` | `/metrics/summary` | viewer | Injection counts per failure type, as JSON |
| `POST` | `/metrics/reset` | admin | Reset all counters |
| `GET` | `/audit` | admin | Latest audit events, newest first. `limit` 1 to 500 (default 100), `principal` and `event` filters |

Prometheus metrics are not on this API. They are served separately on port 8000; see [Monitoring](../user-guide/monitoring.md).

## Errors

| Code | Meaning |
| --- | --- |
| `401` | Missing or invalid token |
| `403` | The token's role is too low. The response names the role needed |
| `404` | Unknown experiment, run or failure type |
| `409` | The request conflicts with current state, for example chaos is paused by the kill switch |
| `422` | The body or configuration is invalid or outside the hard limits |
| `429` | Locked out after repeated failed logins. See the `Retry-After` header |
| `503` | The agent is shutting down |
