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
3. **Recovery.** Injections still running are left to finish (everything that can
   stop a run, such as an abort, the kill switch, a closing window or a removed
   label, stays active until they have). Then the steady state
   is checked for up to `recovery_timeout_seconds`.
4. **Verdict.**

| Verdict | Meaning |
| --- | --- |
| `confirmed` | The steady state was restored after the chaos. |
| `falsified` | The steady state was not restored within `recovery_timeout_seconds`, **or** the run was aborted because an [SLO](#slo-abort) was breached. Worth investigating. |
| `inconclusive` | The run was cut short by something other than the system (operator abort, kill switch, shutdown, a closing window, a removed label), was refused, hit an error, or an SLO could not be checked. Nothing can be concluded. |

`GET /experiment-runs/{id}` shows the status (`checking_baseline`, `running`,
`recovering`, `completed`, `aborted`, `refused`, `error`), the verdict, the reason
for anything other than a clean result, and the last steady-state round before and
after the chaos with each check's outcome and latency.

The steady state (`steady_state`) is checked **before and after** the chaos. To
check the system **while** the chaos runs, and stop early if it leaves its limits,
add an [`slo`](#slo-abort) block.

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
| `selector` | none | Which agents may run it, by label. See [Target selectors](#target-selectors). Without it, any agent. |
| `windows` | none | When the experiment may run. See [Time windows](#time-windows). Without it, always. |
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

### `steady_state.checks[]` (type `prometheus`)

Instead of (or as well as) an HTTP check, a check can ask Prometheus. See
[Prometheus checks](#prometheus-checks) for the fields.

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

## Target selectors

The same `config.yaml` is usually shared by many sidecars in many environments. A
selector says where an experiment may run, and each agent knows its own labels. If
they do not match, the experiment is refused. An experiment written for staging
cannot run in production because someone copied the config.

```yaml
experiments:
  - name: cpu-recovery
    # ...
    selector:
      match_labels:
        env: staging
        app: checkout
      match_expressions:
        - key: tier
          operator: In
          values: [canary, preview]
        - key: chaos-optout
          operator: DoesNotExist
```

### Giving the agent its labels

| Setting | Meaning |
| --- | --- |
| `CHAOS_TARGET_LABELS` | Comma-separated `key=value` pairs, e.g. `env=staging,app=checkout`. |
| `CHAOS_TARGET_LABELS_FILE` | A file with one `key="value"` per line. This is the exact format Kubernetes writes for the downward API field `metadata.labels`, so a pod's labels can be mounted straight in. |
| `CHAOS_TARGET_ID` | The agent's stable identity, used to pick which agents run an experiment when `sample_percent` is set. Defaults to the host name (the pod name on Kubernetes). |

If both are set the file wins on a clash. `k8s/chaos-demo.yaml` mounts the pod's
labels this way. `GET /target` shows the labels the agent currently has.

The server refuses to start if either setting is invalid or the file cannot be
read, rather than failing every selector without saying why.

Label rules are Kubernetes': a key is `name` or `prefix/name`, a name is up to 63
letters, digits, `-`, `_` or `.` starting and ending with a letter or digit, and a
prefix is a DNS name. A value is empty or follows the name rule.

### Selector rules

| Field | Meaning |
| --- | --- |
| `match_labels` | Every pair must be present with exactly that value. |
| `match_expressions[].key` | The label to test. |
| `match_expressions[].operator` | `In`, `NotIn` (need `values`), `Exists`, `DoesNotExist` (take none). |

- Every condition must hold. A selector must have at least one: an empty selector
  would match every agent, so it is a config error.
- An agent with no labels matches no selector. `NotIn` and `DoesNotExist` are
  satisfied by a missing label, as in Kubernetes.
- At most 20 label conditions, 20 expressions and 20 values per expression.

### What the agent does with them

| When | What happens |
| --- | --- |
| You start a run on an agent that does not match | Refused with `409`, listing every unmet condition (for example `env must be 'staging' but is 'prod'`). No run is created and nothing is probed. The target is checked before the time window. |
| The labels stop matching during the baseline check | The run ends `refused`. Nothing was injected. |
| The labels stop matching during the chaos | The run is aborted, injections are cut short and their effects removed. Verdict `inconclusive`. |
| Dry run | Never blocked. It runs, and its `selection` says what a real run would have been told. |

The labels file is re-read about once a second and **fails closed**: if it cannot
be read or is invalid, the agent has no labels and a running experiment stops,
rather than carrying on with stale ones. With Kubernetes the downward API file
follows the pod's labels (with the kubelet's sync delay), so this is a remote off
switch:

```bash
kubectl -n chaos-demo label pod <pod> env-     # remove the label, the chaos stops
```

A run records the labels it started with (`target_labels`) and so does the
`experiment.started` audit event.

## Choosing who and how many

Selection is a series of gates, checked in this order. Each is reported with its
reason, so a refusal, a dry run and the preview all say the same thing.

| Gate | Question | Configured in |
| --- | --- | --- |
| `eligibility` | Has this agent opted in to chaos at all? | `agent.eligibility` |
| `selector` | Do its labels match the experiment? | `experiments[].selector` |
| `window` | Is it inside the allowed time, and out of every blackout? | `experiments[].windows` |
| `sample` | Is it in the sampled share of the matching agents? | `selector.sample_percent` |
| `kill_switch` | Is the kill switch holding chaos back? | `kill_switch` |
| `idle` | Is anything already running? | |

### Opt-in eligibility

Labels you forget fail safe if eligibility is **opt-in**: a pod only runs chaos if
it carries a label that says so.

```yaml
agent:
  eligibility:
    match_labels:
      chaos-enabled: "true"
```

A pod without `chaos-enabled=true` cannot run an experiment, a manual injection
or the continuous loop. This is a policy for the whole agent, set in the config
by the operator; it cannot be changed through the API. Dry runs are not gated.

Removing the label is an off switch: it aborts a running experiment (checked
continuously) and the loop's running injections (checked every 0.25 seconds, so
not only on the next tick), and their effects are removed. `GET /status` shows
`eligible` and `eligibility_reason`.

The same rule can express exclusion instead of opt-in, using `NotIn` or
`DoesNotExist`:

```yaml
agent:
  eligibility:
    match_expressions:
      - key: chaos
        operator: NotIn
        values: [opt-out]
      - key: namespace
        operator: NotIn
        values: [prod]
```

The agent only knows the labels it is given. To exclude by namespace, pass the
namespace in as a label (on Kubernetes with the downward API, for example
`CHAOS_TARGET_LABELS=namespace=$(POD_NAMESPACE)`).

### Sampling: how many

`sample_percent` limits an experiment to a share of the agents that match.

```yaml
selector:
  match_labels:
    env: staging
  sample_percent: 10
```

Each agent decides for itself, with no coordination. It hashes its identity, the
experiment name and a **sample key** into a number from 0 to 100, and runs if that
is below `sample_percent`. So:

- The same agents are picked every time, until you change the sample key. Pass a
  new key to draw a new sample: `POST /experiments/{name}/run` with
  `{"sample_key": "2026-11-05"}` (letters, digits, `.`, `_`, `:` and `-`, up to 64).
- An agent's identity is `CHAOS_TARGET_ID`, or the host name (the pod name on
  Kubernetes). `GET /target` shows it.
- The share picked is close to the percentage for a large fleet and only
  approximate for a small one. It is a dice roll per agent, **not a count**: ten
  agents at 10% may well pick none, or three. If you need "exactly one", name it
  with a selector instead.

### At most N at once

`schedule.max_concurrent_injections` (1 to 4) limits how many faults of one
experiment run at the same time, on top of the rule that a type never overlaps with
itself. With `1`, an experiment with `cpu` and `network` faults runs them one at a
time. A fault that would exceed the cap waits for the next roll.

### Why was this agent (not) selected?

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  localhost:9000/experiments/cpu-recovery/selection
```

```json
{
  "experiment": "cpu-recovery",
  "agent_id": "checkout-7d9f6-abcde",
  "would_run": false,
  "gates": [
    {"gate": "eligibility", "ok": false, "detail": "chaos-enabled must be 'true' but is not set"},
    {"gate": "selector", "ok": true, "detail": "labels match the selector"},
    {"gate": "sample", "ok": false, "detail": "this agent is not in the 10% sample (position 42.7, needs to be below 10)"},
    {"gate": "kill_switch", "ok": true, "detail": "the kill switch is not holding chaos back"},
    {"gate": "idle", "ok": true, "detail": "nothing else is running"}
  ]
}
```

It changes nothing. `?sample_key=` previews a different sample. The resource budget
is checked when you start a run, not here.

A **dry run** reports instead of being blocked: it still rehearses the flow
(baseline check, steady state, verdict), and the run's `selection` field records
exactly what a real run on this agent would have been told. Every real run records
its `selection` too, and so does the `experiment.started` audit event.

## Time windows

A window set limits **when** an experiment may run, so chaos only happens when
people are around to watch it (and not during a release freeze, a sale, or the
night shift's quiet hours).

```yaml
experiments:
  - name: cpu-recovery
    # ...
    windows:
      timezone: Pacific/Honolulu   # IANA name, default UTC
      allow:
        - days: [mon, tue, wed, thu]
          start: "10:00"
          end: "16:00"
        - days: [fri]
          start: "10:00"
          end: "12:00"
```

| Field | Default | Notes |
| --- | --- | --- |
| `timezone` | `UTC` | An IANA name such as `Europe/Berlin`. Unknown names are a config error. |
| `allow` | none | Up to 20 windows. Without any, it may run at any time except during a blackout. |
| `blackouts` | none | Up to 50 periods when it must never run. See [Blackouts](#blackouts). At least one of `allow` or `blackouts` is required. |
| `allow[].days` | every day | Any of `mon` to `sun`. The day the window **starts** on. |
| `allow[].start` | required | `HH:MM`, 24-hour, `00:00` to `23:59`. |
| `allow[].end` | required | `HH:MM`, or `24:00` for midnight. Must differ from `start`. |

How the rules work:

- A window is open from `start` (inclusive) to `end` (exclusive).
- If `end` is not later than `start`, the window runs past midnight. With
  `days: [fri]`, `22:00` to `02:00` is open on Saturday at 01:00 and closed on
  Sunday at 01:00. Use `00:00` to `24:00` for a whole day.
- Windows that overlap or touch count as one continuous window.
- Times are wall-clock times in `timezone`. On a daylight-saving change day the
  window keeps its clock times, so it is an hour shorter or longer. A boundary
  inside the skipped or repeated hour resolves the way Python's `zoneinfo` does.

### Blackouts

A blackout is a date range when the experiment must never run, whatever the
windows say: a deploy freeze, a sale, a holiday.

```yaml
windows:
  timezone: Pacific/Honolulu
  allow:
    - days: [mon, tue, wed, thu]
      start: "10:00"
      end: "16:00"
  blackouts:
    - from: 2026-12-20
      to: 2027-01-03
      reason: holiday freeze
```

| Field | Notes |
| --- | --- |
| `from`, `to` | Dates, `YYYY-MM-DD`, **both inclusive**, whole days in `timezone`. `to` must not be before `from`; a blackout lasts at most 366 days. Quoted or not (YAML turns bare dates into dates, which is fine). |
| `reason` | Optional, up to 100 characters. Shown in refusals and in the API. |

- A blackout beats an allowed window. A window that falls inside one is closed.
- A window that would run into a blackout is cut off at its start: the fit check
  and the mid-run abort both use that, so chaos stops when the freeze begins.
- Overlapping or touching blackouts count as one and keep both reasons.
- `allow` can be left out: "any time except these dates".
- The next opening skips over blackouts, and lands on a real window, not just on
  the day after the blackout ends.

What the agent does with them:

| When | What happens |
| --- | --- |
| You start a run in a blackout | Refused with `409`: `In a blackout period (holiday freeze); the next allowed time opens ...`. |
| You start a run outside every window | Refused with `409`. The message gives the next opening in the window's own zone. No run is created and nothing is probed. |
| You start a run but the chaos would outlast the window | Refused with `409`. The estimate is the schedule plus the longest single fault (with `max_injections`, the time until the last allowed injection starts, plus the longest fault). The baseline and recovery checks are not counted. |
| The window closes during the baseline check | The run ends `refused`. Nothing was injected. |
| The window closes during the chaos | The run is aborted, injections are cut short and their effects removed. Verdict `inconclusive`, reason "the allowed time window closed". |
| Dry run | Never blocked. It runs, and its `selection` says what a real run would have been told. |

There is no override in the API. A window is a safety limit; to change it, edit
the config and `POST /config/reload`. The windows decide only whether a run may
happen. Nothing starts an experiment on its own, so a window never triggers one.

`GET /experiments` and `GET /experiments/{name}` show whether each window is open
now and when it closes or next opens (UTC), and a run reports `window_closes_at`.

## API

| Endpoint | Role | |
| --- | --- | --- |
| `GET /experiments` | viewer | Summaries of every defined experiment. |
| `GET /experiments/{name}` | viewer | One experiment in full, with whether its window is open and whether this agent matches its selector. |
| `GET /experiments/{name}/selection` | viewer | Every selection gate with its reason; whether this agent would run it now. |
| `GET /target` | viewer | The labels this agent has, which selectors are matched against. |
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
| `409` | The agent loop is running; another experiment is running; an injection is running; the kill switch is holding chaos back; this agent does not match the experiment's [selector](#target-selectors); or the experiment is outside its [time windows](#time-windows) (or would not finish before one closes). |
| `422` | A fault does not fit the container's resource budget (same rule as a manual injection), or the body is invalid. |
| `503` | The agent is shutting down. |

Dry runs are not refused for the injection, kill switch, budget, selector, sample,
eligibility or time window reasons, since they change nothing. They run, and the run
records which of those a real run would have hit (see
[Why was this agent (not) selected?](#why-was-this-agent-not-selected)).

While an experiment runs, `POST /agent/start` and non-dry-run `POST /inject/manual`
return `409`. Two sources of chaos at once would make the result unreadable.

## Safety

- The kill switch gates experiments like everything else. If it trips mid-run, the
  injections are aborted and the run ends `aborted` with an inconclusive verdict.
- Stop an experiment with `POST /experiment-runs/abort`, not `POST /agent/stop`
  (that one only stops the agent loop). `POST /inject/abort` cuts running
  injections short, but the experiment keeps rolling its schedule.
- A closing time window, or labels that stop matching the selector, abort a running
  experiment the same way.
- Shutdown aborts a running experiment and removes its effects before exiting.
- Every start and finish is in the audit log (`experiment.started`,
  `experiment.finished`). The injections inside a run are recorded with
  `source: experiment`, the experiment name and the run id, attributed to the
  person who started it.

## SLO abort

The steady state tells you whether the system came back. An **SLO** is a limit it
must stay within *while the chaos runs*, and the experiment stops the moment it does
not. Without one, an experiment injects for its full schedule whatever happens to
your users.

```yaml
experiments:
  - name: cpu-recovery
    # ...
    slo:
      checks:
        - type: prometheus
          name: error-rate
          url: http://prometheus.monitoring:9090
          query: >
            sum(rate(http_requests_total{code=~"5.."}[1m]))
            / sum(rate(http_requests_total[1m]))
          operator: "<"
          threshold: 0.01
        - type: prometheus
          name: p99-latency
          url: http://prometheus.monitoring:9090
          query: >
            histogram_quantile(0.99,
              sum(rate(http_request_duration_seconds_bucket[1m])) by (le))
          operator: "<"
          threshold: 0.5
      check_interval_seconds: 5
      breach_after: 2
      grace_seconds: 60
```

| Field | Default | Range | Notes |
| --- | --- | --- | --- |
| `checks` | required | 1 to 10 | `http` or `prometheus` checks, names unique. All must pass in a round. |
| `check_interval_seconds` | 5 | 1 to 60 | Time between rounds. |
| `breach_after` | 2 | 1 to 20 | Failing rounds **in a row** before it trips. A passing round resets the count, so one noisy sample does not end an experiment. |
| `grace_seconds` | 0 | 0 to 600 | Wait this long after the chaos starts before the first round. For metrics that need time to show an effect, such as a `rate(...[1m])` window. |

### What happens on a breach

| Situation | Result |
| --- | --- |
| A check evaluated and **violated its limit** | The run is aborted: injections are cut short and their effects removed. Status `aborted`, verdict **`falsified`**, reason `SLO breached: error-rate: 0.08 is not < 0.01`. The system did not stay within its limits, which is what the experiment exists to find. |
| A check **could not be evaluated** (metrics server down, query rejected, no data) | Aborted too, verdict **`inconclusive`**, reason `SLO could not be checked: ...`. Nothing can be concluded about the system, but carrying on without being able to see it is not safe. |
| Both kinds in one round | A violated limit wins: falsified. |

It is watched for the whole chaos phase, including while the last injections finish
after the schedule ends. After an SLO abort the chaos is already gone, so the
agent still waits up to `recovery_timeout_seconds` for the steady state to return and
records whether it did.

The run's `slo` field shows the rounds so far, the last round's results, and whether
and how it tripped. It is live while the run is in progress.

**Dry runs** evaluate the SLO checks and record the result, but are never aborted by
them. A dry run is therefore also the way to prove your queries are right and
Prometheus is reachable before a real run.

### Prometheus checks

```yaml
- type: prometheus
  name: error-rate
  url: http://prometheus.monitoring:9090
  query: 'sum(rate(errors_total[1m])) / sum(rate(requests_total[1m]))'
  operator: "<"
  threshold: 0.01
  timeout_seconds: 5
  on_no_data: fail
  bearer_token_env: PROM_TOKEN
```

| Field | Default | Notes |
| --- | --- | --- |
| `url` | required | The Prometheus base URL (`/api/v1/query` is added). `http` or `https`, no credentials in the URL. |
| `query` | required | A PromQL instant query, up to 1000 characters. It must return **one** value: aggregate with `sum()`, `avg()`, `max()` or `histogram_quantile()`. |
| `operator`, `threshold` | required | The check passes while `value <operator> threshold` (`<`, `<=`, `>`, `>=`). |
| `timeout_seconds` | 5 | 1 to 30. |
| `on_no_data` | `fail` | When the query returns nothing (or NaN, as `0/0` does with no traffic): `fail` counts it as *unavailable*, `pass` counts it as passing. |
| `bearer_token_env` | none | The **name** of an environment variable holding a bearer token. The token itself never goes in the config. |

A query that cannot be answered is *unavailable*, not a breach: the server is down
or slow, it returned an HTTP error or a malformed or oversized response (over 1 MiB),
the query was rejected, it returned several series, or the token variable is not
set. Redirects are not followed. Only the status code or error type is recorded,
never a server message or the token.

The query runs on a Prometheus you chose in the config, and the API cannot create or
change experiments, so a caller can only run queries you wrote.

## Metrics

| Metric | Meaning |
| --- | --- |
| `chaos_experiments_total{status,verdict}` | Finished experiments. |
| `chaos_experiment_active` | 1 while an experiment is running. |
| `chaos_slo_aborts_total{kind}` | Experiments stopped by their SLO. `kind` is `breach` or `unavailable`. |
