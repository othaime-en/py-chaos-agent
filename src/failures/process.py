import os
import psutil
from ..metrics import INJECTIONS_TOTAL

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
}

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
        return False, (
            f"Target name '{target_name}' is too broad and could kill critical processes. "
            f"Use a more specific application name (e.g., 'myapp' instead of 'python')"
        )
    
    # Require minimum length for specificity
    if len(target_lower) < 3:
        return False, (
            f"Target name '{target_name}' is too short (min 3 chars). "
            f"Use a specific application name to avoid accidental kills"
        )
    
    return True, ""


def is_critical_process(proc_name: str, cmdline: list) -> bool:
    """
    Check if a process is critical to system operation.
    
    Args:
        proc_name: Process name
        cmdline: Process command line arguments
        
    Returns:
        bool: True if process is critical and should never be killed
    """
    proc_name_lower = proc_name.lower() if proc_name else ""
    
    # Check against critical process list
    if proc_name_lower in CRITICAL_PROCESSES:
        return True
    
    # Check command line for critical indicators
    if cmdline:
        cmdline_str = " ".join(cmdline).lower()
        for critical in CRITICAL_PROCESSES:
            if critical in cmdline_str:
                return True
    
    return False


def get_safe_target_processes(target_name):
    """
    Find target processes while excluding the chaos agent itself.

    Returns a list of safe-to-kill processes that match the target name.
    """
    my_pid = os.getpid()
    my_parent_pid = os.getppid()

    # Get all PIDs in our process tree to avoid killing ourselves
    protected_pids = {my_pid, my_parent_pid}
    try:
        my_process = psutil.Process(my_pid)
        # Add all our children to protected list
        for child in my_process.children(recursive=True):
            protected_pids.add(child.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    safe_targets = []

    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            try:
                # Skip if this is a protected process
                if proc.info["pid"] in protected_pids:
                    continue

                # Skip if parent is the chaos agent (our direct children)
                if proc.info["ppid"] == my_pid:
                    continue

                proc_name = proc.info["name"] or ""
                cmdline = proc.info["cmdline"] or []
                
                # CRITICAL: Check if this is a system-critical process
                if is_critical_process(proc_name, cmdline):
                    print(
                        f"[PROCESS] Skipping critical system process: "
                        f"{proc_name} (PID: {proc.info['pid']})"
                    )
                    continue

                # Match by process name
                if target_name.lower() in proc_name.lower():
                    safe_targets.append(proc)
                    continue

                # Also try matching by command line for better targeting
                if cmdline:
                    cmdline_str = " ".join(cmdline).lower()
                    if target_name.lower() in cmdline_str:
                        # Additional check: don't kill if cmdline contains 'chaos'
                        if "chaos" not in cmdline_str and "agent.py" not in cmdline_str:
                            safe_targets.append(proc)

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # Process disappeared or we don't have access - skip it
                continue

    except Exception as e:
        print(f"[PROCESS] Error scanning processes: {e}")

    return safe_targets


def inject_process(config: dict, dry_run: bool = False):
    target_name = config.get("target_name")
    if not target_name:
        print("[PROCESS] No target_name specified in config")
        return

    # VALIDATION: Check if target name is safe
    is_valid, error_msg = validate_target_name(target_name)
    if not is_valid:
        print(f"[PROCESS] Invalid target name: {error_msg}")
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
        return

    # Find safe target processes
    target_procs = get_safe_target_processes(target_name)

    if not target_procs:
        print(f"[PROCESS] No killable process named '{target_name}' found")
        INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
        return

    # Kill only the first match for controlled chaos
    target = target_procs[0]

    try:
        pid = target.pid
        name = target.info["name"]
        cmdline = (
            " ".join(target.info.get("cmdline", []))
            if target.info.get("cmdline")
            else "N/A"
        )

        if dry_run:
            print(f"[DRY RUN] Would kill process '{name}' (PID: {pid})")
            print(f"[DRY RUN] Command line: {cmdline}")
            INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
            return

        print(f"[PROCESS] Killing '{name}' (PID: {pid})...")
        print(f"[PROCESS] Command line: {cmdline[:100]}...")  # Truncate long cmdlines

        # Use terminate() first (SIGTERM) - graceful
        target.terminate()

        # Wait briefly for graceful termination
        try:
            target.wait(timeout=3)
            print(f"[PROCESS] Process {pid} terminated gracefully")
        except psutil.TimeoutExpired:
            # If still alive after 3s, force kill (SIGKILL)
            print(f"[PROCESS] Process {pid} didn't terminate, force killing...")
            target.kill()
            target.wait(timeout=2)

        INJECTIONS_TOTAL.labels(failure_type="process", status="success").inc()

    except psutil.NoSuchProcess:
        print(f"[PROCESS] Process disappeared before kill (PID: {pid})")
        INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
    except psutil.AccessDenied:
        print(f"[PROCESS] Access denied killing process (PID: {pid})")
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
        print(f"[PROCESS] Failed: {e}")