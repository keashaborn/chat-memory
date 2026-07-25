from __future__ import annotations

"""Authorization adapter for browser text and leased Realtime voice search."""

from fastapi import HTTPException, Request

from rag_engine.lifeswitch_auth import require_actor_matches_owner
from rag_engine.supabase_actor_auth import require_verified_supabase_actor
from rag_engine.voice_session_router import require_active_voice_session


VOICE_SEARCH_AUTHORIZATION_HEADER = "x-vs-web-search-authorization"
TEXT_SEARCH_AUTHORIZATION_VALUE = "supabase_fresh_web_search_v1"
VOICE_SEARCH_AUTHORIZATION_VALUE = "supabase_fresh_voice_lease_v1"


async def require_web_search_actor_v1(
    req: Request,
    owner_user_id: str,
    *,
    internal_assertion_required: bool = False,
) -> str:
    mode = (
        req.headers.get(VOICE_SEARCH_AUTHORIZATION_HEADER) or ""
    ).strip()
    if not mode:
        if internal_assertion_required:
            raise HTTPException(
                status_code=403,
                detail="internal_web_search_authorization_required",
            )
        return await require_verified_supabase_actor(req, owner_user_id)
    if mode not in {
        TEXT_SEARCH_AUTHORIZATION_VALUE,
        VOICE_SEARCH_AUTHORIZATION_VALUE,
    }:
        raise HTTPException(
            status_code=403,
            detail="invalid_web_search_authorization",
        )
    owner = require_actor_matches_owner(req, owner_user_id)
    if mode == VOICE_SEARCH_AUTHORIZATION_VALUE:
        await require_active_voice_session(req, owner)
    return owner


__all__ = [
    "TEXT_SEARCH_AUTHORIZATION_VALUE",
    "VOICE_SEARCH_AUTHORIZATION_HEADER",
    "VOICE_SEARCH_AUTHORIZATION_VALUE",
    "require_web_search_actor_v1",
]
