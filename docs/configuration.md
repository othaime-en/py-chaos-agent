# Configuration Reference

## Overview

Py-Chaos-Agent is configured via a YAML file that defines agent behavior and failure injection parameters. The default configuration file is `config.yaml` in the application root.

## Configuration Structure

```yaml
agent:
  # Agent-level settings

failures:
  # Individual failure type configurations
```

## Agent Configuration

### `agent.interval_seconds`

**Type:** Integer  
**Default:** None (required)  
**Description:** How often (in seconds) the agent checks for potential chaos injections.

```yaml
agent:
  interval_seconds: 10 # Check every 10 seconds
```

Lower values increase chaos frequency but consume more resources. Recommended range: 5-60 seconds.

### `agent.dry_run`

**Type:** Boolean  
**Default:** `false`  
**Description:** When `true`, the agent logs what it would do without executing actual chaos.

```yaml
agent:
  dry_run: true # Test configuration without actual chaos
```

**Use cases:**

- Testing configuration changes
- Validating probability settings
- Demonstrating chaos behavior safely

## Failure Configurations

All failure types share common parameters:

### Common Parameters

#### `enabled`

**Type:** Boolean  
**Required:** Yes  
**Description:** Whether this failure type is active.

```yaml
failures:
  cpu:
    enabled: false # Disable CPU chaos
```

#### `probability`

**Type:** Float (0.0 - 1.0)  
**Required:** Yes  
**Description:** Chance of injection occurring each interval.

```yaml
failures:
  cpu:
    probability: 0.3 # 30% chance per check
```

**Guidelines:**

- Start with low probabilities (0.1-0.3) for testing
- Higher probabilities create more aggressive chaos
- Probability of 1.0 means inject every interval
- Probability of 0.0 effectively disables the failure

#### `duration_seconds`

**Type:** Integer  
**Required:** For CPU, Memory, Network  
**Description:** How long the chaos effect lasts.

```yaml
failures:
  cpu:
    duration_seconds: 5 # Hog CPU for 5 seconds
```

## CPU Failure

Spawns processes that consume CPU cycles.

### Configuration

```yaml
failures:
  cpu:
    enabled: true
    probability: 0.3
    duration_seconds: 5
    cores: 2
```

### Parameters

#### `cores`

**Type:** Integer  
**Default:** 1  
**Description:** Number of CPU cores to stress simultaneously.

**Examples:**

```yaml
cores: 1   # Light CPU stress
cores: 2   # Moderate CPU stress
cores: 4   # Heavy CPU stress
```

**Considerations:**

- Setting `cores` higher than available CPU can cause severe performance degradation
- Use dry-run to verify impact before production testing
- CPU stress blocks during execution; avoid very long durations

### Example Scenarios

**Light, frequent CPU spikes:**

```yaml
cpu:
  enabled: true
  probability: 0.5
  duration_seconds: 3
  cores: 1
```

**Heavy, rare CPU stress:**

```yaml
cpu:
  enabled: true
  probability: 0.1
  duration_seconds: 30
  cores: 4
```

## Memory Failure

Allocates and holds memory for a specified duration.

### Configuration

```yaml
failures:
  memory:
    enabled: true
    probability: 0.2
    duration_seconds: 10
    mb: 200
```

### Parameters

#### `mb`

**Type:** Integer  
**Default:** 100  
**Description:** Amount of memory to allocate in megabytes.

**Examples:**

```yaml
mb: 50    # Light memory pressure
mb: 200   # Moderate memory pressure
mb: 1024  # Heavy memory pressure (1GB)
```

**Considerations:**

- Memory injection runs in a background thread
- Allocated memory is filled with data to prevent OS optimization
- Setting `mb` higher than available memory may cause OOM kills
- The chaos agent itself requires memory; account for this

### Example Scenarios

**Gradual memory pressure:**

```yaml
memory:
  enabled: true
  probability: 0.4
  duration_seconds: 15
  mb: 100
```

**Sudden memory spike:**

```yaml
memory:
  enabled: true
  probability: 0.1
  duration_seconds: 5
  mb: 500
```

## Process Failure

Terminates target processes by name or command line.

### Configuration

```yaml
failures:
  process:
    enabled: true
    probability: 0.3
    target_name: "target-app"
```

### Parameters

#### `target_name`

**Type:** String  
**Required:** Yes  
**Description:** Name or substring to match against process names or command lines.

**Examples:**

```yaml
target_name: "nginx"        # Kill nginx processes
target_name: "python"       # Kill Python processes
target_name: "target-app"   # Kill specific app
```

