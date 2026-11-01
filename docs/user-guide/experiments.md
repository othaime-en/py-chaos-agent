# Experiments

An experiment turns "inject some chaos and see" into a test with a question and an
answer. You write down what you expect (the **hypothesis**), define what "healthy"
means (the **steady state**), say which faults to inject and how fast (the
**faults** and **schedule**), and the agent tells you whether your expectation held
(the **verdict**).

Experiments are defined in `config.yaml` and started through the API. They are
separate from the `failures:` section, which drives the continuous agent loop.

## Example

```yaml
experiments:
  - name: cpu-recovery
    description: Saturate two cores for a short burst.
    hypothesis: >
      After two cores are saturated for 20 seconds, the service answers
      /health again within 60 seconds.
    steady_state:
      checks:
        - name: health
          url: "http://localhost:8080/health"
          max_latency_ms: 500
      required_consecutive_successes: 3
      baseline_timeout_seconds: 30
      recovery_timeout_seconds: 60
    faults:
      cpu:
        cores: 2
        duration_seconds: 20
    schedule:
      duration_seconds: 60
      interval_seconds: 15
      max_injections: 3
```

Run it:

```bash
curl -X POST -H "Authorization: Bearer $CHAOS_API_TOKEN" \
  http://127.0.0.1:9000/experiments/cpu-recovery/run
# {"status":"started","run":{"id":"3f9c1a7b2d04","status":"checking_baseline", ...}}

curl -H "Authorization: Bearer $CHAOS_API_TOKEN" \
  http://127.0.0.1:9000/experiment-runs/3f9c1a7b2d04
```

## How a run works

1. **Baseline.** The steady-state checks must pass before anything is injected.
   If the target is not healthy to begin with, the run ends as `refused` and no
   chaos happens. A failure you cannot attribute to the experiment proves nothing.
2. **Chaos.** The faults are rolled every `interval_seconds` (the first roll is
   immediate) until `duration_seconds` pass or `max_injections` injections have
   started. Everything the agent already guarantees applies: the kill switch, one
   injection per type at a time, the resource budget, cleanup on abort.
3. **Recovery.** Injections still running are left to finish. Then the steady state
   is checked for up to `recovery_timeout_seconds`.
4. **Verdict.**

| Verdict | Meaning |
| --- | --- |
| `confirmed` | The steady state was restored after the chaos. |
| `falsified` | It was not restored within `recovery_timeout_seconds`. Worth investigating. |
| `inconclusive` | The run was cut short (operator abort, kill switch, shutdown), refused, or hit an error. Nothing can be concluded. |

`GET /experiment-runs/{id}` shows the status (`checking_baseline`, `running`,
`recovering`, `completed`, `aborted`, `refused`, `error`), the verdict, the reason
for anything other than a clean result, and the last steady-state round before and
after the chaos with each check's outcome and latency.

!!! note "Not yet: watching during the chaos"
    The steady state is checked before and after the chaos, not while it runs. A
    hypothesis like "the service stays healthy during the burst" cannot be
    verified yet; write it as a recovery claim. Checking during the run, and
    stopping early on a breach, comes with SLO-based abort.

## Fields

### Experiment

| Field | Default | Notes |
| --- | --- | --- |
| `name` | required | Lowercase letters, digits, `.`, `_`, `-`; up to 64 characters; unique. |
| `description` | empty | Up to 1000 characters. |
| `hypothesis` | required | What you expect, up to 500 characters. Not blank. |
| `steady_state` | required | See below. |
| `faults` | required | One or more of `cpu`, `memory`, `process`, `network`. |
| `schedule` | required | See below. |
| `dry_run` | `false` | Log what would happen without doing it. The API can override it per run. |

### `steady_state`

