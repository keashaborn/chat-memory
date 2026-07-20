from __future__ import annotations

import os
import json
import uuid
import decimal
import datetime as _dt
import hashlib
import secrets
import math
import asyncpg
from fastapi import APIRouter, HTTPException, Query, Body, Request
from rag_engine.lifeswitch_auth import require_actor_matches_owner
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")

SCHEMA = os.getenv("LIFESWITCH_TRAINING_SCHEMA", "lifeswitch_training")
PEOPLE_SCHEMA = os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")

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

def _conditioning_row_to_jsonable(r):
    out = _row_to_jsonable(r)

    raw = out.get("dose_config")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            out["dose_config"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            out["dose_config"] = {}
    elif not isinstance(raw, dict):
        out["dose_config"] = {}

    return out


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _clean_text(v, max_len: int | None = None) -> str:
    s = str(v or "").strip()
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s



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


async def _unique_imported_workout_name(conn, owner_user_id: str, base_name: str) -> str:
    base = _clean_text(base_name or "Imported Workout", 160) or "Imported Workout"
    candidate = base
    for i in range(0, 50):
        if i == 0:
            candidate = base
        elif i == 1:
            candidate = f"{base} (Imported)"
        else:
            candidate = f"{base} (Imported {i})"

        row = await conn.fetchrow(
            f"""
            select workout_template_id
            from {SCHEMA}.workout_template
            where owner_user_id=$1::uuid
              and lower(name)=lower($2)
            limit 1
            """,
            owner_user_id,
            candidate,
        )
        if not row:
            return candidate

    return f"{base} (Imported {secrets.token_hex(3)})"


async def _db():
    return await asyncpg.connect(DSN)


async def _has_people_permission(conn, grantor_user_id: str, grantee_user_id: str, scope: str) -> bool:
    row = await conn.fetchrow(
        f"""
        select rp.relationship_permission_id
        from {PEOPLE_SCHEMA}.relationship_permission rp
        join {PEOPLE_SCHEMA}.relationship r
          on r.relationship_id=rp.relationship_id
        where rp.grantor_user_id=$1::uuid
          and rp.grantee_user_id=$2::uuid
          and rp.permission_scope=$3
          and rp.is_enabled=true
          and r.status='accepted'
        limit 1
        """,
        grantor_user_id,
        grantee_user_id,
        scope,
    )
    return bool(row)


async def _resolve_training_view_target(conn, viewer_user_id: str, target_user_id: str = "") -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        allowed = await _has_people_permission(conn, target, viewer, "training:view")
        if not allowed:
            raise HTTPException(status_code=403, detail="training:view permission required")

    return target, delegated


# ----------------------------
# My Exercises
# ----------------------------

@router.get("/my_exercises")
async def list_my_exercises(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        where_active = "" if include_inactive else "and is_active=true"
        rows = await conn.fetch(
            f"""
            select
              my_exercise_id, owner_user_id,
              exercise_id, display_name, kind, modality,
              brand_name, model_name, matched_text, matched_source,
              exercise_role,
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
    conn = await _db()
    try:
        async with conn.transaction():
            existing = await conn.fetchrow(
                f"""
                select my_exercise_id, exercise_role
                from {SCHEMA}.my_exercise
                where owner_user_id=$1::uuid and exercise_id=$2
                for update
                """,
                owner,
                exercise_id.strip(),
            )
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.my_exercise
                  (owner_user_id, exercise_id, display_name, kind, modality,
                   brand_name, model_name, matched_text, matched_source,
                   exercise_role, is_active)
                values
                  ($1::uuid, $2, $3, $4, $5,
                   $6, $7, $8, $9, coalesce($10, 'strength'), true)
                on conflict (owner_user_id, exercise_id) do update
                  set display_name=excluded.display_name,
                      kind=excluded.kind,
                      modality=excluded.modality,
                      brand_name=excluded.brand_name,
                      model_name=excluded.model_name,
                      matched_text=excluded.matched_text,
                      matched_source=excluded.matched_source,
                      exercise_role=coalesce($10, {SCHEMA}.my_exercise.exercise_role),
                      updated_at=now(),
                      is_active=true
                returning
                  my_exercise_id, owner_user_id,
                  exercise_id, display_name, kind, modality,
                  brand_name, model_name, matched_text, matched_source,
                  exercise_role,
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
                (clean_role or None),
            )
            previous_role = str(existing["exercise_role"]) if existing else None
            next_role = str(row["exercise_role"]) if row else ""
            if row and previous_role != next_role:
                await conn.execute(
                    f"""
                    insert into {SCHEMA}.my_exercise_role_event
                      (owner_user_id, my_exercise_id, exercise_id,
                       previous_role, new_role, changed_by_user_id, change_source)
                    values ($1::uuid, $2::uuid, $3, $4, $5, $1::uuid, 'user')
                    """,
                    owner,
                    row["my_exercise_id"],
                    row["exercise_id"],
                    previous_role,
                    next_role,
                )
        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()

@router.post("/my_exercises/{my_exercise_id}/deactivate")
async def deactivate_my_exercise(
    my_exercise_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    mid = _as_uuid(my_exercise_id, "my_exercise_id")
    owner = require_actor_matches_owner(req, owner_user_id)
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
# Conditioning Library / Prescriptions
# ----------------------------

@router.get("/conditioning_library")
async def list_conditioning_library(
    include_inactive: int = Query(0, ge=0, le=1),
):
    conn = await _db()
    try:
        where_active = "" if include_inactive else "where is_active=true"
        rows = await conn.fetch(
            f"""
            select
              conditioning_library_id, slug, name, category, modality,
              purpose, default_duration_min, default_frequency_per_week,
              default_intensity, interference_risk, joint_stress, equipment,
              progression_notes, contraindication_notes,
              sort_order, is_active, created_at, updated_at
            from {SCHEMA}.conditioning_library
            {where_active}
            order by sort_order asc, lower(name) asc
            """
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/my_conditioning_prescriptions")
async def list_my_conditioning_prescriptions(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        where_active = "" if include_inactive else "and p.is_active=true"
        rows = await conn.fetch(
            f"""
            select
              p.my_conditioning_prescription_id,
              p.owner_user_id,
              p.conditioning_library_id,
              p.name,
              p.category,
              p.modality,
              p.purpose,
              p.target_duration_min,
              p.target_frequency_per_week,
              p.target_intensity,
              p.preferred_timing,
              p.recovery_constraints,
              p.notes,
              p.dose_type,
              p.dose_config,
              p.is_active,
              p.created_at,
              p.updated_at,
              l.slug as library_slug,
              l.name as library_name
            from {SCHEMA}.my_conditioning_prescription p
            left join {SCHEMA}.conditioning_library l
              on l.conditioning_library_id=p.conditioning_library_id
            where p.owner_user_id=$1::uuid
              {where_active}
            order by p.updated_at desc, lower(p.name) asc
            """,
            owner,
        )
        return JSONResponse([_conditioning_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/my_conditioning_prescriptions/upsert")
async def upsert_my_conditioning_prescription(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    my_conditioning_prescription_id: str | None = Query(None),
    conditioning_library_id: str | None = Query(None),

    name: str = Query(..., min_length=1, max_length=160),
    category: str = Query("", max_length=120),
    modality: str = Query("", max_length=120),
    purpose: str = Query("", max_length=800),

    target_duration_min: int = Query(0, ge=0, le=600),
    target_frequency_per_week: float = Query(0, ge=0, le=21),
    target_intensity: str = Query("", max_length=400),

    preferred_timing: str = Query("", max_length=400),
    recovery_constraints: str = Query("", max_length=800),
    notes: str = Query("", max_length=1200),

    dose_type: str = Query("open", max_length=40),
    dose_config: str = Query("{}", max_length=12000),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    pid = (
        _as_uuid(
            my_conditioning_prescription_id,
            "my_conditioning_prescription_id",
        )
        if my_conditioning_prescription_id
        else None
    )
    libid = (
        _as_uuid(conditioning_library_id, "conditioning_library_id")
        if conditioning_library_id
        else None
    )

    allowed_dose_types = {
        "open",
        "time",
        "distance",
        "rounds",
        "intervals",
        "laps",
        "repetitions",
        "loaded_carry",
    }

    clean_dose_type = str(dose_type or "open").strip().lower()
    if clean_dose_type not in allowed_dose_types:
        raise HTTPException(status_code=400, detail="invalid dose_type")

    try:
        parsed_dose_config = json.loads(dose_config or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid dose_config_json")

    if not isinstance(parsed_dose_config, dict):
        raise HTTPException(
            status_code=400,
            detail="dose_config_must_be_object",
        )

    dose_config_json = json.dumps(
        parsed_dose_config,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    conn = await _db()
    try:
        if pid:
            existing_owner = await conn.fetchval(
                f"""
                select owner_user_id
                from {SCHEMA}.my_conditioning_prescription
                where my_conditioning_prescription_id=$1::uuid
                """,
                pid,
            )
            if existing_owner and str(existing_owner) != owner:
                raise HTTPException(
                    status_code=403,
                    detail="actor_owner_mismatch",
                )

            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.my_conditioning_prescription
                  (
                    my_conditioning_prescription_id,
                    owner_user_id,
                    conditioning_library_id,
                    name,
                    category,
                    modality,
                    purpose,
                    target_duration_min,
                    target_frequency_per_week,
                    target_intensity,
                    preferred_timing,
                    recovery_constraints,
                    notes,
                    dose_type,
                    dose_config,
                    is_active
                  )
                values
                  (
                    $1::uuid,
                    $2::uuid,
                    $3::uuid,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9,
                    $10,
                    $11,
                    $12,
                    $13,
                    $14,
                    $15::jsonb,
                    true
                  )
                on conflict (my_conditioning_prescription_id) do update
                  set conditioning_library_id=excluded.conditioning_library_id,
                      name=excluded.name,
                      category=excluded.category,
                      modality=excluded.modality,
                      purpose=excluded.purpose,
                      target_duration_min=excluded.target_duration_min,
                      target_frequency_per_week=excluded.target_frequency_per_week,
                      target_intensity=excluded.target_intensity,
                      preferred_timing=excluded.preferred_timing,
                      recovery_constraints=excluded.recovery_constraints,
                      notes=excluded.notes,
                      dose_type=excluded.dose_type,
                      dose_config=excluded.dose_config,
                      is_active=true,
                      updated_at=now()
                returning
                  my_conditioning_prescription_id,
                  owner_user_id,
                  conditioning_library_id,
                  name,
                  category,
                  modality,
                  purpose,
                  target_duration_min,
                  target_frequency_per_week,
                  target_intensity,
                  preferred_timing,
                  recovery_constraints,
                  notes,
                  dose_type,
                  dose_config,
                  is_active,
                  created_at,
                  updated_at
                """,
                pid,
                owner,
                libid,
                name.strip(),
                category.strip(),
                modality.strip(),
                purpose.strip(),
                int(target_duration_min),
                float(target_frequency_per_week),
                target_intensity.strip(),
                preferred_timing.strip(),
                recovery_constraints.strip(),
                notes.strip(),
                clean_dose_type,
                dose_config_json,
            )
        else:
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.my_conditioning_prescription
                  (
                    owner_user_id,
                    conditioning_library_id,
                    name,
                    category,
                    modality,
                    purpose,
                    target_duration_min,
                    target_frequency_per_week,
                    target_intensity,
                    preferred_timing,
                    recovery_constraints,
                    notes,
                    dose_type,
                    dose_config,
                    is_active
                  )
                values
                  (
                    $1::uuid,
                    $2::uuid,
                    $3,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9,
                    $10,
                    $11,
                    $12,
                    $13,
                    $14::jsonb,
                    true
                  )
                on conflict (owner_user_id, name) do update
                  set conditioning_library_id=excluded.conditioning_library_id,
                      category=excluded.category,
                      modality=excluded.modality,
                      purpose=excluded.purpose,
                      target_duration_min=excluded.target_duration_min,
                      target_frequency_per_week=excluded.target_frequency_per_week,
                      target_intensity=excluded.target_intensity,
                      preferred_timing=excluded.preferred_timing,
                      recovery_constraints=excluded.recovery_constraints,
                      notes=excluded.notes,
                      dose_type=excluded.dose_type,
                      dose_config=excluded.dose_config,
                      is_active=true,
                      updated_at=now()
                returning
                  my_conditioning_prescription_id,
                  owner_user_id,
                  conditioning_library_id,
                  name,
                  category,
                  modality,
                  purpose,
                  target_duration_min,
                  target_frequency_per_week,
                  target_intensity,
                  preferred_timing,
                  recovery_constraints,
                  notes,
                  dose_type,
                  dose_config,
                  is_active,
                  created_at,
                  updated_at
                """,
                owner,
                libid,
                name.strip(),
                category.strip(),
                modality.strip(),
                purpose.strip(),
                int(target_duration_min),
                float(target_frequency_per_week),
                target_intensity.strip(),
                preferred_timing.strip(),
                recovery_constraints.strip(),
                notes.strip(),
                clean_dose_type,
                dose_config_json,
            )

        return JSONResponse(
            _conditioning_row_to_jsonable(row)
            if row
            else {"error": "upsert_failed"}
        )
    finally:
        await conn.close()


@router.post("/my_conditioning_prescriptions/{my_conditioning_prescription_id}/deactivate")
async def deactivate_my_conditioning_prescription(
    my_conditioning_prescription_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    pid = _as_uuid(my_conditioning_prescription_id, "my_conditioning_prescription_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.my_conditioning_prescription
               set is_active=false, updated_at=now()
             where my_conditioning_prescription_id=$1::uuid
               and owner_user_id=$2::uuid
            returning my_conditioning_prescription_id, owner_user_id, is_active, updated_at
            """,
            pid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


# ----------------------------
# Conditioning Session Log
# ----------------------------

@router.post("/conditioning_sessions/create")
async def create_conditioning_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str = Query(..., min_length=10, max_length=10),
    my_conditioning_prescription_id: str | None = Query(None),

    name: str = Query(..., min_length=1, max_length=180),
    category: str = Query("", max_length=120),
    modality: str = Query("", max_length=120),

    duration_min: float = Query(0, ge=0, le=1440),
    intensity: str = Query("", max_length=400),
    distance: str = Query("", max_length=160),
    heart_rate_avg: float | None = Query(None, ge=0, le=260),
    recovery_impact: str = Query("", max_length=400),
    notes: str = Query("", max_length=1600),

    dose_type: str = Query("open", max_length=40),
    dose_config: str = Query("{}", max_length=12000),
):
    owner = require_actor_matches_owner(req, owner_user_id)

    pid = (
        _as_uuid(
            my_conditioning_prescription_id,
            "my_conditioning_prescription_id",
        )
        if my_conditioning_prescription_id
        else None
    )

    try:
        day_val = _dt.date.fromisoformat(day)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid day")

    allowed_dose_types = {
        "open",
        "time",
        "distance",
        "rounds",
        "intervals",
        "laps",
        "repetitions",
        "loaded_carry",
    }

    clean_dose_type = str(dose_type or "open").strip().lower()
    if clean_dose_type not in allowed_dose_types:
        raise HTTPException(status_code=400, detail="invalid dose_type")

    try:
        parsed_dose_config = json.loads(dose_config or "{}")
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="invalid dose_config_json",
        )

    if not isinstance(parsed_dose_config, dict):
        raise HTTPException(
            status_code=400,
            detail="dose_config_must_be_object",
        )

    dose_config_json = json.dumps(
        parsed_dose_config,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.conditioning_session_log
              (
                owner_user_id,
                my_conditioning_prescription_id,
                day,
                name,
                category,
                modality,
                duration_min,
                intensity,
                distance,
                heart_rate_avg,
                recovery_impact,
                notes,
                dose_type,
                dose_config,
                is_active
              )
            values
              (
                $1::uuid,
                $2::uuid,
                $3::date,
                $4,
                $5,
                $6,
                $7,
                $8,
                $9,
                $10,
                $11,
                $12,
                $13,
                $14::jsonb,
                true
              )
            returning
              conditioning_session_log_id,
              owner_user_id,
              my_conditioning_prescription_id,
              day,
              name,
              category,
              modality,
              duration_min,
              intensity,
              distance,
              heart_rate_avg,
              recovery_impact,
              notes,
              dose_type,
              dose_config,
              is_active,
              created_at,
              updated_at
            """,
            owner,
            pid,
            day_val,
            name.strip(),
            category.strip(),
            modality.strip(),
            float(duration_min),
            intensity.strip(),
            distance.strip(),
            float(heart_rate_avg)
            if heart_rate_avg is not None
            else None,
            recovery_impact.strip(),
            notes.strip(),
            clean_dose_type,
            dose_config_json,
        )

        return JSONResponse(
            _conditioning_row_to_jsonable(row)
        )
    finally:
        await conn.close()


@router.get("/conditioning_sessions")
async def list_conditioning_sessions(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str | None = Query(None),
    include_inactive: int = Query(0, ge=0, le=1),
    limit: int = Query(100, ge=1, le=500),
    target_user_id: str = Query("", max_length=80),
):
    viewer = require_actor_matches_owner(req, owner_user_id)

    day_val = None
    if day:
        try:
            day_val = _dt.date.fromisoformat(day)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid day")

    conn = await _db()
    try:
        owner, delegated = await _resolve_training_view_target(conn, viewer, target_user_id)

        where = ["c.owner_user_id=$1::uuid"]
        args = [owner, delegated]

        if day_val:
            args.append(day_val)
            where.append(f"c.day=${len(args)}::date")

        if not include_inactive:
            where.append("c.is_active=true")

        rows = await conn.fetch(
            f"""
            select
              c.conditioning_session_log_id,
              c.owner_user_id,
              c.my_conditioning_prescription_id,
              c.day,
              c.name,
              c.category,
              c.modality,
              c.duration_min,
              c.intensity,
              c.distance,
              c.heart_rate_avg,
              c.recovery_impact,
              c.notes,
              c.dose_type,
              c.dose_config,
              c.is_active,
              c.created_at,
              c.updated_at,
              $1::uuid as _target_user_id,
              $2::boolean as _delegated_view,
              p.name as prescription_name
            from {SCHEMA}.conditioning_session_log c
            left join {SCHEMA}.my_conditioning_prescription p
              on p.my_conditioning_prescription_id=c.my_conditioning_prescription_id
            where {' and '.join(where)}
            order by c.day desc, c.created_at desc
            limit {int(limit)}
            """,
            *args,
        )
        return JSONResponse([_conditioning_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/conditioning_sessions/{conditioning_session_log_id}")
async def get_conditioning_session(
    conditioning_session_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(conditioning_session_log_id, "conditioning_session_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            select
              conditioning_session_log_id,
              owner_user_id,
              my_conditioning_prescription_id,
              day,
              name,
              category,
              modality,
              duration_min,
              intensity,
              distance,
              heart_rate_avg,
              recovery_impact,
              notes,
              dose_type,
              dose_config,
              is_active,
              created_at,
              updated_at
            from {SCHEMA}.conditioning_session_log
            where conditioning_session_log_id=$1::uuid
              and owner_user_id=$2::uuid
            """,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="conditioning session not found")
        return JSONResponse(_conditioning_row_to_jsonable(row))
    finally:
        await conn.close()


