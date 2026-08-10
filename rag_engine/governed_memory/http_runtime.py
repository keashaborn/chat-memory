from __future__ import annotations

"""Explicit runtime composition for governed-memory Supabase authentication.

Nothing in this module reads environment variables or performs I/O at import or
construction time.  The default JWKS fetcher is lazy and runs only while an
actual request is authenticated.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import json
import math
import re
import threading
import time
from types import MappingProxyType
from typing import Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request as UrlRequest,
    build_opener,
)

from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePublicKey,
    SECP256R1,
)
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import Request
from jwt import PyJWK
from jwt.exceptions import InvalidKeyError

from .auth import ActorScope, VerifiedActor
from .http_api import ActorResolver
from .http_auth import (
    GovernedMemoryRequestAuthenticator,
    HttpAuthError,
    KeyNotFound,
    KeyResolverUnavailable,
    SupabaseJwtVerifier,
)


_ALLOWED_JWK_ALGORITHMS: Final = frozenset({"ES256", "RS256"})
_AUTHORITY_HEADER_NAMES: Final = frozenset(
    {
        "actor-user-id",
        "owner-user-id",
        "user-id",
        "x-actor-id",
        "x-actor-user-id",
        "x-memory-owner-id",
        "x-memory-owner-user-id",
        "x-owner-id",
        "x-owner-user-id",
        "x-supabase-user-id",
        "x-user-id",
        "x-vs-actor-user-id",
        "x-vs-owner-user-id",
    }
)
_HEADER_NAME_RE: Final = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z", re.ASCII)
_JWK_KID_RE: Final = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z", re.ASCII)
_PRIVATE_JWK_FIELDS: Final = frozenset(
    {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}
)


class JwksFetcher(Protocol):
    def __call__(
        self,
        url: str,
        timeout_seconds: int,
        maximum_bytes: int,
    ) -> bytes: ...


class _DuplicateJsonKey(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: object,
        _file_pointer: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _bounded_positive_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_literal_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_runtime_urls(config: "SupabaseHttpRuntimeConfig") -> None:
    if (
        not isinstance(config.issuer, str)
        or not config.issuer
        or config.issuer != config.issuer.strip()
        or config.issuer.endswith("/")
        or not isinstance(config.jwks_url, str)
        or config.jwks_url
        != f"{config.issuer}/.well-known/jwks.json"
    ):
        raise HttpAuthError("auth_configuration_invalid")
    try:
        issuer = urlsplit(config.issuer)
        jwks = urlsplit(config.jwks_url)
        issuer_port = issuer.port
        jwks_port = jwks.port
    except ValueError as exc:
        raise HttpAuthError("auth_configuration_invalid") from exc
    if (
        not issuer.hostname
        or issuer.username is not None
        or issuer.password is not None
        or issuer.query
        or issuer.fragment
        or jwks.username is not None
        or jwks.password is not None
        or jwks.query
        or jwks.fragment
        or issuer.path != "/auth/v1"
        or issuer.scheme != jwks.scheme
        or issuer.hostname != jwks.hostname
        or issuer_port != jwks_port
    ):
        raise HttpAuthError("auth_configuration_invalid")
    if issuer.scheme == "https":
        return
    if (
        issuer.scheme == "http"
        and config.allow_disposable_loopback_http is True
        and _is_literal_loopback(issuer.hostname)
    ):
        return
    raise HttpAuthError("auth_configuration_invalid")


@dataclass(frozen=True, slots=True)
class SupabaseHttpRuntimeConfig:
    issuer: str
    audience: str
    jwks_url: str
    expected_service_token: str = field(repr=False)
    service_token_header: str = "x-governed-memory-service-token"
    allow_disposable_loopback_http: bool = False
    cache_ttl_seconds: int = 300
    minimum_refresh_interval_seconds: int = 10
    fetch_timeout_seconds: int = 5
    maximum_jwks_bytes: int = 65_536
    maximum_jwks_keys: int = 16
    maximum_request_headers: int = 128
    maximum_request_header_bytes: int = 65_536

    def __post_init__(self) -> None:
        if (
            self.audience != "authenticated"
            or type(self.allow_disposable_loopback_http) is not bool
            or not _bounded_positive_int(
                self.cache_ttl_seconds, minimum=1, maximum=3_600
            )
            or not _bounded_positive_int(
                self.minimum_refresh_interval_seconds,
                minimum=1,
                maximum=60,
            )
            or self.minimum_refresh_interval_seconds > self.cache_ttl_seconds
            or not _bounded_positive_int(
                self.fetch_timeout_seconds, minimum=1, maximum=30
            )
            or not _bounded_positive_int(
                self.maximum_jwks_bytes, minimum=1_024, maximum=262_144
            )
            or not _bounded_positive_int(
                self.maximum_jwks_keys, minimum=1, maximum=64
            )
            or not _bounded_positive_int(
                self.maximum_request_headers, minimum=1, maximum=256
            )
            or not _bounded_positive_int(
                self.maximum_request_header_bytes,
                minimum=1_024,
                maximum=262_144,
            )
        ):
            raise HttpAuthError("auth_configuration_invalid")
        _validate_runtime_urls(self)


def _default_fetch_jwks(
    url: str,
    timeout_seconds: int,
    maximum_bytes: int,
) -> bytes:
    request = UrlRequest(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "governed-memory-jwks-v1",
        },
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200 or response.geturl() != url:
                raise KeyResolverUnavailable
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise KeyResolverUnavailable from exc
                if declared_length < 0 or declared_length > maximum_bytes:
                    raise KeyResolverUnavailable
            body = response.read(maximum_bytes + 1)
    except KeyResolverUnavailable:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
        raise KeyResolverUnavailable from exc
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise KeyResolverUnavailable
    return body


def _checked_cache_clock(clock: Callable[[], float]) -> float:
    try:
        value = clock()
    except Exception as exc:
        raise KeyResolverUnavailable from exc
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise KeyResolverUnavailable
    return float(value)


def _parse_jwks(
    body: object,
    *,
    maximum_bytes: int,
    maximum_keys: int,
) -> Mapping[tuple[str, str], object]:
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise KeyResolverUnavailable
    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_closed_json_object,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
    ) as exc:
        raise KeyResolverUnavailable from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise KeyResolverUnavailable
    raw_keys = document["keys"]
    if not raw_keys or len(raw_keys) > maximum_keys:
        raise KeyResolverUnavailable

    parsed: dict[tuple[str, str], object] = {}
    for raw_key in raw_keys:
        if not isinstance(raw_key, dict):
            raise KeyResolverUnavailable
        algorithm = raw_key.get("alg")
        if algorithm not in _ALLOWED_JWK_ALGORITHMS:
            continue
        key_id = raw_key.get("kid")
        if not isinstance(key_id, str) or _JWK_KID_RE.fullmatch(key_id) is None:
            raise KeyResolverUnavailable
        expected_key_type = "RSA" if algorithm == "RS256" else "EC"
        if raw_key.get("kty") != expected_key_type:
            raise KeyResolverUnavailable
        if algorithm == "ES256" and raw_key.get("crv") != "P-256":
            raise KeyResolverUnavailable
        if raw_key.get("use") not in (None, "sig"):
            raise KeyResolverUnavailable
        key_ops = raw_key.get("key_ops")
        if key_ops is not None and key_ops != ["verify"]:
            raise KeyResolverUnavailable
        if _PRIVATE_JWK_FIELDS.intersection(raw_key):
            raise KeyResolverUnavailable
        pair = (key_id, algorithm)
        if pair in parsed:
            raise KeyResolverUnavailable
        try:
            public_key = PyJWK.from_dict(raw_key, algorithm=algorithm).key
        except (InvalidKeyError, KeyError, TypeError, ValueError) as exc:
            raise KeyResolverUnavailable from exc
        if algorithm == "RS256":
            if (
                not isinstance(public_key, RSAPublicKey)
                or public_key.key_size < 2_048
                or public_key.key_size > 8_192
            ):
                raise KeyResolverUnavailable
        elif not isinstance(public_key, EllipticCurvePublicKey) or not isinstance(
            public_key.curve, SECP256R1
        ):
            raise KeyResolverUnavailable
        parsed[pair] = public_key
    if not parsed:
        raise KeyResolverUnavailable
    return MappingProxyType(parsed)


class BoundedCachedJwksKeyResolver:
    """Thread-safe, lazy, bounded public-key cache with exact kid/alg lookup."""

    __slots__ = (
        "_cache",
        "_cache_clock",
        "_config",
        "_expires_at",
        "_fetcher",
        "_last_refresh_failed",
        "_last_refresh_attempt_at",
        "_loaded_at",
        "_lock",
    )

    def __init__(
        self,
        config: SupabaseHttpRuntimeConfig,
        *,
        fetcher: JwksFetcher = _default_fetch_jwks,
        cache_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(config, SupabaseHttpRuntimeConfig)
            or not callable(fetcher)
            or not callable(cache_clock)
        ):
            raise HttpAuthError("auth_configuration_invalid")
        self._config = config
        self._fetcher = fetcher
        self._cache_clock = cache_clock
        self._lock = threading.Lock()
        self._cache: Mapping[tuple[str, str], object] = MappingProxyType({})
        self._last_refresh_failed = False
        self._last_refresh_attempt_at: float | None = None
        self._loaded_at: float | None = None
        self._expires_at: float | None = None

    def __call__(self, key_id: str, algorithm: str) -> object:
        if (
            not isinstance(key_id, str)
            or _JWK_KID_RE.fullmatch(key_id) is None
            or algorithm not in _ALLOWED_JWK_ALGORITHMS
        ):
            raise KeyNotFound
        with self._lock:
            now = _checked_cache_clock(self._cache_clock)
            cache_is_fresh = (
                self._loaded_at is not None
                and self._expires_at is not None
                and self._loaded_at <= now < self._expires_at
            )
            pair = (key_id, algorithm)
            if cache_is_fresh and pair in self._cache:
                return self._cache[pair]
            refresh_interval_elapsed = (
                self._last_refresh_attempt_at is None
                or (
                    self._last_refresh_attempt_at <= now
                    and now - self._last_refresh_attempt_at
                    >= self._config.minimum_refresh_interval_seconds
                )
            )
            if not refresh_interval_elapsed:
                if not cache_is_fresh or self._last_refresh_failed:
                    raise KeyResolverUnavailable
                raise KeyNotFound
            if not cache_is_fresh or pair not in self._cache:
                self._last_refresh_attempt_at = now
                try:
                    body = self._fetcher(
                        self._config.jwks_url,
                        self._config.fetch_timeout_seconds,
                        self._config.maximum_jwks_bytes,
                    )
                    replacement = _parse_jwks(
                        body,
                        maximum_bytes=self._config.maximum_jwks_bytes,
                        maximum_keys=self._config.maximum_jwks_keys,
                    )
                except KeyResolverUnavailable:
                    self._last_refresh_failed = True
                    raise
                except Exception as exc:
                    self._last_refresh_failed = True
                    raise KeyResolverUnavailable from exc
                self._cache = replacement
                self._last_refresh_failed = False
                self._loaded_at = now
                self._expires_at = now + self._config.cache_ttl_seconds
            try:
                return self._cache[pair]
            except KeyError as exc:
                raise KeyNotFound from exc


def _is_authority_header(name: str) -> bool:
    if name in _AUTHORITY_HEADER_NAMES:
        return True
    return bool(frozenset(name.split("-")).intersection({"owner", "actor"}))


def _request_headers_from_raw_scope(
    request: object,
    *,
    service_token_header: str,
    maximum_headers: int,
    maximum_bytes: int,
) -> Mapping[str, str]:
    if not isinstance(request, Request):
        raise HttpAuthError("auth_headers_invalid")
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, (list, tuple)):
        raise HttpAuthError("auth_headers_invalid")
    if len(raw_headers) > maximum_headers:
        raise HttpAuthError("auth_headers_invalid")
    result: dict[str, str] = {}
    total_bytes = 0
    for pair in raw_headers:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or not isinstance(pair[0], bytes)
            or not isinstance(pair[1], bytes)
            or _HEADER_NAME_RE.fullmatch(pair[0]) is None
        ):
            raise HttpAuthError("auth_headers_invalid")
        total_bytes += len(pair[0]) + len(pair[1])
        if total_bytes > maximum_bytes:
            raise HttpAuthError("auth_headers_invalid")
        name = pair[0].decode("ascii").lower()
        if name in result:
            if (
                name == "authorization"
                or name == service_token_header
                or _is_authority_header(name)
            ):
                raise HttpAuthError("auth_header_ambiguous")
            raise HttpAuthError("auth_headers_invalid")
        try:
            value = pair[1].decode("latin-1")
        except UnicodeDecodeError as exc:
            raise HttpAuthError("auth_headers_invalid") from exc
        result[name] = value
    return MappingProxyType(result)


class _SupabaseActorResolver:
    __slots__ = ("_authenticator", "_config")

    def __init__(
        self,
        authenticator: GovernedMemoryRequestAuthenticator,
        config: SupabaseHttpRuntimeConfig,
    ) -> None:
        self._authenticator = authenticator
        self._config = config

    async def __call__(
        self,
        request: Request,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        headers = _request_headers_from_raw_scope(
            request,
            service_token_header=self._config.service_token_header,
            maximum_headers=self._config.maximum_request_headers,
            maximum_bytes=self._config.maximum_request_header_bytes,
        )
        return await asyncio.to_thread(
            self._authenticator.authenticate,
            headers,
            scopes=scopes,
        )


def create_supabase_actor_resolver(
    config: SupabaseHttpRuntimeConfig,
    *,
    fetcher: JwksFetcher = _default_fetch_jwks,
    token_clock: Callable[[], datetime],
    cache_clock: Callable[[], float] = time.monotonic,
) -> ActorResolver:
    """Construct the lazy async resolver injected into ``create_owner_memory_app``."""

    if not isinstance(config, SupabaseHttpRuntimeConfig) or not callable(token_clock):
        raise HttpAuthError("auth_configuration_invalid")
    key_resolver = BoundedCachedJwksKeyResolver(
        config,
        fetcher=fetcher,
        cache_clock=cache_clock,
    )
    verifier = SupabaseJwtVerifier(
        issuer=config.issuer,
        audience=config.audience,
        key_resolver=key_resolver,
        clock=token_clock,
    )
    authenticator = GovernedMemoryRequestAuthenticator(
        verifier=verifier,
        expected_service_token=config.expected_service_token,
        service_token_header=config.service_token_header,
    )
    return _SupabaseActorResolver(authenticator, config)


__all__ = [
    "BoundedCachedJwksKeyResolver",
    "JwksFetcher",
    "SupabaseHttpRuntimeConfig",
    "create_supabase_actor_resolver",
]
