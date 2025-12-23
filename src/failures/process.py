import os
import psutil
from ..metrics import INJECTIONS_TOTAL

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
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
            try:
                # Skip if this is a protected process
                if proc.info['pid'] in protected_pids:
                    continue
                
                # Skip if parent is the chaos agent (our direct children)
                if proc.info['ppid'] == my_pid:
                    continue
                
                # Match by process name
                proc_name = proc.info['name'] or ''
                if target_name.lower() in proc_name.lower():
                    safe_targets.append(proc)
                    continue
                
                # Also try matching by command line for better targeting
                cmdline = proc.info['cmdline']
                if cmdline:
                    cmdline_str = ' '.join(cmdline).lower()
                    if target_name.lower() in cmdline_str:
                        # Additional check: don't kill if cmdline contains 'chaos'
                        if 'chaos' not in cmdline_str and 'agent.py' not in cmdline_str:
                            safe_targets.append(proc)
                
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # Process disappeared or we don't have access - skip it
                continue
                
    except Exception as e:
        print(f"[PROCESS] Error scanning processes: {e}")
    
    return safe_targets

def inject_process(config: dict, dry_run: bool = False):
    target_name = config.get('target_name')
    if not target_name:
        print("[PROCESS] No target_name specified in config")
        return
    
    # Find safe target processes
    target_procs = get_safe_target_processes(target_name)
    
    if not target_procs:
        print(f"[PROCESS] No killable process named '{target_name}' found")
        INJECTIONS_TOTAL.labels(failure_type='process', status='skipped').inc()
        return
    
    # Kill only the first match for controlled chaos
    target = target_procs[0]
    
    try:
        pid = target.pid
        name = target.info['name']
        cmdline = ' '.join(target.info.get('cmdline', [])) if target.info.get('cmdline') else 'N/A'
        
        if dry_run:
            print(f"[DRY RUN] Would kill process '{name}' (PID: {pid})")
            print(f"[DRY RUN] Command line: {cmdline}")
            INJECTIONS_TOTAL.labels(failure_type='process', status='skipped').inc()
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
        
        INJECTIONS_TOTAL.labels(failure_type='process', status='success').inc()
        
    except psutil.NoSuchProcess:
        print(f"[PROCESS] Process disappeared before kill (PID: {pid})")
        INJECTIONS_TOTAL.labels(failure_type='process', status='skipped').inc()
    except psutil.AccessDenied:
        print(f"[PROCESS] Access denied killing process (PID: {pid})")
        INJECTIONS_TOTAL.labels(failure_type='process', status='failed').inc()
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type='process', status='failed').inc()
        print(f"[PROCESS] Failed: {e}")