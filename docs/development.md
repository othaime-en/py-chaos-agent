# Development Guide

## Getting Started

### Prerequisites

- Python 3.10 or higher
- Docker and Docker Compose
- Git
- Virtual environment tool (venv, virtualenv, or conda)

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/othaime-en/py-chaos-agent.git
cd py-chaos-agent

# Create virtual environment
python -m venv venv

# Activate virtual environment
source venv/bin/activate  # On Linux/Mac
# or
venv\Scripts\activate     # On Windows

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Verify installation
python -c "import psutil, yaml, prometheus_client; print('Dependencies OK')"
```

## Project Structure

```
py-chaos-agent/
├── src/
│   ├── __init__.py
│   ├── agent.py              # Main agent loop
│   ├── config.py             # Configuration loading
│   ├── metrics.py            # Prometheus metrics
│   └── failures/
│       ├── __init__.py
│       ├── cpu.py            # CPU stress injection
│       ├── memory.py         # Memory pressure injection
│       ├── process.py        # Process termination
│       └── network.py        # Network latency injection
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # Test fixtures
│   ├── test_config.py        # Configuration tests
│   ├── test_failures.py      # Failure module tests
│   ├── test_integration.py   # Integration tests
│   └── test_metrics.py       # Metrics tests
├── docker/
│   ├── Dockerfile            # Chaos agent image
│   ├── Dockerfile.target     # Target app image
│   └── target-app.py         # Simple test application
├── k8s/
│   └── chaos-demo.yaml       # Kubernetes manifests
├── terraform/
│   └── *.tf                  # Infrastructure as code
├── docs/
│   └── *.md                  # Documentation
├── config.yaml               # Default configuration
├── docker-compose.yml        # Local development setup
├── requirements.txt          # Runtime dependencies
├── requirements-dev.txt      # Development dependencies
└── README.md
```

## Development Workflow

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/test_failures.py

# Run specific test function
pytest tests/test_failures.py::TestCPUFailures::test_inject_cpu_dry_run

# Run with coverage report
pytest --cov=src --cov-report=term-missing

# Generate HTML coverage report
pytest --cov=src --cov-report=html
# Open htmlcov/index.html in browser

# Run only fast tests (skip slow/integration tests)
pytest -m "not slow"
```

### Code Quality

```bash
# Format code with Black
black src tests

# Check code style with flake8
flake8 src tests

# Type checking with mypy
mypy src

# Run all quality checks (what CI runs)
black --check src tests
flake8 src tests
mypy src --install-types --non-interactive
```

### Local Testing

```bash
# Test with dry-run mode
python -m src.agent

# Run with Docker Compose
docker-compose up --build

# View logs
docker-compose logs -f chaos-agent

# Stop containers
docker-compose down
```

### Debugging

```bash
# Run agent with Python debugger
python -m pdb -m src.agent

# Or use breakpoints in code
import pdb; pdb.set_trace()

# Debug in container
docker-compose run --rm chaos-agent /bin/bash
```

## Adding New Features

### Adding a New Failure Type

**1. Create the module**

```python
# src/failures/disk.py
from ..metrics import INJECTIONS_TOTAL, INJECTION_ACTIVE

def inject_disk(config: dict, dry_run: bool = False):
    """Inject disk I/O chaos."""
    operation = config.get("operation", "fill")
    size_mb = config.get("size_mb", 100)

    if dry_run:
        print(f"[DRY RUN] Would inject disk {operation}: {size_mb}MB")
        INJECTIONS_TOTAL.labels(failure_type="disk", status="skipped").inc()
        return

    print(f"[DISK] Injecting {operation}: {size_mb}MB...")
    INJECTION_ACTIVE.labels(failure_type="disk").set(1)

    try:
        # Implement disk chaos here
        # Example: dd if=/dev/zero of=/tmp/fill bs=1M count=size_mb

        INJECTIONS_TOTAL.labels(failure_type="disk", status="success").inc()
    except Exception as e:
        INJECTIONS_TOTAL.labels(failure_type="disk", status="failed").inc()
        print(f"[DISK] Failed: {e}")
    finally:
        INJECTION_ACTIVE.labels(failure_type="disk").set(0)
```

**2. Register in agent.py**

```python
# src/agent.py
FAILURE_MODULES = {
    "cpu": ".failures.cpu",
    "memory": ".failures.memory",
    "process": ".failures.process",
    "network": ".failures.network",
    "disk": ".failures.disk",  # Add new module
}
```

**3. Add configuration section**

```yaml
# config.yaml
failures:
  # ... existing failures

  disk:
    enabled: true
    probability: 0.2
    operation: "fill" # or "slow"
    size_mb: 500
    duration_seconds: 10
```

**4. Write tests**

```python
# tests/test_failures.py
class TestDiskFailures:
    def test_inject_disk_dry_run(self, capsys):
        config = {"operation": "fill", "size_mb": 100}
        inject_disk(config, dry_run=True)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "100MB" in captured.out

    def test_inject_disk_metrics(self):
        config = {"operation": "fill", "size_mb": 50}
        inject_disk(config, dry_run=True)
        assert INJECTIONS_TOTAL.labels(
            failure_type="disk", status="skipped"
        )._value.get() == 1
```

**5. Update documentation**

Add section to `docs/configuration.md` describing the new failure type.

### Adding New Configuration Options

**1. Update config.py dataclasses**

```python
# src/config.py
@dataclass
class AgentConfig:
    interval_seconds: int
    dry_run: bool
    max_concurrent_injections: int = 1  # New option
```

**2. Update config loading**

```python
agent_cfg = AgentConfig(
    interval_seconds=raw["agent"]["interval_seconds"],
    dry_run=raw["agent"].get("dry_run", False),
    max_concurrent_injections=raw["agent"].get("max_concurrent_injections", 1),
)
```

**3. Write tests**

```python
def test_load_config_with_new_option(self, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
agent:
  interval_seconds: 10
  dry_run: false
  max_concurrent_injections: 3
failures:
  cpu:
    enabled: true
    probability: 0.5
    duration_seconds: 5
    cores: 2
    """)

    config = load_config(str(config_file))
    assert config.agent.max_concurrent_injections == 3
```

## Testing Guidelines

### Test Organization

- **Unit tests**: Test individual functions in isolation
- **Integration tests**: Test multiple components together
- **Parametrized tests**: Test multiple inputs efficiently

### Writing Good Tests

```python
# Good: Clear test name, single responsibility
def test_inject_cpu_with_multiple_cores(self):
    config = {"duration_seconds": 1, "cores": 2}
    inject_cpu(config, dry_run=False)
    # Assert expected behavior

# Bad: Unclear name, testing multiple things
def test_cpu(self):
    config = {}
    inject_cpu(config, True)
    inject_cpu(config, False)
    # Multiple assertions
```

### Test Fixtures

```python
# conftest.py
import pytest

@pytest.fixture
def sample_config():
    """Provide a standard test configuration."""
    return {
        "duration_seconds": 1,
        "cores": 1,
        "probability": 0.5
    }

# Use in tests
def test_with_fixture(self, sample_config):
    inject_cpu(sample_config, dry_run=True)
```

### Mocking External Dependencies

```python
from unittest.mock import patch, MagicMock

@patch('src.failures.network._run_cmd')
def test_network_injection(self, mock_run_cmd):
    # Control external command behavior
    mock_run_cmd.return_value = MagicMock(returncode=0)

    config = {"interface": "eth0", "delay_ms": 100, "duration_seconds": 1}
    inject_network(config, dry_run=False)

    # Verify command was called correctly
    mock_run_cmd.assert_called()
```
