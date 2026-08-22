from __future__ import annotations

"""Verified request identity and actor context shared by SeeBx capabilities."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException, Request

from seebx.core.ownership import (
    require_actor_matches_owner,
    require_authenticated_actor,
)
from seebx.adapters.supabase import (
    SupabaseAccessTokenInvalid,
    SupabaseAuthConfigurationError,
    SupabaseAuthUnavailable,
    VerifiedSupabaseIdentity,
    verify_supabase_access_token_identity,
)
from seebx.core.voice_observability import voice_turn_id_from_request
from seebx.core.voice_identity import require_active_voice_session


TEXT_AUTHORITY = "supabase_access_token_v1"
VOICE_AUTHORITY = "active_voice_session_lease_v1"
_MAX_TOKEN_LENGTH = 16_384


@dataclass(frozen=True, slots=True)
class ActorContext:
    owner_user_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str
    authority: str


def _bearer_token(request: Request) -> str:
    authorization = (request.headers.get("authorization") or "").strip()
    scheme, separator, raw_token = authorization.partition(" ")
    token = raw_token.strip()
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or len(token) > _MAX_TOKEN_LENGTH
        or any(character.isspace() for character in token)
    ):
        raise HTTPException(
            status_code=401,
            detail="missing_or_invalid_supabase_bearer",
        )
    return token


def _request_uuid(value: object, *, status_code: int, detail: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status_code, detail=detail) from None


async def _verified_supabase_identity(
    request: Request,
) -> VerifiedSupabaseIdentity:
    token = _bearer_token(request)
    try:
        identity = await asyncio.to_thread(
            verify_supabase_access_token_identity,
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

    return identity


def _require_asserted_actor(
    request: Request,
    identity: VerifiedSupabaseIdentity,
) -> None:
    asserted_actor = (request.headers.get("x-vs-actor-user-id") or "").strip()
    if not asserted_actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")
    asserted_actor = _request_uuid(
        asserted_actor,
        status_code=401,
        detail="invalid_actor_user_id",
    )
    if asserted_actor != identity.actor_user_id:
        raise HTTPException(status_code=403, detail="actor_assertion_mismatch")


async def require_verified_supabase_identity(
    request: Request,
    owner_user_id: str,
) -> VerifiedSupabaseIdentity:
    identity = await _verified_supabase_identity(request)
    owner = _request_uuid(
        owner_user_id,
        status_code=400,
        detail="invalid_owner_user_id",
    )
    if identity.actor_user_id != owner:
        raise HTTPException(
            status_code=403,
            detail="supabase_actor_owner_mismatch",
        )

    _require_asserted_actor(request, identity)
    return identity


async def require_verified_supabase_request_identity(
    request: Request,
) -> VerifiedSupabaseIdentity:
    identity = await _verified_supabase_identity(request)
    _require_asserted_actor(request, identity)
    return identity


async def require_verified_supabase_actor(
    request: Request,
    owner_user_id: str,
) -> str:
    identity = await require_verified_supabase_identity(request, owner_user_id)
    return identity.actor_user_id



def require_verified_actor_matches_owner(
    verified_actor_user_id: str,
    owner_user_id: str,
) -> str:
    """Bind a previously verified request actor to a database-derived owner."""
    actor = _request_uuid(
        verified_actor_user_id,
        status_code=401,
        detail="invalid_verified_actor_user_id",
    )
    owner = _request_uuid(
        owner_user_id,
        status_code=400,
        detail="invalid_owner_user_id",
    )
    if actor != owner:
        raise HTTPException(status_code=403, detail="actor_owner_mismatch")
    return owner


def actor_authority(request: Request) -> str:
    if voice_turn_id_from_request(request) is not None:
        return VOICE_AUTHORITY
    return TEXT_AUTHORITY


async def require_request_actor(request: Request) -> str:
    """Return an actor proven by Supabase or an active voice-session lease."""
    if voice_turn_id_from_request(request) is not None:
        owner = require_authenticated_actor(request)
        await require_active_voice_session(request, owner)
        return owner
    identity = await require_verified_supabase_request_identity(request)
    return identity.actor_user_id


async def require_actor(request: Request, owner_user_id: str) -> str:
    if voice_turn_id_from_request(request) is not None:
        owner = require_actor_matches_owner(request, owner_user_id)
        await require_active_voice_session(request, owner)
        return owner
    return await require_verified_supabase_actor(request, owner_user_id)


async def require_actor_context(
    request: Request,
    owner_user_id: str,
) -> ActorContext:
    if voice_turn_id_from_request(request) is not None:
        owner = UUID(require_actor_matches_owner(request, owner_user_id))
        session_id = await require_active_voice_session(request, str(owner))
        material = json.dumps(
            {
                "authority": VOICE_AUTHORITY,
                "owner_user_id": str(owner),
                # Preserve the existing hash contract during module extraction.
                "schema": "response-memory-actor-context-v1",
                "session_id": str(session_id),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return ActorContext(
            owner_user_id=owner,
            session_id=session_id,
            authentication_manifest_sha256=hashlib.sha256(material).hexdigest(),
            authority=VOICE_AUTHORITY,
        )

    identity = await require_verified_supabase_identity(request, owner_user_id)
    return ActorContext(
        owner_user_id=UUID(identity.actor_user_id),
        session_id=UUID(identity.session_id),
        authentication_manifest_sha256=(
            identity.authentication_manifest_sha256
        ),
        authority=TEXT_AUTHORITY,
    )


__all__ = [
    "ActorContext",
    "TEXT_AUTHORITY",
    "VOICE_AUTHORITY",
    "actor_authority",
    "require_actor",
    "require_request_actor",
    "require_actor_context",
    "require_verified_supabase_actor",
    "require_verified_supabase_identity",
    "require_verified_supabase_request_identity",
    "require_verified_actor_matches_owner",
]
