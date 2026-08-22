from __future__ import annotations

import datetime as _dt
import hashlib
import secrets

from fastapi import Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_training_sharing_postgres import (
    WorkoutShareImportError,
    lifeswitch_training_sharing_repository,
)
from seebx.core.identity import require_actor

from .common import _as_uuid, _clean_text, _row_to_jsonable


def _new_share_token() -> str:
    return secrets.token_urlsafe(32)

def _token_hash(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="token required")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _without_token_hash(row):
    out = _row_to_jsonable(row)
    if isinstance(out, dict):
        out.pop("token_hash", None)
    return out

async def create_workout_template_share(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    workout_template_id: str = Query(..., min_length=1),
    label: str = Body(""),
    notes: str = Body(""),
):
    owner = await require_actor(req, owner_user_id)
    wid = _as_uuid(workout_template_id, "workout_template_id")
    token = _new_share_token()
    token_hash = _token_hash(token)

    async with lifeswitch_training_sharing_repository(req) as repository:
        template, share = await repository.create_share(
            owner_user_id=owner,
            workout_template_id=wid,
            token_hash=token_hash,
            label=_clean_text(label, 200),
            notes=_clean_text(notes, 2000),
        )
    if not template:
        raise HTTPException(status_code=404, detail="workout template not found")

    out = _without_token_hash(share)
    out["token"] = token
    out["workout_name"] = str(template["name"])
    return JSONResponse(out)

async def list_workout_template_shares(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = await require_actor(req, owner_user_id)
    async with lifeswitch_training_sharing_repository(req) as repository:
        rows = await repository.list_shares(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def preview_workout_template_share(
    req: Request,
    token: str = Query(..., min_length=10),
):
    token_hash = _token_hash(token)
    async with lifeswitch_training_sharing_repository(req) as repository:
        preview = await repository.preview_share(
            token_hash=token_hash,
            now=_dt.datetime.now(_dt.timezone.utc),
        )
    if not preview:
        raise HTTPException(status_code=404, detail="share not found")
    if preview.expired:
        out = _row_to_jsonable(preview.share)
        out["status"] = "expired"
        return JSONResponse(out)

    segments_by_parent = {}
    for segment in preview.segments:
        key = str(segment["workout_template_exercise_id"])
        segments_by_parent.setdefault(key, []).append(_row_to_jsonable(segment))

    exercises = []
    for exercise in preview.exercises:
        out = _row_to_jsonable(exercise)
        out["segments"] = segments_by_parent.get(
            str(exercise["workout_template_exercise_id"]), []
        )
        exercises.append(out)

    out = _row_to_jsonable(preview.share)
    out["exercises"] = exercises
    return JSONResponse(out)

async def import_workout_template_share(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    token: str = Query(..., min_length=10),
):
    importer = await require_actor(req, owner_user_id)
    token_hash = _token_hash(token)

    try:
        async with lifeswitch_training_sharing_repository(req) as repository:
            imported = await repository.import_share(
                importer_user_id=importer,
                token_hash=token_hash,
                now=_dt.datetime.now(_dt.timezone.utc),
            )
    except WorkoutShareImportError as error:
        status_code = 404 if error.code == "not_found" else 400
        raise HTTPException(status_code=status_code, detail=error.detail) from error

    return JSONResponse(
        {
            "imported_workout": _row_to_jsonable(imported.imported_workout),
            "copied_exercises": [
                _row_to_jsonable(exercise) for exercise in imported.copied_exercises
            ],
        }
    )

async def revoke_workout_template_share(
    workout_template_share_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    share_id = _as_uuid(
        workout_template_share_id, "workout_template_share_id"
    )
    owner = await require_actor(req, owner_user_id)
    async with lifeswitch_training_sharing_repository(req) as repository:
        row = await repository.revoke_share(
            workout_template_share_id=share_id,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="active share not found")
    return JSONResponse(_row_to_jsonable(row))
