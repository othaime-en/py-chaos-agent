# Kubernetes Deployment

The agent runs as a sidecar in the same pod as your application. `k8s/chaos-demo.yaml` is a complete working example. This page covers deploying it and what to carry over to your own workloads.

## Deploy the Demo

You need a cluster (kind, minikube or cloud) with the two images loaded. See [Installation](../getting-started/installation.md#kubernetes-installation).

```bash
kubectl apply -f k8s/chaos-demo.yaml

kubectl -n chaos-demo create secret generic chaos-api-token \
  --from-literal=token="$(openssl rand -hex 32)" \
  --from-literal=metrics-token="$(openssl rand -hex 32)"

kubectl -n chaos-demo logs -l app=resilient-app -c chaos-agent -f
```

The pod stays in `ContainerCreating` until the Secret exists. The demo config sets `agent.auto_start: true`, so chaos begins on its own.

## What the Manifest Creates

| Resource | Purpose |
| --- | --- |
| Namespace `chaos-demo` | Keeps the demo isolated |
| ConfigMap `chaos-config` | The agent's `config.yaml` |
| Deployment `resilient-app` | One pod with the `target-app` and `chaos-agent` containers |
| Service `resilient-app` | The target app on port 8080 |
| Service `chaos-agent-api` | Metrics on port 8000. The control API is deliberately not exposed |
| NetworkPolicies | Deny everything, then allow the app within the namespace, metrics from a `monitoring` namespace, and DNS |

## Reach the Agent

```bash
# Control API
kubectl -n chaos-demo port-forward deploy/resilient-app 9000:9000
TOKEN=$(kubectl -n chaos-demo get secret chaos-api-token -o jsonpath='{.data.token}' | base64 -d)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9000/status
```

Prometheus scrapes `chaos-agent-api.chaos-demo:8000` with the metrics token. See [Monitoring](monitoring.md). To rotate a token, update the Secret; see [API Security](api-security.md#kubernetes).

## Adapt It for Your Workload

Your pod spec needs the following, all of which the demo has:

- **`shareProcessNamespace: true`**, so the agent can see and signal the app's processes.
- **Capabilities `NET_ADMIN` and `KILL`**, with everything else dropped. `NET_ADMIN` is for network latency (`tc`), `KILL` for signalling the app.
- **Resource limits on the `chaos-agent` container.** CPU and memory injections are sized to a fraction of them, so they are the blast-radius boundary. With `safety.require_cgroup_limits: true` the agent refuses to inject if the limits are missing.
- **The token Secret**, mounted and referenced by `CHAOS_API_TOKEN_FILE` and `CHAOS_METRICS_TOKEN_FILE`.
- **Pod labels from the downward API** (`CHAOS_TARGET_LABELS_FILE`), if you use [experiment](experiments.md) selectors or eligibility.

Point `kill_switch.target_health_url` at your app's health endpoint. The sidecar shares the pod network, so `localhost` reaches it.

## Change the Configuration

The ConfigMap is mounted with `subPath`, so edits do not reach a running pod. After editing it, restart:

```bash
kubectl -n chaos-demo rollout restart deployment/resilient-app
```

## Clean Up

```bash
kubectl delete namespace chaos-demo
```

For AWS EKS with Terraform, see [Terraform (AWS EKS)](terraform.md).
