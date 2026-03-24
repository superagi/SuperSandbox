#!/usr/bin/env bash
# Bootstrap Minikube with gVisor inside a container that has Docker socket access.
# Intended for: docker compose -f docker-compose.minikube-gvisor.yml up
set -euo pipefail

log() { echo "[minikube-gvisor] $*"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    log "missing required command: $1"
    exit 1
  }
}

ARCH="$(uname -m)"
case "${ARCH}" in
  aarch64 | arm64) K8S_ARCH="arm64" ;;
  x86_64) K8S_ARCH="amd64" ;;
  *)
    log "unsupported arch: ${ARCH}"
    exit 1
    ;;
esac

export MINIKUBE_IN_STYLE="${MINIKUBE_IN_STYLE:-false}"
export KUBECONFIG="${KUBECONFIG:-/root/.kube/config}"

if ! command -v docker >/dev/null 2>&1; then
  log "installing docker CLI (client only)..."
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" >/etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce-cli
fi

if ! command -v minikube >/dev/null 2>&1; then
  log "installing minikube..."
  curl -fsSL -o /usr/local/bin/minikube "https://storage.googleapis.com/minikube/releases/latest/minikube-linux-${K8S_ARCH}"
  chmod +x /usr/local/bin/minikube
fi

if ! command -v kubectl >/dev/null 2>&1; then
  log "installing kubectl..."
  KUBECTL_VER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VER}/bin/linux/${K8S_ARCH}/kubectl"
  chmod +x /usr/local/bin/kubectl
fi

need_cmd docker
need_cmd minikube
need_cmd kubectl

mkdir -p "$(dirname "${KUBECONFIG}")"

PROFILE="${MINIKUBE_PROFILE:-opensandbox-gvisor}"
CPUS="${MINIKUBE_CPUS:-2}"
MEMORY="${MINIKUBE_MEMORY:-3072}"
K8S_VER="${MINIKUBE_KUBERNETES_VERSION:-stable}"

log "starting minikube (driver=docker, containerd)..."
minikube start \
  --profile="${PROFILE}" \
  --driver=docker \
  --container-runtime=containerd \
  --cpus="${CPUS}" \
  --memory="${MEMORY}" \
  --kubernetes-version="${K8S_VER}" \
  --force

log "enabling gvisor addon..."
minikube addons enable gvisor --profile="${PROFILE}"

log "ensuring RuntimeClass gvisor exists..."
kubectl apply -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
EOF

log "ensuring opensandbox namespace..."
kubectl create namespace opensandbox --dry-run=client -o yaml | kubectl apply -f -

log "RuntimeClass:"
kubectl get runtimeclass gvisor

log "Done. Kubeconfig: ${KUBECONFIG}"
log "From the host, point kubectl at the same kubeconfig path you mounted (~/.kube/config)."
