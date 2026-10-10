# Blast Radius and Resource Limits

This page explains what each failure can actually affect, what keeps it
contained, and what does not.

## Short version

- CPU and memory injections run **inside the chaos-agent container**. If that
  container has CPU and memory limits, the kernel enforces them, so an injection
  cannot take more than the container's limit from the node.
- On top of the kernel's limits, the agent reads them and sizes injections to a
  **fraction** of what is available. CPU requests above the budget are
  **clamped**. Memory requests above the budget are **refused**.
- Without container limits, none of this protection exists. The agent warns, and
  can be told to refuse (`safety.require_cgroup_limits`).
- Limits contain the *cost to the cluster*. They do not make the *target* feel
  resource starvation directly. See [What limits do not do](#what-limits-do-not-do).

## What each failure affects

| Failure | Directly affects | Bounded by |
| --- | --- | --- |
| CPU | The agent container's CPU. The target only feels it through contention for shared node CPU. | Container CPU limit x `cpu_fraction` (at least 1 core), then the hard ceiling of 16 cores |
| Memory | The agent container's memory. The target only feels it through node-level memory pressure. | Free memory under the container limit x `memory_fraction`, then the hard ceiling of 2048 MB |
| Process kill | Processes whose name matches `target_name` in the shared PID namespace, so this pod only | Name validation and the critical-process list. Not a resource limit. |
| Network latency | The pod's network interface, so **every container in the pod**, including the agent's own API and the Kubernetes probes | `delay_ms` up to 10000, `duration_seconds` up to 300, validated interface name |

## How the budget is computed

At the moment of each injection the agent reads its own cgroup (v2 or v1,
including parent cgroups such as the pod, using the tightest limit found):

- **CPU:** `max_cores = max(1, floor(cpu_limit x cpu_fraction))`, capped at 16.
  If there is no CPU limit it uses the number of CPUs it may run on.
- **Memory:** `headroom = memory_limit - working_set`, where the working set is
  usage minus reclaimable file cache (the measure the kubelet uses for eviction).
  The budget is `headroom x memory_fraction`, capped at 2048 MB. It is also capped
  by the host's reported available memory.
- **Concurrent injections** (the loop plus manual API calls) share one budget
  through reservations, so two requests that are each safe cannot add up to an
  unsafe one.

Example: a container with a 1 CPU and 512 MiB limit, using about 70 MiB, has a
budget of 1 core and about 220 MB. A request for `cores: 4` runs with 1 and logs
a warning. A request for `mb: 300` is refused with the reason in the log.

Why clamp CPU but refuse memory: fewer CPU workers still produce load, so
reducing is harmless. A smaller memory allocation silently runs a different
experiment than the one you asked for, and an oversized one risks an OOM kill.

## Setting it up

Set limits on the chaos-agent container. The Kubernetes demo manifest and the
Compose file already do this:

```yaml
# Kubernetes
resources:
  requests: { cpu: 100m, memory: 128Mi }
  limits:   { cpu: "1",  memory: 512Mi }
```

```yaml
# Docker Compose
cpus: 1.0
mem_limit: 512m
pids_limit: 256
```

Then tune the `safety` section of `config.yaml`:

| Setting | Default | Range | Meaning |
| --- | --- | --- | --- |
| `cpu_fraction` | 0.8 | 0.1 to 0.9 | Share of the CPU limit injections may use |
| `memory_fraction` | 0.5 | 0.1 to 0.8 | Share of free memory injections may use |
| `require_cgroup_limits` | false | true or false | Refuse CPU and memory injections when the container has no limit |

Use `require_cgroup_limits: true` in Kubernetes and any shared environment. The
demo manifest does.

These settings can only be changed in the config file. The API rejects any
attempt to change them, so a remote caller cannot loosen them.

## Checking what the agent sees

```bash
curl -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/limits
```

```json
// abbreviated
{
  "cgroup": {"version": 2, "cpu_limit_cores": 1.0, "memory_limit_mb": 512, "memory_working_set_mb": 71},
  "budget": {"max_cores": 1, "max_memory_mb": 220, "cpu_limited_by": "cgroup", "memory_limited_by": "cgroup"},
  "reserved": {"cores": 0, "memory_mb": 0},
  "warnings": []
}
```

`warnings` is non-empty when no limit is detected. `POST /inject/manual` checks
the same budget first: an unsafe memory request returns 422 with the reason, and a
clamped CPU request returns `clamped_cores` in the response.

Clamped injections are counted in the Prometheus metric
`chaos_injections_clamped_total{failure_type="cpu"}`. Refusals are counted as
`chaos_injections_total{status="failed"}`.

## Without container limits

If the container has no limit, the agent cannot know what is safe for the node. It
falls back to the host's CPU count and available memory, logs a warning at
startup, and lists the warning under `/limits`. In that state a CPU injection
competes with every pod on the node. Set limits, or set
`require_cgroup_limits: true` so the agent refuses instead.

## What limits do not do

The agent runs in its own cgroup, separate from the target's. That has a
consequence worth understanding:

- A CPU burn or memory allocation inside the agent container does **not** consume
  the target container's CPU or memory limit. The target is affected only when the
  node itself is short of CPU or memory.
- So these two failures test "how does my app behave on a busy or
  memory-pressured node", not "how does my app behave when it hits its own
  limits".
- Exercising the target's own limits would require running inside the target's
  cgroup. That is not supported today.

Process kill and network latency are different: they act on the target (or the
whole pod) directly, and are bounded by validation rather than by resource limits.

For what happens to an injection's effect when the agent stops or is killed, see
[Shutdown and Cleanup](shutdown-and-cleanup.md).
