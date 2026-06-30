from __future__ import annotations

import uuid
from fastapi import HTTPException, Request


def _uuid_text(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def require_actor_matches_owner(req: Request, owner_user_id: str) -> str:
    """
    Require x-vs-actor-user-id to match the owner_user_id being requested.

    The frontend/BFF is responsible for resolving the authenticated Supabase user
    and sending x-vs-actor-user-id. Brains verifies it before trusting owner_user_id.
    """
    actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")

    actor_uid = _uuid_text(actor, "actor_user_id")
    owner_uid = _uuid_text(owner_user_id, "owner_user_id")

    if actor_uid != owner_uid:
        raise HTTPException(status_code=403, detail="actor_owner_mismatch")

    return owner_uid
