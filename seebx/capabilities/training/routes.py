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
from fastapi import APIRouter, HTTPException, Query, Body, Header, Request
from seebx.core.ownership import require_actor_matches_owner
from seebx.adapters.lifeswitch_postgres import connect_lifeswitch
from seebx.adapters.lifeswitch_training_exercises_postgres import (
    lifeswitch_training_exercises_repository,
)
from seebx.adapters.lifeswitch_training_access_postgres import (
    lifeswitch_training_access_repository,
)
from seebx.adapters.lifeswitch_training_conditioning_postgres import (
    lifeswitch_training_conditioning_repository,
)
from seebx.adapters.lifeswitch_training_sharing_postgres import (
    WorkoutShareImportError,
    lifeswitch_training_sharing_repository,
)
from seebx.adapters.lifeswitch_training_templates_postgres import (
    TemplateUpsertError,
    lifeswitch_training_templates_repository,
)
from seebx.adapters.lifeswitch_training_writes_postgres import (
    TrainingWriterError,
    correct_training_session as write_training_correction,
    create_training_session as write_training_session,
    set_transaction_actor,
    void_training_session as write_training_void,
)
from seebx.capabilities.training.write_errors import training_writer_http_error
from fastapi.responses import JSONResponse

router = APIRouter()

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


def _require_idempotency_key(value: str) -> str:
    key = _clean_text(value, 128)
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")
    return key



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


async def _db(req: Request):
    return await connect_lifeswitch(req)


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
    async with lifeswitch_training_exercises_repository(req) as repository:
        rows = await repository.list_exercises(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])


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


@router.post("/my_exercises/{my_exercise_id}/deactivate")
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

# ----------------------------
# Conditioning Library / Prescriptions
# ----------------------------

@router.get("/conditioning_library")
async def list_conditioning_library(
    req: Request,
    include_inactive: int = Query(0, ge=0, le=1),
):
    async with lifeswitch_training_conditioning_repository(req) as repository:
        rows = await repository.list_library(include_inactive=bool(include_inactive))
    return JSONResponse([_row_to_jsonable(row) for row in rows])


@router.get("/my_conditioning_prescriptions")
async def list_my_conditioning_prescriptions(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_conditioning_repository(req) as repository:
        rows = await repository.list_prescriptions(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_conditioning_row_to_jsonable(row) for row in rows])


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

    async with lifeswitch_training_conditioning_repository(req) as repository:
        if pid:
            existing_owner = await repository.get_prescription_owner(
                prescription_id=pid
            )
            if existing_owner and str(existing_owner) != owner:
                raise HTTPException(status_code=403, detail="actor_owner_mismatch")
        row = await repository.upsert_prescription(
            prescription_id=pid,
            owner_user_id=owner,
            conditioning_library_id=libid,
            name=name.strip(),
            category=category.strip(),
            modality=modality.strip(),
            purpose=purpose.strip(),
            target_duration_min=int(target_duration_min),
            target_frequency_per_week=float(target_frequency_per_week),
            target_intensity=target_intensity.strip(),
            preferred_timing=preferred_timing.strip(),
            recovery_constraints=recovery_constraints.strip(),
            notes=notes.strip(),
            dose_type=clean_dose_type,
            dose_config_json=dose_config_json,
        )
    return JSONResponse(
        _conditioning_row_to_jsonable(row) if row else {"error": "upsert_failed"}
    )


