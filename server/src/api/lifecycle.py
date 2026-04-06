# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
API routes for OpenSandbox Lifecycle API.

This module defines FastAPI routes that map to the OpenAPI specification endpoints.
All business logic is delegated to the service layer that backs each operation.
"""

import asyncio
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter, Header, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response, StreamingResponse

from src.api.schema import (
    CreateSandboxRequest,
    CreateSandboxResponse,
    Endpoint,
    ErrorResponse,
    ListSandboxesRequest,
    ListSandboxesResponse,
    PaginationRequest,
    RenewSandboxExpirationRequest,
    RenewSandboxExpirationResponse,
    Sandbox,
    SandboxFilter,
    TerminalTokenResponse,
    UpdateSandboxEnvRequest,
    UpdateSandboxEnvResponse,
    UpdateSandboxResourceLimitsRequest,
    UpdateSandboxResourceLimitsResponse,
)
from src.config import get_config
from src.services.factory import create_sandbox_service
from src.services.terminal_auth import create_terminal_token, validate_terminal_token

# RFC 2616 Section 13.5.1
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Headers that shouldn't be forwarded to untrusted/internal backends
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
}

# Initialize router
router = APIRouter(tags=["Sandboxes"])

# Initialize service based on configuration from config.toml (defaults to docker)
sandbox_service = create_sandbox_service()


# ============================================================================
# Sandbox CRUD Operations
# ============================================================================

@router.post(
    "/sandboxes",
    response_model=CreateSandboxResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Sandbox creation accepted for asynchronous provisioning"},
        400: {"model": ErrorResponse, "description": "The request was invalid or malformed"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        409: {"model": ErrorResponse, "description": "The operation conflicts with the current state"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def create_sandbox(
    request: CreateSandboxRequest,
    wait: bool = Query(True, description="When false, return immediately with Pending status and provision in the background. Poll GET /sandboxes/{id} for readiness."),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> CreateSandboxResponse:
    """
    Create a sandbox from a container image.

    Creates a new sandbox from a container image with optional resource limits,
    environment variables, and metadata. Sandboxes are provisioned directly from
    the specified image without requiring a pre-created template.

    When ``wait=false``, the sandbox is created asynchronously: the endpoint
    returns immediately with ``Pending`` status and the caller should poll
    ``GET /sandboxes/{id}`` until the status transitions to ``Running``
    (or ``Failed``).

    Args:
        request: Sandbox creation request
        wait: If True (default), block until sandbox is running. If False, return immediately.
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        CreateSandboxResponse: Accepted sandbox creation request

    Raises:
        HTTPException: If sandbox creation scheduling fails
    """
    if wait:
        return sandbox_service.create_sandbox(request)
    return sandbox_service.create_sandbox_async(request)


# Search endpoint
@router.get(
    "/sandboxes",
    response_model=ListSandboxesResponse,
    responses={
        200: {"description": "Paginated collection of sandboxes"},
        400: {"model": ErrorResponse, "description": "The request was invalid or malformed"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def list_sandboxes(
    state: Optional[List[str]] = Query(None, description="Filter by lifecycle state. Pass multiple times for OR logic."),
    metadata: Optional[str] = Query(None, description="Arbitrary metadata key-value pairs for filtering (URL encoded)."),
    page: int = Query(1, ge=1, description="Page number for pagination"),
    page_size: int = Query(20, ge=1, le=200, alias="pageSize", description="Number of items per page"),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> ListSandboxesResponse:
    """
    List sandboxes with optional filtering and pagination.

    List all sandboxes with optional filtering and pagination using query parameters.
    All filter conditions use AND logic. Multiple `state` parameters use OR logic within states.

    Args:
        state: Filter by lifecycle state.
        metadata: Arbitrary metadata key-value pairs for filtering.
        page: Page number for pagination.
        page_size: Number of items per page.
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        ListSandboxesResponse: Paginated list of sandboxes
    """
    # Parse metadata query string into dictionary
    metadata_dict = {}
    if metadata:
        from urllib.parse import parse_qsl
        try:
            # Parse query string format: key=value&key2=value2
            # strict_parsing=True rejects malformed segments like "a=1&broken"
            parsed = parse_qsl(metadata, keep_blank_values=True, strict_parsing=True)
            metadata_dict = dict(parsed)
        except Exception as e:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_METADATA_FORMAT", "message": f"Invalid metadata format: {str(e)}"}
            )

    # Construct request object
    request = ListSandboxesRequest(
        filter=SandboxFilter(state=state, metadata=metadata_dict if metadata_dict else None),
        pagination=PaginationRequest(page=page, pageSize=page_size)
    )

    import logging
    logger = logging.getLogger(__name__)
    logger.info("ListSandboxes: %s", request.filter)

    # Delegate to the service layer for filtering and pagination
    return sandbox_service.list_sandboxes(request)


@router.get(
    "/sandboxes/{sandbox_id}",
    response_model=Sandbox,
    responses={
        200: {"description": "Sandbox current state and metadata"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def get_sandbox(
    sandbox_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> Sandbox:
    """
    Fetch a sandbox by id.

    Returns the complete sandbox information including image specification,
    status, metadata, and timestamps.

    Args:
        sandbox_id: Unique sandbox identifier
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        Sandbox: Complete sandbox information

    Raises:
        HTTPException: If sandbox not found or access denied
    """
    # Delegate to the service layer for sandbox lookup
    return sandbox_service.get_sandbox(sandbox_id)


@router.delete(
    "/sandboxes/{sandbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Sandbox successfully deleted"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        409: {"model": ErrorResponse, "description": "The operation conflicts with the current state"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def delete_sandbox(
    sandbox_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> Response:
    """
    Delete a sandbox.

    Terminates sandbox execution. The sandbox will transition through Stopping state to Terminated.

    Args:
        sandbox_id: Unique sandbox identifier
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        Response: 204 No Content

    Raises:
        HTTPException: If sandbox not found or deletion fails
    """
    # Delegate to the service layer for deletion
    sandbox_service.delete_sandbox(sandbox_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/sandboxes/{sandbox_id}",
    response_model=UpdateSandboxResourceLimitsResponse,
    response_model_by_alias=True,
    responses={
        200: {"description": "Resource limits updated successfully"},
        400: {"model": ErrorResponse, "description": "Invalid resource format or storage shrink attempted"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
        409: {"model": ErrorResponse, "description": "Sandbox in a state that doesn't allow updates"},
        422: {"model": ErrorResponse, "description": "StorageClass doesn't support volume expansion"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def update_sandbox_resource_limits(
    sandbox_id: str,
    request: UpdateSandboxResourceLimitsRequest,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> UpdateSandboxResourceLimitsResponse:
    """
    Update resource limits on a running or paused sandbox.

    Allows updating CPU, memory, and storage limits. All fields in resourceLimits
    are optional — only provided fields are updated.

    For CPU & memory: updates the container's resource limits/requests on the
    underlying pod via the workload controller.

    For storage: expands the workspace PVC. Shrinking is not supported.

    Args:
        sandbox_id: Unique sandbox identifier
        request: Resource limits update request
        x_request_id: Unique request identifier for tracing (optional).

    Returns:
        UpdateSandboxResourceLimitsResponse: Updated sandbox state and resource limits
    """
    return sandbox_service.update_resource_limits(sandbox_id, request)


@router.put(
    "/sandboxes/{sandbox_id}/env",
    response_model=UpdateSandboxEnvResponse,
    responses={
        200: {"description": "Environment variables updated successfully"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
        409: {"model": ErrorResponse, "description": "Sandbox in a state that doesn't allow updates"},
        501: {"model": ErrorResponse, "description": "Runtime does not support env updates"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def update_sandbox_env(
    sandbox_id: str,
    request: UpdateSandboxEnvRequest,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> UpdateSandboxEnvResponse:
    """
    Update environment variables on a running or paused sandbox.

    Replaces all user-defined environment variables on the sandbox CRD.
    Internal env vars (e.g. EXECD) are preserved automatically.

    - Running sandbox: env is patched in the CRD. Takes full effect on next pod restart.
    - Paused sandbox: env is patched in the CRD. Applied when the sandbox is resumed.

    Args:
        sandbox_id: Unique sandbox identifier
        request: Env update request with new env vars
        x_request_id: Unique request identifier for tracing (optional).

    Returns:
        UpdateSandboxEnvResponse: Updated environment variables
    """
    return sandbox_service.update_env(sandbox_id, request)


# ============================================================================
# Sandbox Lifecycle Operations
# ============================================================================

@router.post(
    "/sandboxes/{sandbox_id}/pause",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Pause operation accepted"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        409: {"model": ErrorResponse, "description": "The operation conflicts with the current state"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def pause_sandbox(
    sandbox_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> Response:
    """
    Pause a running sandbox.

    Scales the sandbox pod to zero while preserving the workspace PVC.
    All data in /workspace survives the pause. The sandbox must be in Running state.
    Returns 409 if the sandbox is not currently Running.
    """
    # Delegate to the service layer for pause orchestration
    sandbox_service.pause_sandbox(sandbox_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/sandboxes/{sandbox_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Resume operation accepted"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        409: {"model": ErrorResponse, "description": "The operation conflicts with the current state"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def resume_sandbox(
    sandbox_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> Response:
    """
    Resume a paused sandbox.

    Recreates the sandbox pod and remounts the workspace PVC at /workspace.
    All data written before pause is available again. The sandbox must be in Paused state.
    Returns 409 if the sandbox is not currently Paused.
    """
    # Delegate to the service layer for resume orchestration
    sandbox_service.resume_sandbox(sandbox_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/sandboxes/{sandbox_id}/renew-expiration",
    response_model=RenewSandboxExpirationResponse,
    response_model_exclude_none=True,
    responses={
        200: {"description": "Sandbox expiration updated successfully"},
        400: {"model": ErrorResponse, "description": "The request was invalid or malformed"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        409: {"model": ErrorResponse, "description": "The operation conflicts with the current state"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def renew_sandbox_expiration(
    sandbox_id: str,
    request: RenewSandboxExpirationRequest,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> RenewSandboxExpirationResponse:
    """
    Renew sandbox expiration.

    Renews the absolute expiration time of a sandbox.
    The new expiration time must be in the future and after the current expiresAt time.

    Args:
        sandbox_id: Unique sandbox identifier
        request: Renewal request with new expiration time
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        RenewSandboxExpirationResponse: Updated expiration time

    Raises:
        HTTPException: If sandbox not found or renewal fails
    """
    # Delegate to the service layer for expiration updates
    return sandbox_service.renew_expiration(sandbox_id, request)


# ============================================================================
# Sandbox Endpoints
# ============================================================================

@router.get(
    "/sandboxes/{sandbox_id}/endpoints/{port}",
    response_model=Endpoint,
    response_model_exclude_none=True,
    responses={
        200: {"description": "Endpoint retrieved successfully"},
        401: {"model": ErrorResponse, "description": "Authentication credentials are missing or invalid"},
        403: {"model": ErrorResponse, "description": "The authenticated user lacks permission for this operation"},
        404: {"model": ErrorResponse, "description": "The requested resource does not exist"},
        500: {"model": ErrorResponse, "description": "An unexpected server error occurred"},
    },
)
async def get_sandbox_endpoint(
    request: Request,
    sandbox_id: str,
    port: int,
    use_server_proxy: bool = Query(False, description="Whether to return a server-proxied URL"),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID", description="Unique request identifier for tracing"),
) -> Endpoint:
    """
    Get sandbox access endpoint.

    Returns the public access endpoint URL for accessing a service running on a specific port
    within the sandbox. The service must be listening on the specified port inside the sandbox
    for the endpoint to be available.

    Args:
        request: FastAPI request object
        sandbox_id: Unique sandbox identifier
        port: Port number where the service is listening inside the sandbox (1-65535)
        use_server_proxy: Whether to return a server-proxied URL
        x_request_id: Unique request identifier for tracing (optional; server generates if omitted).

    Returns:
        Endpoint: Public endpoint URL

    Raises:
        HTTPException: If sandbox not found or endpoint not available
    """
    # Delegate to the service layer for endpoint resolution
    endpoint = sandbox_service.get_endpoint(sandbox_id, port)

    if use_server_proxy:
        # Construct proxy URL
        base_url = str(request.base_url).rstrip("/")
        base_url = base_url.replace("https://", "").replace("http://", "")
        endpoint.endpoint = f"{base_url}/sandboxes/{sandbox_id}/proxy/{port}"

    return endpoint


@router.api_route(
    "/sandboxes/{sandbox_id}/proxy/{port}/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy_sandbox_endpoint_request(request: Request, sandbox_id: str, port: int, full_path: str):
    """
    Receives all incoming requests, determines the target sandbox from path parameter,
    and asynchronously proxies the request to it.
    """

    _touch_sandbox_activity(sandbox_id)
    endpoint = sandbox_service.get_endpoint(sandbox_id, port, resolve_internal=True)

    target_host = endpoint.endpoint
    query_string = request.url.query

    client: httpx.AsyncClient = request.app.state.http_client

    try:
        upgrade_header = request.headers.get("Upgrade", "")
        if upgrade_header.lower() == "websocket":
            raise HTTPException(status_code=400, detail="Websocket upgrade is not supported yet")

        # Filter headers
        hop_by_hop = set(HOP_BY_HOP_HEADERS)
        connection_header = request.headers.get("connection")
        if connection_header:
            hop_by_hop.update(
                header.strip().lower()
                for header in connection_header.split(",")
                if header.strip()
            )
        headers = {}
        for key, value in request.headers.items():
            key_lower = key.lower()
            if (
                key_lower != "host"
                and key_lower not in hop_by_hop
                and key_lower not in SENSITIVE_HEADERS
            ):
                headers[key] = value

        req = client.build_request(
            method=request.method,
            url=f"http://{target_host}/{full_path}",
            params=query_string if query_string else None,
            headers=headers,
            content=request.stream() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None,
        )

        resp = await client.send(req, stream=True)

        hop_by_hop = set(HOP_BY_HOP_HEADERS)
        connection_header = resp.headers.get("connection")
        if connection_header:
            hop_by_hop.update(
                header.strip().lower()
                for header in connection_header.split(",")
                if header.strip()
            )
        response_headers = {
            key: value
            for key, value in resp.headers.items()
            if key.lower() not in hop_by_hop
        }

        return StreamingResponse(
            content=resp.aiter_bytes(),
            status_code=resp.status_code,
            headers=response_headers,
        )
    except httpx.ConnectError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to the backend sandbox {endpoint}: {e}",
        )
    except HTTPException:
        # Preserve explicit HTTP exceptions raised above (e.g. websocket upgrade not supported).
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An internal error occurred in the proxy: {e}"
        )


# ============================================================================
# Sandbox Logs & Terminal
# ============================================================================

_lifecycle_logger = logging.getLogger(__name__)


def _touch_sandbox_activity(sandbox_id: str) -> None:
    """Best-effort activity marker update."""
    try:
        sandbox_service.touch_last_activity(sandbox_id)
    except Exception:
        _lifecycle_logger.debug(
            "Failed to touch activity for sandbox %s",
            sandbox_id,
            exc_info=True,
        )


@router.get(
    "/sandboxes/{sandbox_id}/logs",
    responses={
        200: {"description": "Pod logs returned successfully"},
        404: {"description": "Sandbox or pod not found"},
        409: {"description": "Sandbox is paused"},
        500: {"description": "An unexpected server error occurred"},
    },
)
async def get_sandbox_logs(
    sandbox_id: str,
    tail: int = Query(100, ge=1, le=10000, description="Number of lines from the end"),
    follow: bool = Query(False, description="Stream logs in real time"),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> Response:
    """
    Get logs from a sandbox pod.

    Returns the stdout/stderr output of the sandbox container.

    - `tail`: Number of lines from the end (default 100, max 10000)
    - `follow`: If true, streams logs in real time (text/plain chunked response)

    Returns 404 if sandbox or pod not found. Returns 409 if sandbox is paused.
    """
    if follow:
        resp = sandbox_service.get_sandbox_logs(
            sandbox_id, tail_lines=tail, follow=True
        )

        async def stream_logs():
            try:
                for line in resp:
                    if isinstance(line, bytes):
                        yield line
                    else:
                        yield line.encode("utf-8")
            except Exception:
                pass
            finally:
                resp.close()

        return StreamingResponse(
            stream_logs(),
            media_type="text/plain; charset=utf-8",
        )
    else:
        logs = sandbox_service.get_sandbox_logs(
            sandbox_id, tail_lines=tail, follow=False
        )
        return Response(content=logs, media_type="text/plain; charset=utf-8")


@router.post(
    "/sandboxes/{sandbox_id}/terminal/token",
    response_model=TerminalTokenResponse,
    responses={
        200: {"description": "Terminal access token"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
    },
)
async def get_terminal_token(
    sandbox_id: str,
    request: Request,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
):
    """
    Generate a short-lived JWT token for authenticating a WebSocket terminal session.

    The returned token must be passed as a query parameter when connecting to the
    WebSocket terminal endpoint: `ws://<host>/sandboxes/{sandbox_id}/terminal?token=<token>`
    """
    _touch_sandbox_activity(sandbox_id)
    # Verify sandbox exists and is running
    sb = sandbox_service.get_sandbox(sandbox_id)
    if sb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"Sandbox {sandbox_id} not found"},
        )

    cfg = get_config()
    secret = cfg.server.terminal_token_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": "Terminal token secret not configured"},
        )

    ttl = cfg.server.terminal_token_ttl_seconds
    token, expires_at = create_terminal_token(secret, sandbox_id, ttl_seconds=ttl)

    # Build the WebSocket URL from the request
    ws_scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{request.url.netloc}/sandboxes/{sandbox_id}/terminal?token={token}"

    return TerminalTokenResponse(url=ws_url, token=token, expiresAt=expires_at)


@router.websocket("/sandboxes/{sandbox_id}/terminal")
async def sandbox_terminal(websocket: WebSocket, sandbox_id: str, token: Optional[str] = Query(None)):
    """
    Interactive WebSocket terminal to a sandbox pod.

    Opens an interactive bash shell (PTY) in the sandbox container via WebSocket.

    - Connect: `ws://<host>/sandboxes/{sandbox_id}/terminal?token=<jwt>`
    - Send: text messages (keystrokes)
    - Receive: text messages (terminal output including ANSI escape codes)
    - Close codes: 1008 (sandbox not found/paused/auth failed), 1011 (internal error)

    Compatible with xterm.js or any WebSocket terminal emulator.
    Returns close frame with reason if sandbox is not running.
    """
    # Validate JWT token before accepting the connection
    cfg = get_config()
    secret = cfg.server.terminal_token_secret
    if secret:
        if not token:
            await websocket.close(code=1008, reason="Missing terminal token")
            return
        err = validate_terminal_token(secret, token, sandbox_id)
        if err:
            await websocket.close(code=1008, reason=err[:123])
            return

    await websocket.accept()
    _touch_sandbox_activity(sandbox_id)

    try:
        ws_client = sandbox_service.exec_sandbox_terminal(sandbox_id)
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            reason = detail.get("message", str(detail))
        else:
            reason = str(detail)
        await websocket.close(code=1008, reason=reason[:123])
        return
    except Exception as e:
        await websocket.close(code=1011, reason=str(e)[:123])
        return

    async def read_from_k8s():
        """Read from K8s exec stream → send to WebSocket."""
        try:
            while ws_client.is_open():
                data = await asyncio.to_thread(ws_client.read_stdout, timeout=1)
                if data:
                    _touch_sandbox_activity(sandbox_id)
                    await websocket.send_text(data)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            _lifecycle_logger.debug("K8s read loop ended: %s", e)

    async def write_to_k8s():
        """Receive from WebSocket → write to K8s exec stream."""
        try:
            while True:
                data = await websocket.receive_text()
                _touch_sandbox_activity(sandbox_id)
                ws_client.write_stdin(data)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            _lifecycle_logger.debug("WebSocket write loop ended: %s", e)

    async def periodic_activity_touch():
        """Periodic heartbeat while terminal session is active."""
        try:
            while ws_client.is_open():
                await asyncio.sleep(30)
                _touch_sandbox_activity(sandbox_id)
        except Exception:
            pass

    read_task = asyncio.create_task(read_from_k8s())
    write_task = asyncio.create_task(write_to_k8s())
    heartbeat_task = asyncio.create_task(periodic_activity_touch())

    try:
        done, pending = await asyncio.wait(
            [read_task, write_task, heartbeat_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        ws_client.close()
        try:
            await websocket.close()
        except Exception:
            pass


# ============================================================================
# Task Execution
# ============================================================================

@router.post(
    "/sandboxes/{sandbox_id}/tasks",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Task submitted successfully"},
        404: {"model": ErrorResponse, "description": "Sandbox not found"},
        409: {"model": ErrorResponse, "description": "Sandbox is paused"},
        502: {"model": ErrorResponse, "description": "Failed to communicate with execd"},
    },
)
async def submit_task(
    sandbox_id: str,
    request: Request,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
):
    """
    Submit a background task to a sandbox.

    Runs a shell command in the background inside the sandbox container via execd.
    Returns a task ID that can be used to poll status, stream logs, or kill the task.

    Request body:
    - `command` (required): Shell command to execute
    - `cwd` (optional): Working directory (default: /workspace)
    - `timeout` (optional): Max runtime in milliseconds
    - `envs` (optional): Environment variables as key-value pairs
    """
    _touch_sandbox_activity(sandbox_id)
    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TASK::INVALID_REQUEST", "message": "command is required"},
        )
    result = sandbox_service.submit_task(
        sandbox_id=sandbox_id,
        command=command,
        cwd=body.get("cwd", "/workspace"),
        timeout_ms=body.get("timeout"),
        envs=body.get("envs"),
    )
    return result


@router.get(
    "/sandboxes/{sandbox_id}/tasks/{task_id}",
    responses={
        200: {"description": "Task status"},
        404: {"model": ErrorResponse, "description": "Sandbox or task not found"},
        409: {"model": ErrorResponse, "description": "Sandbox is paused"},
    },
)
async def get_task_status(
    sandbox_id: str,
    task_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
):
    """
    Get task execution status.

    Returns whether the task is still running, its exit code, and timestamps.
    """
    _touch_sandbox_activity(sandbox_id)
    return sandbox_service.get_task_status(sandbox_id, task_id)


@router.get(
    "/sandboxes/{sandbox_id}/tasks/{task_id}/logs",
    responses={
        200: {"description": "Task logs (plain text)"},
        404: {"model": ErrorResponse, "description": "Sandbox or task not found"},
        409: {"model": ErrorResponse, "description": "Sandbox is paused"},
    },
)
async def get_task_logs(
    sandbox_id: str,
    task_id: str,
    cursor: Optional[int] = Query(None, ge=0, description="Line cursor for incremental reads"),
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> Response:
    """
    Get task stdout/stderr logs.

    Supports cursor-based incremental reads for polling long-running tasks.
    Pass the cursor from the `X-Task-Log-Cursor` response header to get only new lines.
    """
    _touch_sandbox_activity(sandbox_id)
    result = sandbox_service.get_task_logs(sandbox_id, task_id, cursor=cursor)
    headers = {}
    if result.get("cursor"):
        headers["X-Task-Log-Cursor"] = str(result["cursor"])
    return Response(
        content=result["body"] if isinstance(result["body"], str) else str(result["body"]),
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


@router.delete(
    "/sandboxes/{sandbox_id}/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Task killed successfully"},
        404: {"model": ErrorResponse, "description": "Sandbox or task not found"},
        409: {"model": ErrorResponse, "description": "Sandbox is paused"},
    },
)
async def kill_task(
    sandbox_id: str,
    task_id: str,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
) -> Response:
    """
    Kill a running task.

    Sends a termination signal to the running command inside the sandbox.
    """
    _touch_sandbox_activity(sandbox_id)
    sandbox_service.kill_task(sandbox_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
