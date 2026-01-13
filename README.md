# Py-Chaos-Agent

![CI/CD](https://github.com/othaime-en/py-chaos-agent/workflows/CI/CD/badge.svg)
![Python](https://img.shields.io/badge/python-3.10-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)

A Python-based chaos engineering sidecar tool for testing the resilience of containerized applications. Py-Chaos-Agent runs alongside your application containers to inject controlled failures and validate system behavior under stress.

## Features

- **Multiple Failure Modes**: CPU stress, memory pressure, process termination, network latency
- **Flexible Configuration**: YAML-based configuration with probability controls
- **Kubernetes Native**: Designed as a sidecar container with proper security contexts
- **Observable**: Prometheus metrics for monitoring chaos experiments
- **Safe by Default**: Self-protection mechanisms and dry-run mode
- **Infrastructure as Code**: Terraform modules for AWS EKS deployment

## Quick Start

### Local Development with Docker Compose

```bash
# Clone the repository
git clone https://github.com/othaime-en/py-chaos-agent.git
cd py-chaos-agent

# Start the target application and chaos agent
docker-compose up --build

# View logs
docker-compose logs -f chaos-agent

# Access metrics
curl http://localhost:8000/metrics

# Access target application
curl http://localhost:8080
```

### Kubernetes Deployment

```bash
# Build and load images (for local testing with kind/minikube)
docker build -t py-chaos-agent:latest -f docker/Dockerfile .
docker build -t target-app:latest -f docker/Dockerfile.target .

# Deploy to Kubernetes
kubectl apply -f k8s/chaos-demo.yaml

# View chaos agent logs
kubectl logs -n chaos-demo -l app=resilient-app -c chaos-agent -f

# View metrics
kubectl port-forward -n chaos-demo svc/resilient-app 8000:8000
curl http://localhost:8000/metrics
```

## Configuration

Configure chaos experiments via `config.yaml`:

```yaml
agent:
  interval_seconds: 10 # How often to potentially inject failures
  dry_run: false # Set to true to test without actual injection

failures:
  cpu:
    enabled: true
    duration_seconds: 5
    probability: 0.3 # 30% chance per interval
    cores: 1

  memory:
    enabled: true
    duration_seconds: 8
    probability: 0.2
    mb: 200

  process:
    enabled: true
    target_name: "target-app"
    probability: 0.4

  network:
    enabled: true
    interface: "eth0"
    delay_ms: 300
    duration_seconds: 10
    probability: 0.25
```

See [Configuration Guide](docs/configuration.md) for detailed options.

## Architecture

Py-Chaos-Agent runs as a sidecar container in Kubernetes, sharing the process and network namespaces with your target application. This allows it to inject failures while maintaining isolation from other pods.

```
┌─────────────────────────────────────┐
│           Kubernetes Pod            │
├─────────────────┬───────────────────┤
│  Target App     │  Chaos Agent      │
│  (port 8080)    │  (port 8000)      │
│                 │                   │
│  Shares: Process Namespace          │
│          Network Namespace          │
└─────────────────────────────────────┘
```

See [Architecture Documentation](docs/architecture.md) for detailed design.

## Safety and Ethics

**WARNING**: This tool is designed for testing environments only.

- Only use on systems you own or have explicit permission to test
- Never run in production without proper safeguards and approval
- Start with dry-run mode to verify behavior
- Monitor systems closely during chaos experiments
- Have rollback procedures ready

The agent includes self-protection mechanisms to avoid terminating itself, but always exercise caution when running chaos experiments.

## Development

### Prerequisites

- Python 3.10+
- Docker and Docker Compose
- kubectl (for Kubernetes testing)
- Terraform (for AWS deployment)

### Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
pytest

# Run tests with coverage
pytest --cov=src --cov-report=html

# Lint and format
black src tests
flake8 src tests
mypy src
```

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run specific test file
pytest tests/test_failures.py

# Run with coverage
pytest --cov=src --cov-report=term-missing
```

See [Development Guide](docs/development.md) for contribution guidelines.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please read the [Development Guide](docs/development.md) before submitting pull requests.

## Acknowledgments

Inspired by chaos engineering principles from Netflix's Chaos Monkey and the broader chaos engineering community.

## Contact

For questions or feedback, please open an issue on GitHub.
