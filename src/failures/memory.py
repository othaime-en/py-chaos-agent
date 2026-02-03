import time
import threading
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE
from ..logging_config import get_logger

logger = get_logger(__name__)


def _hold_memory(mb, duration):
    """
    Allocate and hold memory for the specified duration.
    Runs in a separate thread to avoid blocking.
    """
    data = []
    allocation_start = time.time()
    
    try:
        logger.info(
            'Beginning memory allocation',
            extra={'target_mb': mb, 'duration_seconds': duration}
        )
        
        # Allocate memory - create unique byte arrays to ensure actual allocation
        for i in range(mb):
            # Create a new bytearray for each MB to ensure actual memory usage
            # Fill with varied data to prevent compression/deduplication
            chunk = bytearray(1024 * 1024)
            for j in range(0, len(chunk), 4096):
                chunk[j] = i % 256
            data.append(chunk)
            
            # Log progress for large allocations
            if (i + 1) % 100 == 0:
                logger.debug(
                    'Memory allocation progress',
                    extra={'allocated_mb': i + 1, 'target_mb': mb}
                )

        allocation_time = time.time() - allocation_start
        logger.info(
            'Memory allocated successfully',
            extra={
                'allocated_mb': mb,
                'allocation_time_seconds': round(allocation_time, 2),
                'chunks_created': len(data)
            }
        )
        
        logger.debug(f'Holding {mb} MB for {duration} seconds')
        time.sleep(duration)
        
        logger.info(
            'Releasing allocated memory',
            extra={'mb': mb}
        )

    except MemoryError as e:
        logger.error(
            'Memory allocation failed - insufficient memory',
            exc_info=True,
            extra={
                'requested_mb': mb,
                'allocated_mb': len(data),
                'error': str(e),
                'error_type': 'MemoryError'
            }
        )
        raise
        
    except Exception as e:
        logger.error(
            'Unexpected error during memory allocation',
            exc_info=True,
            extra={
                'requested_mb': mb,
                'allocated_mb': len(data),
                'error': str(e),
                'error_type': type(e).__name__
            }
        )
        raise
        
    finally:
        # Cleanup
        data_len = len(data)
        data.clear()
        del data
        logger.debug(
            'Memory cleanup completed',
            extra={'freed_chunks': data_len}
        )


def inject_memory(config: dict, dry_run: bool = False):
    mb = config.get("mb", 100)
    duration = config["duration_seconds"]

    if dry_run:
        logger.info(
            'Memory injection (DRY RUN)',
            extra={
                'mb': mb,
                'duration_seconds': duration,
                'dry_run': True
            }
        )
        INJECTIONS_TOTAL.labels(failure_type="memory", status="skipped").inc()
        return

    logger.info(
        'Starting memory pressure injection',
        extra={
            'mb': mb,
            'duration_seconds': duration,
            'operation': 'memory_pressure'
        }
    )
    
    INJECTION_ACTIVE.labels(failure_type="memory").set(1)

    def _injection_thread():
        """Run memory injection in a thread to avoid blocking main loop."""
        thread_id = threading.get_ident()
        logger.debug(
            'Memory injection thread started',
            extra={'thread_id': thread_id}
        )
        
        try:
            _hold_memory(mb, duration)
            INJECTIONS_TOTAL.labels(failure_type="memory", status="success").inc()
            
            logger.info(
                'Memory pressure injection completed successfully',
                extra={
                    'mb': mb,
                    'duration_seconds': duration,
                    'status': 'success',
                    'thread_id': thread_id
                }
            )
            
        except MemoryError as e:
            INJECTIONS_TOTAL.labels(failure_type="memory", status="failed").inc()
            
            logger.error(
                'Memory injection failed - insufficient memory',
                exc_info=True,
                extra={
                    'mb': mb,
                    'error': str(e),
                    'error_type': 'MemoryError',
                    'status': 'failed',
                    'thread_id': thread_id
                }
            )
            
        except Exception as e:
            INJECTIONS_TOTAL.labels(failure_type="memory", status="failed").inc()
            
            logger.error(
                'Memory injection failed with unexpected error',
                exc_info=True,
                extra={
                    'mb': mb,
                    'error': str(e),
                    'error_type': type(e).__name__,
                    'status': 'failed',
                    'thread_id': thread_id
                }
            )
            
        finally:
            INJECTION_ACTIVE.labels(failure_type="memory").set(0)
            logger.debug(
                'Memory injection thread completing',
                extra={'thread_id': thread_id}
            )

    # Run in daemon thread so it doesn't block other injections
    thread = threading.Thread(target=_injection_thread, daemon=True, name='memory-injection')
    thread.start()
    
    logger.debug(
        'Memory injection thread spawned',
        extra={'thread_id': thread.ident, 'thread_name': thread.name}
    )
