import os
import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

import psutil
from ..metrics import INJECTIONS_TOTAL
from ..logging_config import get_logger

logger = get_logger(__name__)

# Critical system processes that should NEVER be killed
CRITICAL_PROCESSES = {
    # Init systems
    "systemd",
    "init",
    "launchd",
    # Container runtimes
    "dockerd",
    "containerd",
    "containerd-shim",
    "runc",
    "crio",
    "podman",
    # Kubernetes
    "kubelet",
    "kube-proxy",
    "kube-apiserver",
    "kube-controller",
    "kube-scheduler",
    # Network & SSH
    "sshd",
    "networkd",
    "networkmanager",
    # System critical
    "dbus-daemon",
    "rsyslogd",
    "journald",
    "udevd",
    # Container pause/infra
    "pause",
    "tini",
}

# A critical name matches an executable name only as a whole word, where a word
# is bounded by anything that is not a letter or digit. So "systemd-journald",
# "containerd-shim-runc-v2" and "dumb-init" are protected, while "initialize"
# and "truncate-job" are not (they merely contain "init" and "runc").
_CRITICAL_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(
        sorted((re.escape(n) for n in CRITICAL_PROCESSES), key=len, reverse=True)
    )
    + r")(?![a-z0-9])"
)

# Programs that run a script, module or jar. For these the thing a person means
# by "the app" is the script, not the interpreter.
_INTERPRETER_RE = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|java|node|nodejs|deno|bun|ruby|perl|php|"
    r"bash|sh|dash|zsh|ksh)$"
)
# Options of interpreters that take a separate value, so it is not mistaken for
# the script name.
_VALUE_OPTIONS = {
    "python": {"-W", "-X", "-Q"},
    "java": {"-cp", "-classpath", "--class-path", "-p", "--module-path"},
    "node": {"-r", "--require", "--loader", "--import"},
}

# Linux truncates the kernel's process name (comm) to 15 characters.
_COMM_MAX = 15

# Overly broad target names that are too generic to safely use
PROHIBITED_TARGETS = {
    "python",
    "python3",
    "java",
    "node",
    "sh",
    "bash",
    "zsh",
    "ksh",
    "systemd",
    "init",
    "root",
    "kubelet",
    "dockerd",
    "containerd",
}


def validate_target_name(target_name: str) -> tuple[bool, str]:
    """
    Validate that target_name is safe and specific enough.

    Returns:
        tuple: (is_valid: bool, error_message: str)
    """
    if not target_name or not target_name.strip():
        return False, "Target name cannot be empty"

    target_lower = target_name.lower().strip()

    # Check against prohibited list
    if target_lower in PROHIBITED_TARGETS:
        logger.warning(
            "Target name rejected - too broad",
            extra={
                "target_name": target_name,
                "reason": "prohibited_target",
                "suggestion": "Use a more specific application name",
            },
        )
        return False, (
            f"Target name '{target_name}' is too broad and could kill critical processes. "
            f"Use a more specific application name (e.g., 'myapp' instead of 'python')"
        )

    # Require minimum length for specificity
    if len(target_lower) < 3:
        logger.warning(
            "Target name rejected - too short",
            extra={
                "target_name": target_name,
                "length": len(target_lower),
                "reason": "too_short",
            },
        )
        return False, (
            f"Target name '{target_name}' is too short (min 3 chars). "
            f"Use a specific application name to avoid accidental kills"
        )

    logger.debug("Target name validation passed", extra={"target_name": target_name})
    return True, ""


def _base(path: Optional[str]) -> str:
    """Lower-cased last path component, or "" if there is none."""
    if not path:
        return ""
    return PurePosixPath(str(path)).name.lower()


def executable_names(
    proc_name: Optional[str], cmdline: Optional[list], exe: Optional[str] = None
) -> Set[str]:
    """
    The names a process runs under: the kernel's process name, the executable's
    file name, and the file name of argv[0]. Deliberately NOT the arguments: a
    process is not "sshd" because its command line mentions sshd.
    """
    names = {
        (proc_name or "").lower(),
        _base(exe),
        _base(cmdline[0] if cmdline else ""),
    }
    names.discard("")
    return names