@router.post("/conditioning_sessions/{conditioning_session_log_id}/deactivate")
async def deactivate_conditioning_session(
    conditioning_session_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(conditioning_session_log_id, "conditioning_session_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.conditioning_session_log
               set is_active=false, updated_at=now()
             where conditioning_session_log_id=$1::uuid
               and owner_user_id=$2::uuid
            returning
              conditioning_session_log_id,
              owner_user_id,
              day,
              name,
              is_active,
              updated_at
            """,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="conditioning session not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


# ----------------------------
# Workout Templates
# ----------------------------



@router.post("/workout_template_shares/create")
async def create_workout_template_share(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    workout_template_id: str = Query(..., min_length=1),
    label: str = Body(""),
    notes: str = Body(""),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    wid = _as_uuid(workout_template_id, "workout_template_id")

    token = _new_share_token()
    thash = _token_hash(token)

    conn = await _db()
    try:
        tpl = await conn.fetchrow(
            f"""
            select workout_template_id, owner_user_id, name, notes, is_active
            from {SCHEMA}.workout_template
            where workout_template_id=$1::uuid
              and owner_user_id=$2::uuid
              and is_active=true
            limit 1
            """,
            wid,
            owner,
        )
        if not tpl:
            raise HTTPException(status_code=404, detail="workout template not found")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.workout_template_share (
              token_hash,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes
            )
            values ($1, $2::uuid, $3::uuid, 'active', $4, $5)
            returning
              workout_template_share_id,
              token_hash,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes,
              expires_at,
              revoked_at,
              created_at,
              updated_at
            """,
            thash,
            owner,
            wid,
            _clean_text(label, 200),
            _clean_text(notes, 2000),
        )

        out = _without_token_hash(row)
        out["token"] = token
        out["workout_name"] = str(tpl["name"])
        return JSONResponse(out)
    finally:
        await conn.close()


@router.get("/workout_template_shares")
async def list_workout_template_shares(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    status_filter = "" if include_inactive else "and s.status='active'"

    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              s.workout_template_share_id,
              s.created_by_user_id,
              s.workout_template_id,
              wt.name as workout_name,
              s.status,
              s.label,
              s.notes,
              s.expires_at,
              s.revoked_at,
              s.created_at,
              s.updated_at
            from {SCHEMA}.workout_template_share s
            join {SCHEMA}.workout_template wt
              on wt.workout_template_id=s.workout_template_id
            where s.created_by_user_id=$1::uuid
              {status_filter}
            order by s.created_at desc
            limit 100
            """,
            owner,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.get("/workout_template_shares/preview")
async def preview_workout_template_share(
    token: str = Query(..., min_length=10),
):
    thash = _token_hash(token)

    conn = await _db()
    try:
        share = await conn.fetchrow(
            f"""
            select
              s.workout_template_share_id,
              s.created_by_user_id,
              coalesce(p.display_name, s.created_by_user_id::text) as creator_display_name,
              s.workout_template_id,
              s.status,
              s.label,
              s.notes,
              s.expires_at,
              s.revoked_at,
              s.created_at,
              s.updated_at,
              wt.name as workout_name,
              wt.notes as workout_notes,
              wt.is_active as workout_is_active
            from {SCHEMA}.workout_template_share s
            join {SCHEMA}.workout_template wt
              on wt.workout_template_id=s.workout_template_id
            left join lifeswitch_people.user_profile p
              on p.user_id=s.created_by_user_id
            where s.token_hash=$1
            limit 1
            """,
            thash,
        )
        if not share:
            raise HTTPException(status_code=404, detail="share not found")

        if share["status"] == "active" and share["expires_at"] < _dt.datetime.now(_dt.timezone.utc):
            await conn.execute(
                f"""
                update {SCHEMA}.workout_template_share
                   set status='expired',
                       updated_at=now()
                 where workout_template_share_id=$1::uuid
                """,
                str(share["workout_template_share_id"]),
            )
            d = _row_to_jsonable(share)
            d["status"] = "expired"
            return JSONResponse(d)

        exercises = await conn.fetch(
            f"""
            select
              workout_template_exercise_id,
              workout_template_id,
              exercise_id,
              display_name_snapshot,
              sort_order,
              set_type,
              planned_sets,
              default_weight,
              default_reps,
              flags,
              created_at,
              updated_at
            from {SCHEMA}.workout_template_exercise
            where workout_template_id=$1::uuid
            order by sort_order asc, created_at asc
            """,
            str(share["workout_template_id"]),
        )

        exercise_ids = [str(r["workout_template_exercise_id"]) for r in exercises]
        seg_rows = []
        if exercise_ids:
            seg_rows = await conn.fetch(
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
                where workout_template_exercise_id = any($1::uuid[])
                order by workout_template_exercise_id, segment_index asc
                """,
                exercise_ids,
            )

        seg_by_parent = {}
        for seg in seg_rows:
            key = str(seg["workout_template_exercise_id"])
            seg_by_parent.setdefault(key, []).append(_row_to_jsonable(seg))

        exercise_out = []
        for ex in exercises:
            d = _row_to_jsonable(ex)
            d["segments"] = seg_by_parent.get(str(ex["workout_template_exercise_id"]), [])
            exercise_out.append(d)

        out = _row_to_jsonable(share)
        out["exercises"] = exercise_out
        return JSONResponse(out)
    finally:
        await conn.close()


@router.post("/workout_template_shares/import")
async def import_workout_template_share(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    token: str = Query(..., min_length=10),
):
    importer = require_actor_matches_owner(req, owner_user_id)
    thash = _token_hash(token)

    conn = await _db()
    try:
        async with conn.transaction():
            share = await conn.fetchrow(
                f"""
                select
                  s.workout_template_share_id,
                  s.created_by_user_id,
                  s.workout_template_id,
                  s.status,
                  s.expires_at,
                  wt.name as workout_name,
                  wt.notes as workout_notes,
                  wt.is_active as workout_is_active
                from {SCHEMA}.workout_template_share s
                join {SCHEMA}.workout_template wt
                  on wt.workout_template_id=s.workout_template_id
                where s.token_hash=$1
                for update
                """,
                thash,
            )
            if not share:
                raise HTTPException(status_code=404, detail="share not found")
            if str(share["status"]) != "active":
                raise HTTPException(status_code=400, detail=f"share is {share['status']}")
            if share["expires_at"] and share["expires_at"] < _dt.datetime.now(_dt.timezone.utc):
                await conn.execute(
                    f"""
                    update {SCHEMA}.workout_template_share
                       set status='expired',
                           updated_at=now()
                     where workout_template_share_id=$1::uuid
                    """,
                    str(share["workout_template_share_id"]),
                )
                raise HTTPException(status_code=400, detail="share expired")
            if not share["workout_is_active"]:
                raise HTTPException(status_code=400, detail="shared workout is inactive")

            new_name = await _unique_imported_workout_name(conn, importer, str(share["workout_name"] or "Imported Workout"))

            new_tpl = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.workout_template (
                  owner_user_id,
                  name,
                  notes,
                  is_active
                )
                values ($1::uuid, $2, $3, true)
                returning workout_template_id, owner_user_id, name, notes, is_active, created_at, updated_at
                """,
                importer,
                new_name,
                share["workout_notes"] or "",
            )

            src_exercises = await conn.fetch(
                f"""
                select
                  workout_template_exercise_id,
                  exercise_id,
                  display_name_snapshot,
                  sort_order,
                  set_type,
                  planned_sets,
                  default_weight,
                  default_reps,
                  flags
                from {SCHEMA}.workout_template_exercise
                where workout_template_id=$1::uuid
                order by sort_order asc, created_at asc
                """,
                str(share["workout_template_id"]),
            )

            copied = []
            for ex in src_exercises:
                exercise_id = str(ex["exercise_id"])
                display_name = _clean_text(ex["display_name_snapshot"] or exercise_id, 240) or exercise_id

                await conn.execute(
                    f"""
                    insert into {SCHEMA}.my_exercise (
                      owner_user_id,
                      exercise_id,
                      display_name,
                      kind,
                      modality,
                      matched_source,
                      is_active
                    )
                    values ($1::uuid, $2, $3, 'strength', 'imported', 'workout_share_import', true)
                    on conflict (owner_user_id, exercise_id)
                    do update set
                      display_name=case
                        when {SCHEMA}.my_exercise.display_name='' then excluded.display_name
                        else {SCHEMA}.my_exercise.display_name
                      end,
                      is_active=true,
                      updated_at=now()
                    """,
                    importer,
                    exercise_id,
                    display_name,
                )

                new_ex = await conn.fetchrow(
                    f"""
                    insert into {SCHEMA}.workout_template_exercise (
                      workout_template_id,
                      exercise_id,
                      display_name_snapshot,
                      sort_order,
                      set_type,
                      planned_sets,
                      default_weight,
                      default_reps,
                      flags
                    )
                    values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
                    returning
                      workout_template_exercise_id,
                      workout_template_id,
                      exercise_id,
                      display_name_snapshot,
                      sort_order,
                      set_type,
                      planned_sets,
                      default_weight,
                      default_reps,
                      flags,
                      created_at,
                      updated_at
                    """,
                    str(new_tpl["workout_template_id"]),
                    exercise_id,
                    ex["display_name_snapshot"],
                    int(ex["sort_order"] or 10),
                    str(ex["set_type"] or "straight"),
                    int(ex["planned_sets"] or 3),
                    ex["default_weight"] or 0,
                    int(ex["default_reps"] or 10),
                    ex["flags"] or "",
                )

                src_segments = await conn.fetch(
                    f"""
                    select segment_index, label, default_weight, default_reps
                    from {SCHEMA}.workout_template_exercise_segment
                    where workout_template_exercise_id=$1::uuid
                    order by segment_index asc
                    """,
                    str(ex["workout_template_exercise_id"]),
                )

                for seg in src_segments:
                    await conn.execute(
                        f"""
                        insert into {SCHEMA}.workout_template_exercise_segment (
                          workout_template_exercise_id,
                          segment_index,
                          label,
                          default_weight,
                          default_reps
                        )
                        values ($1::uuid, $2, $3, $4, $5)
                        on conflict (workout_template_exercise_id, segment_index)
                        do update set
                          label=excluded.label,
                          default_weight=excluded.default_weight,
                          default_reps=excluded.default_reps,
                          updated_at=now()
                        """,
                        str(new_ex["workout_template_exercise_id"]),
                        int(seg["segment_index"] or 1),
                        seg["label"] or "",
                        seg["default_weight"] or 0,
                        int(seg["default_reps"] or 0),
                    )

                copied.append(_row_to_jsonable(new_ex))

        return JSONResponse({
            "imported_workout": _row_to_jsonable(new_tpl),
            "copied_exercises": copied,
        })
    finally:
        await conn.close()


@router.post("/workout_template_shares/{workout_template_share_id}/revoke")
async def revoke_workout_template_share(
    workout_template_share_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(workout_template_share_id, "workout_template_share_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.workout_template_share
               set status='revoked',
                   revoked_at=now(),
                   updated_at=now()
             where workout_template_share_id=$1::uuid
               and created_by_user_id=$2::uuid
               and status='active'
            returning
              workout_template_share_id,
              created_by_user_id,
              workout_template_id,
              status,
              label,
              notes,
              expires_at,
              revoked_at,
              created_at,
              updated_at
            """,
            sid,
            owner,
        )
        if not row:
            raise HTTPException(status_code=404, detail="active share not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()



@router.get("/workout_templates")
async def list_workout_templates(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    workout_template_id: str | None = Query(None),
    name: str = Query(..., min_length=1, max_length=120),
    notes: str | None = Query(None, max_length=400),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None

    conn = await _db()
    try:
        if wid:
            existing_owner = await conn.fetchval(
                f"select owner_user_id from {SCHEMA}.workout_template where workout_template_id=$1::uuid",
                wid,
            )
            if existing_owner and str(existing_owner) != owner:
                raise HTTPException(status_code=403, detail="actor_owner_mismatch")

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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    owner = require_actor_matches_owner(req, owner_user_id)
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
async def list_workout_template_exercises(workout_template_id: str, req: Request):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.workout_template where workout_template_id=$1::uuid",
            wid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template not found")
        require_actor_matches_owner(req, str(owner))

        rows = await conn.fetch(
            f"""
            select
              workout_template_exercise_id, workout_template_id,
              exercise_id, display_name_snapshot, sort_order, set_type,
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
    wid = _as_uuid(workout_template_id, "workout_template_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.workout_template where workout_template_id=$1::uuid and is_active",
            wid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template not found or inactive")
        require_actor_matches_owner(req, str(owner))

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.workout_template_exercise
              (workout_template_id, exercise_id, display_name_snapshot, sort_order, set_type, planned_sets, default_weight, default_reps, flags)
            values
              ($1::uuid, $2, nullif($3, ''), $4, $5, $6, $7, $8, $9)
            on conflict (workout_template_id, exercise_id) do update
              set display_name_snapshot=coalesce(excluded.display_name_snapshot, {SCHEMA}.workout_template_exercise.display_name_snapshot),
                  sort_order=excluded.sort_order,
                  set_type=excluded.set_type,
                  planned_sets=excluded.planned_sets,
                  default_weight=excluded.default_weight,
                  default_reps=excluded.default_reps,
                  flags=excluded.flags,
                  updated_at=now()
            returning
              workout_template_exercise_id, workout_template_id,
              exercise_id, display_name_snapshot, sort_order, set_type, planned_sets, default_weight, default_reps, flags,
              created_at, updated_at
            """,
            wid,
            exercise_id.strip(),
              (display_name_snapshot or "").strip(),
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
    req: Request,
):
    wid = _as_uuid(workout_template_id, "workout_template_id")
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"select owner_user_id from {SCHEMA}.workout_template where workout_template_id=$1::uuid",
            wid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template not found")
        require_actor_matches_owner(req, str(owner))

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
async def list_workout_template_exercise_segments(workout_template_exercise_id: str, req: Request):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"""
            select wt.owner_user_id
            from {SCHEMA}.workout_template_exercise e
            join {SCHEMA}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
            """,
            weid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template_exercise not found")
        require_actor_matches_owner(req, str(owner))

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
    req: Request,
    segment_index: int = Query(1, ge=1, le=50),
    label: str | None = Query(None, max_length=120),
    default_weight: float = Query(0),
    default_reps: int = Query(0, ge=0, le=1000),
):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"""
            select wt.owner_user_id
            from {SCHEMA}.workout_template_exercise e
            join {SCHEMA}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
              and wt.is_active=true
            """,
            weid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template_exercise not found or inactive")
        require_actor_matches_owner(req, str(owner))

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
    req: Request,
):
    weid = _as_uuid(workout_template_exercise_id, "workout_template_exercise_id")
    segid = _as_uuid(workout_template_exercise_segment_id, "workout_template_exercise_segment_id")
    conn = await _db()
    try:
        owner = await conn.fetchval(
            f"""
            select wt.owner_user_id
            from {SCHEMA}.workout_template_exercise e
            join {SCHEMA}.workout_template wt
              on wt.workout_template_id=e.workout_template_id
            where e.workout_template_exercise_id=$1::uuid
            """,
            weid,
        )
        if not owner:
            raise HTTPException(status_code=404, detail="workout_template_exercise not found")
        require_actor_matches_owner(req, str(owner))

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

