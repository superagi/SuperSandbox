#!/bin/bash
set -e

echo "=== Enabling gVisor ==="
minikube addons enable gvisor

echo "=== Installing arm64 gVisor binaries ==="
minikube ssh -- "sudo curl -fsSL -o /usr/local/bin/runsc https://storage.googleapis.com/gvisor/releases/release/latest/aarch64/runsc && sudo chmod +x /usr/local/bin/runsc"
minikube ssh -- "sudo curl -fsSL -o /usr/local/bin/containerd-shim-runsc-v1 https://storage.googleapis.com/gvisor/releases/release/latest/aarch64/containerd-shim-runsc-v1 && sudo chmod +x /usr/local/bin/containerd-shim-runsc-v1"
minikube ssh -- "sudo ln -sf /usr/local/bin/runsc /usr/bin/runsc && sudo ln -sf /usr/local/bin/containerd-shim-runsc-v1 /usr/bin/containerd-shim-runsc-v1"

echo "=== Fixing containerd config ==="
docker exec minikube python3 -c "
with open('/etc/containerd/config.toml','r') as f: lines = f.readlines()
seen=False; skip=False; result=[]
for l in lines:
    if 'containerd.runtimes.runsc]' in l:
        if seen: skip=True; continue
        seen=True
    elif skip:
        if l.strip().startswith('['): skip=False
        else: continue
    result.append(l)
with open('/etc/containerd/config.toml','w') as f: f.writelines(result)
"
minikube ssh -- "sudo systemctl restart containerd"

echo "=== Installing agent-sandbox-controller ==="
kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.2.1/manifest.yaml

echo "=== Creating opensandbox namespace ==="
kubectl create namespace opensandbox || true

echo "=== Pre-pulling images ==="
minikube image pull python:3.11-slim
minikube image pull opensandbox/execd:v1.0.6

echo "=== ALL READY ==="
kubectl get nodes
kubectl get runtimeclass
