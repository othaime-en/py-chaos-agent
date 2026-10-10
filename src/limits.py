"""
Hard limits for failure injection parameters.

These are absolute ceilings enforced everywhere a value can enter the system:
config file load, every API write, and (as a last line of defense) the
injectors themselves. They are deliberately conservative. A future change will
make CPU and memory ceilings relative to the container's cgroup limits; until
then these constants are the safety net.

This module has no imports so any layer can use it without cycles.
"""

# Agent loop
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 3600

# Shared by every failure type
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 300

# CPU stress: one worker process per core
MIN_CORES = 1
MAX_CORES = 16

# Memory pressure, in megabytes
MIN_MEMORY_MB = 1
MAX_MEMORY_MB = 2048

# Network latency, in milliseconds
MIN_DELAY_MS = 1
MAX_DELAY_MS = 10000

# Process kill target name
MIN_TARGET_NAME_LENGTH = 3
MAX_TARGET_NAME_LENGTH = 128

# Linux interface names are at most 15 characters (IFNAMSIZ - 1)
MAX_INTERFACE_LENGTH = 15


def within_upper_bound(value: object, maximum: int) -> bool:
    """
    True if `value` is a real number no greater than `maximum`.

    Booleans and non-numeric values return False. Only the upper bound is
    checked: lower bounds are enforced by the schemas, and the injectors keep
    accepting small edge values (such as a zero duration) for direct callers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value <= maximum


# Safety fractions (see src/resources.py). These bound how much of the
# container's real CPU and memory budget an injection may use. The ceilings
# stop an operator from configuring "use 100%".
MIN_SAFETY_FRACTION = 0.1
MAX_CPU_FRACTION = 0.9
MAX_MEMORY_FRACTION = 0.8
DEFAULT_CPU_FRACTION = 0.8
DEFAULT_MEMORY_FRACTION = 0.5