@router.post("/sessions/complete")
async def complete_training_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    payload: dict = Body(...),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    day = _clean_text(payload.get("day"), 10)
    name = _clean_text(payload.get("name"), 160)
    notes = _clean_text(payload.get("notes"), 800)
    workout_template_id = _clean_text(payload.get("workout_template_id"), 80)
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None

    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        day_val = _dt.date.fromisoformat(day)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid day")

    def parse_timestamp(value, field_name: str):
        raw = _clean_text(value, 80)
        if not raw:
            return None
        try:
            parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail=f"invalid {field_name}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed

    finished = parse_timestamp(payload.get("finished_at"), "finished_at") or _dt.datetime.now(_dt.timezone.utc)
    started = parse_timestamp(payload.get("started_at"), "started_at") or finished
    if finished < started:
        raise HTTPException(status_code=400, detail="finished_at must be at or after started_at")

    raw_sets = payload.get("sets")
    if not isinstance(raw_sets, list) or not raw_sets:
        raise HTTPException(status_code=400, detail="at least one completed set is required")
    if len(raw_sets) > 500:
        raise HTTPException(status_code=400, detail="too many completed sets")

    normalized_sets = []
    for position, raw_set in enumerate(raw_sets, start=1):
        if not isinstance(raw_set, dict):
            raise HTTPException(status_code=400, detail=f"set {position} must be an object")

        exercise_id = _clean_text(raw_set.get("exercise_id"), 200)
        exercise_name = _clean_text(raw_set.get("exercise_name"), 240)
        if not exercise_id or not exercise_name:
            raise HTTPException(status_code=400, detail=f"set {position} requires exercise_id and exercise_name")

        try:
            exercise_sort_order = int(raw_set.get("exercise_sort_order", 0))
            set_index = int(raw_set.get("set_index", 1))
        except Exception:
            raise HTTPException(status_code=400, detail=f"set {position} has invalid ordering")
        if set_index < 1 or set_index > 200:
            raise HTTPException(status_code=400, detail=f"set {position} has invalid set_index")

        set_type = _clean_text(raw_set.get("set_type") or "straight", 40).lower()
        if set_type not in ("straight", "drop"):
            raise HTTPException(status_code=400, detail=f"set {position} has invalid set_type")

        flags = _clean_text(raw_set.get("flags"), 240)
        set_notes = _clean_text(raw_set.get("notes"), 800)
        segments = []

        if set_type == "drop":
            raw_segments = raw_set.get("segments")
            if not isinstance(raw_segments, list) or not raw_segments:
                raise HTTPException(status_code=400, detail=f"drop set {position} requires at least one segment")
            if len(raw_segments) > 50:
                raise HTTPException(status_code=400, detail=f"drop set {position} has too many segments")

            seen_segment_indexes = set()
            for segment_position, raw_segment in enumerate(raw_segments, start=1):
                if not isinstance(raw_segment, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"drop set {position} segment {segment_position} must be an object",
                    )
                try:
                    segment_index = int(raw_segment.get("segment_index", segment_position))
                    weight = float(raw_segment.get("weight", 0))
                    reps = int(raw_segment.get("reps", 0))
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=f"drop set {position} segment {segment_position} has invalid numbers",
                    )
                if segment_index < 1 or segment_index > 50 or segment_index in seen_segment_indexes:
                    raise HTTPException(
                        status_code=400,
                        detail=f"drop set {position} has invalid or duplicate segment_index",
                    )
                if not math.isfinite(weight) or weight < 0 or reps < 1 or reps > 1000:
                    raise HTTPException(
                        status_code=400,
                        detail=f"drop set {position} segment {segment_position} requires nonnegative weight and positive reps",
                    )
                seen_segment_indexes.add(segment_index)
                segments.append(
                    {
                        "segment_index": segment_index,
                        "label": _clean_text(raw_segment.get("label"), 120),
                        "weight": weight,
                        "reps": reps,
                        "volume": weight * reps,
                        "notes": _clean_text(raw_segment.get("notes"), 800),
                    }
                )

            segments.sort(key=lambda segment: segment["segment_index"])
            weight = segments[0]["weight"]
            reps = segments[0]["reps"]
            volume = sum(segment["volume"] for segment in segments)
        else:
            try:
                weight = float(raw_set.get("weight", 0))
                reps = int(raw_set.get("reps", 0))
            except Exception:
                raise HTTPException(status_code=400, detail=f"set {position} has invalid weight or reps")
            if not math.isfinite(weight) or weight < 0 or reps < 1 or reps > 1000:
                raise HTTPException(
                    status_code=400,
                    detail=f"set {position} requires nonnegative weight and positive reps",
                )
            volume = weight * reps

        normalized_sets.append(
            {
                "exercise_id": exercise_id,
                "exercise_name": exercise_name,
                "exercise_sort_order": exercise_sort_order,
                "set_index": set_index,
                "set_type": set_type,
                "weight": weight,
                "reps": reps,
                "volume": volume,
                "flags": flags,
                "notes": set_notes,
                "segments": segments,
            }
        )

    conn = await _db()
    try:
        async with conn.transaction():
            if wid:
                template_ok = await conn.fetchval(
                    f"""
                    select 1
                    from {SCHEMA}.workout_template
                    where workout_template_id=$1::uuid
                      and owner_user_id=$2::uuid
                      and is_active=true
                    """,
                    wid,
                    owner,
                )
                if not template_ok:
                    raise HTTPException(status_code=404, detail="workout template not found or inactive")

            session = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.training_session
                  (owner_user_id, day, workout_template_id, name, notes,
                   started_at, finished_at, is_active)
                values
                  ($1::uuid, $2::date, $3::uuid, $4, $5, $6::timestamptz,
                   $7::timestamptz, true)
                returning
                  training_session_id, owner_user_id, day, workout_template_id,
                  name, notes, started_at, finished_at, is_active, created_at, updated_at
                """,
                owner,
                day_val,
                wid,
                name,
                notes,
                started,
                finished,
            )
            session_id = session["training_session_id"]

            for completed_set in normalized_sets:
                set_row = await conn.fetchrow(
                    f"""
                    insert into {SCHEMA}.training_set_log
                      (training_session_id, owner_user_id, workout_template_id,
                       exercise_id, exercise_name, exercise_sort_order, set_index,
                       set_type, weight, reps, volume, flags, notes,
                       exercise_role_snapshot, is_active)
                    values
                      ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7,
                       $8, $9, $10, $11, $12, $13,
                       coalesce((
                         select exercise_role
                         from {SCHEMA}.my_exercise
                         where owner_user_id=$2::uuid and exercise_id=$4
                       ), 'strength'), true)
                    returning training_set_log_id
                    """,
                    session_id,
                    owner,
                    wid,
                    completed_set["exercise_id"],
                    completed_set["exercise_name"],
                    completed_set["exercise_sort_order"],
                    completed_set["set_index"],
                    completed_set["set_type"],
                    completed_set["weight"],
                    completed_set["reps"],
                    completed_set["volume"],
                    completed_set["flags"],
                    completed_set["notes"],
                )

                for segment in completed_set["segments"]:
                    await conn.execute(
                        f"""
                        insert into {SCHEMA}.training_set_log_segment
                          (training_set_log_id, segment_index, label, weight,
                           reps, volume, notes)
                        values ($1::uuid, $2, $3, $4, $5, $6, $7)
                        """,
                        set_row["training_set_log_id"],
                        segment["segment_index"],
                        segment["label"],
                        segment["weight"],
                        segment["reps"],
                        segment["volume"],
                        segment["notes"],
                    )

            result = _row_to_jsonable(session)
            result["set_count"] = len(normalized_sets)
            return JSONResponse(result)
    finally:
        await conn.close()


@router.post("/sessions/create")
async def create_training_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="session creation moved to atomic /sessions/complete",
    )


@router.get("/sessions")
async def list_training_sessions(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str | None = Query(None),
    include_inactive: int = Query(0, ge=0, le=1),
    limit: int = Query(100, ge=1, le=500),
    target_user_id: str = Query("", max_length=80),
):
    viewer = require_actor_matches_owner(req, owner_user_id)

    day_val = None
    if day:
        try:
            day_val = _dt.date.fromisoformat(day)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid day")

    conn = await _db()
    try:
        owner, delegated = await _resolve_training_view_target(conn, viewer, target_user_id)

        where = ["s.owner_user_id=$1::uuid"]
        args = [owner, delegated]
        if day_val:
            args.append(day_val)
            where.append(f"s.day=${len(args)}::date")
        if not include_inactive:
            where.append("s.is_active=true")
            where.append("s.finished_at is not null")

        having = (
            "having count(l.training_set_log_id) filter (where l.is_active=true) > 0"
            if not include_inactive
            else ""
        )

        rows = await conn.fetch(
            f"""
            select
              s.training_session_id, s.owner_user_id, s.day, s.workout_template_id,
              s.name, s.notes, s.started_at, s.finished_at, s.is_active,
              s.created_at, s.updated_at,
              $1::uuid as _target_user_id,
              $2::boolean as _delegated_view,
              coalesce(count(l.training_set_log_id) filter (where l.is_active=true), 0)::int as set_count,
              coalesce(count(distinct l.exercise_id) filter (where l.is_active=true), 0)::int as exercise_count,
              coalesce(sum(l.volume) filter (where l.is_active=true), 0)::float as volume,
              coalesce(count(l.training_set_log_id) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
              ), 0)::int as strength_set_count,
              coalesce(count(distinct l.exercise_id) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
              ), 0)::int as strength_exercise_count,
              coalesce(sum(l.volume) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
              ), 0)::float as strength_volume,
              coalesce(count(l.training_set_log_id) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='rehab'
              ), 0)::int as rehab_set_count,
              coalesce(count(distinct l.exercise_id) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='rehab'
              ), 0)::int as rehab_exercise_count,
              coalesce(sum(l.volume) filter (
                where l.is_active=true
                  and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='rehab'
              ), 0)::float as rehab_volume,
              case
                when count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
                ) > 0
                and count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='rehab'
                ) > 0 then 'mixed'
                when count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
                ) > 0 then 'strength'
                when count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='rehab'
                ) > 0 then 'rehab'
                else 'unclassified'
              end as session_role,
              coalesce(bool_or(
                coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength')='strength'
              ) filter (where l.is_active=true), false) as counts_toward_strength
            from {SCHEMA}.training_session s
            left join {SCHEMA}.training_set_log l
              on l.training_session_id=s.training_session_id
            left join {SCHEMA}.my_exercise me
              on me.owner_user_id=l.owner_user_id
             and me.exercise_id=l.exercise_id
            where {' and '.join(where)}
            group by s.training_session_id
            {having}
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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    viewer = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        owner, delegated = await _resolve_training_view_target(conn, viewer, target_user_id)

        row = await conn.fetchrow(
            f"""
            select
              training_session_id, owner_user_id, day, workout_template_id,
              name, notes, started_at, finished_at, is_active, created_at, updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {SCHEMA}.training_session
            where training_session_id=$1::uuid
              and owner_user_id=$2::uuid
            """,
            sid,
            owner,
            owner,
            delegated,
        )
        if not row:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()



