from __future__ import annotations

"""Supabase issuer, JWKS, and JWT verification adapter."""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)


_ALLOWED_ALGORITHMS = ("ES256", "RS256")
_JWKS_CACHE_SECONDS = 600


class SupabaseAuthConfigurationError(RuntimeError):
    pass


class SupabaseAuthUnavailable(RuntimeError):
    pass


class SupabaseAccessTokenInvalid(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedSupabaseIdentity:
    actor_user_id: str
    session_id: str
    authentication_manifest_sha256: str


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


def verify_supabase_access_token_identity(
    token: str,
    *,
    settings: SupabaseAuthSettings | None = None,
) -> VerifiedSupabaseIdentity:
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
    session_id = _uuid_text(claims.get("session_id"))
    manifest = hashlib.sha256(
        json.dumps(
            {
                "audience": "authenticated",
                "expires_at": claims["exp"],
                "issued_at": claims["iat"],
                "issuer": active_settings.issuer,
                "role": "authenticated",
                "schema": "supabase-response-identity-v1",
                "session_id": session_id,
                "subject": subject,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return VerifiedSupabaseIdentity(
        actor_user_id=subject,
        session_id=session_id,
        authentication_manifest_sha256=manifest,
    )


def verify_supabase_access_token(
    token: str,
    *,
    settings: SupabaseAuthSettings | None = None,
) -> str:
    return verify_supabase_access_token_identity(
        token,
        settings=settings,
    ).actor_user_id


__all__ = [
    "SupabaseAccessTokenInvalid",
    "SupabaseAuthConfigurationError",
    "SupabaseAuthSettings",
    "SupabaseAuthUnavailable",
    "VerifiedSupabaseIdentity",
    "verify_supabase_access_token",
    "verify_supabase_access_token_identity",
]
