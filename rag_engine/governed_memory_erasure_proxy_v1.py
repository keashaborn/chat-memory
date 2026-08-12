from __future__ import annotations

"""Permissioned Unix-socket proxy for the conversation-erasure API.

Only the browser bearer token and the frozen machine service token cross this
boundary. Legacy actor, owner, cookie, forwarding, and service-token headers
are never copied. The target is a fixed filesystem socket; redirects are not
followed and no TCP or configured HTTP proxy is used.
"""

import asyncio
import http.client
import json
import re
import socket
from dataclasses import dataclass
from typing import Mapping, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response


ERASURE_COLLECTION_PATH = "/memory/conversations/erasure-requests"
SERVICE_TOKEN_HEADER = "x-governed-memory-service-token"
SUCCESSOR_SOCKET_PATH = "/run/governed-memory/http.sock"
MAX_REQUEST_BODY_BYTES = 65_536
MAX_RESPONSE_BODY_BYTES = 131_072
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 12.0
MAX_AUTHORIZATION_BYTES = 16_384
MAX_SERVICE_TOKEN_BYTES = 4_096
_BEARER_RE = re.compile(
    r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
    re.ASCII | re.IGNORECASE,
)
_STATUS_PATH_RE = re.compile(
    r"^/memory/conversations/erasure-requests/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_ALLOWED_STATUS = frozenset({200, 202, 400, 401, 403, 404, 409, 503})


@dataclass(frozen=True, slots=True)
class ProxyResult:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class ErasureProxyTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        service_token: str,
        body: bytes,
    ) -> ProxyResult: ...


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self._socket_path)
        except BaseException:
            connection.close()
            raise
        self.sock = connection


class UnixSocketErasureTransport:
    """Bounded stdlib transport over one fixed permissioned Unix socket."""

    def request(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        service_token: str,
        body: bytes,
    ) -> ProxyResult:
        connection = _UnixHTTPConnection(
            SUCCESSOR_SOCKET_PATH,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        headers = {
            "Authorization": authorization,
            SERVICE_TOKEN_HEADER: service_token,
            "Accept": "application/json",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if connection.sock is not None:
                connection.sock.settimeout(READ_TIMEOUT_SECONDS)
            raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BODY_BYTES:
                raise ValueError("successor response exceeds proxy limit")
            return ProxyResult(
                status_code=response.status,
                headers={name.lower(): value for name, value in response.getheaders()},
                body=raw,
            )
        finally:
            connection.close()


def _failure(code: str, status_code: int = 503) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code}},
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _exact_authorization(request: Request) -> str | None:
    values = [
        value
        for name, value in request.scope.get("headers", ())
        if bytes(name).lower() == b"authorization"
    ]
    if len(values) != 1:
        return None
    raw = bytes(values[0])
    if len(raw) > MAX_AUTHORIZATION_BYTES:
        return None
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if _BEARER_RE.fullmatch(value) is None:
        return None
    return value


async def _bounded_body(request: Request, *, allow_body: bool) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BODY_BYTES:
            raise ValueError("request body exceeds proxy limit")
        chunks.append(bytes(chunk))
    body = b"".join(chunks)
    if allow_body:
        content_type = (request.headers.get("content-type") or "").split(
            ";", 1
        )[0].strip().lower()
        if not body or content_type != "application/json":
            raise ValueError("canonical JSON body required")
    elif body:
        raise ValueError("GET body prohibited")
    return body


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _closed_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _safe_response(result: ProxyResult) -> Response:
    content_type = (
        result.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if (
        result.status_code not in _ALLOWED_STATUS
        or content_type != "application/json"
        or len(result.body) > MAX_RESPONSE_BODY_BYTES
    ):
        return _failure("memory_successor_unavailable")
    try:
        parsed = json.loads(
            result.body,
            object_pairs_hook=_closed_json_object,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return _failure("memory_successor_unavailable")
    if not isinstance(parsed, dict):
        return _failure("memory_successor_unavailable")
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    retry_after = result.headers.get("retry-after")
    if retry_after is not None and retry_after.isdecimal() and int(retry_after) <= 60:
        headers["Retry-After"] = retry_after
    location = result.headers.get("location")
    if location is not None and _STATUS_PATH_RE.fullmatch(location):
        headers["Location"] = location
    try:
        return JSONResponse(
            status_code=result.status_code,
            content=parsed,
            headers=headers,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return _failure("memory_successor_unavailable")


def governed_memory_proxy_service_token_is_valid(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= MAX_SERVICE_TOKEN_BYTES
        and all(33 <= byte <= 126 for byte in encoded)
    )


def create_governed_memory_erasure_proxy_router_v1(
    *,
    service_token: str | None,
    transport: ErasureProxyTransport | None = None,
) -> APIRouter:
    router = APIRouter()
    frozen_token = (
        service_token
        if governed_memory_proxy_service_token_is_valid(service_token)
        else ""
    )
    target = transport or UnixSocketErasureTransport()

    async def proxy(request: Request, *, path: str, allow_body: bool) -> Response:
        if not frozen_token:
            return _failure("memory_successor_proxy_unconfigured")
        if request.url.query:
            return _failure("memory_request_invalid", 400)
        authorization = _exact_authorization(request)
        if authorization is None:
            return _failure("memory_authentication_required", 401)
        try:
            body = await _bounded_body(request, allow_body=allow_body)
        except ValueError:
            return _failure("memory_request_invalid", 400)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    target.request,
                    method=request.method,
                    path=path,
                    authorization=authorization,
                    service_token=frozen_token,
                    body=body,
                ),
                timeout=READ_TIMEOUT_SECONDS + CONNECT_TIMEOUT_SECONDS + 1.0,
            )
        except Exception:
            code = (
                "memory_operation_outcome_unknown"
                if request.method == "POST"
                else "memory_successor_unavailable"
            )
            return _failure(code)
        return _safe_response(result)

    @router.post(ERASURE_COLLECTION_PATH, include_in_schema=False)
    async def request_conversation_erasure_proxy(request: Request) -> Response:
        return await proxy(
            request,
            path=ERASURE_COLLECTION_PATH,
            allow_body=True,
        )

    @router.get(
        ERASURE_COLLECTION_PATH + "/{operation_id}",
        include_in_schema=False,
    )
    async def read_conversation_erasure_proxy(
        operation_id: str,
        request: Request,
    ) -> Response:
        path = ERASURE_COLLECTION_PATH + "/" + operation_id
        if _STATUS_PATH_RE.fullmatch(path) is None:
            return _failure("memory_request_invalid", 400)
        return await proxy(request, path=path, allow_body=False)

    return router


__all__ = [
    "ERASURE_COLLECTION_PATH",
    "ErasureProxyTransport",
    "ProxyResult",
    "SUCCESSOR_SOCKET_PATH",
    "UnixSocketErasureTransport",
    "create_governed_memory_erasure_proxy_router_v1",
    "governed_memory_proxy_service_token_is_valid",
]
