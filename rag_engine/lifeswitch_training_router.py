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
              exercise_id, sort_order, set_type,
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
    set_type: str = Query("straight", max_length=40),
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
              (workout_template_id, exercise_id, sort_order, set_type, planned_sets, default_weight, default_reps, flags)
            values
              ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
            on conflict (workout_template_id, exercise_id) do update
              set sort_order=excluded.sort_order,
                  set_type=excluded.set_type,
                  planned_sets=excluded.planned_sets,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  flags=excluded.flags,
                  updated_at=now()
            returning
              workout_template_exercise_id, workout_template_id,
              exercise_id, sort_order, set_type, planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            """,
            wid,
            exercise_id.strip(),
            int(sort_order),
              (set_type or "straight").strip().lower(),
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



# ----------------------------
# Workout Template Exercise Segments
# ----------------------------

@router.get("/workout_template_exercises/{workout_template_exercise_id}/segments")
async def list_workout_template_exercise_segments(workout_template_exercise_id: str):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              workout_template_exercise_segment_id,
              workout_template_exercise_id,
              segment_index,
              label,
              default_weight,
              default_reps,
              created_at,
              updated_at
            from {SCHEMA}.workout_template_exercise_segment
            where workout_template_exercise_id=$1::uuid
            order by segment_index asc, created_at asc
            """,
            weid,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/upsert")
