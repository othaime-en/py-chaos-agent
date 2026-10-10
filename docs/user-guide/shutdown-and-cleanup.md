# Shutdown and Cleanup

What happens to an injection's effect when something stops it, and the one case
that no code can fully cover.

## Guarantees by scenario

| What happens | Result |
| --- | --- |
| `SIGTERM` or `SIGINT` (standalone agent or API server) | In-flight injections are woken and aborted, the `tc` rule is removed, CPU workers are stopped, and the process exits promptly. It does not wait out the injection. |
| `POST /agent/stop` | The loop stops and any running injection is aborted. Its effect is removed within milliseconds, not at the end of its duration. |
| `POST /inject/abort` | Aborts every running injection (manual or from the loop). The loop keeps running. |
| Kill switch trips | The loop stops and running injections are aborted. |
| Normal interpreter exit or an unhandled exception | Cleanups run from an `atexit` hook. |
| `SIGKILL`, OOM kill, node failure | **No code can run.** See [After a hard kill](#after-a-hard-kill). |

The API server needed special handling. uvicorn's graceful shutdown waits for
running background tasks, and an injection can last 5 minutes. Without a signal
hook, `SIGTERM` during an injection would wait it out, and Kubernetes would
`SIGKILL` the pod after its grace period with the rule still applied. The server
now cleans up the moment the signal arrives, before uvicorn begins its own
shutdown.

## After a hard kill

If the agent is killed without warning while a network rule is applied, the rule
stays in the pod's network namespace. In Kubernetes that namespace belongs to the
pod, so a restarted container inherits it, and your target keeps its added latency.

To repair this, the agent checks at **startup** whether the interface in its
`network` config still has a netem rule, and removes it (it logs
`Removed stale network rule left by a previous run`). This runs whether or not the
network failure is currently enabled.

What this means in practice:

- Until the agent starts again, the latency remains. If the container never
  restarts, remove it yourself:

  ```bash
  kubectl -n chaos-demo exec deploy/resilient-app -c chaos-agent -- tc qdisc del dev eth0 root
  ```

- Deleting the pod always clears it, because the network namespace goes with it.
- The agent treats a **netem** root qdisc on the configured interface as its own.
  It never removes other kinds of root qdisc during cleanup or repair.

CPU workers are the other process-level leftover. They are daemons and check once
every 0.2 seconds that their parent is alive, so a killed agent does not leave
workers burning CPU until their duration runs out. Memory is returned by the
kernel when the process dies.

## One injection per type

Only one injection of each type (`cpu`, `memory`, `network`) runs at a time.
Overlapping ones used to be able to corrupt each other's cleanup.

- A request that arrives while one is running is **skipped**: logged with the
  reason and counted as `chaos_injections_total{status="skipped"}`. The agent loop
  simply moves on.
- `POST /inject/manual` returns **409** (`A network injection is already running`),
  or **503** while the agent is shutting down.
- Dry runs are never blocked.
- Process kills are instantaneous and are not guarded.
- `GET /status` lists what is running under `active_injections`.

Every `tc` command and every change to rule ownership happens under one lock, and
a rule is removed only by the injection that applied it (or by shutdown). A late
cleanup from an old injection can therefore never delete a rule that a newer one
applied.

## Known gaps

- **Latency persists after a hard kill until the next agent start** (above).
- **The managed interface's root qdisc is replaced during an injection.** If
  something else (a CNI bandwidth plugin, for example) had a root qdisc on that
  interface, it is removed when the injection starts and is not restored when it
  ends. This predates this change.
- **Aborted injections still count as `success`** in the metrics, since the effect
  was applied.
- **`--reload` (development only)** uses uvicorn's reloader, which bypasses the
  signal hook. The lifespan and `atexit` cleanups still run.

## Verifying it yourself

With Docker (needs the `netem` kernel module on the host):

```bash
export CHAOS_API_TOKEN=$(openssl rand -hex 32)
docker run -d --name chaos --cap-add NET_ADMIN --cap-add KILL \
  -p 127.0.0.1:9000:9000 -e CHAOS_API_TOKEN -e CHAOS_API_HOST=0.0.0.0 \
  -v $PWD/config.yaml:/app/config.yaml:ro py-chaos-agent:latest

# start a 5 minute network injection, then confirm the rule exists
curl -H "Authorization: Bearer $CHAOS_API_TOKEN" -H 'Content-Type: application/json' \
  -X POST localhost:9000/inject/manual \
  -d '{"failure_type":"network","config":{"duration_seconds":300}}'
docker exec chaos tc qdisc show dev eth0      # shows a netem rule

# SIGTERM mid-injection: the container should stop within a second or two
docker stop chaos
```

Then repeat with `docker kill chaos` (SIGKILL) and `docker start chaos`, and check
the log for `Removed stale network rule left by a previous run`.
