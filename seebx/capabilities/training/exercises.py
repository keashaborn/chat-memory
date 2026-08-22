from __future__ import annotations

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_training_exercises_postgres import lifeswitch_training_exercises_repository
from seebx.core.ownership import require_actor_matches_owner

from .common import _as_uuid, _clean_text, _row_to_jsonable


async def list_my_exercises(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_exercises_repository(req) as repository:
        rows = await repository.list_exercises(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def upsert_my_exercise(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    exercise_id: str = Query(..., min_length=1, max_length=200),
    display_name: str = Query(..., min_length=1, max_length=200),
    kind: str = Query("", max_length=80),
    modality: str = Query("", max_length=120),
    brand_name: str | None = Query(None, max_length=120),
    model_name: str | None = Query(None, max_length=120),
    matched_text: str | None = Query(None, max_length=240),
    matched_source: str | None = Query(None, max_length=120),
    exercise_role: str | None = Query(None, max_length=20),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    clean_role = _clean_text(exercise_role, 20).lower()
    if clean_role and clean_role not in {"strength", "rehab"}:
        raise HTTPException(status_code=400, detail="exercise_role must be strength or rehab")
    async with lifeswitch_training_exercises_repository(req) as repository:
        row = await repository.upsert_exercise(
            owner_user_id=owner,
            exercise_id=exercise_id.strip(),
            display_name=display_name.strip(),
            kind=(kind or "").strip(),
            modality=(modality or "").strip(),
            brand_name=(brand_name or None),
            model_name=(model_name or None),
            matched_text=(matched_text or None),
            matched_source=(matched_source or None),
            exercise_role=(clean_role or None),
        )
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})

async def deactivate_my_exercise(
    my_exercise_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    mid = _as_uuid(my_exercise_id, "my_exercise_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_exercises_repository(req) as repository:
        row = await repository.deactivate_exercise(
            my_exercise_id=mid,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_row_to_jsonable(row))
