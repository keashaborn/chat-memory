from __future__ import annotations

"""Verify the original Supabase user JWT at the Brains trust boundary."""

import asyncio
import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)


_ALLOWED_ALGORITHMS = ("ES256", "RS256")
_MAX_TOKEN_LENGTH = 16_384
_JWKS_CACHE_SECONDS = 600


class SupabaseAuthConfigurationError(RuntimeError):
    pass


class SupabaseAuthUnavailable(RuntimeError):
    pass


class SupabaseAccessTokenInvalid(RuntimeError):
    pass


def _uuid_text(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise SupabaseAccessTokenInvalid("invalid_uuid_claim") from None


def _https_endpoint(value: str, name: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise SupabaseAuthConfigurationError(f"{name}_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SupabaseAuthConfigurationError(f"{name}_invalid")
    return urlunsplit(
        (
            "https",
            parsed.hostname.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


@dataclass(frozen=True)
class SupabaseAuthSettings:
    issuer: str
    jwks_url: str

    @classmethod
    def from_env(cls) -> "SupabaseAuthSettings":
        issuer = _https_endpoint(
            os.getenv("SUPABASE_ISSUER") or "",
            "supabase_issuer",
        )
        jwks_url = _https_endpoint(
            os.getenv("SUPABASE_JWKS_URL") or "",
            "supabase_jwks_url",
        )
        expected_jwks = f"{issuer}/.well-known/jwks.json"
        if jwks_url != expected_jwks:
            raise SupabaseAuthConfigurationError(
                "supabase_jwks_url_must_match_issuer"
            )
        return cls(issuer=issuer, jwks_url=jwks_url)


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(
        jwks_url,
        cache_jwk_set=True,
        lifespan=_JWKS_CACHE_SECONDS,
        timeout=5,
    )


def _bearer_token(req: Request) -> str:
    authorization = (req.headers.get("authorization") or "").strip()
    scheme, separator, raw_token = authorization.partition(" ")
    token = raw_token.strip()
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or len(token) > _MAX_TOKEN_LENGTH
        or any(char.isspace() for char in token)
    ):
        raise HTTPException(
            status_code=401,
            detail="missing_or_invalid_supabase_bearer",
        )
    return token


def verify_supabase_access_token(
    token: str,
    *,
    settings: SupabaseAuthSettings | None = None,
) -> str:
    active_settings = settings or SupabaseAuthSettings.from_env()
    try:
        header = jwt.get_unverified_header(token)
        algorithm = str(header.get("alg") or "")
        key_id = str(header.get("kid") or "")
        if (
            algorithm not in _ALLOWED_ALGORITHMS
            or not key_id
            or len(key_id) > 256
        ):
            raise SupabaseAccessTokenInvalid("invalid_jwt_header")
        signing_key = _jwks_client(
            active_settings.jwks_url
        ).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(_ALLOWED_ALGORITHMS),
            audience="authenticated",
            issuer=active_settings.issuer,
            leeway=30,
            options={
                "require": [
                    "iss",
                    "aud",
                    "exp",
                    "iat",
                    "sub",
                    "role",
                    "session_id",
                    "is_anonymous",
                ],
            },
        )
    except PyJWKClientConnectionError as exc:
        raise SupabaseAuthUnavailable("supabase_jwks_unavailable") from exc
    except SupabaseAccessTokenInvalid:
        raise
    except (InvalidTokenError, PyJWKClientError, TypeError, ValueError) as exc:
        raise SupabaseAccessTokenInvalid(
            "invalid_supabase_access_token"
        ) from exc

    if (
        claims.get("role") != "authenticated"
        or claims.get("is_anonymous") is not False
        or isinstance(claims.get("exp"), bool)
        or not isinstance(claims.get("exp"), (int, float))
        or isinstance(claims.get("iat"), bool)
        or not isinstance(claims.get("iat"), (int, float))
    ):
        raise SupabaseAccessTokenInvalid("invalid_supabase_claims")

    subject = _uuid_text(claims.get("sub"))
    _uuid_text(claims.get("session_id"))
    return subject


async def require_verified_supabase_actor(
    req: Request,
    owner_user_id: str,
) -> str:
    token = _bearer_token(req)
    try:
        verified_actor = await asyncio.to_thread(
            verify_supabase_access_token,
            token,
        )
    except SupabaseAuthConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="supabase_auth_configuration_invalid",
        ) from None
    except SupabaseAuthUnavailable:
        raise HTTPException(
            status_code=503,
            detail="supabase_jwks_unavailable",
        ) from None
    except SupabaseAccessTokenInvalid:
        raise HTTPException(
            status_code=401,
            detail="invalid_supabase_access_token",
        ) from None

    try:
        owner = _uuid_text(owner_user_id)
    except SupabaseAccessTokenInvalid:
        raise HTTPException(
            status_code=400,
            detail="invalid_owner_user_id",
        ) from None
    if verified_actor != owner:
        raise HTTPException(
            status_code=403,
            detail="supabase_actor_owner_mismatch",
        )

    asserted_actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not asserted_actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")
    try:
        asserted_actor = _uuid_text(asserted_actor)
    except SupabaseAccessTokenInvalid:
        raise HTTPException(
            status_code=401,
            detail="invalid_actor_user_id",
        ) from None
    if asserted_actor != verified_actor:
        raise HTTPException(
            status_code=403,
            detail="actor_assertion_mismatch",
        )

    return verified_actor


__all__ = [
    "SupabaseAccessTokenInvalid",
    "SupabaseAuthConfigurationError",
    "SupabaseAuthSettings",
    "SupabaseAuthUnavailable",
    "require_verified_supabase_actor",
    "verify_supabase_access_token",
]
