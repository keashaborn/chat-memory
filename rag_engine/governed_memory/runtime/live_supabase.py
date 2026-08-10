from __future__ import annotations

"""Bounded live Supabase account and session checks for the owner boundary.

The adapter checks ``/auth/v1/user`` first, then calls a staged no-argument
PostgREST RPC that tests the JWT ``session_id`` against ``auth.sessions``.
Both calls use the original bearer token and the configured publishable key.
No response or decision is cached.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import asyncio
import json
import re
import ssl
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request as UrlRequest,
    build_opener,
)
from uuid import UUID

from fastapi import Request

from ..auth import ActorRole, VerifiedActor
from ..http_auth import HttpAuthError


_BEARER_RE: Final = re.compile(
    r"Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z",
    re.ASCII | re.IGNORECASE,
)
_MAX_AUTHORIZATION_BYTES: Final = 16_384

UserFetcher = Callable[[str, str, str, int, int], bytes]
SessionFetcher = Callable[[str, str, str, int, int], bytes]


class _DuplicateJsonKey(ValueError):
    pass


class _LiveAuthorityUnavailable(RuntimeError):
    pass


class _LiveAuthorityDenied(RuntimeError):
    pass


class _LiveSessionDenied(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _bounded_positive_int(value: object, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _checked_issuer(issuer: object) -> tuple[str, str, str]:
    if (
        not isinstance(issuer, str)
        or not issuer
        or issuer != issuer.strip()
        or len(issuer) > 4_096
    ):
        raise HttpAuthError("auth_live_configuration_invalid")
    try:
        issuer.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HttpAuthError("auth_live_configuration_invalid") from exc
    try:
        parsed = urlsplit(issuer)
        port = parsed.port
    except ValueError as exc:
        raise HttpAuthError("auth_live_configuration_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or parsed.path != "/auth/v1"
    ):
        raise HttpAuthError("auth_live_configuration_invalid")
    normalized = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )
    if normalized != issuer:
        raise HttpAuthError("auth_live_configuration_invalid")
    session_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/rest/v1/rpc/governed_memory_current_session_v1",
            "",
            "",
        )
    )
    return normalized, f"{normalized}/user", session_url


def _checked_api_key(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HttpAuthError("auth_live_configuration_invalid")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HttpAuthError("auth_live_configuration_invalid") from exc
    if len(encoded) > 16_384 or any(byte < 33 or byte > 126 for byte in encoded):
        raise HttpAuthError("auth_live_configuration_invalid")
    return value


def _authorization_from_request(request: Request) -> str:
    if not isinstance(request, Request):
        raise HttpAuthError("auth_live_request_invalid")
    raw_headers = request.scope.get("headers")
    if not isinstance(raw_headers, list):
        raise HttpAuthError("auth_live_request_invalid")
    matches: list[bytes] = []
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise HttpAuthError("auth_live_request_invalid")
        try:
            name = item[0].decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise HttpAuthError("auth_live_request_invalid") from exc
        if name == "authorization":
            matches.append(item[1])
    if len(matches) != 1 or len(matches[0]) > _MAX_AUTHORIZATION_BYTES:
        raise HttpAuthError("auth_live_authorization_invalid")
    try:
        authorization = matches[0].decode("ascii")
    except UnicodeDecodeError as exc:
        raise HttpAuthError("auth_live_authorization_invalid") from exc
    if _BEARER_RE.fullmatch(authorization) is None:
        raise HttpAuthError("auth_live_authorization_invalid")
    return authorization


def _default_fetch_user(
    url: str,
    authorization: str,
    api_key: str,
    timeout_seconds: int,
    maximum_bytes: int,
) -> bytes:
    request = UrlRequest(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
            "apikey": api_key,
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "User-Agent": "governed-memory-live-user",
        },
    )
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200 or response.geturl() != url:
                raise _LiveAuthorityDenied
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise _LiveAuthorityUnavailable
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise _LiveAuthorityUnavailable from exc
                if declared_length < 1 or declared_length > maximum_bytes:
                    raise _LiveAuthorityUnavailable
            body = response.read(maximum_bytes + 1)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise _LiveAuthorityDenied from exc
        raise _LiveAuthorityUnavailable from exc
    except _LiveAuthorityDenied:
        raise
    except _LiveAuthorityUnavailable:
        raise
    except (URLError, OSError, TimeoutError, ValueError, ssl.SSLError) as exc:
        raise _LiveAuthorityUnavailable from exc
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise _LiveAuthorityUnavailable
    return body


def _default_fetch_session(
    url: str,
    authorization: str,
    api_key: str,
    timeout_seconds: int,
    maximum_bytes: int,
) -> bytes:
    request = UrlRequest(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
            "apikey": api_key,
            "Cache-Control": "no-store",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
            "User-Agent": "governed-memory-live-session",
        },
    )
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200 or response.geturl() != url:
                raise _LiveAuthorityUnavailable
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise _LiveAuthorityUnavailable
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise _LiveAuthorityUnavailable from exc
                if declared_length < 1 or declared_length > maximum_bytes:
                    raise _LiveAuthorityUnavailable
            body = response.read(maximum_bytes + 1)
    except HTTPError as exc:
        if exc.code == 401:
            raise _LiveSessionDenied from exc
        raise _LiveAuthorityUnavailable from exc
    except _LiveSessionDenied:
        raise
    except _LiveAuthorityUnavailable:
        raise
    except (URLError, OSError, TimeoutError, ValueError, ssl.SSLError) as exc:
        raise _LiveAuthorityUnavailable from exc
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise _LiveAuthorityUnavailable
    return body


def _parse_authoritative_user_id(body: object, *, maximum_bytes: int) -> UUID:
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise HttpAuthError("auth_live_response_invalid")
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
        raise HttpAuthError("auth_live_response_invalid") from exc
    if not isinstance(document, dict) or not isinstance(document.get("id"), str):
        raise HttpAuthError("auth_live_response_invalid")
    raw_user_id = document["id"]
    try:
        user_id = UUID(raw_user_id)
    except (AttributeError, ValueError) as exc:
        raise HttpAuthError("auth_live_response_invalid") from exc
    if str(user_id) != raw_user_id:
        raise HttpAuthError("auth_live_response_invalid")
    return user_id


def _parse_authoritative_session(
    body: object,
    *,
    maximum_bytes: int,
) -> tuple[UUID, UUID, bool]:
    if not isinstance(body, bytes) or not body or len(body) > maximum_bytes:
        raise HttpAuthError("auth_live_session_response_invalid")
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
        raise HttpAuthError("auth_live_session_response_invalid") from exc
    expected_keys = {"owner_user_id", "session_id", "session_present"}
    if (
        type(document) is not list
        or len(document) != 1
        or not isinstance(document[0], dict)
        or set(document[0]) != expected_keys
        or type(document[0]["owner_user_id"]) is not str
        or type(document[0]["session_id"]) is not str
        or type(document[0]["session_present"]) is not bool
    ):
        raise HttpAuthError("auth_live_session_response_invalid")
    row = document[0]
    try:
        owner_user_id = UUID(row["owner_user_id"])
        session_id = UUID(row["session_id"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise HttpAuthError("auth_live_session_response_invalid") from exc
    if (
        str(owner_user_id) != row["owner_user_id"]
        or str(session_id) != row["session_id"]
    ):
        raise HttpAuthError("auth_live_session_response_invalid")
    return owner_user_id, session_id, row["session_present"]


@dataclass(frozen=True, slots=True)
class LiveSupabaseAuthorityConfig:
    issuer: str
    api_key: str = field(repr=False)
    timeout_seconds: int = 5
    maximum_response_bytes: int = 65_536
    issuer_url: str = field(init=False)
    user_url: str = field(init=False)
    session_url: str = field(init=False)

    def __post_init__(self) -> None:
        issuer_url, user_url, session_url = _checked_issuer(self.issuer)
        _checked_api_key(self.api_key)
        if (
            not _bounded_positive_int(
                self.timeout_seconds,
                minimum=1,
                maximum=10,
            )
            or not _bounded_positive_int(
                self.maximum_response_bytes,
                minimum=1_024,
                maximum=131_072,
            )
        ):
            raise HttpAuthError("auth_live_configuration_invalid")
        object.__setattr__(self, "issuer_url", issuer_url)
        object.__setattr__(self, "user_url", user_url)
        object.__setattr__(self, "session_url", session_url)


@dataclass(frozen=True, slots=True)
class LiveSupabaseUserVerifier:
    config: LiveSupabaseAuthorityConfig
    fetcher: UserFetcher = _default_fetch_user

    def __post_init__(self) -> None:
        if not isinstance(self.config, LiveSupabaseAuthorityConfig) or not callable(
            self.fetcher
        ):
            raise HttpAuthError("auth_live_configuration_invalid")

    async def __call__(self, request: Request, actor: VerifiedActor) -> None:
        if not isinstance(actor, VerifiedActor) or actor.role is not ActorRole.OWNER:
            raise HttpAuthError("auth_live_actor_invalid")
        authorization = _authorization_from_request(request)
        try:
            body = await asyncio.wait_for(
                asyncio.to_thread(
                    self.fetcher,
                    self.config.user_url,
                    authorization,
                    self.config.api_key,
                    self.config.timeout_seconds,
                    self.config.maximum_response_bytes,
                ),
                timeout=self.config.timeout_seconds + 1,
            )
        except _LiveAuthorityDenied as exc:
            raise HttpAuthError("auth_live_session_denied") from exc
        except HttpAuthError:
            raise
        except (
            _LiveAuthorityUnavailable,
            asyncio.TimeoutError,
            OSError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise HttpAuthError("auth_live_authority_unavailable") from exc
        except Exception as exc:
            raise HttpAuthError("auth_live_authority_unavailable") from exc
        user_id = _parse_authoritative_user_id(
            body,
            maximum_bytes=self.config.maximum_response_bytes,
        )
        if user_id != actor.owner_user_id or user_id != actor.actor_id:
            raise HttpAuthError("auth_live_user_mismatch")


@dataclass(frozen=True, slots=True)
class LiveSupabaseSessionVerifier:
    config: LiveSupabaseAuthorityConfig
    fetcher: SessionFetcher = _default_fetch_session

    def __post_init__(self) -> None:
        if not isinstance(self.config, LiveSupabaseAuthorityConfig) or not callable(
            self.fetcher
        ):
            raise HttpAuthError("auth_live_configuration_invalid")

    async def __call__(self, request: Request, actor: VerifiedActor) -> None:
        if not isinstance(actor, VerifiedActor) or actor.role is not ActorRole.OWNER:
            raise HttpAuthError("auth_live_actor_invalid")
        authorization = _authorization_from_request(request)
        try:
            body = await asyncio.wait_for(
                asyncio.to_thread(
                    self.fetcher,
                    self.config.session_url,
                    authorization,
                    self.config.api_key,
                    self.config.timeout_seconds,
                    self.config.maximum_response_bytes,
                ),
                timeout=self.config.timeout_seconds + 1,
            )
        except _LiveSessionDenied as exc:
            raise HttpAuthError("auth_live_session_denied") from exc
        except HttpAuthError:
            raise
        except (
            _LiveAuthorityUnavailable,
            asyncio.TimeoutError,
            OSError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise HttpAuthError("auth_live_authority_unavailable") from exc
        except Exception as exc:
            raise HttpAuthError("auth_live_authority_unavailable") from exc
        owner_user_id, session_id, session_present = _parse_authoritative_session(
            body,
            maximum_bytes=self.config.maximum_response_bytes,
        )
        if (
            owner_user_id != actor.owner_user_id
            or owner_user_id != actor.actor_id
            or session_id != actor.session_id
        ):
            raise HttpAuthError("auth_live_session_mismatch")
        if not session_present:
            raise HttpAuthError("auth_live_session_denied")


@dataclass(frozen=True, slots=True)
class LiveSupabaseAuthorityVerifier:
    user_verifier: LiveSupabaseUserVerifier
    session_verifier: LiveSupabaseSessionVerifier

    def __post_init__(self) -> None:
        if not isinstance(
            self.user_verifier, LiveSupabaseUserVerifier
        ) or not isinstance(
            self.session_verifier, LiveSupabaseSessionVerifier
        ):
            raise HttpAuthError("auth_live_configuration_invalid")

    async def __call__(self, request: Request, actor: VerifiedActor) -> None:
        await self.user_verifier(request, actor)
        await self.session_verifier(request, actor)


__all__ = [
    "LiveSupabaseAuthorityConfig",
    "LiveSupabaseAuthorityVerifier",
    "LiveSupabaseSessionVerifier",
    "LiveSupabaseUserVerifier",
    "SessionFetcher",
    "UserFetcher",
]
