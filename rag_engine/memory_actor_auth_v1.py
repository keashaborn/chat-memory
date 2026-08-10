from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol
from uuid import UUID

from fastapi import HTTPException, Request

from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.http_auth import HttpAuthError
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.supabase_actor_auth import (
    require_verified_supabase_actor,
    require_verified_supabase_identity,
)
from rag_engine.voice_observability_v1 import voice_turn_id_from_request
from rag_engine.voice_session_router import require_active_voice_session


TEXT_AUTHORITY = "supabase_access_token_v1"
VOICE_AUTHORITY = "active_voice_session_lease_v1"
_SUCCESSOR_REFUSAL_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}


@dataclass(frozen=True, slots=True)
class MemoryActorContextV1:
    owner_user_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str
    authority: str


class MemoryLiveAuthorityVerifierV1(Protocol):
    async def __call__(self, request: Request, actor: VerifiedActor) -> None: ...


async def _require_live_authority(
    req: Request,
    context: MemoryActorContextV1,
    verifier: MemoryLiveAuthorityVerifierV1 | None,
) -> None:
    if verifier is None:
        raise HTTPException(
            status_code=503,
            detail="successor_live_authority_unconfigured",
            headers=_SUCCESSOR_REFUSAL_HEADERS,
        )
    actor = VerifiedActor(
        owner_user_id=context.owner_user_id,
        actor_id=context.owner_user_id,
        session_id=context.session_id,
        role=ActorRole.OWNER,
        scopes=(ActorScope.READ_CLAIMS,),
        authentication_manifest_sha256=context.authentication_manifest_sha256,
        authenticated_at=datetime.now(timezone.utc),
    )
    try:
        await verifier(req, actor)
    except HttpAuthError as exc:
        code = exc.code
        unavailable = code in {
            "auth_live_authority_unavailable",
            "auth_live_configuration_invalid",
        }
        raise HTTPException(
            status_code=503 if unavailable else 401,
            detail=(
                "successor_live_authority_unavailable"
                if unavailable
                else "successor_live_authority_denied"
            ),
            headers=_SUCCESSOR_REFUSAL_HEADERS,
        ) from None


def memory_actor_authority_v1(req: Request) -> str:
    return VOICE_AUTHORITY if voice_turn_id_from_request(req) is not None else TEXT_AUTHORITY


async def require_memory_actor_v1(req: Request, owner_user_id: str) -> str:
    if voice_turn_id_from_request(req) is not None:
        owner = require_actor_matches_owner(req, owner_user_id)
        await require_active_voice_session(req, owner)
        return owner
    return await require_verified_supabase_actor(req, owner_user_id)


async def require_memory_actor_context_v1(
    req: Request,
    owner_user_id: str,
    *,
    live_authority_verifier: MemoryLiveAuthorityVerifierV1 | None = None,
    require_live_authority: bool = False,
) -> MemoryActorContextV1:
    if voice_turn_id_from_request(req) is not None:
        owner = UUID(require_actor_matches_owner(req, owner_user_id))
        session_id = await require_active_voice_session(req, str(owner))
        material = json.dumps(
            {
                "authority": VOICE_AUTHORITY,
                "owner_user_id": str(owner),
                "schema": "response-memory-actor-context-v1",
                "session_id": str(session_id),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        context = MemoryActorContextV1(
            owner_user_id=owner,
            session_id=session_id,
            authentication_manifest_sha256=hashlib.sha256(material).hexdigest(),
            authority=VOICE_AUTHORITY,
        )
        if require_live_authority:
            await _require_live_authority(req, context, live_authority_verifier)
        return context
    identity = await require_verified_supabase_identity(req, owner_user_id)
    context = MemoryActorContextV1(
        owner_user_id=UUID(identity.actor_user_id),
        session_id=UUID(identity.session_id),
        authentication_manifest_sha256=(
            identity.authentication_manifest_sha256
        ),
        authority=TEXT_AUTHORITY,
    )
    if require_live_authority:
        await _require_live_authority(req, context, live_authority_verifier)
    return context


__all__ = [
    "TEXT_AUTHORITY",
    "VOICE_AUTHORITY",
    "MemoryActorContextV1",
    "MemoryLiveAuthorityVerifierV1",
    "memory_actor_authority_v1",
    "require_memory_actor_context_v1",
    "require_memory_actor_v1",
]
