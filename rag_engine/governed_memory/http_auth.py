from __future__ import annotations

"""Pure Supabase JWT authentication for the governed-memory HTTP boundary.

The adapter has no environment, filesystem, or network access.  A route layer
supplies its least required scopes, and a caller supplies a key resolver backed
by an independently configured JWKS cache.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hmac
import re
from types import MappingProxyType
from typing import Final, Protocol
from uuid import UUID

import jwt
from jwt.exceptions import (
    DecodeError,
    InvalidKeyError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
)

from .auth import ActorRole, ActorScope, VerifiedActor
from .contracts import ContractViolation, canonical_sha256


_ALLOWED_ALGORITHMS: Final = frozenset({"ES256", "RS256"})
_BEARER_RE: Final = re.compile(
    r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z",
    re.ASCII | re.IGNORECASE,
)
_HEADER_NAME_RE: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z", re.ASCII)
_KID_RE: Final = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z", re.ASCII)
_MAX_AUTHORIZATION_BYTES: Final = 16_384
_MAX_SERVICE_TOKEN_BYTES: Final = 16_384
_REQUIRED_CLAIMS: Final = (
    "iss",
    "aud",
    "sub",
    "session_id",
    "role",
    "is_anonymous",
    "iat",
    "exp",
)
_RESERVED_AUTHORITY_HEADERS: Final = frozenset(
    {
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


class KeyResolver(Protocol):
    """Resolve one already-trusted asymmetric verification key."""

    def __call__(self, key_id: str, algorithm: str) -> object: ...


class KeyNotFound(LookupError):
    """The requested ``kid`` is absent from the trusted JWKS snapshot."""


class KeyResolverUnavailable(RuntimeError):
    """The trusted JWKS snapshot cannot currently be obtained."""


class HttpAuthError(ValueError):
    """Content-free, stable authentication failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _checked_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        now = clock()
    except Exception as exc:
        raise HttpAuthError("auth_clock_invalid") from exc
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise HttpAuthError("auth_clock_invalid")
    offset = now.utcoffset()
    if offset is None:
        raise HttpAuthError("auth_clock_invalid")
    return now.astimezone(UTC)


def _checked_uuid(value: object, code: str) -> UUID:
    if not isinstance(value, str):
        raise HttpAuthError(code)
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise HttpAuthError(code) from exc
    if str(parsed) != value:
        raise HttpAuthError(code)
    return parsed


def _checked_scopes(scopes: object) -> tuple[ActorScope, ...]:
    if not isinstance(scopes, tuple) or not scopes or any(
        not isinstance(scope, ActorScope) for scope in scopes
    ):
        raise HttpAuthError("auth_scopes_invalid")
    if tuple(sorted(set(scopes), key=lambda scope: scope.value)) != scopes:
        raise HttpAuthError("auth_scopes_invalid")
    return scopes


def _bearer_token(authorization: object) -> str:
    if not isinstance(authorization, str):
        raise HttpAuthError("auth_header_missing")
    try:
        size = len(authorization.encode("ascii"))
    except UnicodeEncodeError as exc:
        raise HttpAuthError("auth_header_invalid") from exc
    if size > _MAX_AUTHORIZATION_BYTES:
        raise HttpAuthError("auth_header_invalid")
    match = _BEARER_RE.fullmatch(authorization)
    if match is None:
        raise HttpAuthError("auth_header_invalid")
    return match.group(1)


