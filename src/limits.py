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


# Kill switch settings (see src/kill_switch.py)
MIN_KS_INTERVAL_SECONDS = 1
MAX_KS_INTERVAL_SECONDS = 3600
MIN_KS_TIMEOUT_SECONDS = 1
MAX_KS_TIMEOUT_SECONDS = 60
MIN_KS_THRESHOLD = 1
MAX_KS_THRESHOLD = 100
MIN_KS_GRACE_SECONDS = 0
MAX_KS_GRACE_SECONDS = 3600
MIN_KS_RECOVERY_INTERVAL_SECONDS = 1
MAX_KS_RECOVERY_INTERVAL_SECONDS = 3600
MAX_HEALTH_URL_LENGTH = 2048


# Experiments (see src/experiments.py and src/steady_state.py)
MAX_EXPERIMENTS = 100
MAX_EXPERIMENT_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1000
MAX_HYPOTHESIS_LENGTH = 500

# How long one experiment may run. Six hours covers a soak test; anything
# longer should be several experiments.
MIN_EXPERIMENT_DURATION_SECONDS = 1
MAX_EXPERIMENT_DURATION_SECONDS = 21600
MIN_EXPERIMENT_INJECTIONS = 1
MAX_EXPERIMENT_INJECTIONS = 1000

# Steady-state checks
MAX_STEADY_STATE_CHECKS = 10
MIN_CHECK_TIMEOUT_SECONDS = 1
MAX_CHECK_TIMEOUT_SECONDS = 30
MIN_CHECK_LATENCY_MS = 1
MAX_CHECK_LATENCY_MS = 30000
MIN_STEADY_CHECK_INTERVAL_SECONDS = 1
MAX_STEADY_CHECK_INTERVAL_SECONDS = 60
MIN_REQUIRED_SUCCESSES = 1
MAX_REQUIRED_SUCCESSES = 20
MIN_BASELINE_TIMEOUT_SECONDS = 0
MAX_BASELINE_TIMEOUT_SECONDS = 600
MIN_RECOVERY_TIMEOUT_SECONDS = 0
MAX_RECOVERY_TIMEOUT_SECONDS = 3600

# Time windows
MAX_TIME_WINDOWS = 20
MAX_TIMEZONE_LENGTH = 64

# Network destinations (see NetworkDestination in src/schemas.py)
MAX_NETWORK_DESTINATIONS = 10
# Shorter prefixes would delay a large part of the network, which is not "one
# dependency" any more. Use no destinations at all to delay the whole interface.
MIN_DESTINATION_PREFIX_V4 = 16
MIN_DESTINATION_PREFIX_V6 = 48
