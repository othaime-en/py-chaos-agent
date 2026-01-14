# Troubleshooting Guide

## Common Issues and Solutions

### Agent Not Starting

#### Symptom

```bash
kubectl get pods -n chaos-demo
# NAME                             READY   STATUS             RESTARTS   AGE
# resilient-app-xxx-xxx            1/2     CrashLoopBackOff   5          3m
```

#### Diagnosis

```bash
# Check agent logs
kubectl logs -n chaos-demo <pod-name> -c chaos-agent

# Common errors:
# - "FileNotFoundError: config.yaml"
# - "ModuleNotFoundError: No module named 'psutil'"
# - "PermissionError: [Errno 13]"
```

#### Solutions

**Config file not found:**

```bash
# Verify ConfigMap exists
kubectl get configmap -n chaos-demo chaos-config

# Verify mount path
kubectl describe pod -n chaos-demo <pod-name> | grep -A 10 Mounts

# Fix: Ensure ConfigMap is properly mounted
```

**Missing dependencies:**

```bash
# Rebuild Docker image with all dependencies
docker build -t py-chaos-agent:latest -f docker/Dockerfile .

# Verify requirements.txt includes all packages
cat requirements.txt
```

**Permission issues:**

```yaml
# Ensure container runs as root for chaos operations
securityContext:
  runAsUser: 0
  capabilities:
    add: ["NET_ADMIN"]
```

### Chaos Not Triggering

#### Symptom

```bash
# Metrics show only skipped injections
curl localhost:8000/metrics | grep chaos_injections_total
# chaos_injections_total{failure_type="cpu",status="skipped"} 100
# chaos_injections_total{failure_type="cpu",status="success"} 0
```

#### Diagnosis

```bash
# Check configuration
kubectl get configmap -n chaos-demo chaos-config -o yaml

# Check agent logs for clues
kubectl logs -n chaos-demo <pod-name> -c chaos-agent -f
```

#### Common Causes and Fixes

**1. Dry-run mode enabled:**

```yaml
# Check config
agent:
  dry_run: true  # This prevents actual chaos!

# Fix: Set to false
agent:
  dry_run: false
```

**2. Low probability:**

```yaml
# With probability 0.1 and interval 30s:
# Expected: ~2 injections per hour
failures:
  cpu:
    probability: 0.1  # Very low!

# Fix: Increase for testing
failures:
  cpu:
    probability: 0.5  # Much more likely
```

**3. All failures disabled:**

```yaml
failures:
  cpu:
    enabled: false
  memory:
    enabled: false
  process:
    enabled: false
  network:
    enabled: false
# Fix: Enable at least one
```

**4. Process not found:**

```bash
# For process failures, target must exist
kubectl logs -n chaos-demo <pod-name> -c chaos-agent | grep "No killable process"

# Check what processes are running
kubectl exec -n chaos-demo <pod-name> -c target-app -- ps aux

# Fix: Update target_name to match actual process
```

### Network Chaos Not Working

#### Symptom

```
[NETWORK] Failed: Operation not permitted
chaos_injections_total{failure_type="network",status="failed"} increasing
```

#### Diagnosis

```bash
# Check if NET_ADMIN capability is granted
kubectl get pod -n chaos-demo <pod-name> -o yaml | grep -A 5 capabilities
```

#### Solution

```yaml
# Add NET_ADMIN capability
containers:
  - name: chaos-agent
    securityContext:
      capabilities:
        add: ["NET_ADMIN"]

# Or use privileged mode (less secure)
containers:
  - name: chaos-agent
    securityContext:
      privileged: true
```

#### Verify Fix

```bash
# Recreate pod
kubectl delete pod -n chaos-demo <pod-name>

# Check logs
kubectl logs -n chaos-demo <pod-name> -c chaos-agent -f
# Should see: [NETWORK] Adding Xms latency...
```

### Process Kills Not Working

#### Symptom

```
[PROCESS] No killable process named 'myapp' found
```

#### Diagnosis

```bash
# Check what processes are visible
kubectl exec -n chaos-demo <pod-name> -c chaos-agent -- ps aux

# If you don't see target app processes, check shareProcessNamespace
kubectl get pod -n chaos-demo <pod-name> -o yaml | grep shareProcessNamespace
```

#### Solution

```yaml
# Enable process namespace sharing
spec:
  shareProcessNamespace: true # CRITICAL for process chaos

  containers:
    - name: target-app
      # ...
    - name: chaos-agent
      # ...
```

