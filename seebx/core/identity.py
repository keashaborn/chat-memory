from __future__ import annotations

"""Verified actor context shared by SeeBx capabilities."""

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from fastapi import Request

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.supabase_actor_auth import (
    require_verified_supabase_actor,
    require_verified_supabase_identity,
)
from rag_engine.voice_observability_v1 import voice_turn_id_from_request
from rag_engine.voice_session_router import require_active_voice_session


TEXT_AUTHORITY = "supabase_access_token_v1"
VOICE_AUTHORITY = "active_voice_session_lease_v1"


@dataclass(frozen=True, slots=True)
class ActorContext:
    owner_user_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str
    authority: str


def actor_authority(request: Request) -> str:
    if voice_turn_id_from_request(request) is not None:
        return VOICE_AUTHORITY
    return TEXT_AUTHORITY


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
    "require_actor_context",
]
