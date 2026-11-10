# Kill Switch

The kill switch pauses chaos automatically when your application becomes
unhealthy, and lets it resume once the application has recovered. It exists so
that an experiment that goes wrong stops itself instead of waiting for a person.

It protects the agent loop, the standalone agent, and `POST /inject/manual`.

## How it works

It watches your application's health endpoint and acts like a circuit breaker:

| State | Chaos | What happens |
| --- | --- | --- |
| **closed** | Allowed | Health is checked every `health_check_interval_seconds`. `consecutive_failures_threshold` failures in a row trip it. |
| **open** | Paused | In-flight injections are aborted and their effects removed. After `recovery_test_interval_seconds` it moves to half-open. |
| **half-open** | Paused | `recovery_success_threshold` healthy checks in a row close it. **One** failed check re-opens it at once. |

Chaos is **paused, not stopped**. The agent loop keeps running, skips injections
while the breaker is not closed, and carries on by itself after recovery.
`GET /status` shows `paused_by_kill_switch`.

### It only watches while chaos is active

The kill switch checks health only while chaos is armed: the loop is running, or
an injection is in flight. While idle it counts nothing, so an application that is
down for its own reasons does not trip it. (If it is already tripped, it keeps
checking so it can detect recovery.)

### Grace periods

- **After a process kill** (`grace_after_process_kill_seconds`): killing the
  target takes it down on purpose, so health checks are skipped for a while.
  Manual process kills earn this grace too.
- **After chaos becomes active** (`grace_after_agent_start_seconds`): a target that
  is still starting up does not trip it. It applies each time chaos is armed.

### What counts as healthy

Any **2xx** response. A timeout, a connection error, any other status code, or an
error inside the check itself counts as a failure. The check fails closed: a bug in
the safety mechanism trips it rather than silently leaving chaos unprotected.

## Configuration

```yaml
kill_switch:
  enabled: true
  target_health_url: "http://localhost:8080/health"
  health_check_interval_seconds: 10
  health_check_timeout_seconds: 2        # must not exceed the interval
  consecutive_failures_threshold: 3
  grace_after_process_kill_seconds: 60
  grace_after_agent_start_seconds: 30
  recovery_test_interval_seconds: 120
  recovery_success_threshold: 3
  resume_after_recovery: true
  allow_api_disable: false
```

Set `target_health_url` to your application's real health endpoint. In Kubernetes
the sidecar shares the pod network, so `localhost` and your app's port work.

| Setting | Range | Notes |
| --- | --- | --- |
| `health_check_interval_seconds` | 1 to 3600 | |
| `health_check_timeout_seconds` | 1 to 60 | Must not exceed the interval |
| `consecutive_failures_threshold` | 1 to 100 | Time to trip is about interval x threshold |
| `grace_after_process_kill_seconds` | 0 to 3600 | |
| `grace_after_agent_start_seconds` | 0 to 3600 | |
| `recovery_test_interval_seconds` | 1 to 3600 | |
| `recovery_success_threshold` | 1 to 100 | |
| `target_health_url` | `http` or `https`, no credentials, up to 2048 characters | |

These settings can only be changed in the config file. The API rejects any attempt
to change them, so a remote caller cannot weaken the protection.

### Resume or latch

- `resume_after_recovery: true` (default): chaos resumes by itself after recovery.
- `resume_after_recovery: false`: after a trip, chaos stays off even once the target
  has recovered, until an operator calls `POST /kill-switch/reset`. A standalone
  agent has no API, so it must be restarted. Use this when an unattended restart of
  chaos after a failure is not acceptable.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /kill-switch/status` | State, last health check and result, trip count and reason, time until the next recovery test |
| `POST /kill-switch/reset` | Close the breaker and clear any latch. **The target must pass a health check right now**; otherwise it returns 409. |
| `POST /inject/manual` | Returns **409** `Chaos paused: ...` while the breaker is not closed. Dry runs are always allowed. |
| `POST /agent/start` | Starts the loop. `enable_kill_switch=false` is **refused (403)** unless `allow_api_disable: true`. |

One kill switch lives for the whole process. Stopping and starting the agent, or
reloading the config, does **not** clear a trip. Reloading applies new settings but
keeps the current state. Only recovery or a verified reset closes it.

## Metrics

| Metric | Meaning |
| --- | --- |
| `chaos_kill_switch_state` | 0 closed, 1 open, 2 half-open |
| `chaos_kill_switch_trips_total` | Number of trips |
| `chaos_kill_switch_health_checks_total{result}` | Health checks made, `healthy` or `unhealthy` |

Alert on `chaos_kill_switch_state != 0`.

## Disabling it

Set `kill_switch.enabled: false`. The agent logs a warning at startup and chaos
runs without automatic protection. Only do this if something else is watching.

To let operators bypass it for a single run through the API, set
`allow_api_disable: true`; then `POST /agent/start?enable_kill_switch=false` skips
it for that run only. The default is that nobody can.

## Known gaps

- **One health endpoint, one signal.** It does not look at error rates or latency
  from Prometheus. That belongs with the experiment model in the roadmap.
- **A short manual injection can finish inside the grace period**, before any
  health check runs. The gate still stops new injections once tripped.
- **Latched state is not persisted.** Restarting the process starts closed.
- **Detection takes time**: about `interval x threshold` seconds, so keep injections
  short relative to that, or lower the interval.