#### Verify Fix

```bash
# Delete and recreate pod
kubectl delete pod -n chaos-demo <pod-name>

# Verify processes are visible
kubectl exec -n chaos-demo <pod-name> -c chaos-agent -- ps aux | grep target
```

### High Failure Rate

#### Symptom

```
chaos_injections_total{failure_type="network",status="failed"} 45
chaos_injections_total{failure_type="network",status="success"} 5
```

#### Diagnosis

```bash
# Get detailed error messages
kubectl logs -n chaos-demo <pod-name> -c chaos-agent | grep "Failed:"

# Common errors:
# - "RTNETLINK answers: File exists" (network rules conflict)
# - "Cannot allocate memory" (insufficient memory)
# - "Command not found: tc" (missing tools)
```

#### Solutions

**Network rules conflict:**

```bash
# Clean up manually
kubectl exec -n chaos-demo <pod-name> -c chaos-agent -- tc qdisc del dev eth0 root 2>/dev/null

# Or restart pod
kubectl delete pod -n chaos-demo <pod-name>
```

**Insufficient memory:**

```yaml
# Reduce memory allocation or increase pod limits
failures:
  memory:
    mb: 100 # Reduced from 500

# Or increase pod resources
resources:
  limits:
    memory: 1Gi # Increased limit
```

**Missing tools:**

```dockerfile
# Ensure Dockerfile installs all required tools
RUN apt-get update && apt-get install -y \
    iproute2 \
    procps \
    && rm -rf /var/lib/apt/lists/*
```

### Metrics Not Appearing

#### Symptom

```bash
curl localhost:8000/metrics
# curl: (7) Failed to connect to localhost port 8000: Connection refused
```

#### Diagnosis

```bash
# Check if metrics server started
kubectl logs -n chaos-demo <pod-name> -c chaos-agent | grep "Metrics"
# Should see: [Metrics] Prometheus exporter running on :8000

# Check if port is exposed
kubectl get pod -n chaos-demo <pod-name> -o yaml | grep -A 5 ports
```

#### Solutions

**Port not exposed:**

```yaml
containers:
  - name: chaos-agent
    ports:
      - containerPort: 8000
        name: metrics
```

**Port forward not working:**

```bash
# Kill existing port forwards
pkill -f "port-forward"

# Create new port forward
kubectl port-forward -n chaos-demo <pod-name> 8000:8000

# Or use service
kubectl port-forward -n chaos-demo svc/resilient-app 8000:8000
```

**Metrics server crashed:**

```python
# Check for exceptions in metrics.py
# Increase debugging
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Docker Compose Issues

#### Symptom

```bash
docker-compose up
# ERROR: for chaos-agent  Cannot start service chaos-agent:
# error creating network: permission denied
```

#### Solutions

**Permission denied:**

```bash
# Run with sudo (Linux)
sudo docker-compose up

# Or add user to docker group
sudo usermod -aG docker $USER
newgrp docker
```

**Containers can't communicate:**

```bash
# Check network
docker network ls
docker network inspect py-chaos-agent_default

# Verify network mode in docker-compose.yml
network_mode: "service:target-app"  # Correct
```

**Volume mount issues:**

```bash
# Check file exists
ls -la config.yaml

# Verify mount in docker-compose.yml
volumes:
  - ./config.yaml:/app/config.yaml:ro

# Debug inside container
docker-compose run --rm chaos-agent ls -la /app/
```

### Kubernetes Deployment Issues

#### Image Pull Errors

```bash
kubectl describe pod -n chaos-demo <pod-name>
# Events:
#   Failed to pull image: ImagePullBackOff
```

**Solutions:**

```bash
# For local images (minikube/kind)
eval $(minikube docker-env)  # or
kind load docker-image py-chaos-agent:latest

# For ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ecr-url>

# Verify image exists
docker images | grep chaos-agent
```

#### Pod Stuck in Pending

```bash
kubectl describe pod -n chaos-demo <pod-name>
# Events:
#   FailedScheduling: 0/3 nodes available: insufficient memory
```

**Solutions:**

```yaml
# Reduce resource requests
resources:
  requests:
    cpu: 100m # Reduced
    memory: 128Mi # Reduced
  limits:
    cpu: 500m
    memory: 512Mi
```

### Configuration Issues

#### Invalid YAML Syntax

```bash
kubectl apply -f config.yaml
# error: error parsing config.yaml: yaml: line 10: did not find expected key
```

**Solutions:**

```bash
# Validate YAML syntax
python -c "import yaml; yaml.safe_load(open('config.yaml'))"

