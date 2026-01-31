from __future__ import annotations

import os
import uuid
import decimal
import datetime as _dt
import asyncpg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

SCHEMA = os.getenv("LIFESWITCH_TRAINING_SCHEMA", "lifeswitch_training")

def _json_safe(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (decimal.Decimal,)):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v

def _row_to_jsonable(r):
    d = dict(r)
    return {k: _json_safe(v) for k, v in d.items()}

def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")

async def _db():
    return await asyncpg.connect(DSN)

# ----------------------------
# My Exercises
# ----------------------------

@router.get("/my_exercises")
async def list_my_exercises(
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await conn.fetch(
            f"""
            select
              my_exercise_id, owner_user_id,
              exercise_id, display_name, kind, modality,
              brand_name, model_name, matched_text, matched_source,
              is_active, created_at, updated_at
            from {SCHEMA}.my_exercise
            where owner_user_id=$1::uuid
              {where_active}
            order by lower(display_name) asc
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.post("/my_exercises/upsert")
async def upsert_my_exercise(
    owner_user_id: str = Query(..., min_length=1),
    exercise_id: str = Query(..., min_length=1, max_length=200),
    display_name: str = Query(..., min_length=1, max_length=200),
    kind: str = Query("", max_length=80),
    modality: str = Query("", max_length=120),
    brand_name: str | None = Query(None, max_length=120),
    model_name: str | None = Query(None, max_length=120),
    matched_text: str | None = Query(None, max_length=240),
    matched_source: str | None = Query(None, max_length=120),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.my_exercise
              (owner_user_id, exercise_id, display_name, kind, modality,
               brand_name, model_name, matched_text, matched_source, is_active)
            values
              ($1::uuid, $2, $3, $4, $5,
               $6, $7, $8, $9, true)
            on conflict (owner_user_id, exercise_id) do update
              set display_name=excluded.display_name,
                  kind=excluded.kind,
                  modality=excluded.modality,
                  brand_name=excluded.brand_name,
                  model_name=excluded.model_name,
                  matched_text=excluded.matched_text,
                  matched_source=excluded.matched_source,
                  updated_at=now(),
                  is_active=true
            returning
              my_exercise_id, owner_user_id,
              exercise_id, display_name, kind, modality,
              brand_name, model_name, matched_text, matched_source,
              is_active, created_at, updated_at
            """,
            owner,
            exercise_id.strip(),
            display_name.strip(),
            (kind or "").strip(),
            (modality or "").strip(),
            (brand_name or None),
            (model_name or None),
            (matched_text or None),
            (matched_source or None),
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()

@router.post("/my_exercises/{my_exercise_id}/deactivate")
async def deactivate_my_exercise(
    my_exercise_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    mid = _as_uuid(my_exercise_id, "my_exercise_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.my_exercise
               set is_active=false, updated_at=now()
             where my_exercise_id=$1::uuid
               and owner_user_id=$2::uuid
            returning my_exercise_id, owner_user_id, is_active, updated_at
            """,
            mid, owner
        )
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()

# ----------------------------
# Workout Templates
# ----------------------------

@router.get("/workout_templates")
async def list_workout_templates(
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await conn.fetch(
            f"""
            select
              workout_template_id, owner_user_id,
              name, notes, is_active, created_at, updated_at
            from {SCHEMA}.workout_template
            where owner_user_id=$1::uuid
              {where_active}
            order by updated_at desc
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.post("/workout_templates/upsert")
async def upsert_workout_template(
    owner_user_id: str = Query(..., min_length=1),
    workout_template_id: str | None = Query(None),
    name: str = Query(..., min_length=1, max_length=120),
    notes: str | None = Query(None, max_length=400),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None

    conn = await _db()
    try:
        if wid:
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.workout_template
                  (workout_template_id, owner_user_id, name, notes, is_active)
                values
                  ($1::uuid, $2::uuid, $3, $4, true)
                on conflict (workout_template_id) do update
                  set name=excluded.name,
                      notes=excluded.notes,
                      updated_at=now(),
                      is_active=true
                returning workout_template_id, owner_user_id, name, notes, is_active, created_at, updated_at
                """,
                wid, owner, name.strip(), (notes or "").strip()
            )
        else:
            # name-unique per owner (matches nutrition pattern)
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.workout_template
                  (owner_user_id, name, notes, is_active)
                values
                  ($1::uuid, $2, $3, true)
                on conflict (owner_user_id, name) do update
                  set notes=excluded.notes,
                      updated_at=now(),
                      is_active=true
                returning workout_template_id, owner_user_id, name, notes, is_active, created_at, updated_at
                """,
                owner, name.strip(), (notes or "").strip()
            )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()

@router.post("/workout_templates/{workout_template_id}/deactivate")
async def deactivate_workout_template(
    workout_template_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.workout_template
               set is_active=false, updated_at=now()
             where workout_template_id=$1::uuid
               and owner_user_id=$2::uuid
            returning workout_template_id, owner_user_id, is_active, updated_at
            """,
            wid, owner
        )
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()

# ----------------------------
# Workout Template Exercises
# ----------------------------

@router.get("/workout_templates/{workout_template_id}/exercises")
async def list_workout_template_exercises(workout_template_id: str):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              workout_template_exercise_id, workout_template_id,
              exercise_id, sort_order,
              planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            from {SCHEMA}.workout_template_exercise
            where workout_template_id=$1::uuid
            order by sort_order asc, created_at asc
            """,
            wid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()

@router.post("/workout_templates/{workout_template_id}/exercises/upsert")
async def upsert_workout_template_exercise(
    workout_template_id: str,
    exercise_id: str = Query(..., min_length=1, max_length=200),
    sort_order: int = Query(0),
    planned_sets: int = Query(3, ge=0, le=50),
    default_weight: float = Query(0),
    default_reps: int = Query(10, ge=0, le=200),
    flags: str | None = Query(None, max_length=240),
):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.workout_template_exercise
              (workout_template_id, exercise_id, sort_order, planned_sets, default_weight, default_reps, flags)
            values
              ($1::uuid, $2, $3, $4, $5, $6, $7)
            on conflict (workout_template_id, exercise_id) do update
              set sort_order=excluded.sort_order,
                  planned_sets=excluded.planned_sets,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  flags=excluded.flags,
                  updated_at=now()
            returning
              workout_template_exercise_id, workout_template_id,
              exercise_id, sort_order, planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            """,
            wid,
            exercise_id.strip(),
            int(sort_order),
            int(planned_sets),
            float(default_weight),
            int(default_reps),
            (flags or "").strip(),
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()

@router.post("/workout_templates/{workout_template_id}/exercises/{workout_template_exercise_id}/delete")
async def delete_workout_template_exercise(
    workout_template_id: str,
    workout_template_exercise_id: str,
):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        res = await conn.execute(
            f"""
            delete from {SCHEMA}.workout_template_exercise
             where workout_template_exercise_id=$1::uuid
               and workout_template_id=$2::uuid
            """,
            weid, wid
        )
        # asyncpg returns "DELETE N"
        return JSONResponse({"ok": True, "result": str(res)})
    finally:
        await conn.close()