def is_critical_process(
    proc_name: Optional[str], cmdline: Optional[list], exe: Optional[str] = None
) -> bool:
    """
    Check if a process is critical to system operation.

    Matches the process's executable names (not its arguments) against the
    critical list as whole words. See _CRITICAL_RE.

    Args:
        proc_name: Process name
        cmdline: Process command line arguments (only argv[0] is used)
        exe: Path of the executable, if known

    Returns:
        bool: True if process is critical and should never be killed
    """
    return any(
        _CRITICAL_RE.search(n) for n in executable_names(proc_name, cmdline, exe)
    )


def script_names(cmdline: Optional[list]) -> Set[str]:
    """
    For an interpreter (python, java, node, a shell, ...) the script, module or
    jar it was started with, with and without its extension. Empty for any other
    program. `python -u target-app.py` yields {"target-app.py", "target-app"}.
    """
    if not cmdline:
        return set()
    interpreter = _base(cmdline[0])
    if not _INTERPRETER_RE.match(interpreter):
        return set()

    family = (
        "python"
        if interpreter.startswith(("python", "pypy"))
        else (
            "java"
            if interpreter == "java"
            else (
                "node"
                if interpreter.startswith("node") or interpreter in ("deno", "bun")
                else ""
            )
        )
    )
    value_options = _VALUE_OPTIONS.get(family, set())

    args = cmdline[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if family == "python" and arg == "-m" and i + 1 < len(args):
            module = args[i + 1].lower()
            return {module, module.rsplit(".", 1)[-1]}
        if family == "java" and arg == "-jar" and i + 1 < len(args):
            return _with_stem(_base(args[i + 1]))
        if arg in ("-c", "-e", "--eval", "--print"):
            return set()  # inline code: there is no script name
        if arg in value_options:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return _with_stem(_base(arg))
    return set()


def _with_stem(name: str) -> Set[str]:
    if not name:
        return set()
    stem = PurePosixPath(name).stem
    return {name, stem} if stem else {name}


def match_kind(
    target_name: str,
    proc_name: Optional[str],
    cmdline: Optional[list],
    exe: Optional[str] = None,
) -> Optional[str]:
    """
    How `target_name` matches a process, or None if it does not.

    The match is EXACT (case-insensitive) against the process's names, never a
    substring and never the arguments:
      "name"   the process name, executable, or argv[0]
      "script" the script, module or jar an interpreter was started with
    """
    target = target_name.lower().strip()
    if not target:
        return None
    if target in executable_names(proc_name, cmdline, exe):
        return "name"
    # comm is cut to 15 characters, so a longer target is compared truncated
    if len(target) > _COMM_MAX and (proc_name or "").lower() == target[:_COMM_MAX]:
        return "name"
    if target in script_names(cmdline):
        return "script"
    return None


def is_pid_1(proc: psutil.Process) -> bool:
    """
    True if this is PID 1 in our PID namespace.

    That is the init process of the namespace this agent shares with the target
    (a Compose `pid: service:...` setup, for example). It is never a safe target;
    see PID1_REFUSAL.
    """
    try:
        return proc.pid == 1
    except (AttributeError, TypeError):
        return False


PID1_REFUSAL = (
    "PID 1 of the shared PID namespace cannot be killed from here. The kernel "
    "discards SIGKILL sent from inside the namespace, and any signal PID 1 has "
    "no handler for, so the kill would do nothing; and if PID 1 did exit, the "
    "kernel would kill every process in the namespace, including this agent. "
    "Run the target under an init (Docker Compose `init: true`, or tini) so the "
    "application is not PID 1."
)


def _protected_pids() -> Set[int]:
    """This agent, everything above it, and everything below it."""
    my_pid = os.getpid()
    protected = {my_pid, os.getppid()}
    try:
        me = psutil.Process(my_pid)
    except (psutil.Error, TypeError) as e:
        logger.warning("Could not inspect own process", extra={"error": str(e)})
        return protected

    for label, getter in (
        ("children", lambda: me.children(recursive=True)),
        ("parents", lambda: me.parents()),
    ):
        try:
            protected.update(p.pid for p in getter())
        except (psutil.Error, AttributeError) as e:
            logger.warning(f"Could not enumerate own {label}", extra={"error": str(e)})
    return protected


def find_target_processes(
    target_name: Optional[str] = None, target_pid: Optional[int] = None
) -> Tuple[List[psutil.Process], Dict[str, int]]:
    """
    Find the processes that match, with scan statistics.

    A process matches if it is not protected and it matches `target_name`
    exactly (see match_kind), `target_pid`, or both when both are given. The
    agent itself, its ancestors and its descendants, zombies, kernel threads
    (no command line) and critical system processes are never candidates.
    """
    my_pid = os.getpid()
    protected = _protected_pids()
    logger.debug(
        "Scanning for target processes",
        extra={
            "target_name": target_name,
            "target_pid": target_pid,
            "protected_pids": sorted(protected),
        },
    )

    stats = {"scanned": 0, "protected": 0, "critical": 0, "unreadable": 0}
    matches: List[psutil.Process] = []

    try:
        for proc in psutil.process_iter(
            ["pid", "name", "exe", "cmdline", "ppid", "create_time", "status"]
        ):
            stats["scanned"] += 1
            try:
                info = proc.info
                pid = info.get("pid")

                if pid in protected or info.get("ppid") == my_pid:
                    stats["protected"] += 1
                    continue
                if info.get("status") == psutil.STATUS_ZOMBIE:
                    continue

                proc_name = info.get("name") or ""
                cmdline = info.get("cmdline") or []
                exe = info.get("exe")

                # No command line: a kernel thread, or one we may not read.
                if not cmdline:
                    stats["unreadable"] += 1
                    continue

                if is_critical_process(proc_name, cmdline, exe):
                    stats["critical"] += 1
                    logger.debug(
                        "Skipping critical system process",
                        extra={
                            "process_name": proc_name,
                            "pid": pid,
                            "reason": "critical_process",
                        },
                    )
                    continue

                if target_pid is not None and pid != target_pid:
                    continue
                if target_name:
                    kind = match_kind(target_name, proc_name, cmdline, exe)
                    if kind is None:
                        continue
                    logger.debug(
                        "Found matching process",
                        extra={
                            "process_name": proc_name,
                            "pid": pid,
                            "match_type": kind,
                        },
                    )
                matches.append(proc)

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ) as e:
                logger.debug(
                    "Could not access process",
                    extra={
                        "pid": proc.info.get("pid"),
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                )
    except Exception as e:
        logger.error("Error scanning processes", exc_info=True, extra={"error": str(e)})

    logger.info(
        "Process scan completed",
        extra={
            "target_name": target_name,
            "target_pid": target_pid,
            "scanned_count": stats["scanned"],
            "skipped_protected": stats["protected"],
            "skipped_critical": stats["critical"],
            "skipped_unreadable": stats["unreadable"],
            "matches_found": len(matches),
        },
    )
    return matches, stats


def get_safe_target_processes(target_name, target_pid: Optional[int] = None):
    """Matching, safe-to-kill processes (see find_target_processes)."""
    return find_target_processes(target_name, target_pid)[0]


def _pid_of(proc) -> Optional[int]:
    pid = proc.info.get("pid", getattr(proc, "pid", None))
    return pid if isinstance(pid, int) else None


def select_target(candidates: list, selection: str = "oldest"):
    """
    Choose ONE process deterministically.

    Returns (chosen, candidates in order). Candidates are ordered by start time
    then PID, so the result never depends on the order the OS lists processes.
    "oldest" (default) is usually the parent or master of a group of workers;
    "newest" is usually the most recently started worker.
    """
    ordered = sorted(
        candidates,
        key=lambda p: (p.info.get("create_time") or 0, _pid_of(p) or 0),
    )
    chosen = ordered[-1] if selection == "newest" else ordered[0]
    return chosen, ordered


def _describe(proc) -> Dict[str, object]:
    cmdline = proc.info.get("cmdline") or []
    return {
        "pid": _pid_of(proc),
        "process_name": proc.info.get("name"),
        "cmdline": " ".join(cmdline)[:100] if cmdline else "N/A",
        "started": proc.info.get("create_time"),
    }


def terminate_process(proc, target_name: str) -> bool:
    """
    Terminate one process and report truthfully.

    Returns True only if the process is actually gone. A refusal (PID 1) or a
    survivor is counted as `failed` and logged with the reason.
    """
    pid = None
    name = None
    try:
        pid = _pid_of(proc)
        name = proc.info.get("name")
        cmdline = (
            " ".join(proc.info.get("cmdline") or [])
            if proc.info.get("cmdline")
            else "N/A"
        )

        if is_pid_1(proc):
            logger.error(
                "Refusing to kill PID 1 of the shared PID namespace",
                extra={
                    "pid": pid,
                    "process_name": name,
                    "status": "failed",
                    "reason": PID1_REFUSAL,
                },
            )
            INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
            return False

        logger.info(
            "Initiating process termination",
            extra={
                "target_name": target_name,
                "process_name": name,
                "pid": pid,
                "cmdline": cmdline[:100],
                "operation": "process_kill",
            },
        )

        # SIGTERM first (graceful)
        logger.debug(f"Sending SIGTERM to process {pid}")
        proc.terminate()

        try:
            proc.wait(timeout=3)
            logger.info(
                "Process terminated gracefully",
                extra={
                    "pid": pid,
                    "process_name": name,
                    "method": "SIGTERM",
                    "status": "success",
                },
            )
            INJECTIONS_TOTAL.labels(failure_type="process", status="success").inc()
            return True

        except psutil.TimeoutExpired:
            logger.warning(
                "Process did not terminate gracefully, force killing",
                extra={"pid": pid, "process_name": name},
            )
            proc.kill()

            try:
                proc.wait(timeout=2)
                logger.info(
                    "Process force killed",
                    extra={
                        "pid": pid,
                        "process_name": name,
                        "method": "SIGKILL",
                        "status": "success",
                    },
                )
                INJECTIONS_TOTAL.labels(failure_type="process", status="success").inc()
                return True

            except psutil.TimeoutExpired:
                logger.error(
                    "Process survived SIGKILL",
                    extra={
                        "pid": pid,
                        "process_name": name,
                        "status": "failed",
                        "note": "This is highly unusual and may indicate a zombie process",
                    },
                )
                INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
                return False

    except psutil.NoSuchProcess:
        logger.info(
            "Process disappeared before kill could complete",
            extra={"pid": pid, "status": "skipped"},
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
        return False

    except psutil.AccessDenied:
        logger.error(
            "Access denied when attempting to kill process",
            extra={
                "pid": pid,
                "process_name": name,
                "status": "failed",
                "error_type": "AccessDenied",
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
        return False


def inject_process(config: dict, dry_run: bool = False):
    """
    Kill ONE process, chosen deterministically.

    Config:
        target_name:      exact process, executable, script, module or jar name
        target_pid:       exact PID (for manual injections). With target_name,
                          the process must match both.
        target_selection: "oldest" (default) or "newest" when several match
    """
    target_name = config.get("target_name")
    target_pid = config.get("target_pid")
    selection = config.get("target_selection") or "oldest"

    if not target_name and target_pid is None:
        logger.warning("Process injection called without target_name or target_pid")
        return

    # VALIDATION: Check if target name is safe
    if target_name:
        is_valid, error_msg = validate_target_name(target_name)
        if not is_valid:
            logger.error(
                "Invalid target name for process injection",
                extra={
                    "target_name": target_name,
                    "validation_error": error_msg,
                    "status": "failed",
                },
            )
            INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
            return

    candidates = get_safe_target_processes(target_name, target_pid)

    if not candidates:
        logger.info(
            "No killable processes found matching target",
            extra={
                "target_name": target_name,
                "target_pid": target_pid,
                "status": "skipped",
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
        return

    target, ordered = select_target(candidates, selection)

    if len(ordered) > 1:
        logger.info(
            "Several processes match; choosing one",
            extra={
                "target_name": target_name,
                "selection": selection,
                "chosen_pid": _pid_of(target),
                "candidates": [_describe(p) for p in ordered],
            },
        )

    try:
        if dry_run:
            logger.info(
                "Process kill (DRY RUN)",
                extra={
                    "target_name": target_name,
                    **_describe(target),
                    "dry_run": True,
                    "selection": selection,
                    "candidate_count": len(ordered),
                    "is_pid_1": is_pid_1(target),
                    "would_be_refused": is_pid_1(target),
                },
            )
            INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
            return

        terminate_process(target, target_name or f"pid {target_pid}")

    except Exception as e:
        logger.error(
            "Process kill failed with unexpected error",
            exc_info=True,
            extra={
                "target_name": target_name,
                "pid": _pid_of(target),
                "error": str(e),
                "error_type": type(e).__name__,
                "status": "failed",
            },
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
