#!/usr/bin/env bash
# Start minikube with gVisor (runs directly on host — no docker-compose needed).
# Usage: ./scripts/start-minikube-gvisor.sh
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-opensandbox-gvisor}"
CPUS="${MINIKUBE_CPUS:-2}"
MEMORY="${MINIKUBE_MEMORY:-3072}"
K8S_VER="${MINIKUBE_KUBERNETES_VERSION:-stable}"

log() { echo "[start-minikube-gvisor] $*"; }

# ── Pre-flight checks ──────────────────────────────────────────────
for cmd in minikube kubectl docker; do
  command -v "$cmd" >/dev/null 2>&1 || { log "ERROR: $cmd not found"; exit 1; }
done

docker info >/dev/null 2>&1 || { log "ERROR: Docker daemon not running"; exit 1; }

# ── Start or resume cluster ────────────────────────────────────────
STATUS="$(minikube status -p "$PROFILE" --format='{{.Host}}' 2>/dev/null || true)"

if [ "$STATUS" = "Running" ]; then
  log "Cluster '$PROFILE' is already running."
else
  log "Starting minikube (profile=$PROFILE, cpus=$CPUS, memory=$MEMORY)..."
  minikube start \
    --profile="$PROFILE" \
    --driver=docker \
    --container-runtime=containerd \
    --cpus="$CPUS" \
    --memory="$MEMORY" \
    --kubernetes-version="$K8S_VER"
fi

# ── Enable gVisor addon ────────────────────────────────────────────
log "Enabling gvisor addon..."
minikube addons enable gvisor --profile="$PROFILE"

# ── Ensure RuntimeClass ────────────────────────────────────────────
log "Applying RuntimeClass 'gvisor'..."
kubectl --context="$PROFILE" apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
EOF

# ── Install agent-sandbox controller + CRD ────────────────────────
AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v0.2.1}"
if kubectl --context="$PROFILE" get crd sandboxes.agents.x-k8s.io >/dev/null 2>&1; then
  log "CRD sandboxes.agents.x-k8s.io already installed."
else
  log "Installing agent-sandbox controller ${AGENT_SANDBOX_VERSION}..."
  kubectl --context="$PROFILE" apply -f \
    "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml"
  log "Waiting for agent-sandbox controller to be ready..."
  kubectl --context="$PROFILE" -n agent-sandbox-system wait --for=condition=Available \
    deployment/agent-sandbox-controller --timeout=120s
fi

# ── Ensure namespace ───────────────────────────────────────────────
log "Ensuring 'opensandbox' namespace..."
kubectl --context="$PROFILE" create namespace opensandbox --dry-run=client -o yaml \
  | kubectl --context="$PROFILE" apply -f -

# ── Verify ─────────────────────────────────────────────────────────
log "Cluster status:"
minikube status -p "$PROFILE"
echo
log "RuntimeClass:"
kubectl --context="$PROFILE" get runtimeclass gvisor
echo
log "CRD:"
kubectl --context="$PROFILE" get crd sandboxes.agents.x-k8s.io
echo
log "Done. Use: kubectl --context=$PROFILE ..."