**Matching behavior:**

- Case-insensitive substring matching
- Matches against process name (e.g., `python3`)
- Also matches against command line arguments
- First matching process is terminated

**Safety mechanisms:**

- Chaos agent never kills itself
- Protected PIDs: agent process, parent, and all children
- Processes with "chaos" or "agent.py" in command line are excluded

### Termination Process

1. Send SIGTERM (graceful shutdown)
2. Wait up to 3 seconds for termination
3. If still running, send SIGKILL (forced termination)
4. Wait up to 2 seconds for confirmation

### Example Scenarios

**High availability testing:**

```yaml
process:
  enabled: true
  probability: 0.5
  target_name: "api-server"
```

**Rare catastrophic failure:**

```yaml
process:
  enabled: true
  probability: 0.05
  target_name: "database"
```

## Network Failure

Injects network latency using Linux traffic control (tc).

### Configuration

```yaml
failures:
  network:
    enabled: true
    probability: 0.25
    duration_seconds: 10
    interface: "eth0"
    delay_ms: 300
```

### Parameters

#### `interface`

**Type:** String  
**Default:** `"eth0"`  
**Description:** Network interface to apply latency to.

**Common values:**

- `eth0` - Primary Ethernet interface
- `wlan0` - Wireless interface
- `lo` - Loopback (for testing)

**Finding your interface:**

```bash
# Inside the container
ip addr show

# Or
ifconfig
```

#### `delay_ms`

**Type:** Integer  
**Default:** 100  
**Description:** Network latency to add in milliseconds.

**Examples:**

```yaml
delay_ms: 50    # Slight delay (good connection)
delay_ms: 200   # Noticeable delay (poor connection)
delay_ms: 1000  # Severe delay (1 second)
```

**Real-world equivalents:**

- 10-50ms: Local network
- 50-150ms: Cross-country
- 150-300ms: Intercontinental
- 300+ms: Satellite connection

### Requirements

**Kubernetes:**

```yaml
securityContext:
  capabilities:
    add: ["NET_ADMIN"]
```

**Docker:**

```yaml
privileged: true # Or add NET_ADMIN capability
```

### Cleanup

Network rules are automatically cleaned up:

- On agent shutdown (SIGTERM/SIGINT)
- Before each new network injection
- On startup (removes any leftover rules)

### Example Scenarios

**Intermittent latency spikes:**

```yaml
network:
  enabled: true
  probability: 0.3
  duration_seconds: 5
  delay_ms: 200
  interface: "eth0"
```

**Sustained poor connectivity:**

```yaml
network:
  enabled: true
  probability: 0.6
  duration_seconds: 30
  delay_ms: 500
  interface: "eth0"
```

## Complete Configuration Examples

### Conservative Testing

For initial resilience testing in non-critical environments:

```yaml
agent:
  interval_seconds: 30
  dry_run: false

failures:
  cpu:
    enabled: true
    probability: 0.2
    duration_seconds: 5
    cores: 1

  memory:
    enabled: true
    probability: 0.15
    duration_seconds: 8
    mb: 100

  process:
    enabled: false # Disabled for initial testing

  network:
    enabled: true
    probability: 0.2
    duration_seconds: 10
    interface: "eth0"
    delay_ms: 150
```

### Aggressive Testing

For chaos engineering in robust test environments:

```yaml
agent:
  interval_seconds: 10
  dry_run: false

failures:
  cpu:
    enabled: true
    probability: 0.5
    duration_seconds: 8
    cores: 2

  memory:
    enabled: true
    probability: 0.4
    duration_seconds: 12
    mb: 300

  process:
    enabled: true
    probability: 0.3
    target_name: "target-app"

  network:
    enabled: true
    probability: 0.4
    duration_seconds: 15
    interface: "eth0"
    delay_ms: 400
```

### Process-Only Testing

For testing application restart and recovery:

```yaml
agent:
  interval_seconds: 20
  dry_run: false

failures:
  cpu:
    enabled: false

  memory:
    enabled: false

  process:
    enabled: true
    probability: 0.8
    target_name: "myapp"

  network:
    enabled: false
```

### Network-Only Testing

For testing distributed system behavior under latency:

```yaml
agent:
  interval_seconds: 15
  dry_run: false

failures:
  cpu:
    enabled: false

  memory:
    enabled: false

  process:
    enabled: false

  network:
    enabled: true
    probability: 0.6
    duration_seconds: 20
    interface: "eth0"
    delay_ms: 300
```
