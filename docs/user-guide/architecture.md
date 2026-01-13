# Architecture Overview

## Design Philosophy

Py-Chaos-Agent follows the sidecar pattern, running as a separate container alongside your application within the same Kubernetes pod. This design provides:

- **Process isolation**: The chaos agent runs independently but can interact with target processes
- **Network sharing**: Both containers share the same network namespace for realistic network chaos
- **Resource visibility**: The agent can monitor and manipulate shared resources
- **Easy deployment**: No changes required to your application code

## System Architecture

```
┌────────────────────────────────────────────────────────────┐
│                      Kubernetes Pod                         │
│                                                             │
│  ┌─────────────────────┐    ┌──────────────────────────┐  │
│  │   Target Application │    │    Py-Chaos-Agent        │  │
│  │                      │    │                          │  │
│  │  - Business logic    │    │  - Config loader         │  │
│  │  - Port 8080         │    │  - Failure injector      │  │
│  │  - Processes visible │◄───┤  - Metrics exporter      │  │
│  │    to chaos agent    │    │  - Port 8000             │  │
│  └─────────────────────┘    └──────────────────────────┘  │
│                                                             │
│  Shared Resources:                                         │
│  - Process Namespace (shareProcessNamespace: true)         │
│  - Network Namespace (via pod networking)                  │
│  - Network interfaces (eth0)                               │
└────────────────────────────────────────────────────────────┘
         │                              │
         │                              │
         ▼                              ▼
    Application Traffic          Prometheus Scraping
    (port 8080)                  (port 8000/metrics)
```

## Component Breakdown

### 1. Configuration Module (`src/config.py`)

Loads and validates YAML configuration files. Uses dataclasses for type safety and easy serialization.

**Responsibilities:**

- Parse YAML configuration
- Validate configuration values
- Provide structured configuration objects to other modules

### 2. Agent Core (`src/agent.py`)

The main event loop that orchestrates chaos injections.

**Responsibilities:**

- Load configuration on startup
- Start metrics server
- Iterate through configured failure types
- Apply probability checks before injection
- Dynamically import and execute failure modules
- Handle graceful shutdown and cleanup

**Flow:**

```
Start → Load Config → Start Metrics Server → Loop:
  - For each failure type:
    - Check if enabled
    - Roll probability dice
    - Import failure module
    - Execute injection
  - Sleep for interval
  - Repeat
```

### 3. Failure Modules (`src/failures/`)

Each failure type is implemented as a separate module with a standardized interface:

```python
def inject_<type>(config: dict, dry_run: bool = False):
    """
    Inject a specific type of failure.

    Args:
        config: Configuration dictionary for this failure type
        dry_run: If True, log what would happen without executing
    """
    pass
```

#### CPU Module (`cpu.py`)

- Uses multiprocessing to spawn CPU-intensive worker processes
- Each worker spins in a tight loop for the configured duration
- Number of cores determines number of worker processes

#### Memory Module (`memory.py`)

- Allocates memory using bytearray to ensure actual memory consumption
- Runs in a background thread to avoid blocking other injections
- Fills allocated memory with varied data to prevent compression

#### Process Module (`process.py`)

- Uses psutil to enumerate and manage processes
- Implements self-protection logic to avoid killing the chaos agent
- Supports graceful termination (SIGTERM) with fallback to SIGKILL
- Matches processes by name or command line

#### Network Module (`network.py`)

- Uses Linux traffic control (tc) to inject network latency
- Requires NET_ADMIN capability in Kubernetes
- Automatically cleans up rules on shutdown
- Idempotent operations for reliability

### 4. Metrics Module (`src/metrics.py`)

Exposes Prometheus metrics for observability.

**Metrics:**

- `chaos_injections_total`: Counter with labels for failure_type and status
- `chaos_injection_active`: Gauge indicating currently active injections

**Implementation:**

- Runs HTTP server in background thread
- Uses prometheus_client library
- Exposed on port 8000 at `/metrics` endpoint

## Data Flow

### Injection Cycle

```
1. Agent wakes up after interval
2. Iterates through failure configurations
3. For each failure:
   a. Check if enabled (skip if not)
   b. Generate random number
   c. Compare to probability threshold
   d. If passes, dynamically import failure module
   e. Call inject_<type> function
   f. Update metrics
4. Sleep for configured interval
5. Repeat
```

### Metrics Flow

```
1. Failure module begins injection
   → Set INJECTION_ACTIVE gauge to 1

2. During injection
   → Perform chaos operation

3. On completion (success or failure)
   → Increment INJECTIONS_TOTAL counter
   → Set INJECTION_ACTIVE gauge to 0

4. Prometheus scrapes metrics endpoint
   → Returns current metric values
```

## Kubernetes Integration
