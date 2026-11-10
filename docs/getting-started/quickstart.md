# Quick Start

Run the agent next to a demo app and trigger your first failure. You need Docker, Docker Compose and `openssl`.

## 1. Start the stack

```bash
git clone https://github.com/othaime-en/kaos.git
cd kaos

# Two different tokens: one for the control API, one for Prometheus to scrape metrics.
{
  echo "CHAOS_API_TOKEN=$(openssl rand -hex 32)"
  echo "CHAOS_METRICS_TOKEN=$(openssl rand -hex 32)"
} > .env

docker-compose up --build -d
```

Compose refuses to start without both tokens. The API (port 9000) and metrics (port 8000) are published on the host's loopback interface only.

## 2. Start the agent

The agent starts idle. Nothing is injected until you start it:

```bash
set -a; source .env; set +a
curl -X POST -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/agent/start
```

It now checks every 10 seconds and injects failures according to the probabilities in `config.yaml`. To start on boot instead, set `agent.auto_start: true`.

## 3. Trigger a failure

Don't wait for the probabilities. Inject a 10-second CPU stress by hand:

```bash
curl -X POST http://127.0.0.1:9000/inject/manual \
  -H "Authorization: Bearer $CHAOS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"failure_type": "cpu", "config": {"duration_seconds": 10}}'
```

!!! tip
    Add `"dry_run": true` to log what would happen without injecting anything.

## 4. See what happened

```bash
docker-compose logs -f chaos-agent

curl -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/status

curl -s -H "Authorization: Bearer $CHAOS_METRICS_TOKEN" http://127.0.0.1:8000/metrics \
  | grep chaos_injections_total
```

## 5. Stop

```bash
curl -X POST -H "Authorization: Bearer $CHAOS_API_TOKEN" http://127.0.0.1:9000/agent/stop
docker-compose down
```

## Next Steps

- [Configuration](configuration.md): tune failure types, probabilities and limits
- [Experiments](../user-guide/experiments.md): run hypothesis-driven tests
- [Kill Switch](../user-guide/kill-switch.md): pause chaos automatically when the target is unhealthy
- [Kubernetes Deployment](../user-guide/kubernetes.md): run it as a sidecar