@router.post("/my_conditioning_prescriptions/{my_conditioning_prescription_id}/deactivate")
async def deactivate_my_conditioning_prescription(
    my_conditioning_prescription_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    pid = _as_uuid(my_conditioning_prescription_id, "my_conditioning_prescription_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    async with lifeswitch_training_conditioning_repository(req) as repository:
        row = await repository.deactivate_prescription(
            prescription_id=pid,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_row_to_jsonable(row))


# ----------------------------
# Conditioning Session Log
# ----------------------------

@router.post("/conditioning_sessions/create")
async def create_conditioning_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    day: str = Query(..., min_length=10, max_length=10),
    my_conditioning_prescription_id: str | None = Query(None),

    name: str = Query(..., min_length=1, max_length=180),
    category: str = Query("", max_length=120),
    modality: str = Query("", max_length=120),

    duration_min: float = Query(0, ge=0, le=1440),
    intensity: str = Query("", max_length=400),
    distance_value: float | None = Query(None, ge=0),
    distance_unit: str | None = Query(None, max_length=8),
    heart_rate_avg: float | None = Query(None, ge=0, le=260),
    recovery_impact: str = Query("", max_length=400),
    notes: str = Query("", max_length=1600),

    dose_type: str = Query("open", max_length=40),
    dose_config: str = Query("{}", max_length=12000),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    write_key = _require_idempotency_key(idempotency_key)

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

    clean_distance_unit = _clean_text(distance_unit, 8).lower() or None
    if (distance_value is None) != (clean_distance_unit is None):
        raise HTTPException(
            status_code=400,
            detail="distance_value and distance_unit must be supplied together",
        )

    intent = {
        "day": day_val.isoformat(),
        "my_conditioning_prescription_id": pid,
        "name": name.strip(),
        "category": category.strip(),
        "modality": modality.strip(),
        "duration_min": float(duration_min),
        "intensity": intensity.strip(),
        "distance_value": float(distance_value) if distance_value is not None else None,
        "distance_unit": clean_distance_unit,
        "heart_rate_avg": float(heart_rate_avg) if heart_rate_avg is not None else None,
        "recovery_impact": recovery_impact.strip(),
        "notes": notes.strip(),
        "dose_type": clean_dose_type,
        "dose_config": parsed_dose_config,
    }

    try:
        async with lifeswitch_training_conditioning_repository(req) as repository:
            row = await repository.create_session(
                owner_user_id=owner,
                intent=intent,
                idempotency_key=write_key,
            )
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
    return JSONResponse(_conditioning_row_to_jsonable(row))


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

    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_conditioning_repository(req) as repository:
        rows = await repository.list_sessions(
            owner_user_id=owner,
            delegated=delegated,
            day=day_val,
            include_inactive=bool(include_inactive),
            limit=limit,
        )
    return JSONResponse([_conditioning_row_to_jsonable(row) for row in rows])


@router.get("/conditioning_sessions/{conditioning_session_log_id}")
async def get_conditioning_session(
    conditioning_session_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    sid = _as_uuid(conditioning_session_log_id, "conditioning_session_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    async with lifeswitch_training_conditioning_repository(req) as repository:
        row = await repository.get_session(
            conditioning_session_log_id=sid,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="conditioning session not found")
    return JSONResponse(_conditioning_row_to_jsonable(row))


@router.post("/conditioning_sessions/{conditioning_session_log_id}/deactivate")
async def deactivate_conditioning_session(
    conditioning_session_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    reason: str = Body("user_deleted", embed=True),
):
    sid = _as_uuid(conditioning_session_log_id, "conditioning_session_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    try:
        async with lifeswitch_training_conditioning_repository(req) as repository:
            voided_id = await repository.void_session(
                conditioning_session_log_id=sid,
                owner_user_id=owner,
                reason=_clean_text(reason, 1000) or "user_deleted",
            )
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
    return JSONResponse(
        {
            "conditioning_session_log_id": str(voided_id),
            "owner_user_id": owner,
            "is_active": False,
            "voided": True,
        }
    )


@router.post("/conditioning_sessions/{conditioning_session_log_id}/correct")
async def correct_conditioning_session(
    conditioning_session_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    payload: dict = Body(...),
):
    sid = _as_uuid(conditioning_session_log_id, "conditioning_session_log_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    write_key = _require_idempotency_key(idempotency_key)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    try:
        async with lifeswitch_training_conditioning_repository(req) as repository:
            row = await repository.correct_session(
                conditioning_session_log_id=sid,
                owner_user_id=owner,
                intent=payload,
                idempotency_key=write_key,
            )
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
    if not row:
        raise HTTPException(status_code=500, detail="conditioning correction unavailable")
    return JSONResponse(_conditioning_row_to_jsonable(row))


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


@router.get("/workout_template_shares")
async def list_workout_template_shares(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_sharing_repository(req) as repository:
        rows = await repository.list_shares(
            owner_user_id=owner,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])


@router.get("/workout_template_shares/preview")
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


@router.post("/workout_template_shares/import")
async def import_workout_template_share(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    token: str = Query(..., min_length=10),
):
    importer = require_actor_matches_owner(req, owner_user_id)
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


@router.post("/workout_template_shares/{workout_template_share_id}/revoke")
async def revoke_workout_template_share(
    workout_template_share_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    share_id = _as_uuid(
        workout_template_share_id, "workout_template_share_id"
    )
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_training_sharing_repository(req) as repository:
        row = await repository.revoke_share(
            workout_template_share_id=share_id,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="active share not found")
    return JSONResponse(_row_to_jsonable(row))


@router.get("/workout_templates")
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


@router.post("/workout_templates/upsert")
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


@router.post("/workout_templates/{workout_template_id}/classify_historical_sessions")
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


@router.post("/workout_templates/{workout_template_id}/deactivate")
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


# ----------------------------
# Workout Template Exercises
# ----------------------------

@router.get("/workout_templates/{workout_template_id}/exercises")
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


@router.post("/workout_templates/{workout_template_id}/exercises/{workout_template_exercise_id}/delete")
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


# ----------------------------
# Workout Template Exercise Segments
# ----------------------------

@router.get("/workout_template_exercises/{workout_template_exercise_id}/segments")
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


@router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/upsert")
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


@router.post("/workout_template_exercises/{workout_template_exercise_id}/segments/{workout_template_exercise_segment_id}/delete")
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


# ----------------------------
# Training Sessions / Set Log
# ----------------------------

@router.post("/sessions/complete")
async def complete_training_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    payload: dict = Body(...),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    write_key = _require_idempotency_key(idempotency_key)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    day = _clean_text(payload.get("day"), 10)
    name = _clean_text(payload.get("name"), 160)
    notes = _clean_text(payload.get("notes"), 800)
    workout_template_id = _clean_text(payload.get("workout_template_id"), 80)
    wid = _as_uuid(workout_template_id, "workout_template_id") if workout_template_id else None
    load_unit = _clean_text(payload.get("load_unit"), 8).lower()

    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if load_unit not in {"lb", "kg"}:
        raise HTTPException(status_code=400, detail="load_unit must be lb or kg")
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
        if not exercise_id:
            raise HTTPException(status_code=400, detail=f"set {position} requires exercise_id")

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
                        "notes": _clean_text(raw_segment.get("notes"), 800),
                    }
                )

            segments.sort(key=lambda segment: segment["segment_index"])
            weight = segments[0]["weight"]
            reps = segments[0]["reps"]
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

        normalized_sets.append(
            {
                "exercise_id": exercise_id,
                "exercise_sort_order": exercise_sort_order,
                "set_index": set_index,
                "set_type": set_type,
                "weight": weight,
                "reps": reps,
                "flags": flags,
                "notes": set_notes,
                "segments": segments,
            }
        )

    conn = await _db(req)
    try:
        async with conn.transaction():
            await set_transaction_actor(conn, actor_user_id=owner)
            intent = {
                "day": day_val.isoformat(),
                "workout_template_id": wid,
                "name": name,
                "notes": notes,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "load_unit": load_unit,
                "sets": normalized_sets,
            }
            session_id = await write_training_session(
                conn,
                intent=intent,
                idempotency_key=write_key,
            )
            session = await conn.fetchrow(
                f"""
                select
                  training_session_id, owner_user_id, day, workout_template_id,
                  name, notes, started_at, finished_at, is_active,
                  created_at, updated_at
                from {SCHEMA}.training_session
                where training_session_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                session_id,
                owner,
            )
            result = _row_to_jsonable(session)
            result["set_count"] = len(normalized_sets)
            return JSONResponse(result)
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
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

    conn = await _db(req)
    try:
        owner, delegated = await _resolve_training_view_target(req, viewer, target_user_id)
        session_source = (
            "training_session"
            if include_inactive
            else "training_session_current_v"
        )

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
            with session_rollup as (
              select
                s.training_session_id, s.owner_user_id, s.day, s.workout_template_id,
                s.name, s.notes, s.started_at, s.finished_at, s.is_active,
                s.created_at, s.updated_at,
                base.workout_role_snapshot,
                role_event.assigned_role as historical_workout_role,
                coalesce(count(l.training_set_log_id) filter (where l.is_active=true), 0)::int as set_count,
                coalesce(count(distinct l.exercise_id) filter (where l.is_active=true), 0)::int as exercise_count,
                coalesce(sum(l.volume) filter (where l.is_active=true), 0)::float as volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::int as strength_set_count,
                coalesce(count(distinct l.exercise_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::int as strength_exercise_count,
                coalesce(sum(l.volume) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='strength'
                ), 0)::float as strength_volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::int as rehab_set_count,
                coalesce(count(distinct l.exercise_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::int as rehab_exercise_count,
                coalesce(sum(l.volume) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='rehab'
                ), 0)::float as rehab_volume,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.effective_role='unknown'
                ), 0)::int as unknown_role_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true and role_resolution.role_conflict
                ), 0)::int as role_conflict_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='capture_role'
                ), 0)::int as capture_role_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='exercise_role_snapshot'
                ), 0)::int as exercise_role_snapshot_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='training_session_role_event'
                ), 0)::int as training_session_role_event_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='workout_role_snapshot'
                ), 0)::int as workout_role_snapshot_resolved_set_count,
                coalesce(count(l.training_set_log_id) filter (
                  where l.is_active=true
                    and role_resolution.resolution_source='unresolved'
                ), 0)::int as unresolved_role_set_count
              from {SCHEMA}.{session_source} s
              join {SCHEMA}.training_session base
                on base.training_session_id=s.training_session_id
               and base.owner_user_id=s.owner_user_id
              left join {SCHEMA}.training_session_role_event role_event
                on role_event.training_session_id=s.training_session_id
               and role_event.owner_user_id=s.owner_user_id
              left join {SCHEMA}.training_set_log l
                on l.training_session_id=s.training_session_id
               and l.owner_user_id=s.owner_user_id
              left join {SCHEMA}.training_set_effective_role_v1 role_resolution
                on role_resolution.training_set_log_id=l.training_set_log_id
               and role_resolution.training_session_id=l.training_session_id
               and role_resolution.owner_user_id=l.owner_user_id
              where {' and '.join(where)}
              group by
                s.training_session_id, s.owner_user_id, s.day,
                s.workout_template_id, s.name, s.notes, s.started_at,
                s.finished_at, s.is_active, s.created_at, s.updated_at,
                base.workout_role_snapshot, role_event.assigned_role
              {having}
            ), classified as (
              select session_rollup.*,
                case
                  when strength_set_count > 0 and rehab_set_count > 0 then 'mixed'
                  when strength_set_count > 0 then 'strength'
                  when rehab_set_count > 0 then 'rehab'
                  else 'unclassified'
                end as session_role
              from session_rollup
            )
            select classified.*,
              $1::uuid as _target_user_id,
              $2::boolean as _delegated_view,
              session_role in ('strength', 'mixed') as counts_toward_strength
            from classified
            order by day desc, created_at desc
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

    conn = await _db(req)
    try:
        owner, delegated = await _resolve_training_view_target(req, viewer, target_user_id)

        row = await conn.fetchrow(
            f"""
            select
              training_session_id, owner_user_id, day, workout_template_id,
              name, notes, started_at, finished_at, is_active, created_at, updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {SCHEMA}.training_session_current_v
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


@router.get("/progression")
async def list_strength_progression(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    start_day: str = Query(..., min_length=10, max_length=10),
    end_day: str = Query(..., min_length=10, max_length=10),
    limit: int = Query(2000, ge=1, le=5000),
    target_user_id: str = Query("", max_length=80),
):
    viewer = require_actor_matches_owner(req, owner_user_id)

    try:
        start_date = _dt.date.fromisoformat(start_day)
        end_date = _dt.date.fromisoformat(end_day)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid progression date range")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_day must be on or after start_day")
    if (end_date - start_date).days > 366:
        raise HTTPException(status_code=400, detail="progression range cannot exceed 367 days")

    conn = await _db(req)
    try:
        owner, delegated = await _resolve_training_view_target(req, viewer, target_user_id)
        rows = await conn.fetch(
            f"""
            select
              s.training_session_id,
              s.day,
              s.name as session_name,
              l.exercise_id,
              max(l.exercise_name) as exercise_name,
              count(l.training_set_log_id)::int as set_count,
              coalesce(sum(l.reps), 0)::int as total_reps,
              coalesce(max(l.weight), 0)::float as max_load,
              coalesce(sum(l.volume), 0)::float as total_volume,
              array_agg(distinct role_resolution.resolution_source
                order by role_resolution.resolution_source
              ) as role_resolution_sources,
              case
                when count(distinct nullif(trim(l.load_unit), '')) = 0 then null
                when count(distinct nullif(trim(l.load_unit), '')) = 1
                  then max(nullif(trim(l.load_unit), ''))
                else 'mixed'
              end as load_unit,
              $4::uuid as _target_user_id,
              $5::boolean as _delegated_view
            from {SCHEMA}.training_session_current_v s
            join {SCHEMA}.training_set_log l
              on l.training_session_id=s.training_session_id
             and l.owner_user_id=s.owner_user_id
            join {SCHEMA}.training_set_effective_role_v1 role_resolution
              on role_resolution.training_set_log_id=l.training_set_log_id
             and role_resolution.training_session_id=l.training_session_id
             and role_resolution.owner_user_id=l.owner_user_id
            where s.owner_user_id=$1::uuid
              and s.day between $2::date and $3::date
              and s.is_active=true
              and s.finished_at is not null
              and l.is_active=true
              and role_resolution.effective_role='strength'
            group by
              s.training_session_id,
              s.day,
              s.name,
              s.created_at,
              l.exercise_id,
              l.exercise_sort_order
            order by
              s.day desc,
              s.created_at desc,
              l.exercise_sort_order asc,
              exercise_name asc
            limit {int(limit)}
            """,
            owner,
            start_date,
            end_date,
            owner,
            delegated,
        )
        return JSONResponse([_row_to_jsonable(row) for row in rows])
    finally:
        await conn.close()



@router.post("/sessions/{training_session_id}/deactivate")
async def deactivate_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    reason: str = Body("user_deleted", embed=True),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = require_actor_matches_owner(req, owner_user_id)

    conn = await _db(req)
    try:
        async with conn.transaction():
            await set_transaction_actor(conn, actor_user_id=owner)
            voided_id = await write_training_void(
                conn,
                training_session_id=sid,
                reason=_clean_text(reason, 1000) or "user_deleted",
            )
        return JSONResponse(
            {
                "training_session_id": str(voided_id),
                "owner_user_id": owner,
                "is_active": False,
                "voided": True,
            }
        )
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
    finally:
        await conn.close()


@router.post("/sessions/{training_session_id}/correct")
async def correct_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    payload: dict = Body(...),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = require_actor_matches_owner(req, owner_user_id)
    write_key = _require_idempotency_key(idempotency_key)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    conn = await _db(req)
    try:
        async with conn.transaction():
            await set_transaction_actor(conn, actor_user_id=owner)
            replacement_id = await write_training_correction(
                conn,
                training_session_id=sid,
                intent=payload,
                idempotency_key=write_key,
            )
            row = await conn.fetchrow(
                f"""
                select
                  training_session_id, owner_user_id, day,
                  workout_template_id, name, notes, started_at, finished_at,
                  supersedes_training_session_id, is_active,
                  created_at, updated_at
                from {SCHEMA}.training_session_current_v
                where training_session_id=$1::uuid
                  and owner_user_id=$2::uuid
                """,
                replacement_id,
                owner,
            )
        if not row:
            raise HTTPException(status_code=500, detail="training correction unavailable")
        result = _row_to_jsonable(row)
        result["set_count"] = len(payload.get("sets") or [])
        return JSONResponse(result)
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error
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

    conn = await _db(req)
    try:
        owner, delegated = await _resolve_training_view_target(req, viewer, target_user_id)

        where_active = "" if include_inactive else "and l.is_active=true"
        current_parent = "" if include_inactive else f"""
              and exists (
                select 1
                from {SCHEMA}.training_session_current_v current_session
                where current_session.training_session_id=l.training_session_id
                  and current_session.owner_user_id=l.owner_user_id
              )
        """
        rows = await conn.fetch(
            f"""
            select
              l.training_set_log_id, l.training_session_id, l.owner_user_id,
              l.workout_template_id, l.exercise_id, l.exercise_name,
              l.set_type, l.exercise_role_snapshot, l.capture_role,
              coalesce(l.capture_role, l.exercise_role_snapshot, 'unknown') as exercise_role,
              role_resolution.effective_role,
              role_resolution.resolution_source,
              role_resolution.role_conflict,
              l.exercise_sort_order, l.set_index, l.weight, l.reps, l.volume,
              l.load_unit,
              l.flags, l.notes, l.is_active, l.created_at, l.updated_at,
              $3::uuid as _target_user_id,
              $4::boolean as _delegated_view
            from {SCHEMA}.training_set_log l
            join {SCHEMA}.training_set_effective_role_v1 role_resolution
              on role_resolution.training_set_log_id=l.training_set_log_id
             and role_resolution.training_session_id=l.training_session_id
             and role_resolution.owner_user_id=l.owner_user_id
            where l.training_session_id=$1::uuid
              and l.owner_user_id=$2::uuid
              {where_active}
              {current_parent}
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
):
    _as_uuid(training_session_id, "training_session_id")
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/update")
async def update_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )


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
    conn = await _db(req)
    try:
        owner, delegated = await _resolve_training_view_target(req, viewer, target_user_id)

        parent = await conn.fetchrow(
            f"""
            select l.training_set_log_id
            from {SCHEMA}.training_set_log l
            join {SCHEMA}.training_session_current_v s
              on s.training_session_id=l.training_session_id
             and s.owner_user_id=l.owner_user_id
            where l.training_set_log_id=$1::uuid
              and l.training_session_id=$2::uuid
              and l.owner_user_id=$3::uuid
              and l.is_active=true
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
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/segments/{training_set_log_segment_id}/delete")
async def delete_training_set_log_segment(
    training_session_id: str,
    training_set_log_id: str,
    training_set_log_segment_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    _as_uuid(training_set_log_segment_id, "training_set_log_segment_id")
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )


@router.post("/sessions/{training_session_id}/sets/{training_set_log_id}/delete")
async def delete_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    require_actor_matches_owner(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )
