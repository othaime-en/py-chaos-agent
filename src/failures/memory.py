import time
import threading
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def _hold_memory(mb, duration):
    """
    Allocate and hold memory for the specified duration.
    Runs in a separate thread to avoid blocking.
    """
    data = []
    try:
        print(f"[MEMORY] Allocating {mb} MB...")
        # Allocate memory - create unique byte arrays to ensure actual allocation
        for i in range(mb):
            # Create a new bytearray for each MB to ensure actual memory usage
            # Fill with varied data to prevent compression/deduplication
            chunk = bytearray(1024 * 1024)
            for j in range(0, len(chunk), 4096):
                chunk[j] = i % 256
            data.append(chunk)
        
        print(f"[MEMORY] Allocated {mb} MB, holding for {duration}s...")
        time.sleep(duration)
        print(f"[MEMORY] Releasing {mb} MB...")
        
    except MemoryError as e:
        print(f"[MEMORY] MemoryError: Could not allocate {mb} MB - {e}")
        raise
    except Exception as e:
        print(f"[MEMORY] Unexpected error during allocation: {e}")
        raise
    finally:
        data.clear()
        del data

def inject_memory(config: dict, dry_run: bool = False):
    mb = config.get('mb', 100)
    duration = config['duration_seconds']

    if dry_run:
        print(f"[DRY RUN] Would allocate {mb} MB for {duration}s")
        INJECTIONS_TOTAL.labels(failure_type='memory', status='skipped').inc()
        return

    print(f"[MEMORY] Starting memory injection: {mb} MB for {duration}s...")
    INJECTION_ACTIVE.labels(failure_type='memory').set(1)

    def _injection_thread():
        """Run memory injection in a thread to avoid blocking main loop."""
        try:
            _hold_memory(mb, duration)
            INJECTIONS_TOTAL.labels(failure_type='memory', status='success').inc()
        except MemoryError:
            INJECTIONS_TOTAL.labels(failure_type='memory', status='failed').inc()
            print(f"[MEMORY] Failed: Insufficient memory to allocate {mb} MB")
        except Exception as e:
            INJECTIONS_TOTAL.labels(failure_type='memory', status='failed').inc()
            print(f"[MEMORY] Failed: {e}")
        finally:
            INJECTION_ACTIVE.labels(failure_type='memory').set(0)
            print(f"[MEMORY] Memory injection completed")

    # Run in daemon thread so it doesn't block other injections
    thread = threading.Thread(target=_injection_thread, daemon=True)
    thread.start()