# Installation

This guide covers the different ways to install and run Py-Chaos-Agent.

## Prerequisites

Before installing Py-Chaos-Agent, ensure you have:

- Python 3.12 or higher
- Docker (for containerized deployment)
- kubectl (for Kubernetes deployment)
- Access to a test environment

## Docker Installation

The recommended way to run Py-Chaos-Agent is using Docker containers.

### Pull Pre-built Images

```bash
# Pull the chaos agent image
docker pull itsothaimeen/py-chaos-agent:latest

# Pull the target app image (for testing)
docker pull itsothaimeen/target-app:latest
```

### Build from Source

```bash
# Clone the repository
git clone https://github.com/othaime-en/py-chaos-agent.git
cd py-chaos-agent

# Build the chaos agent image
docker build -t py-chaos-agent:1.0.0 -f docker/Dockerfile .

# Build the target app image
docker build -t target-app:1.0.0 -f docker/Dockerfile.target .
```

## Local Development Installation

For local development and testing:

```bash
# Clone the repository
git clone https://github.com/othaime-en/py-chaos-agent.git
cd py-chaos-agent

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies (hash-verified)
pip install --require-hashes -r requirements.txt

# Or install everything for development (includes the runtime set)
pip install --require-hashes -r requirements-dev.txt
```

## Kubernetes Installation

For Kubernetes deployment, you'll need:

- A running Kubernetes cluster (kind, minikube, or cloud-based)
- kubectl configured to access your cluster

```bash
# Load images (for local testing with kind/minikube)
kind load docker-image py-chaos-agent:1.0.0
kind load docker-image target-app:1.0.0

# Or for minikube
minikube image load py-chaos-agent:1.0.0
minikube image load target-app:1.0.0
```

See the [Kubernetes Deployment](../user-guide/kubernetes.md) guide for detailed deployment instructions.

## Verify Installation

### Docker Verification

```bash
# Run a quick test with docker-compose
docker-compose up

# In another terminal, check if services are running
curl http://localhost:8080  # Target app
curl http://localhost:8000/metrics  # Chaos agent metrics
```

### Local Verification

```bash
# Activate your virtual environment
source venv/bin/activate

# Run tests
pytest

# Check Python version
python --version  # Should be 3.12+
```

## Next Steps

Once installation is complete:

1. Review the [Configuration Guide](configuration.md) to customize your chaos experiments
2. Follow the [Quick Start Guide](quickstart.md) to run your first chaos test
3. Explore different [Failure Modes](../user-guide/failure-modes.md)

## Troubleshooting

### Docker Issues

If you encounter Docker-related issues:

```bash
# Check Docker is running
docker --version
docker ps

# Check Docker Compose
docker-compose --version
```

### Python Version Issues

Ensure you're using Python 3.12+:

```bash
# Check Python version
python --version

# If needed, specify python3.12
python3.12 -m venv venv
```

### Permission Issues

On Linux, you may need to add your user to the docker group:

```bash
sudo usermod -aG docker $USER
# Log out and back in for changes to take effect
```

## Getting Help

If you encounter issues during installation:

- Check the [FAQ](../faq.md)
- Open an issue on [GitHub](https://github.com/othaime-en/py-chaos-agent/issues)
- Review existing issues for solutions
