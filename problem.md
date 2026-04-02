# Staging Environment Issues — Sandbox Endpoint & Logs

**Date:** 2026-04-01 (updated)
**Sandbox ID:** `4e535c1e-cebd-4635-9ba6-7af38eef1cff`
**Environment:** Staging (`sandbox.superagii.com`)

---

## Test Setup

- Python HTTP server started on port 8080 inside a running sandbox
- Server confirmed working from inside the sandbox via `curl localhost:8080`
- Response: `{"message": "Hello from SuperSandbox!", "status": "running"}`

---

## Issue 1 (CRITICAL): Ingress Proxy Not Deployed / Not Wired Up

**Symptom:**
```bash
# With the OpenSandbox-Ingress-To header
curl -k "https://sandbox.dev.superagi.com/" \
  -H "OpenSandbox-Ingress-To: 4e535c1e-cebd-4635-9ba6-7af38eef1cff-8080"
# => 404 Not Found (9 bytes, plain text)

# Without the header — same response
curl -k "https://sandbox.dev.superagi.com/"
# => 404 Not Found (9 bytes, plain text)
```

Both responses come from `server: APISIX/3.14.1` with no custom headers.

**Root Cause (confirmed):**
The Go-based **OpenSandbox Ingress Proxy** (`components/ingress/`) is **not deployed or not reachable** in staging. The evidence:

1. If the ingress proxy was running, requests **without** the header would return:
   `"OpenSandbox Ingress: missing header 'Opensandbox-Ingress-To' or 'Host'"` (from `components/ingress/pkg/proxy/host.go:44`)

2. If the ingress proxy was running and the sandbox wasn't found, it would return:
   `"OpenSandbox Ingress: sandbox not found: <namespace>/..."` (from `components/ingress/pkg/proxy/proxy.go:66`)

3. Instead, we get APISIX's default 404 — meaning APISIX has **no upstream/route** configured for `sandbox.dev.superagi.com` to forward traffic to the ingress proxy.

**How the ingress proxy works (when deployed):**
- It's a standalone Go HTTP server (`components/ingress/main.go`)
- Uses a K8s dynamic informer to watch Sandbox CRs (`agents.x-k8s.io/v1alpha1/sandboxes`)
- In `header` mode: reads `OpenSandbox-Ingress-To` header → parses `<sandbox-id>-<port>` → looks up Sandbox CR → gets `status.serviceFQDN` → reverse proxies to `<serviceFQDN>:<port>`
- Resolves sandbox by resource name candidates: `sandbox-<id>`, raw `<id>`, `sandbox-<id>` (legacy)

**Fix — 3 steps required:**
1. **Deploy the ingress proxy** in the staging K8s cluster (build from `components/ingress/`)
2. **Start it with correct flags:**
   ```bash
   opensandbox-ingress \
     -namespace <sandbox-namespace> \
     -provider-type agent-sandbox \
     -mode header \
     -port 8080
   ```
3. **Configure APISIX** to route `sandbox.dev.superagi.com` traffic to the ingress proxy K8s service

---

## Issue 2: Proxy Route Returns 301 Redirect

**Symptom:**
```bash
curl -D - "https://sandbox.superagii.com/sandboxes/{id}/proxy/8080/" \
  -H "OPEN-SANDBOX-API-KEY: ..."
# => 301 Moved Permanently
# Location: https://sandbox.dev.superagi.com:443/
# server: APISIX/3.14.1
```

**Additional finding:**
Requests to `sandbox.superagii.com` **do** reach the FastAPI server for other paths. For example:
```bash
curl -k "https://sandbox.superagii.com/" \
  -H "OpenSandbox-Ingress-To: ..." \
  -H "OPEN-SANDBOX-API-KEY: ..."
# => {"detail":"Not Found"}  (JSON — this IS from FastAPI)
```

This confirms the API server is reachable, but the `/sandboxes/{id}/proxy/{port}/` path specifically is being intercepted by APISIX before it reaches FastAPI.

**Root Cause:**
APISIX has a route rule that matches `/sandboxes/*/proxy/*` and redirects it to `sandbox.dev.superagi.com` (likely intended to redirect to the ingress proxy). But since the ingress proxy isn't deployed (Issue 1), the redirect leads to another 404.

**Fix:**
- Option A: Remove the APISIX redirect rule for proxy paths so they pass through to the FastAPI server's built-in proxy handler (`server/src/api/lifecycle.py:444-526`)
- Option B: Once the ingress proxy is deployed (Issue 1 fix), the redirect will work — but this is an unnecessary hop since the FastAPI server can proxy directly

---

## Issue 3: Pod Logs API Returns 500

