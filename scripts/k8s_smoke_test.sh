#!/usr/bin/env bash
#
# End-to-end check of the Kubernetes demo on a real cluster (CI runs it on kind).
#
# Needs: kubectl pointed at a cluster that already has the images
# py-chaos-agent:1.0.0 and target-app:1.0.0 (for kind: `kind load docker-image`),
# plus curl and openssl.
#
# It proves what unit tests cannot:
#   * both containers start with the hardened securityContext
#   * the agent runs as UID 10001 and holds exactly NET_ADMIN + KILL
#   * the target runs as UID 10002 and holds nothing
#   * the API enforces its token
#   * a network injection really installs (and removes) a netem qdisc
#   * a process injection really kills the target (CAP_KILL across users)
set -euo pipefail

NS=chaos-demo
APP=deploy/resilient-app
MANIFEST="${MANIFEST:-k8s/chaos-demo.yaml}"
PORT="${PORT:-19000}"
CAP_NET_ADMIN_AND_KILL="0000000000001020" # bit 12 + bit 5

pf_pid=""
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok:   $*"; }

dump() {
  echo "---- diagnostics ----" >&2
  kubectl -n "$NS" get pods -o wide >&2 || true
  kubectl -n "$NS" describe pod -l app=resilient-app >&2 || true
  kubectl -n "$NS" logs -l app=resilient-app -c chaos-agent --tail=80 >&2 || true
  kubectl -n "$NS" logs -l app=resilient-app -c target-app --tail=20 >&2 || true
}
cleanup() {
  status=$?
  [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null || true
  [ "$status" -ne 0 ] && dump
  exit "$status"
}
trap cleanup EXIT

exec_agent() { kubectl -n "$NS" exec "$APP" -c chaos-agent -- "$@"; }

api() { # api METHOD PATH [json]  -> prints "<code> <body>"
  local method=$1 path=$2 body=${3:-}
  curl -sS -o /tmp/smoke-body -w '%{http_code}' -X "$method" \
    -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
    ${body:+-d "$body"} "http://127.0.0.1:${PORT}${path}"
}

wait_until() { # wait_until SECONDS DESCRIPTION command...
  local deadline=$((SECONDS + $1)) what=$2
  shift 2
  until "$@" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "timed out waiting for: $what"
    sleep 1
  done
}

# ---------------------------------------------------------------- deploy
kubectl apply -f "$MANIFEST"
TOKEN=$(openssl rand -hex 32)
kubectl -n "$NS" create secret generic chaos-api-token --from-literal=token="$TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" rollout status "$APP" --timeout=240s
ok "deployment is ready (both containers started under the hardened securityContext)"

# ----------------------------------------------- identity and capabilities
pid_of() { exec_agent sh -c "pgrep -f '$1' | head -1"; }
status_field() { # status_field PID FIELD
  exec_agent sh -c "awk '/^$2:/ {print \$2}' /proc/$1/status"
}

# The [x] form keeps pgrep from matching the shell that is running it.
agent_pid=$(pid_of '[s]rc.api_server')
[ -n "$agent_pid" ] || fail "agent process not found"
[ "$(status_field "$agent_pid" Uid)" = "10001" ] || fail "agent is not UID 10001"
caps=$(status_field "$agent_pid" CapEff)
[ "$caps" = "$CAP_NET_ADMIN_AND_KILL" ] \
  || fail "agent effective capabilities are $caps, expected $CAP_NET_ADMIN_AND_KILL (NET_ADMIN + KILL)"
ok "agent runs as UID 10001 with exactly NET_ADMIN + KILL ($caps)"

target_pid=$(pid_of '[t]arget-app.py')
[ -n "$target_pid" ] || fail "target process not found"
[ "$(status_field "$target_pid" Uid)" = "10002" ] || fail "target is not UID 10002"
[ "$(status_field "$target_pid" CapEff)" = "0000000000000000" ] || fail "target holds capabilities"
ok "target runs as UID 10002 with no capabilities"

exec_agent sh -c 'touch /app/should-fail 2>/dev/null' && fail "root filesystem is writable"
exec_agent sh -c 'touch /tmp/ok && touch /var/log/chaos-agent/ok' || fail "tmp or log dir not writable"
ok "root filesystem is read-only; /tmp and the log directory are writable"

# ------------------------------------------------------------------ API
kubectl -n "$NS" port-forward "$APP" "${PORT}:9000" >/tmp/smoke-pf.log 2>&1 &
pf_pid=$!
wait_until 30 "port-forward" curl -sf "http://127.0.0.1:${PORT}/health"

code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/status")
[ "$code" = "401" ] || [ "$code" = "403" ] || fail "unauthenticated /status returned $code"
[ "$(api GET /status)" = "200" ] || fail "authenticated /status failed"
ok "API rejects requests without the token and accepts it with"

# Stop the demo's own random chaos so the checks below are deterministic.
[ "$(api POST /agent/stop)" = "200" ] || fail "could not stop the agent loop"

# -------------------------------------------------------------- network
[ "$(api POST /inject/manual '{"failure_type":"network"}')" = "200" ] \
  || fail "network injection refused: $(cat /tmp/smoke-body)"
wait_until 30 "netem qdisc on eth0" \
  bash -c "kubectl -n $NS exec $APP -c chaos-agent -- tc qdisc show dev eth0 | grep -q netem"
ok "network injection installed a netem qdisc (NET_ADMIN works for a non-root user)"

[ "$(api POST /agent/stop)" = "200" ] || fail "stop failed"
wait_until 30 "netem qdisc removed" \
  bash -c "! kubectl -n $NS exec $APP -c chaos-agent -- tc qdisc show dev eth0 | grep -q netem"
ok "stopping the agent removed the qdisc"

# -------------------------------------------------------------- process
before=$(kubectl -n "$NS" get pods -l app=resilient-app \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="target-app")].restartCount}')
[ "$(api POST /inject/manual '{"failure_type":"process"}')" = "200" ] \
  || fail "process injection refused: $(cat /tmp/smoke-body)"
wait_until 60 "target-app restart" bash -c "
  now=\$(kubectl -n $NS get pods -l app=resilient-app \
    -o jsonpath='{.items[0].status.containerStatuses[?(@.name==\"target-app\")].restartCount}')
  [ \"\$now\" -gt \"$before\" ]"
ok "process injection killed the target across users (KILL works for a non-root user)"

echo "All checks passed."
