from __future__ import annotations

"""Canonical request actor and owner binding for SeeBx capabilities."""

import uuid
from fastapi import HTTPException, Request


def _uuid_text(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def require_actor_matches_owner(req: Request, owner_user_id: str) -> str:
    """
    Require the asserted actor header to match the requested owner.

    This assertion is not standalone identity authority. Callers must also bind
    it to a verified Supabase token or an active voice-session lease.
    """
    actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")

    actor_uid = _uuid_text(actor, "actor_user_id")
    owner_uid = _uuid_text(owner_user_id, "owner_user_id")

    if actor_uid != owner_uid:
        raise HTTPException(status_code=403, detail="actor_owner_mismatch")

    return owner_uid


def require_authenticated_actor(req: Request) -> str:
    """Return the backend-authenticated actor as canonical UUID text."""
    actor = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not actor:
        raise HTTPException(status_code=401, detail="missing_actor_user_id")
    return _uuid_text(actor, "actor_user_id")


__all__ = ["require_actor_matches_owner", "require_authenticated_actor"]
