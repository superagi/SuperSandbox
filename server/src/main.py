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
FastAPI application entry point for OpenSandbox Lifecycle API.

This module initializes the FastAPI application with middleware, routes,
and configuration for the sandbox lifecycle management service.
"""

import copy
import logging.config
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from src.config import get_config_path, load_config
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

# Load configuration before initializing routers/middleware
app_config = load_config()

# Unify logging format (including uvicorn access/error logs) with timestamp prefix.
_log_config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
_fmt = "%(levelprefix)s %(asctime)s [%(request_id)s] %(name)s: %(message)s"
_datefmt = "%Y-%m-%d %H:%M:%S%z"

# Inject request_id into log records so one request's logs can be correlated.
_log_config["filters"] = {
    "request_id": {"()": "src.middleware.request_id.RequestIdFilter"},
}
_log_config["handlers"]["default"]["filters"] = ["request_id"]
_log_config["handlers"]["access"]["filters"] = ["request_id"]

# Enable colors and set format for both default and access loggers
_log_config["formatters"]["default"]["fmt"] = _fmt
_log_config["formatters"]["default"]["datefmt"] = _datefmt
_log_config["formatters"]["default"]["use_colors"] = True

_log_config["formatters"]["access"]["fmt"] = _fmt
_log_config["formatters"]["access"]["datefmt"] = _datefmt
_log_config["formatters"]["access"]["use_colors"] = True

# Ensure project loggers (src.*) emit at configured level using the default handler.
_log_config["loggers"]["src"] = {
    "handlers": ["default"],
    "level": app_config.server.log_level.upper(),
    "propagate": False,
}

logging.config.dictConfig(_log_config)
logging.getLogger().setLevel(
    getattr(logging, app_config.server.log_level.upper(), logging.INFO)
)

from src.api.lifecycle import router  # noqa: E402
from src.middleware.auth import AuthMiddleware  # noqa: E402
from src.middleware.request_id import RequestIdMiddleware  # noqa: E402
from src.services.runtime_resolver import (  # noqa: E402
    validate_secure_runtime_on_startup,
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=180.0)

    # Validate secure runtime configuration at startup
    try:
        # Determine which runtime client to create based on config
        docker_client = None
        k8s_client = None
        runtime_type = app_config.runtime.type

        if runtime_type == "docker":
            import docker

            docker_client = docker.from_env()
            logger.info("Validating secure runtime for Docker backend")
        elif runtime_type == "kubernetes":
            from src.services.k8s.client import K8sClient

            k8s_client = K8sClient(app_config.kubernetes)
            logger.info("Validating secure runtime for Kubernetes backend")

        await validate_secure_runtime_on_startup(
            app_config,
            docker_client=docker_client,
            k8s_client=k8s_client,
        )

    except Exception as exc:
        logger.error("Secure runtime validation failed: %s", exc)
        raise

    yield
    await app.state.http_client.aclose()


# Initialize FastAPI application
app = FastAPI(
    title="SuperSandbox API",
    version="0.2.0",
    description=(
        "SuperSandbox manages isolated Linux sandbox environments on Kubernetes with gVisor.\n\n"
        "## Sandbox Lifecycle\n"
        "- **Create** — `POST /sandboxes` — Provisions a sandbox with a persistent /workspace volume\n"
        "- **Get** — `GET /sandboxes/{id}` — Returns sandbox status (Pending, Running, Paused, Terminated)\n"
        "- **List** — `GET /sandboxes` — List sandboxes with filtering and pagination\n"
        "- **Pause** — `POST /sandboxes/{id}/pause` — Scales pod to zero; /workspace data persists\n"
        "- **Resume** — `POST /sandboxes/{id}/resume` — Recreates pod; /workspace data restored\n"
        "- **Delete** — `DELETE /sandboxes/{id}` — Removes sandbox, pod, and workspace volume\n\n"
        "## Logs & Terminal\n"
        "- **Logs** — `GET /sandboxes/{id}/logs?tail=100&follow=false` — Pod stdout/stderr\n"
        "- **Terminal** — `WebSocket /sandboxes/{id}/terminal` — Interactive bash shell (PTY). "
        "Connect with any WebSocket client or xterm.js. Send text (keystrokes), receive text (terminal output).\n\n"
        "## Proxy\n"
        "- **Endpoint** — `GET /sandboxes/{id}/endpoints/{port}` — Get pod IP and port\n"
        "- **Proxy** — `ANY /sandboxes/{id}/proxy/{port}/{path}` — HTTP proxy to sandbox services\n"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Attach global config for runtime access
app.state.config = app_config

# Middleware run in reverse order of addition: last added = first to run (outermost).
# Add auth and CORS first so they run after RequestIdMiddleware.
app.add_middleware(AuthMiddleware, config=app_config)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# RequestIdMiddleware last = outermost: runs first, so every response (including
# 401 from AuthMiddleware) gets X-Request-ID and logs have request_id in context.
app.add_middleware(RequestIdMiddleware)

# Include API routes at versioned prefix only
app.include_router(router, prefix="/v1")

DEFAULT_ERROR_CODE = "GENERAL::UNKNOWN_ERROR"
DEFAULT_ERROR_MESSAGE = "An unexpected error occurred."


def _normalize_error_detail(detail: Any) -> dict[str, str]:
    """
    Ensure HTTP errors always conform to {"code": "...", "message": "..."}.
    """
    if isinstance(detail, dict):
        code = detail.get("code") or DEFAULT_ERROR_CODE
        message = detail.get("message") or DEFAULT_ERROR_MESSAGE
        return {"code": code, "message": message}
    message = str(detail) if detail else DEFAULT_ERROR_MESSAGE
    return {"code": DEFAULT_ERROR_CODE, "message": message}


@app.exception_handler(HTTPException)
async def sandbox_http_exception_handler(request: Request, exc: HTTPException):
    """
    Flatten FastAPI HTTPException payload to the standard error schema.
    """
    content = _normalize_error_detail(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema["components"] = openapi_schema.get("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "apiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "OPEN-SANDBOX-API-KEY",
            "description": (
                "API Key for authentication (optional — only required when the server "
                "has an API key configured). Provide via this header or set the "
                "OPEN_SANDBOX_API_KEY environment variable for SDK clients."
            ),
        }
    }
    # Apply security globally but as optional (empty object = no auth required)
    openapi_schema["security"] = [{"apiKeyAuth": []}, {}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        dict: Health status
    """
    return {"status": "healthy"}


_SENSITIVE_KEYS = {"api_key", "kubeconfig_path", "password"}


def _redact(obj):
    """Recursively redact sensitive fields from a config dict."""
    if isinstance(obj, dict):
        return {
            k: ("******" if k in _SENSITIVE_KEYS and v else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item) for item in obj]
    return obj


@app.get("/config", tags=["System"])
async def get_server_config():
    """
    Return the active server configuration with sensitive fields redacted.
    """
    config_data = _redact(app_config.model_dump())
    config_data["config_path"] = str(get_config_path())
    return config_data


if __name__ == "__main__":
    import uvicorn

    # Run the application
    uvicorn.run(
        "src.main:app",
        host=app_config.server.host,
        port=app_config.server.port,
        reload=True,
        log_config=_log_config,
    )
