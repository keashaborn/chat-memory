from __future__ import annotations

from fastapi import Request

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.supabase_actor_auth import require_verified_supabase_actor
from rag_engine.voice_observability_v1 import voice_turn_id_from_request
from rag_engine.voice_session_router import require_active_voice_session


TEXT_AUTHORITY = "supabase_access_token_v1"
VOICE_AUTHORITY = "active_voice_session_lease_v1"


def memory_actor_authority_v1(req: Request) -> str:
    return VOICE_AUTHORITY if voice_turn_id_from_request(req) is not None else TEXT_AUTHORITY


async def require_memory_actor_v1(req: Request, owner_user_id: str) -> str:
    if voice_turn_id_from_request(req) is not None:
        owner = require_actor_matches_owner(req, owner_user_id)
        await require_active_voice_session(req, owner)
        return owner
    return await require_verified_supabase_actor(req, owner_user_id)


__all__ = [
    "TEXT_AUTHORITY",
    "VOICE_AUTHORITY",
    "memory_actor_authority_v1",
    "require_memory_actor_v1",
]
