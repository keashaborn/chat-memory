from __future__ import annotations

from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_training_templates_postgres import (
    TemplateUpsertError,
    lifeswitch_training_templates_repository,
)
from seebx.core.ownership import require_actor_matches_owner

from .common import _as_uuid, _clean_text, _require_idempotency_key, _row_to_jsonable


async def list_workout_templates(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_templates_repository(req) as repository:
        rows = await repository.list_templates(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def upsert_workout_template(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    workout_template_id: str | None = Query(None),
    name: str = Query(..., min_length=1, max_length=120),
    notes: str | None = Query(None, max_length=400),
    workout_role: str | None = Query(None),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    template_id = (
        _as_uuid(workout_template_id, "workout_template_id")
        if workout_template_id
        else None
    )
    role = _clean_text(workout_role, 16).lower()
    if role not in {"strength", "rehab"}:
        raise HTTPException(
            status_code=400, detail="workout_role must be strength or rehab"
        )
    write_key = _require_idempotency_key(idempotency_key)

    def verify_existing_owner(existing_owner) -> None:
        if existing_owner and str(existing_owner) != owner:
            raise HTTPException(status_code=403, detail="actor_owner_mismatch")

    try:
        async with lifeswitch_training_templates_repository(req) as repository:
            row = await repository.upsert_template(
                owner_user_id=owner,
                workout_template_id=template_id,
                name=name.strip(),
                notes=(notes or "").strip(),
                workout_role=role,
                idempotency_key=write_key,
                verify_existing_owner=verify_existing_owner,
            )
    except TemplateUpsertError as error:
        raise HTTPException(status_code=500, detail="upsert_failed") from error
    return JSONResponse(_row_to_jsonable(row))

async def classify_historical_workout_sessions(
    workout_template_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    workout_role: str = Query(..., min_length=1, max_length=16),
    reason: str | None = Query(None, max_length=240),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    template_id = _as_uuid(workout_template_id, "workout_template_id")
    role = _clean_text(workout_role, 16).lower()
    if role not in {"strength", "rehab"}:
        raise HTTPException(
            status_code=400, detail="workout_role must be strength or rehab"
        )
    write_key = _require_idempotency_key(idempotency_key)
    async with lifeswitch_training_templates_repository(req) as repository:
        count = await repository.classify_historical_sessions(
            owner_user_id=owner,
            workout_template_id=template_id,
            workout_role=role,
            reason=_clean_text(reason, 240)
            or "User applied workout role to older unclassified sessions",
            idempotency_key=write_key,
        )
    return JSONResponse({"ok": True, "classified_session_count": count})

async def deactivate_workout_template(
    workout_template_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    template_id = _as_uuid(workout_template_id, "workout_template_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_templates_repository(req) as repository:
        row = await repository.deactivate_template(
            workout_template_id=template_id,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_row_to_jsonable(row))

async def list_workout_template_exercises(workout_template_id: str, req: Request):
    template_id = _as_uuid(workout_template_id, "workout_template_id")
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.list_template_exercises(
            workout_template_id=template_id,
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(status_code=404, detail="workout_template not found")
    return JSONResponse([_row_to_jsonable(row) for row in result.value])

async def upsert_workout_template_exercise(
    workout_template_id: str,
    req: Request,
    exercise_id: str = Query(..., min_length=1, max_length=200),
    display_name_snapshot: str | None = Query(None, max_length=240),
    sort_order: int = Query(0),
    set_type: str = Query("straight", max_length=40),
    planned_sets: int = Query(3, ge=0, le=50),
    default_weight: float = Query(0),
    default_reps: int = Query(10, ge=0, le=200),
    flags: str | None = Query(None, max_length=240),
):
    template_id = _as_uuid(workout_template_id, "workout_template_id")
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.upsert_template_exercise(
            workout_template_id=template_id,
            exercise_id=exercise_id.strip(),
            display_name_snapshot=(display_name_snapshot or "").strip(),
            sort_order=int(sort_order),
            set_type=(set_type or "straight").strip().lower(),
            planned_sets=int(planned_sets),
            default_weight=float(default_weight),
            default_reps=int(default_reps),
            flags=(flags or "").strip(),
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(
            status_code=404, detail="workout_template not found or inactive"
        )
    row = result.value
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})

async def delete_workout_template_exercise(
    workout_template_id: str,
    workout_template_exercise_id: str,
    req: Request,
):
    template_id = _as_uuid(workout_template_id, "workout_template_id")
    exercise_id = _as_uuid(
        workout_template_exercise_id, "workout_template_exercise_id"
    )
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.delete_template_exercise(
            workout_template_id=template_id,
            workout_template_exercise_id=exercise_id,
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(status_code=404, detail="workout_template not found")
    return JSONResponse({"ok": True, "result": str(result.value)})

async def list_workout_template_exercise_segments(
    workout_template_exercise_id: str, req: Request
):
    exercise_id = _as_uuid(
        workout_template_exercise_id, "workout_template_exercise_id"
    )
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.list_exercise_segments(
            workout_template_exercise_id=exercise_id,
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(
            status_code=404, detail="workout_template_exercise not found"
        )
    return JSONResponse([_row_to_jsonable(row) for row in result.value])

async def upsert_workout_template_exercise_segment(
    workout_template_exercise_id: str,
    req: Request,
    segment_index: int = Query(1, ge=1, le=50),
    label: str | None = Query(None, max_length=120),
    default_weight: float = Query(0),
    default_reps: int = Query(0, ge=0, le=1000),
):
    exercise_id = _as_uuid(
        workout_template_exercise_id, "workout_template_exercise_id"
    )
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.upsert_exercise_segment(
            workout_template_exercise_id=exercise_id,
            segment_index=int(segment_index),
            label=(label or "").strip(),
            default_weight=float(default_weight),
            default_reps=int(default_reps),
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="workout_template_exercise not found or inactive",
        )
    row = result.value
    return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})

async def delete_workout_template_exercise_segment(
    workout_template_exercise_id: str,
    workout_template_exercise_segment_id: str,
    req: Request,
):
    exercise_id = _as_uuid(
        workout_template_exercise_id, "workout_template_exercise_id"
    )
    segment_id = _as_uuid(
        workout_template_exercise_segment_id,
        "workout_template_exercise_segment_id",
    )
    async with lifeswitch_training_templates_repository(req) as repository:
        result = await repository.delete_exercise_segment(
            workout_template_exercise_id=exercise_id,
            workout_template_exercise_segment_id=segment_id,
            authorize_owner=lambda owner: require_actor_matches_owner(req, owner),
        )
    if result is None:
        raise HTTPException(
            status_code=404, detail="workout_template_exercise not found"
        )
    return JSONResponse({"ok": True, "result": str(result.value)})
