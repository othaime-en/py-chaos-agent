# Py-Chaos-Agent

![CI/CD](https://github.com/othaime-en/py-chaos-agent/workflows/CI/CD/badge.svg)
![Python](https://img.shields.io/badge/python-3.10-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Coverage](https://img.shields.io/badge/coverage-92%25-brightgreen)

A Python-based chaos engineering sidecar tool for testing the resilience of containerized applications.

## Overview

Py-Chaos-Agent runs alongside your application containers to inject controlled failures and validate system behavior under stress. It's designed to help you build more resilient systems by proactively testing how they handle various failure scenarios.

## Key Features

- **Multiple Failure Modes**: CPU stress, memory pressure, process termination, network latency
- **Flexible Configuration**: YAML-based configuration with probability controls
- **Kubernetes Native**: Designed as a sidecar container with proper security contexts
- **Observable**: Prometheus metrics for monitoring chaos experiments
- **Safe by Default**: Self-protection mechanisms and dry-run mode
- **Infrastructure as Code**: Terraform modules for AWS EKS deployment

## Quick Example

```yaml
agent:
  interval_seconds: 10
  dry_run: false

failures:
  cpu:
    enabled: true
    probability: 0.3
    duration_seconds: 5
    cores: 1
```

## Why Chaos Engineering?

Chaos engineering helps you:

- Identify weaknesses before they cause outages
- Build confidence in system resilience
- Validate monitoring and alerting systems
- Improve incident response procedures
- Test auto-scaling and recovery mechanisms

## Getting Started

Ready to start chaos testing? Check out our [Quick Start Guide](getting-started/quickstart.md) to get up and running in minutes.

## Safety First

!!! warning "Testing Environments Only"
This tool is designed for testing environments only. Always exercise caution and never run in production without proper safeguards and approval.

See our [Safety & Ethics](safety.md) guidelines for responsible chaos engineering practices.

## Project Status

Py-Chaos-Agent is actively maintained and used in development and staging environments. We welcome contributions and feedback from the community.

## License

This project is licensed under the MIT License - see the [LICENSE](https://github.com/othaimeen/py-chaos-agent/blob/main/LICENSE) file for details.
