from __future__ import annotations

import os

from fastapi import HTTPException, Request

from seebx.adapters.lifeswitch_training_access_postgres import lifeswitch_training_access_repository

from .common import _as_uuid


async def _resolve_training_view_target(req: Request, viewer_user_id: str, target_user_id: str = "") -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        if os.getenv("LIFESWITCH_DELEGATED_READS_ENABLED", "0") != "1":
            raise HTTPException(status_code=403, detail="delegated_access_disabled")
        async with lifeswitch_training_access_repository(req) as repository:
            allowed = await repository.has_people_permission(
                grantor_user_id=target,
                grantee_user_id=viewer,
                scope="training:view",
            )
        if not allowed:
            raise HTTPException(status_code=403, detail="training:view permission required")

    return target, delegated
