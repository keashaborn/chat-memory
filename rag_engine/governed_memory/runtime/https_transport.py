from __future__ import annotations

"""Retry-free bounded HTTPS transport with an injected test seam."""

from dataclasses import dataclass, field
import http.client
import re
import ssl
from typing import Callable, Mapping, Protocol
from urllib.parse import SplitResult, urlsplit

from ..contracts import ContractViolation, canonical_json_bytes, require_exact_int


_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
DEFAULT_MAX_REQUEST_BYTES = 262_144
DEFAULT_MAX_RESPONSE_BYTES = 524_288


class HttpsTransportError(RuntimeError):
    """Base transport error; messages are stable codes and never include bodies."""


class HttpsRequestRejectedBeforeSend(HttpsTransportError):
    """Local validation proved that no connection attempt occurred."""


class HttpsOutcomeUnknown(HttpsTransportError):
    """A failure occurred after the caller persisted its dispatch marker."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpsRequest:
    url: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        _validated_target(self.url)
        _validated_headers(self.headers)
        if not isinstance(self.body, bytes) or not self.body:
            raise ContractViolation("invalid_https_request_body")
        if len(self.body) > DEFAULT_MAX_REQUEST_BYTES:
            raise ContractViolation("https_request_body_too_large")
        require_exact_int(
            self.max_response_bytes,
            code="invalid_https_response_limit",
            minimum=1,
            maximum=4_194_304,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpsResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        require_exact_int(
            self.status,
            code="invalid_https_response_status",
            minimum=100,
            maximum=599,
        )
        _validated_headers(self.headers, allow_duplicate_names=True)
        if not isinstance(self.body, bytes):
            raise ContractViolation("invalid_https_response_body")

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        values = [value for key, value in self.headers if key.lower() == lowered]
        if len(values) > 1:
            raise ContractViolation("duplicate_https_response_header")
        return values[0] if values else None


class HttpsTransport(Protocol):
    def post(self, request: HttpsRequest) -> HttpsResponse:
        """Perform exactly one POST attempt; never retry or follow redirects."""


def canonical_json_request_bytes(
    material: object,
    *,
    maximum_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
) -> bytes:
    limit = require_exact_int(
        maximum_bytes,
        code="invalid_https_request_limit",
        minimum=1,
        maximum=4_194_304,
    )
    try:
        body = canonical_json_bytes(material)
    except ContractViolation as exc:
        raise HttpsRequestRejectedBeforeSend(
            "canonical_json_failed_before_send"
        ) from exc
    if not body or len(body) > limit:
        raise HttpsRequestRejectedBeforeSend(
            "request_size_rejected_before_send"
        )
    return body


def _validated_target(url: object) -> SplitResult:
    if not isinstance(url, str) or not url or _CONTROL_RE.search(url):
        raise ContractViolation("invalid_https_url")
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.fragment
        or target.query
        or not target.path.startswith("/")
    ):
        raise ContractViolation("invalid_https_url")
    try:
        port = target.port
    except ValueError as exc:
        raise ContractViolation("invalid_https_url") from exc
    if port is not None and not 1 <= port <= 65_535:
        raise ContractViolation("invalid_https_url")
    return target


def _validated_headers(
    headers: object,
    *,
    allow_duplicate_names: bool = False,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(headers, tuple):
        raise ContractViolation("invalid_https_headers")
    seen: set[str] = set()
    checked: list[tuple[str, str]] = []
    for pair in headers:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ContractViolation("invalid_https_headers")
        name, value = pair
        if (
            not isinstance(name, str)
            or not _HEADER_NAME_RE.fullmatch(name)
            or not isinstance(value, str)
            or not value
            or _CONTROL_RE.search(value)
        ):
            raise ContractViolation("invalid_https_headers")
        lowered = name.lower()
        if not allow_duplicate_names and lowered in seen:
            raise ContractViolation("duplicate_https_request_header")
        seen.add(lowered)
        checked.append((name, value))
    return tuple(checked)


ConnectionFactory = Callable[
    [str, int, float, ssl.SSLContext], http.client.HTTPSConnection
]


def _default_connection_factory(
    host: str,
    port: int,
    timeout_seconds: float,
    context: ssl.SSLContext,
) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(
        host,
        port=port,
        timeout=timeout_seconds,
        context=context,
    )


class RetryFreeBoundedHttpsTransport:
    """Direct HTTPS only: verified TLS, no proxies, redirects, or retries."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        connection_factory: ConnectionFactory | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= 120
        ):
            raise ContractViolation("invalid_https_timeout")
        self._timeout_seconds = float(timeout_seconds)
        self._connection_factory = connection_factory or _default_connection_factory
        context = tls_context or ssl.create_default_context()
        if (
            not isinstance(context, ssl.SSLContext)
            or context.check_hostname is not True
            or context.verify_mode != ssl.CERT_REQUIRED
            or (
                context.maximum_version != ssl.TLSVersion.MAXIMUM_SUPPORTED
                and context.maximum_version < ssl.TLSVersion.TLSv1_2
            )
        ):
            raise ContractViolation("insecure_https_tls_context")
        if context.minimum_version < ssl.TLSVersion.TLSv1_2:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls_context = context

    def post(self, request: HttpsRequest) -> HttpsResponse:
        if not isinstance(request, HttpsRequest):
            raise HttpsRequestRejectedBeforeSend("invalid_request_before_send")
        try:
            target = _validated_target(request.url)
            headers = dict(_validated_headers(request.headers))
        except ContractViolation as exc:
            raise HttpsRequestRejectedBeforeSend(
                "invalid_request_before_send"
            ) from exc
        if headers.get("Content-Type") != "application/json":
            raise HttpsRequestRejectedBeforeSend(
                "content_type_rejected_before_send"
            )
        path = target.path
        port = target.port or 443
        connection: http.client.HTTPSConnection | None = None
        try:
            connection = self._connection_factory(
                target.hostname or "",
                port,
                self._timeout_seconds,
                self._tls_context,
            )
            connection.request("POST", path, body=request.body, headers=headers)
            raw_response = connection.getresponse()
            body = raw_response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise HttpsOutcomeUnknown("https_response_too_large_after_dispatch")
            response_headers = tuple(
                (str(name).lower(), str(value))
                for name, value in raw_response.getheaders()
            )
            return HttpsResponse(
                status=raw_response.status,
                headers=response_headers,
                body=body,
            )
        except HttpsOutcomeUnknown:
            raise
        except Exception as exc:
            raise HttpsOutcomeUnknown("https_failure_after_dispatch") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def json_headers(*, bearer_token: str) -> tuple[tuple[str, str], ...]:
    if (
        not isinstance(bearer_token, str)
        or not bearer_token
        or len(bearer_token.encode("utf-8")) > 1_024
        or _CONTROL_RE.search(bearer_token)
        or any(character.isspace() for character in bearer_token)
    ):
        raise HttpsRequestRejectedBeforeSend("invalid_bearer_token_before_send")
    return (
        ("Accept", "application/json"),
        ("Authorization", f"Bearer {bearer_token}"),
        ("Content-Type", "application/json"),
    )


def content_type_is_json(response: HttpsResponse) -> bool:
    value = response.header("content-type")
    return value is not None and value.split(";", 1)[0].strip().lower() == (
        "application/json"
    )


__all__ = [
    "canonical_json_request_bytes",
    "content_type_is_json",
    "DEFAULT_MAX_REQUEST_BYTES",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "HttpsOutcomeUnknown",
    "HttpsRequest",
    "HttpsRequestRejectedBeforeSend",
    "HttpsResponse",
    "HttpsTransport",
    "HttpsTransportError",
    "json_headers",
    "RetryFreeBoundedHttpsTransport",
]
