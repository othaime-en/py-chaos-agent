# Process Targeting

How the process failure decides which process to kill, and what it will never kill.

## Short version

- `target_name` is matched **exactly** (case-insensitive) against the process's
  names. It is never a substring, and it never looks at command-line arguments.
- It kills **one** process, chosen the same way every time.
- It will never kill the agent, anything above or below the agent, critical system
  processes, or PID 1.
- It reports success only if the process is actually gone.

## What `target_name` matches

A process matches if `target_name` equals any of these (ignoring case):

| Source | Example process | Matches |
| --- | --- | --- |
| The process name | `nginx` | `nginx` |
| The executable file name | `/opt/bin/billing` | `billing` |
| `argv[0]`'s file name | `/usr/bin/myapp --port 80` | `myapp` |
| The script, module or jar of an interpreter | `python -u target-app.py` | `target-app` or `target-app.py` |
| | `python -m src.api_server` | `src.api_server` or `api_server` |
| | `java -jar /opt/Billing.jar` | `billing` or `billing.jar` |

Interpreters recognised for the last row: `python`, `pypy`, `java`, `node`,
`nodejs`, `deno`, `bun`, `ruby`, `perl`, `php`, and the common shells.

Examples with `target_name: "target-app"`:

| Process | Match? | Why |
| --- | --- | --- |
| `python target-app.py` | Yes | script name |
| `target-app` | Yes | process name |
| `tail -f /var/log/target-app.log` | No | only an argument mentions it |
| `kubectl logs target-app` | No | argument |
| `target-app-helper` | No | a longer name, not equal |
| `vim target-app.py` | No | `vim` is not an interpreter |

Linux cuts a process's kernel name to 15 characters. A longer `target_name` is also
compared against that truncated form.

To see what the agent sees, run `ps -eo pid,comm,args` inside the agent container
and use the `comm`, the executable, or the script name. A dry run
(`POST /inject/manual` with `"dry_run": true`) logs the candidates and the choice.

## What is never killed

- The agent itself, its parent, and every ancestor and descendant.
- Zombies, and kernel threads (processes with no command line).
- **Critical system processes**: systemd, init, tini, dockerd, containerd, runc,
  kubelet and other `kube-*` components, sshd, NetworkManager, the Kubernetes
  `pause` process, and similar.
- **PID 1** of the PID namespace the agent shares (see below).

### How the critical list is matched

Against the process's **executable names only** (name, executable, `argv[0]`),
and only as a **whole word**, where a word is bounded by anything that is not a
letter or digit.

| Executable | Protected? |
| --- | --- |
| `systemd-journald`, `containerd-shim-runc-v2`, `dumb-init` | Yes (the critical name is a whole word) |
| `initialize`, `truncate-job`, `reinit`, `paused` | No (they only contain `init`, `runc`, `pause`) |
| `python initialize.py` | No (only the executable counts, not the script argument) |
| `pause-service`, `init-db` | Yes. A hyphen-separated word counts. Rename the executable if you need to target it |

Earlier versions matched the whole command line as a substring, so
`python initialize.py` and `truncate-job` were wrongly treated as critical.

## Choosing one process

When several processes match, exactly one is killed, and the choice never depends
on the order the operating system lists processes:

| `target_selection` | Picks |
| --- | --- |
| `oldest` (default) | The process that started first (ties break on the lower PID). Usually the parent or master. |
| `newest` | The process that started last. Usually the latest worker. |

All candidates and the chosen PID are logged at INFO (`Several processes match;
choosing one`).

To target one specific process, use `target_pid`. With both set, the process must
match both, which protects against PID reuse:

```bash
curl -s -H "$H" -H 'Content-Type: application/json' -X POST localhost:9000/inject/manual \
  -d '{"failure_type":"process","config":{"target_pid":4242}}'
```

`target_pid` must be 2 or higher. PIDs change between runs, so it is mainly for
manual injections.

## Termination

1. Send SIGTERM and wait up to 3 seconds.
2. If it is still running, send SIGKILL and wait up to 2 seconds.
3. Count the injection as `success` only if the process is gone. A process that
   survives SIGKILL, an access-denied error, or a refusal is counted as `failed`
   with the reason logged.

## PID 1 and Docker Compose

In Kubernetes (`shareProcessNamespace: true`) the pod's `pause` process is PID 1,
your app is PID 2 or higher, and everything works.

In Docker Compose the agent joins the target container's PID namespace
(`pid: "service:target-app"`). If the target is the container's main process, **it
is PID 1**, and two kernel rules make it impossible to kill from the agent:

1. A signal sent to PID 1 from inside its own PID namespace is discarded unless
   PID 1 has a handler for it. SIGKILL is always discarded. `kill` still reports
   success, so the old code logged a successful kill that never happened.
2. If PID 1 did exit, the kernel would kill **every process in the namespace**,
   including the agent.

So the agent refuses (`Refusing to kill PID 1 of the shared PID namespace`), counts
it as `failed`, and sends nothing. A dry run shows `would_be_refused: true`.

**Fix:** run the target under an init so the application is not PID 1. The Compose
file in this repo sets `init: true` on `target-app`, which puts `tini` at PID 1 and
the application at PID 2.

Even then, when the application dies, `tini` exits and the container with it. The
kernel tears down the namespace and the agent goes with it. The Compose file sets
`restart: always` on the agent so it comes back with the restarted target. In
Kubernetes this does not happen, because `pause` stays up when your app dies.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `No killable processes found matching target` | The name did not match exactly. Check `ps -eo pid,comm,args` in the agent container and use the `comm`, executable or script name. A substring or an argument no longer matches. |
| Worked before, finds nothing now | You relied on substring or argument matching. Use the exact name. |
| `Refusing to kill PID 1` | The target is PID 1 of the shared namespace. Run it under an init (`init: true` in Compose) or use Kubernetes. |
| `Invalid target name` | Too short, a generic name such as `python`, or an unsafe character. |
| The process you expected is not chosen | Several match. Check the `Several processes match` log, then set `target_selection` or `target_pid`. |
| A target named like `pause-service` is never found | Its executable contains a critical name as a whole word. Rename it. |