async def upsert_workout_template_exercise_segment(
    workout_template_exercise_id: str,
    segment_index: int = Query(1, ge=1, le=50),
    label: str | None = Query(None, max_length=120),
    default_weight: float = Query(0),
    default_reps: int = Query(0, ge=0, le=1000),
):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.workout_template_exercise_segment
              (workout_template_exercise_id, segment_index, label, default_weight, default_reps)
            values
              ($1::uuid, $2, $3, $4, $5)
            on conflict (workout_template_exercise_id, segment_index) do update
              set label=excluded.label,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  updated_at=now()
            returning
              workout_template_exercise_segment_id,
              workout_template_exercise_id,
              segment_index,
              label,
              default_weight,
              default_reps,
              created_at,
              updated_at
            """,
            weid,
            int(segment_index),
            (label or "").strip(),
            float(default_weight),
            int(default_reps),
        )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()


@router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/{workout_template_exercise_segment_id}/delete")
async def delete_workout_template_exercise_segment(
    workout_template_exercise_id: str,
    workout_template_exercise_segment_id: str,
):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    segid = _as_uuid(workout_template_exercise_segment_id, "workout_template_exercise_segment_id")
    conn = await _db()
    try:
        res = await conn.execute(
            f"""
            delete from {SCHEMA}.workout_template_exercise_segment
             where workout_template_exercise_segment_id=$1::uuid
               and workout_template_exercise_id=$2::uuid
            """,
            segid,
            weid,
        )
        return JSONResponse({"ok": True, "result": str(res)})
    finally:
        await conn.close()


# ----------------------------
# Training Sessions / Set Log
# ----------------------------

@router.post("/sessions/create")
async def create_training_session(
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    name: str = Query(..., min_length=1, max_length=160),
    workout_template_id: str | None = Query(None),
    notes: str | None = Query(None, max_length=800),
    started_at: str | None = Query(None),
    finished_at: str | None = Query(None),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None

    try:
        day_val = _dt.date.fromisoformat(day)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid day")

    started = None
    finished = None
    try:
        if started_at:
            started = _dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if finished_at:
            finished = _dt.datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid timestamp")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.training_session
              (owner_user_id, day, workout_template_id, name, notes, started_at, finished_at, is_active)
            values
              ($1::uuid, $2::date, $3::uuid, $4, $5, $6::timestamptz, $7::timestamptz, true)
            returning
              training_session_id, owner_user_id, day, workout_template_id, name, notes,
              started_at, finished_at, is_active, created_at, updated_at
            """,
            owner,
            day_val,
            wid,
            name.strip(),
            (notes or "").strip(),
            started,
            finished,
        )
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.get("/sessions")
async def list_training_sessions(
    owner_user_id: str = Query(..., min_length=1),
    day: str | None = Query(None),
    include_inactive: int = Query(0, ge=0, le=1),
    limit: int = Query(100, ge=1, le=500),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")

    day_val = None
    if day:
        try:
            day_val = _dt.date.fromisoformat(day)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid day")

    conn = await _db()
    try:
        where = ["s.owner_user_id=$1::uuid"]
        args = [owner]
        if day_val:
            args.append(day_val)
            where.append(f"s.day=${len(args)}::date")
        if not include_inactive:
            where.append("s.is_active=true")

        rows = await conn.fetch(
            f"""
            select
              s.training_session_id, s.owner_user_id, s.day, s.workout_template_id,
              s.name, s.notes, s.started_at, s.finished_at, s.is_active,
              s.created_at, s.updated_at,
              coalesce(count(l.training_set_log_id) filter (where l.is_active=true), 0)::int as set_count,
              coalesce(count(distinct l.exercise_id) filter (where l.is_active=true), 0)::int as exercise_count,
              coalesce(sum(l.volume) filter (where l.is_active=true), 0)::float as volume
            from {SCHEMA}.training_session s
            left join {SCHEMA}.training_set_log l
              on l.training_session_id=s.training_session_id
            where {' and '.join(where)}
            group by s.training_session_id
            order by s.day desc, s.created_at desc
            limit {int(limit)}
            """,
            *args,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/sessions/{training_session_id}")
async def get_training_session(
    training_session_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            select
              training_session_id, owner_user_id, day, workout_template_id,
              name, notes, started_at, finished_at, is_active, created_at, updated_at
            from {SCHEMA}.training_session
            where training_session_id=$1::uuid
              and owner_user_id=$2::uuid
            """,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()



@router.post("/sessions/{training_session_id}/deactivate")
async def deactivate_training_session(
    training_session_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.training_session
               set is_active=false, updated_at=now()
             where training_session_id=$1::uuid
               and owner_user_id=$2::uuid
            returning
              training_session_id, owner_user_id, day, name, is_active, updated_at
            """,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.get("/sessions/{training_session_id}/sets")
async def list_training_session_sets(
    training_session_id: str,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await conn.fetch(
            f"""
            select
              training_set_log_id, training_session_id, owner_user_id,
              workout_template_id, exercise_id, exercise_name,
              exercise_sort_order, set_index, weight, reps, volume,
              flags, notes, is_active, created_at, updated_at
            from {SCHEMA}.training_set_log
            where training_session_id=$1::uuid
              and owner_user_id=$2::uuid
              {where_active}
            order by exercise_sort_order asc, set_index asc, created_at asc
            """,
            sid,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/add")
async def add_training_set_log(
    training_session_id: str,
    owner_user_id: str = Query(..., min_length=1),
    exercise_id: str = Query(..., min_length=1, max_length=200),
    exercise_name: str = Query(..., min_length=1, max_length=240),
    workout_template_id: str | None = Query(None),
    exercise_sort_order: int = Query(0),
    set_index: int = Query(1, ge=1, le=200),
    weight: float = Query(0),
    reps: int = Query(0, ge=0, le=1000),
    flags: str | None = Query(None, max_length=240),
    notes: str | None = Query(None, max_length=800),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None
    volume = float(weight) * int(reps)

    conn = await _db()
    try:
        session = await conn.fetchrow(
            f"""
            select training_session_id, workout_template_id
            from {SCHEMA}.training_session
            where training_session_id=$1::uuid
              and owner_user_id=$2::uuid
              and is_active=true
            """,
            sid,
            owner,
        )
        if not session:
            raise HTTPException(status_code=404, detail="session not found")

        if wid is None and session.get("workout_template_id"):
            wid = str(session["workout_template_id"])

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.training_set_log
              (training_session_id, owner_user_id, workout_template_id,
               exercise_id, exercise_name, exercise_sort_order, set_index,
               weight, reps, volume, flags, notes, is_active)
            values
              ($1::uuid, $2::uuid, $3::uuid,
               $4, $5, $6, $7,
               $8, $9, $10, $11, $12, true)
            returning
              training_set_log_id, training_session_id, owner_user_id,
              workout_template_id, exercise_id, exercise_name,
              exercise_sort_order, set_index, weight, reps, volume,
              flags, notes, is_active, created_at, updated_at
            """,
            sid,
            owner,
            wid,
            exercise_id.strip(),
            exercise_name.strip(),
            int(exercise_sort_order),
            int(set_index),
            float(weight),
            int(reps),
            float(volume),
            (flags or "").strip(),
            (notes or "").strip(),
        )

        await conn.execute(
            f"update {SCHEMA}.training_session set updated_at=now() where training_session_id=$1::uuid",
            sid,
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/update")
async def update_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    owner_user_id: str = Query(..., min_length=1),
    exercise_sort_order: int | None = Query(None),
    set_index: int | None = Query(None, ge=1, le=200),
    weight: float | None = Query(None),
    reps: int | None = Query(None, ge=0, le=1000),
    flags: str | None = Query(None, max_length=240),
    notes: str | None = Query(None, max_length=800),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        old = await conn.fetchrow(
            f"""
            select weight, reps
            from {SCHEMA}.training_set_log
            where training_set_log_id=$1::uuid
              and training_session_id=$2::uuid
              and owner_user_id=$3::uuid
            """,
            setid,
            sid,
            owner,
        )
        if not old:
            raise HTTPException(status_code=404, detail="set not found")

        next_weight = float(weight) if weight is not None else float(old["weight"])
        next_reps = int(reps) if reps is not None else int(old["reps"])
        next_volume = next_weight * next_reps

        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.training_set_log
               set exercise_sort_order=coalesce($4, exercise_sort_order),
                   set_index=coalesce($5, set_index),
                   weight=$6,
                   reps=$7,
                   volume=$8,
                   flags=coalesce($9, flags),
                   notes=coalesce($10, notes),
                   updated_at=now()
             where training_set_log_id=$1::uuid
               and training_session_id=$2::uuid
               and owner_user_id=$3::uuid
            returning
              training_set_log_id, training_session_id, owner_user_id,
              workout_template_id, exercise_id, exercise_name,
              exercise_sort_order, set_index, weight, reps, volume,
              flags, notes, is_active, created_at, updated_at
            """,
            setid,
            sid,
            owner,
            exercise_sort_order,
            set_index,
            next_weight,
            next_reps,
            next_volume,
            (flags.strip() if flags is not None else None),
            (notes.strip() if notes is not None else None),
        )

        await conn.execute(
            f"update {SCHEMA}.training_session set updated_at=now() where training_session_id=$1::uuid",
            sid,
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/delete")
async def delete_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.training_set_log
               set is_active=false, updated_at=now()
             where training_set_log_id=$1::uuid
               and training_session_id=$2::uuid
               and owner_user_id=$3::uuid
            returning training_set_log_id, training_session_id, owner_user_id, is_active, updated_at
            """,
            setid,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="set not found")

        await conn.execute(
            f"update {SCHEMA}.training_session set updated_at=now() where training_session_id=$1::uuid",
            sid,
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
