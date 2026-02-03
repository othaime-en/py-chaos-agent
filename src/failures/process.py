import os
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
        logger.warning(
            'Target name rejected - too broad',
            extra={
                'target_name': target_name,
                'reason': 'prohibited_target',
                'suggestion': 'Use a more specific application name'
            }
        )
        return False, (
            f"Target name '{target_name}' is too broad and could kill critical processes. "
            f"Use a more specific application name (e.g., 'myapp' instead of 'python')"
        )

    # Require minimum length for specificity
    if len(target_lower) < 3:
        logger.warning(
            'Target name rejected - too short',
            extra={
                'target_name': target_name,
                'length': len(target_lower),
                'reason': 'too_short'
            }
        )
        return False, (
            f"Target name '{target_name}' is too short (min 3 chars). "
            f"Use a specific application name to avoid accidental kills"
        )

    logger.debug(
        'Target name validation passed',
        extra={'target_name': target_name}
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

    logger.debug(
        'Scanning for target processes',
        extra={
            'target_name': target_name,
            'my_pid': my_pid,
            'my_parent_pid': my_parent_pid
        }
    )

    # Get all PIDs in our process tree to avoid killing ourselves
    protected_pids = {my_pid, my_parent_pid}
    try:
        my_process = psutil.Process(my_pid)
        # Add all our children to protected list
        for child in my_process.children(recursive=True):
            protected_pids.add(child.pid)
        
        logger.debug(
            'Protected PIDs identified',
            extra={'protected_pids': list(protected_pids)}
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.warning(
            'Could not enumerate child processes',
            extra={'error': str(e)}
        )

    safe_targets = []
    scanned_count = 0
    skipped_protected = 0
    skipped_critical = 0

    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            scanned_count += 1
            
            try:
                # Skip if this is a protected process
                if proc.info["pid"] in protected_pids:
                    skipped_protected += 1
                    continue

                # Skip if parent is the chaos agent (our direct children)
                if proc.info["ppid"] == my_pid:
                    skipped_protected += 1
                    continue

                proc_name = proc.info["name"] or ""
                cmdline = proc.info["cmdline"] or []

                # CRITICAL: Check if this is a system-critical process
                if is_critical_process(proc_name, cmdline):
                    skipped_critical += 1
                    logger.debug(
                        'Skipping critical system process',
                        extra={
                            'process_name': proc_name,
                            'pid': proc.info['pid'],
                            'reason': 'critical_process'
                        }
                    )
                    continue

                # Match by process name
                if target_name.lower() in proc_name.lower():
                    logger.debug(
                        'Found matching process by name',
                        extra={
                            'process_name': proc_name,
                            'pid': proc.info['pid'],
                            'match_type': 'name'
                        }
                    )
                    safe_targets.append(proc)
                    continue

                # Also try matching by command line for better targeting
                if cmdline:
                    cmdline_str = " ".join(cmdline).lower()
                    if target_name.lower() in cmdline_str:
                        # Additional check: don't kill if cmdline contains 'chaos'
                        if "chaos" not in cmdline_str and "agent.py" not in cmdline_str:
                            logger.debug(
                                'Found matching process by cmdline',
                                extra={
                                    'process_name': proc_name,
                                    'pid': proc.info['pid'],
                                    'cmdline': cmdline_str[:100],
                                    'match_type': 'cmdline'
                                }
                            )
                            safe_targets.append(proc)

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
                # Process disappeared or we don't have access - skip it
                logger.debug(
                    'Could not access process',
                    extra={
                        'pid': proc.info.get('pid'),
                        'error': str(e),
                        'error_type': type(e).__name__
                    }
                )
                continue

    except Exception as e:
        logger.error(
            'Error scanning processes',
            exc_info=True,
            extra={'error': str(e)}
        )

    logger.info(
        'Process scan completed',
        extra={
            'target_name': target_name,
            'scanned_count': scanned_count,
            'skipped_protected': skipped_protected,
            'skipped_critical': skipped_critical,
            'matches_found': len(safe_targets)
        }
    )

    return safe_targets


def inject_process(config: dict, dry_run: bool = False):
    target_name = config.get("target_name")
    
    if not target_name:
        logger.warning('Process injection called without target_name in config')
        return

    # VALIDATION: Check if target name is safe
    is_valid, error_msg = validate_target_name(target_name)
    if not is_valid:
        logger.error(
            'Invalid target name for process injection',
            extra={
                'target_name': target_name,
                'validation_error': error_msg,
                'status': 'failed'
            }
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
        return

    # Find safe target processes
    target_procs = get_safe_target_processes(target_name)

    if not target_procs:
        logger.info(
            'No killable processes found matching target',
            extra={
                'target_name': target_name,
                'status': 'skipped'
            }
        )
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
            logger.info(
                'Process kill (DRY RUN)',
                extra={
                    'target_name': target_name,
                    'process_name': name,
                    'pid': pid,
                    'cmdline': cmdline[:100],
                    'dry_run': True
                }
            )
            INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
            return

        logger.info(
            'Initiating process termination',
            extra={
                'target_name': target_name,
                'process_name': name,
                'pid': pid,
                'cmdline': cmdline[:100],
                'operation': 'process_kill'
            }
        )

        # Use terminate() first (SIGTERM) - graceful
        logger.debug(f'Sending SIGTERM to process {pid}')
        target.terminate()

        # Wait briefly for graceful termination
        try:
            target.wait(timeout=3)
            logger.info(
                'Process terminated gracefully',
                extra={
                    'pid': pid,
                    'process_name': name,
                    'method': 'SIGTERM',
                    'status': 'success'
                }
            )
        except psutil.TimeoutExpired:
            # If still alive after 3s, force kill (SIGKILL)
            logger.warning(
                'Process did not terminate gracefully, force killing',
                extra={'pid': pid, 'process_name': name}
            )
            target.kill()
            target.wait(timeout=2)
            logger.info(
                'Process force killed',
                extra={
                    'pid': pid,
                    'process_name': name,
                    'method': 'SIGKILL',
                    'status': 'success'
                }
            )

        INJECTIONS_TOTAL.labels(failure_type="process", status="success").inc()

    except psutil.NoSuchProcess:
        logger.info(
            'Process disappeared before kill could complete',
            extra={
                'pid': pid,
                'process_name': name,
                'status': 'skipped'
            }
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="skipped").inc()
        
    except psutil.AccessDenied:
        logger.error(
            'Access denied when attempting to kill process',
            extra={
                'pid': pid,
                'process_name': name,
                'status': 'failed',
                'error_type': 'AccessDenied'
            }
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
        
    except Exception as e:
        logger.error(
            'Process kill failed with unexpected error',
            exc_info=True,
            extra={
                'target_name': target_name,
                'pid': pid if 'pid' in locals() else None,
                'error': str(e),
                'error_type': type(e).__name__,
                'status': 'failed'
            }
        )
        INJECTIONS_TOTAL.labels(failure_type="process", status="failed").inc()