# Use online validator: https://www.yamllint.com/

# Common issues:
# - Incorrect indentation (use spaces, not tabs)
# - Missing colons
# - Unquoted special characters
```

#### Configuration Not Loading

```bash
# Agent keeps using old configuration
kubectl logs -n chaos-demo <pod-name> -c chaos-agent | grep CONFIG
# [CONFIG] Interval: 10s  # Old value!
```

**Solutions:**

```bash
# ConfigMap updates don't auto-reload pods
# Must delete pod to pick up changes
kubectl delete pod -n chaos-demo <pod-name>

# Or use a rolling restart
kubectl rollout restart deployment -n chaos-demo resilient-app

# Verify ConfigMap updated
kubectl get configmap -n chaos-demo chaos-config -o yaml
```

### Performance Issues

#### High CPU Usage

```bash
kubectl top pod -n chaos-demo
# NAME                             CPU(cores)   MEMORY(bytes)
# resilient-app-xxx-xxx            950m         150Mi
```

**Diagnosis:**

```bash
# Check if CPU chaos is stuck
kubectl logs -n chaos-demo <pod-name> -c chaos-agent | grep CPU

# Check metrics
curl localhost:8000/metrics | grep chaos_injection_active
# chaos_injection_active{failure_type="cpu"} 1  # Still active!
```

**Solutions:**

```yaml
# Reduce CPU chaos intensity
failures:
  cpu:
    cores: 1 # Reduced from 4
    duration_seconds: 5 # Reduced from 30

# Or adjust interval
agent:
  interval_seconds: 60 # Increased from 10
```

#### Memory Leak

```bash
kubectl top pod -n chaos-demo
# NAME                             CPU(cores)   MEMORY(bytes)
# resilient-app-xxx-xxx            100m         950Mi  # Increasing!
```

**Solutions:**

```bash
# Check for stuck memory injections
curl localhost:8000/metrics | grep memory

# Set proper resource limits
resources:
  limits:
    memory: 512Mi  # Pod will be OOM killed if exceeded

# Restart pod periodically if needed
kubectl delete pod -n chaos-demo <pod-name>
```

## Debugging Techniques

### Enable Debug Logging

```python
# Add to src/agent.py
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
```

### Interactive Debugging

```bash
# Access chaos agent container
kubectl exec -it -n chaos-demo <pod-name> -c chaos-agent -- /bin/bash

# Test commands manually
tc qdisc show dev eth0
ps aux | grep target
python -c "import psutil; print(psutil.cpu_percent())"
```

### Check System Resources

```bash
# Inside container
free -h          # Memory
df -h            # Disk
ip addr show     # Network interfaces
ps auxf          # Process tree
```

### Validate Metrics Manually

```python
# Python script to check metrics
import requests

response = requests.get('http://localhost:8000/metrics')
for line in response.text.split('\n'):
    if 'chaos_' in line and not line.startswith('#'):
        print(line)
```

## Getting Help

### Before Opening an Issue

1. **Check logs:**

   ```bash
   kubectl logs -n chaos-demo <pod-name> -c chaos-agent > agent.log
   kubectl describe pod -n chaos-demo <pod-name> > pod-describe.txt
   ```

2. **Collect configuration:**

   ```bash
   kubectl get configmap -n chaos-demo chaos-config -o yaml > config.yaml
   kubectl get pod -n chaos-demo <pod-name> -o yaml > pod.yaml
   ```

3. **Get metrics:**

   ```bash
   curl localhost:8000/metrics > metrics.txt
   ```

4. **Document steps:**
   - What you tried
   - What you expected
   - What actually happened
   - Environment details (Kubernetes version, cloud provider)

### Issue Template

```markdown
## Environment

- Kubernetes version:
- Cloud provider:
- Chaos agent version:

## Problem Description

[Clear description of the issue]

## Steps to Reproduce

1.
2.
3.

## Expected Behavior

[What should happen]

## Actual Behavior

[What actually happens]

## Logs
```

[Paste relevant logs]

````

## Configuration
```yaml
[Paste relevant config]
````

```

## Additional Resources

- [Kubernetes Troubleshooting Guide](https://kubernetes.io/docs/tasks/debug/)
- [Docker Debugging Guide](https://docs.docker.com/config/containers/troubleshoot/)
- [Prometheus Troubleshooting](https://prometheus.io/docs/prometheus/latest/troubleshooting/)
```