@router.post("/sessions/{training_session_id}/deactivate")
async def deactivate_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = require_actor_matches_owner(req, owner_user_id)

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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    viewer = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        owner, delegated = await _resolve_training_view_target(conn, viewer, target_user_id)

        where_active = "" if include_inactive else "and l.is_active=true"
        rows = await conn.fetch(
            f"""
            select
              l.training_set_log_id, l.training_session_id, l.owner_user_id,
              l.workout_template_id, l.exercise_id, l.exercise_name,
              l.set_type, l.exercise_role_snapshot,
              coalesce(l.exercise_role_snapshot, me.exercise_role, 'strength') as exercise_role,
              l.exercise_sort_order, l.set_index, l.weight, l.reps, l.volume,
              l.flags, l.notes, l.is_active, l.created_at, l.updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {SCHEMA}.training_set_log l
            left join {SCHEMA}.my_exercise me
              on me.owner_user_id=l.owner_user_id
             and me.exercise_id=l.exercise_id
            where l.training_session_id=$1::uuid
              and l.owner_user_id=$2::uuid
              {where_active}
            order by l.exercise_sort_order asc, l.set_index asc, l.created_at asc
            """,
            sid,
            owner,
            owner,
            delegated,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/add")
async def add_training_set_log(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    exercise_id: str = Query(..., min_length=1, max_length=200),
    exercise_name: str = Query(..., min_length=1, max_length=240),
    workout_template_id: str | None = Query(None),
    exercise_sort_order: int = Query(0),
    set_index: int = Query(1, ge=1, le=200),
    set_type: str = Query("straight", max_length=40),
    weight: float = Query(0),
    reps: int = Query(0, ge=0, le=1000),
    flags: str | None = Query(None, max_length=240),
    notes: str | None = Query(None, max_length=800),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = require_actor_matches_owner(req, owner_user_id)
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
               exercise_id, exercise_name, exercise_sort_order, set_index, set_type,
               weight, reps, volume, flags, notes, exercise_role_snapshot, is_active)
            values
              ($1::uuid, $2::uuid, $3::uuid,
               $4, $5, $6, $7, $8,
               $9, $10, $11, $12, $13,
               coalesce((
                 select exercise_role
                 from {SCHEMA}.my_exercise
                 where owner_user_id=$2::uuid and exercise_id=$4
               ), 'strength'), true)
            returning
              training_set_log_id, training_session_id, owner_user_id,
              workout_template_id, exercise_id, exercise_name,
              set_type, exercise_role_snapshot,
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
              (set_type or "straight").strip().lower(),
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
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    exercise_sort_order: int | None = Query(None),
    set_index: int | None = Query(None, ge=1, le=200),
    set_type: str | None = Query(None, max_length=40),
    weight: float | None = Query(None),
    reps: int | None = Query(None, ge=0, le=1000),
    flags: str | None = Query(None, max_length=240),
    notes: str | None = Query(None, max_length=800),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        old = await conn.fetchrow(
            f"""
            select weight, reps, set_type
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
        next_set_type = set_type.strip().lower() if set_type is not None else str(old.get("set_type") or "straight")

        segment_total = await conn.fetchval(
            f"""
            select coalesce(sum(volume), 0)
            from {SCHEMA}.training_set_log_segment
            where training_set_log_id=$1::uuid
            """,
            setid,
        )
        segment_total_float = float(segment_total or 0)
        next_volume = segment_total_float if next_set_type == "drop" and segment_total_float > 0 else next_weight * next_reps

        row = await conn.fetchrow(
            f"""
            update {SCHEMA}.training_set_log
               set exercise_sort_order=coalesce($4, exercise_sort_order),
                   set_index=coalesce($5, set_index),
                   set_type=coalesce($6, set_type),
                   weight=$7,
                   reps=$8,
                   volume=$9,
                   flags=coalesce($10, flags),
                   notes=coalesce($11, notes),
                   updated_at=now()
             where training_set_log_id=$1::uuid
               and training_session_id=$2::uuid
               and owner_user_id=$3::uuid
            returning
              training_set_log_id, training_session_id, owner_user_id,
              workout_template_id, exercise_id, exercise_name,
              set_type, exercise_role_snapshot,
              exercise_sort_order, set_index, weight, reps, volume,
              flags, notes, is_active, created_at, updated_at
            """,
            setid,
            sid,
            owner,
            exercise_sort_order,
            set_index,
            next_set_type,
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


@router.get("/sessions/{training_session_id}/sets/{training_set_log_id}/segments")
async def list_training_set_log_segments(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    viewer = require_actor_matches_owner(req, owner_user_id)
    conn = await _db()
    try:
        owner, delegated = await _resolve_training_view_target(conn, viewer, target_user_id)

        parent = await conn.fetchrow(
            f"""
            select training_set_log_id
            from {SCHEMA}.training_set_log
            where training_set_log_id=$1::uuid
              and training_session_id=$2::uuid
              and owner_user_id=$3::uuid
              and is_active=true
            """,
            setid,
            sid,
            owner,
        )
        if not parent:
            raise HTTPException(status_code=404, detail="set not found")

        rows = await conn.fetch(
            f"""
            select
              training_set_log_segment_id,
              training_set_log_id,
              segment_index,
              label,
              weight,
              reps,
              volume,
              notes,
              created_at,
              updated_at,
              $2::uuid as _target_user_id,
              $3::boolean as _delegated_view
            from {SCHEMA}.training_set_log_segment
            where training_set_log_id=$1::uuid
            order by segment_index asc, created_at asc
            """,
            setid,
            owner,
            delegated,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/segments/add")
async def add_training_set_log_segment(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    segment_index: int = Query(1, ge=1, le=50),
    label: str | None = Query(None, max_length=120),
    weight: float = Query(0),
    reps: int = Query(0, ge=0, le=1000),
    notes: str | None = Query(None, max_length=800),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    volume = float(weight) * int(reps)

    conn = await _db()
    try:
        parent = await conn.fetchrow(
            f"""
            select training_set_log_id
            from {SCHEMA}.training_set_log
            where training_set_log_id=$1::uuid
              and training_session_id=$2::uuid
              and owner_user_id=$3::uuid
              and is_active=true
            """,
            setid,
            sid,
            owner,
        )
        if not parent:
            raise HTTPException(status_code=404, detail="set not found")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.training_set_log_segment
              (training_set_log_id, segment_index, label, weight, reps, volume, notes)
            values
              ($1::uuid, $2, $3, $4, $5, $6, $7)
            on conflict (training_set_log_id, segment_index) do update
              set label=excluded.label,
                  weight=excluded.weight,
                  reps=excluded.reps,
                  volume=excluded.volume,
                  notes=excluded.notes,
                  updated_at=now()
            returning
              training_set_log_segment_id,
              training_set_log_id,
              segment_index,
              label,
              weight,
              reps,
              volume,
              notes,
              created_at,
              updated_at
            """,
            setid,
            int(segment_index),
            (label or "").strip(),
            float(weight),
            int(reps),
            float(volume),
            (notes or "").strip(),
        )

        total = await conn.fetchval(
            f"""
            select coalesce(sum(volume), 0)
            from {SCHEMA}.training_set_log_segment
            where training_set_log_id=$1::uuid
            """,
            setid,
        )
        await conn.execute(
            f"""
            update {SCHEMA}.training_set_log
               set volume=$4,
                   updated_at=now()
             where training_set_log_id=$1::uuid
               and training_session_id=$2::uuid
               and owner_user_id=$3::uuid
            """,
            setid,
            sid,
            owner,
            float(total or 0),
        )
        await conn.execute(
            f"update {SCHEMA}.training_session set updated_at=now() where training_session_id=$1::uuid",
            sid,
        )

        return JSONResponse(_row_to_jsonable(row) if row else {"error": "insert_failed"})
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/segments/{training_set_log_segment_id}/delete")
async def delete_training_set_log_segment(
    training_session_id: str,
    training_set_log_id: str,
    training_set_log_segment_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    segid = _as_uuid(training_set_log_segment_id, "training_set_log_segment_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db()
    try:
        res = await conn.execute(
            f"""
            delete from {SCHEMA}.training_set_log_segment
             where training_set_log_segment_id=$1::uuid
               and training_set_log_id=$2::uuid
               and exists (
                 select 1
                 from {SCHEMA}.training_set_log l
                 where l.training_set_log_id=$2::uuid
                   and l.training_session_id=$3::uuid
                   and l.owner_user_id=$4::uuid
               )
            """,
            segid,
            setid,
            sid,
            owner,
        )

        total = await conn.fetchval(
            f"""
            select coalesce(sum(volume), 0)
            from {SCHEMA}.training_set_log_segment
            where training_set_log_id=$1::uuid
            """,
            setid,
        )
        await conn.execute(
            f"""
            update {SCHEMA}.training_set_log
               set volume=$4,
                   updated_at=now()
             where training_set_log_id=$1::uuid
               and training_session_id=$2::uuid
               and owner_user_id=$3::uuid
            """,
            setid,
            sid,
            owner,
            float(total or 0),
        )
        await conn.execute(
            f"update {SCHEMA}.training_session set updated_at=now() where training_session_id=$1::uuid",
            sid,
        )

        return JSONResponse({"ok": True, "result": str(res)})
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/delete")
async def delete_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

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