| Field | Default | Range | Notes |
| --- | --- | --- | --- |
| `checks` | required | 1 to 10 | All must pass in a round. Names must be unique. |
| `check_interval_seconds` | 2 | 1 to 60 | Pause between rounds. |
| `required_consecutive_successes` | 2 | 1 to 20 | Passing rounds in a row needed. One failure resets the count. |
| `baseline_timeout_seconds` | 30 | 0 to 600 | How long to wait for a healthy baseline before refusing. |
| `recovery_timeout_seconds` | 60 | 0 to 3600 | How long to wait for recovery after the chaos. |

A pass already in progress may finish after a timeout, and one round always runs,
so a timeout of 0 means "look once".

### `steady_state.checks[]` (type `http`)

| Field | Default | Notes |
| --- | --- | --- |
| `name` | required | Used in results. |
| `url` | required | `http` or `https`, with a host, no credentials. |
| `expected_status` | any 2xx | An exact status code (100 to 599) if you need one. |
| `max_latency_ms` | none | Fail if the response headers arrive later. Cannot exceed `timeout_seconds * 1000`. |
| `timeout_seconds` | 2 | 1 to 30. |

Redirects are not followed. A timeout, connection error, wrong status, slow
response, or bug in the probe all count as unhealthy.

### `faults`

Each fault takes the same parameters, with the same hard limits, as in the global
`failures:` section, with two differences: it is **enabled by default** and its
`probability` defaults to **1.0** (it fires on every roll). At least one fault must
be able to fire.

### `schedule`

| Field | Default | Range | Notes |
| --- | --- | --- | --- |
| `duration_seconds` | required | 1 to 21600 | How long faults keep being rolled. |
| `interval_seconds` | 10 | 1 to 3600 | Time between rolls. |
| `max_injections` | none | 1 to 1000 | Stop after this many injections have started. Dry runs count. |

## API

| Endpoint | Role | |
| --- | --- | --- |
| `GET /experiments` | viewer | Summaries of every defined experiment. |
| `GET /experiments/{name}` | viewer | One experiment in full. |
| `POST /experiments/{name}/run` | operator | Start a run. Returns `202` and the run. Optional body `{"dry_run": true}`. |
| `GET /experiment-runs` | viewer | Recent runs, newest first (the last 50 are kept in memory). |
| `GET /experiment-runs/{id}` | viewer | One run. |
| `POST /experiment-runs/abort` | operator | End the running experiment now. |

Experiments cannot be created or edited through the API. A remote caller can only
run what the operator defined in the config file, so the steady-state URLs and
fault parameters stay under operator control. `POST /config/reload` picks up
changes for the next run; a run in progress keeps the copy it started with.

### Refusals

`POST /experiments/{name}/run` returns:

| Status | When |
| --- | --- |
| `404` | No such experiment. |
| `409` | The agent loop is running; another experiment is running; an injection is running; or the kill switch is holding chaos back. |
| `422` | A fault does not fit the container's resource budget (same rule as a manual injection), or the body is invalid. |
| `503` | The agent is shutting down. |

Dry runs skip the injection, kill switch and budget refusals, since they change
nothing.

While an experiment runs, `POST /agent/start` and non-dry-run `POST /inject/manual`
return `409`. Two sources of chaos at once would make the result unreadable.

## Safety

- The kill switch gates experiments like everything else. If it trips mid-run, the
  injections are aborted and the run ends `aborted` with an inconclusive verdict.
- Stop an experiment with `POST /experiment-runs/abort`, not `POST /agent/stop`
  (that one only stops the agent loop). `POST /inject/abort` cuts running
  injections short, but the experiment keeps rolling its schedule.
- Shutdown aborts a running experiment and removes its effects before exiting.
- Every start and finish is in the audit log (`experiment.started`,
  `experiment.finished`). The injections inside a run are recorded with
  `source: experiment`, the experiment name and the run id, attributed to the
  person who started it.

## Metrics

| Metric | Meaning |
| --- | --- |
| `chaos_experiments_total{status,verdict}` | Finished experiments. |
| `chaos_experiment_active` | 1 while an experiment is running. |
