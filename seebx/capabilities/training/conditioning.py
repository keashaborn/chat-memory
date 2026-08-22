from __future__ import annotations

import datetime as _dt
import json

from fastapi import Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_training_conditioning_postgres import lifeswitch_training_conditioning_repository
from seebx.adapters.lifeswitch_training_writes_postgres import TrainingWriterError
from seebx.core.ownership import require_actor_matches_owner

from .access import _resolve_training_view_target
from .common import _as_uuid, _clean_text, _require_idempotency_key, _row_to_jsonable
from .write_errors import training_writer_http_error


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

async def list_conditioning_library(
    req: Request,
    include_inactive: int = Query(0, ge=0, le=1),
):
    async with lifeswitch_training_conditioning_repository(req) as repository:
        rows = await repository.list_library(include_inactive=bool(include_inactive))
    return JSONResponse([_row_to_jsonable(row) for row in rows])

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
