from __future__ import annotations

"""SeeBx platform HTTP security and correlation boundary."""

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from seebx.core.request_ids import get_request_id


PUBLIC_SERVICE_TOKEN_EXACT = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)
PUBLIC_GET_SERVICE_TOKEN_PREFIXES = (
    "/catalog/",
    "/lifeswitch/training/workout_template_shares/preview",
    "/lifeswitch/people/invitations/preview",
)
SENSITIVE_NO_STORE_PREFIXES = (
    "/admin/",
    "/voice/",
    "/telemetry/",
    "/metrics/",
    "/trusted-web/",
    "/threads/active",
    "/attachments",
    "/memory/",
    "/chat-history/",
    "/conversation/",
)
SENSITIVE_NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
    "x-content-type-options": "nosniff",
}


def service_token_required(path: str, method: str) -> bool:
    normalized_path = str(path or "")
    if normalized_path in PUBLIC_SERVICE_TOKEN_EXACT:
        return False
    if normalized_path.startswith(
        ("/docs/", "/redoc/", "/openapi")
    ):
        return False
    if str(method or "").upper() == "GET":
        return not normalized_path.startswith(
            PUBLIC_GET_SERVICE_TOKEN_PREFIXES
        )
    return True


def install_http_boundary(app: FastAPI) -> None:
    """Install the platform boundary once, preserving middleware order."""

    if getattr(app.state, "seebx_http_boundary_installed", False):
        raise RuntimeError("seebx_http_boundary_already_installed")
    app.state.seebx_http_boundary_installed = True

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = get_request_id(request)
        request.state.request_id = request_id
        response = await call_next(request)
        try:
            response.headers["x-request-id"] = request_id
        except Exception:
            pass
        return response

    @app.middleware("http")
    async def service_token_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        expected = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
        if not expected:
            request_id = get_request_id(request)
            return JSONResponse(
                {
                    "status": "unavailable",
                    "detail": "service_token_not_configured",
                },
                status_code=503,
                headers={"x-request-id": request_id},
            )

        if not service_token_required(
            request.url.path,
            request.method,
        ):
            return await call_next(request)

        provided = (
            request.headers.get("x-vs-service-token") or ""
        ).strip()
        if not hmac.compare_digest(provided, expected):
            request_id = get_request_id(request)
            return JSONResponse(
                {
                    "status": "unauthorized",
                    "detail": "missing_or_invalid_service_token",
                },
                status_code=401,
                headers={"x-request-id": request_id},
            )
        return await call_next(request)

    @app.middleware("http")
    async def sensitive_no_store_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(SENSITIVE_NO_STORE_PREFIXES):
            for name, value in SENSITIVE_NO_STORE_HEADERS.items():
                response.headers[name] = value
        return response


__all__ = [
    "PUBLIC_GET_SERVICE_TOKEN_PREFIXES",
    "PUBLIC_SERVICE_TOKEN_EXACT",
    "SENSITIVE_NO_STORE_HEADERS",
    "SENSITIVE_NO_STORE_PREFIXES",
    "install_http_boundary",
    "service_token_required",
]
