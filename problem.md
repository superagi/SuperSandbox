# Staging Environment Issues — Sandbox Endpoint & Logs

**Date:** 2026-03-31
**Sandbox ID:** `4e535c1e-cebd-4635-9ba6-7af38eef1cff`
**Environment:** Staging (`sandbox.superagii.com`)

---

## Test Setup

- Python HTTP server started on port 8080 inside a running sandbox
- Server confirmed working from inside the sandbox via `curl localhost:8080`
- Response: `{"message": "Hello from SuperSandbox!", "status": "running"}`

---

## Issue 1: Ingress Endpoint Returns 404

**Symptom:**
```bash
curl -k "https://sandbox.dev.superagi.com/" \
  -H "OpenSandbox-Ingress-To: 4e535c1e-cebd-4635-9ba6-7af38eef1cff-8080"
# => 404 Not Found
```

**Endpoint API response:**
```json
{
  "endpoint": "sandbox.dev.superagi.com",
  "headers": {
    "OpenSandbox-Ingress-To": "4e535c1e-cebd-4635-9ba6-7af38eef1cff-8080"
  }
}
```

**Root Cause (probable):**
APISIX gateway does not have a route configured to match the `OpenSandbox-Ingress-To` header and forward traffic to the sandbox pod. The endpoint API returns the ingress URL assuming the route exists, but APISIX has no matching rule.

**Possible fixes:**
1. Verify APISIX route rules exist for header-based sandbox routing (`OpenSandbox-Ingress-To` header matching)
2. Check if the ingress controller/operator is supposed to auto-create APISIX routes when sandboxes are created — it may not be running or may lack permissions
3. Verify the APISIX config at `components/ingress/` matches the staging deployment
4. Check APISIX admin API for existing routes: `curl http://<apisix-admin>:9180/apisix/admin/routes`

---

## Issue 2: Proxy Route Returns 301 Redirect

**Symptom:**
```bash
curl -D - "https://sandbox.superagii.com/sandboxes/{id}/proxy/8080/" \
  -H "OPEN-SANDBOX-API-KEY: ..."
# => 301 Moved Permanently
# Location: https://sandbox.dev.superagi.com:443/
```

**Root Cause (probable):**
APISIX is matching the `/sandboxes/{id}/proxy/8080/` path with a catch-all redirect rule before it reaches the FastAPI server. The proxy route never hits the Python backend — APISIX intercepts it first.

**Possible fixes:**
1. Check APISIX route priority — ensure the API server routes (e.g., `/sandboxes/*`) have higher priority than the ingress redirect rules
2. Verify the APISIX upstream for the sandbox API server is correctly configured and the route matches `/sandboxes/*/proxy/*` paths
3. The redirect to `sandbox.dev.superagi.com` suggests a misconfigured route that's treating proxy paths as ingress traffic instead of API traffic

---

## Issue 3: Pod Logs API Returns 500

**Symptom:**
```bash
curl "https://sandbox.superagii.com/sandboxes/{id}/logs" \
  -H "OPEN-SANDBOX-API-KEY: ..."
# => 500 Internal Server Error
```

**Code path:**
1. `get_sandbox_logs()` — `server/src/api/lifecycle.py:557`
2. `get_sandbox_pod_name()` — `server/src/services/k8s/kubernetes_service.py:981`
3. `read_pod_log(pod_name, container="sandbox")` — `server/src/services/k8s/client.py:388`
4. K8s API: `read_namespaced_pod_log()` — `client.py:420`

**Possible causes:**

### A. Container name mismatch
The logs API hardcodes `container="sandbox"` (`kubernetes_service.py:1041`). If the pod's container is named differently, the K8s API will fail.

**Verify:**
```bash
kubectl get pod -l opensandbox.io/id=4e535c1e-cebd-4635-9ba6-7af38eef1cff \
  -o jsonpath='{.items[*].spec.containers[*].name}'
```

### B. RBAC — missing `pods/log` permission
The server's service account may lack permission to read pod logs.

**Verify:**
```bash
kubectl auth can-i get pods/log --as=system:serviceaccount:<namespace>:<sa-name>
```

### C. No error handling around `read_pod_log`
`get_sandbox_logs()` in `kubernetes_service.py:1025-1044` has no `try/except` — any K8s API exception propagates as an unhandled 500. Compare with `get_endpoint()` which wraps K8s calls in try/except.

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
| Ingress routing (APISIX) | NOT WORKING (404) |
| Proxy routing (APISIX) | NOT WORKING (301 redirect) |
| Pod logs API | NOT WORKING (500) |

---

## Recommended Investigation Order

1. **APISIX routes** — check if header-based routing rules exist for `OpenSandbox-Ingress-To`. This is the core issue blocking external sandbox access.
2. **APISIX route priority** — ensure API server routes aren't being hijacked by ingress redirect rules (fixes proxy 301).
3. **Pod logs RBAC** — verify service account permissions for `pods/log`.
4. **Add error handling** — wrap `read_pod_log` in try/except to get proper error messages instead of generic 500.
