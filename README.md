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

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please read the [Development Guide](docs/development.md) before submitting pull requests.

## Acknowledgments

Inspired by chaos engineering principles from Netflix's Chaos Monkey and the broader chaos engineering community.

## Contact

For questions or feedback, please open an issue on GitHub.