@dataclass(frozen=True, slots=True)
class SupabaseJwtVerifier:
    """Verify one Supabase user access token without performing I/O."""

    issuer: str
    audience: str
    key_resolver: KeyResolver
    clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        if (
            not isinstance(self.issuer, str)
            or not self.issuer
            or self.issuer != self.issuer.strip()
            or not isinstance(self.audience, str)
            or not self.audience
            or self.audience != self.audience.strip()
            or not callable(self.key_resolver)
            or not callable(self.clock)
        ):
            raise HttpAuthError("auth_configuration_invalid")

    def verify_bearer(
        self,
        authorization: object,
        *,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        checked_scopes = _checked_scopes(scopes)
        token = _bearer_token(authorization)

        try:
            header = jwt.get_unverified_header(token)
        except DecodeError as exc:
            raise HttpAuthError("auth_token_malformed") from exc
        except InvalidTokenError as exc:
            raise HttpAuthError("auth_token_header_invalid") from exc
        except (TypeError, ValueError) as exc:
            raise HttpAuthError("auth_token_malformed") from exc
        if (
            not isinstance(header, dict)
            or header.get("typ") != "JWT"
            or "crit" in header
        ):
            raise HttpAuthError("auth_token_header_invalid")
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in _ALLOWED_ALGORITHMS:
            raise HttpAuthError("auth_algorithm_denied")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or _KID_RE.fullmatch(key_id) is None:
            raise HttpAuthError("auth_key_id_invalid")

        try:
            verification_key = self.key_resolver(key_id, algorithm)
        except KeyNotFound as exc:
            raise HttpAuthError("auth_key_not_found") from exc
        except KeyResolverUnavailable as exc:
            raise HttpAuthError("auth_key_resolution_unavailable") from exc
        except Exception as exc:
            raise HttpAuthError("auth_key_resolver_failed") from exc
        if verification_key is None:
            raise HttpAuthError("auth_key_not_found")

        try:
            claims = jwt.decode(
                token,
                verification_key,
                algorithms=[algorithm],
                options={
                    "require": list(_REQUIRED_CLAIMS),
                    "verify_aud": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_iss": False,
                    "verify_nbf": False,
                    "verify_signature": True,
                    "verify_sub": False,
                },
            )
        except MissingRequiredClaimError as exc:
            raise HttpAuthError("auth_required_claim_missing") from exc
        except InvalidSignatureError as exc:
            raise HttpAuthError("auth_signature_invalid") from exc
        except InvalidKeyError as exc:
            raise HttpAuthError("auth_key_invalid") from exc
        except (DecodeError, InvalidTokenError, TypeError, ValueError) as exc:
            raise HttpAuthError("auth_token_invalid") from exc
        if not isinstance(claims, dict):
            raise HttpAuthError("auth_token_invalid")

        if claims.get("iss") != self.issuer:
            raise HttpAuthError("auth_issuer_mismatch")
        if type(claims.get("aud")) is not str or claims["aud"] != self.audience:
            raise HttpAuthError("auth_audience_mismatch")
        subject = _checked_uuid(claims.get("sub"), "auth_subject_invalid")
        session_id = _checked_uuid(
            claims.get("session_id"), "auth_session_id_invalid"
        )
        if claims.get("role") != "authenticated":
            raise HttpAuthError("auth_role_denied")
        if type(claims.get("is_anonymous")) is not bool:
            raise HttpAuthError("auth_anonymous_claim_invalid")
        if claims["is_anonymous"]:
            raise HttpAuthError("auth_anonymous_forbidden")

        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        if type(issued_at) is not int:
            raise HttpAuthError("auth_iat_invalid")
        if type(expires_at) is not int:
            raise HttpAuthError("auth_exp_invalid")
        now = _checked_clock(self.clock)
        now_seconds = int(now.timestamp())
        if issued_at > now_seconds:
            raise HttpAuthError("auth_token_not_yet_valid")
        if expires_at <= issued_at:
            raise HttpAuthError("auth_token_lifetime_invalid")
        if expires_at <= now_seconds:
            raise HttpAuthError("auth_token_expired")
        if "nbf" in claims:
            not_before = claims["nbf"]
            if type(not_before) is not int:
                raise HttpAuthError("auth_nbf_invalid")
            if not_before > now_seconds:
                raise HttpAuthError("auth_token_not_yet_valid")

        authentication_manifest_sha256 = canonical_sha256(
            "governed_memory.supabase_http_auth",
            {
                "algorithm": algorithm,
                "audience": self.audience,
                "expires_at_unix": expires_at,
                "issued_at_unix": issued_at,
                "issuer": self.issuer,
                "key_id": key_id,
                "role": "authenticated",
                "schema": "governed-memory-supabase-http-auth-v1",
                "scopes": checked_scopes,
                "session_id": session_id,
                "subject": subject,
            },
        )
        try:
            return VerifiedActor(
                owner_user_id=subject,
                actor_id=subject,
                session_id=session_id,
                role=ActorRole.OWNER,
                scopes=checked_scopes,
                authentication_manifest_sha256=authentication_manifest_sha256,
                authenticated_at=now,
            )
        except ContractViolation as exc:
            raise HttpAuthError("auth_actor_construction_failed") from exc


def _normalize_headers(headers: object) -> Mapping[str, str]:
    if not isinstance(headers, Mapping):
        raise HttpAuthError("auth_headers_invalid")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if (
            not isinstance(raw_name, str)
            or _HEADER_NAME_RE.fullmatch(raw_name) is None
            or not isinstance(raw_value, str)
        ):
            raise HttpAuthError("auth_headers_invalid")
        name = raw_name.lower()
        if name in normalized:
            raise HttpAuthError("auth_header_ambiguous")
        normalized[name] = raw_value
    return MappingProxyType(normalized)


def _is_explicit_authority_header(name: str) -> bool:
    if name in _RESERVED_AUTHORITY_HEADERS:
        return True
    parts = frozenset(name.split("-"))
    return bool(parts.intersection({"owner", "actor"}))


@dataclass(frozen=True, slots=True)
class GovernedMemoryRequestAuthenticator:
    """Require infrastructure identity, then derive user identity from JWT."""

    verifier: SupabaseJwtVerifier
    expected_service_token: str = field(repr=False)
    service_token_header: str = "x-governed-memory-service-token"

    def __post_init__(self) -> None:
        try:
            token_size = len(self.expected_service_token.encode("utf-8"))
        except (AttributeError, UnicodeEncodeError) as exc:
            raise HttpAuthError("auth_service_token_configuration_invalid") from exc
        if token_size < 1 or token_size > _MAX_SERVICE_TOKEN_BYTES:
            raise HttpAuthError("auth_service_token_configuration_invalid")
        if (
            not isinstance(self.verifier, SupabaseJwtVerifier)
            or not isinstance(self.service_token_header, str)
            or self.service_token_header != self.service_token_header.lower()
            or _HEADER_NAME_RE.fullmatch(self.service_token_header) is None
            or _is_explicit_authority_header(self.service_token_header)
        ):
            raise HttpAuthError("auth_service_token_configuration_invalid")

    def authenticate(
        self,
        headers: Mapping[str, str],
        *,
        scopes: tuple[ActorScope, ...],
    ) -> VerifiedActor:
        normalized = _normalize_headers(headers)
        if any(_is_explicit_authority_header(name) for name in normalized):
            raise HttpAuthError("auth_explicit_authority_header_forbidden")
        supplied_service_token = normalized.get(self.service_token_header)
        if supplied_service_token is None:
            raise HttpAuthError("auth_service_token_missing")
        try:
            service_token_matches = hmac.compare_digest(
                supplied_service_token.encode("utf-8"),
                self.expected_service_token.encode("utf-8"),
            )
        except (AttributeError, UnicodeEncodeError) as exc:
            raise HttpAuthError("auth_service_token_invalid") from exc
        if not service_token_matches:
            raise HttpAuthError("auth_service_token_invalid")
        return self.verifier.verify_bearer(
            normalized.get("authorization"),
            scopes=scopes,
        )


__all__ = [
    "GovernedMemoryRequestAuthenticator",
    "HttpAuthError",
    "KeyNotFound",
    "KeyResolver",
    "KeyResolverUnavailable",
    "SupabaseJwtVerifier",
]