**Symptom:**
```bash
curl "https://sandbox.superagii.com/sandboxes/{id}/logs" \
  -H "OPEN-SANDBOX-API-KEY: ..."
# => 500 Internal Server Error (21 bytes, plain text)
# x-apisix-upstream-status: 500  (confirms the 500 is from the FastAPI backend)
```

**Code path:**
```
get_sandbox_logs()                          → lifecycle.py:557
  └─ sandbox_service.get_sandbox_logs()     → kubernetes_service.py:1025
       ├─ get_sandbox_pod_name(sandbox_id)  → kubernetes_service.py:981
       │    └─ k8s_client.get_pod_name_for_sandbox()  → client.py:459
       └─ k8s_client.read_pod_log()         → client.py:388
            └─ core_v1_api.read_namespaced_pod_log()  → K8s API
```

**Possible causes (in order of likelihood):**

### A. RBAC — missing `pods/log` permission
The server's service account may lack permission to read pod logs. The task APIs work because they go through `execd` (exec into pod → HTTP to localhost:44772), not through the K8s logs API.

**Verify:**
```bash
kubectl auth can-i get pods/log \
  --as=system:serviceaccount:<namespace>:<sa-name> \
  -n <sandbox-namespace>
```

### B. Container name mismatch
The logs API hardcodes `container="sandbox"` (`kubernetes_service.py:1041`). If the pod's main container has a different name, the K8s API will fail.

**Verify:**
```bash
kubectl get pod -l opensandbox.io/id=4e535c1e-cebd-4635-9ba6-7af38eef1cff \
  -o jsonpath='{.items[*].spec.containers[*].name}'
# Expected: "sandbox" (set in agent_sandbox_provider.py:364)
```

### C. No error handling — generic 500 hides real error
`get_sandbox_logs()` in `kubernetes_service.py:1025-1044` has **no try/except** around the `read_pod_log` call. Any K8s API exception (RBAC denial, timeout, etc.) propagates as an unhandled 500 with no useful detail. Compare with `get_endpoint()` which properly wraps K8s calls.

**Fix (code):**
```python
# kubernetes_service.py - get_sandbox_logs should wrap the K8s call
def get_sandbox_logs(self, sandbox_id, tail_lines=100, follow=False):
    pod_name = self.get_sandbox_pod_name(sandbox_id)
    try:
        return self.k8s_client.read_pod_log(
            namespace=self.namespace,
            pod_name=pod_name,
            container="sandbox",
            tail_lines=tail_lines,
            follow=follow,
        )
    except Exception as e:
        logger.error(f"Failed to read logs for {sandbox_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail={
                "code": SandboxErrorCodes.K8S_API_ERROR,
                "message": f"Failed to read pod logs: {str(e)}",
            },
        ) from e
```

---

## Architecture Overview

```
                        ┌─────────────────────────────────┐
                        │         APISIX Gateway          │
                        │       (sandbox-gateway)         │
                        └──────┬──────────────┬───────────┘
                               │              │
              sandbox.superagii.com    sandbox.dev.superagi.com
                               │              │
                               ▼              ▼
                    ┌──────────────┐   ┌──────────────────┐
                    │  FastAPI     │   │  Ingress Proxy   │
                    │  API Server  │   │  (Go, NOT       │
                    │              │   │   DEPLOYED)      │
                    └──────┬───────┘   └──────────────────┘
                           │
                           │ K8s API
                           ▼
                    ┌──────────────┐
                    │  Sandbox Pod │
                    │  (port 8080) │
                    │  ✅ WORKING  │
                    └──────────────┘
```

---

## What IS Working

| Component | Status |
|---|---|
| Sandbox creation | OK |
| Sandbox listing | OK |
| Task submission (execd) | OK |
| Task status polling | OK |
| Task logs | OK |
| HTTP server inside sandbox (port 8080) | OK |
| Endpoint resolution API | OK (returns URL + headers) |
| Ingress proxy deployment | **NOT DEPLOYED** |
| Ingress routing via `sandbox.dev.superagi.com` | **NOT WORKING** (APISIX 404 — no upstream) |
| Proxy routing via `/sandboxes/{id}/proxy/8080/` | **NOT WORKING** (APISIX 301 redirect) |
| Pod logs API | **NOT WORKING** (500 — likely RBAC or missing error handling) |

---

## Recommended Fix Order

1. **Deploy the ingress proxy** (`components/ingress/`) — this is the core blocker for external sandbox access via `sandbox.dev.superagi.com`
2. **Configure APISIX upstream** for `sandbox.dev.superagi.com` → ingress proxy service
3. **Fix APISIX route priority** — stop intercepting `/sandboxes/*/proxy/*` paths so the FastAPI proxy handler works as a fallback
4. **Fix pod logs RBAC** — grant `pods/log` read permission to the server's service account
5. **Add error handling** to `get_sandbox_logs()` — wrap `read_pod_log` in try/except for proper error messages
