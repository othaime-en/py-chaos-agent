# Monitoring

The agent exposes Prometheus metrics on port 8000 and a JSON summary on the control API.

## Scrape the Agent

Metrics are served at `GET /metrics`. They listen on loopback by default and require a scrape token that differs from the API token. For the Prometheus job, the access rules and TLS, see [API Security](api-security.md#the-metrics-endpoint). On Kubernetes, scrape the `chaos-agent-api` Service; see [Kubernetes Deployment](kubernetes.md).

## Metrics

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `chaos_injections_total` | counter | `failure_type`, `status` | Injections by outcome: `success`, `aborted` (cut short), `skipped` (dry run or overlap guard), `failed` |
| `chaos_injections_clamped_total` | counter | `failure_type` | Injections reduced to fit the container's resource budget |
| `chaos_injection_active` | gauge | `failure_type` | Currently running injection |
| `chaos_experiments_total` | counter | `status`, `verdict` | Finished experiments. Status: `completed`, `aborted`, `refused`, `error`. Verdict: `confirmed`, `falsified`, `inconclusive` |
| `chaos_experiment_active` | gauge | | `1` while an experiment runs |
| `chaos_slo_aborts_total` | counter | `kind` | Experiments stopped by their SLO checks: `breach` or `unavailable` |
| `chaos_kill_switch_state` | gauge | | `0` closed (chaos allowed), `1` open (tripped), `2` half-open (testing recovery) |
| `chaos_kill_switch_trips_total` | counter | | Times the kill switch tripped |
| `chaos_kill_switch_health_checks_total` | counter | `result` | Target health checks: `healthy` or `unhealthy` |
| `chaos_auth_failures_total` | counter | `surface`, `reason` | Rejected logins. Surface: `api` or `metrics`. Reason: `missing`, `invalid`, `expired`, `locked_out` |
| `chaos_auth_lockouts_total` | counter | `surface` | Clients locked out after repeated failures |
| `chaos_audit_write_failures_total` | counter | | Audit events that could not be written |

Counters live in the agent process and reset when it restarts.

## Useful Queries

```promql
# Injection outcomes over the last hour
sum by (failure_type, status) (increase(chaos_injections_total[1h]))

# Chaos is paused: the kill switch has tripped
chaos_kill_switch_state == 1

# An experiment was stopped by its SLO
increase(chaos_slo_aborts_total[1d]) > 0

# Someone is guessing tokens
increase(chaos_auth_lockouts_total[10m]) > 0

# The audit log is losing events
increase(chaos_audit_write_failures_total[5m]) > 0
```

## Without Prometheus

`GET /metrics/summary` on the control API (viewer role) returns per-failure-type counts as JSON:

```json
{
  "timestamp": "2026-11-06T15:40:23.200098Z",
  "metrics": {
    "cpu": { "success": 1.0, "aborted": 0.0, "failed": 0.0, "skipped": 1.0, "active": 0.0, "total": 2.0 }
  }
}
```

The same events are in the agent's logs, and in the [audit log](api-security.md#audit-log) for who did what.
