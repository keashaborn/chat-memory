from __future__ import annotations

import datetime as _dt
import decimal
import json
import os
import uuid

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_measurements_postgres import (
    MeasurementEntryWrite,
    lifeswitch_measurements_repository,
)
from seebx.core.identity import require_actor


router = APIRouter()


def _json_safe(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, decimal.Decimal):
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


def _clean_date(v: str, name: str = "local_date") -> _dt.date:
    try:
        return _dt.date.fromisoformat(str(v).strip())
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}; expected YYYY-MM-DD")


def _clean_text(v, max_len: int | None = None) -> str:
    s = str(v or "").strip()
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def _json_param(v):
    if v is None:
        return None
    if not isinstance(v, (dict, list)):
        raise HTTPException(status_code=400, detail="json fields must be objects or arrays")
    return json.dumps(v)


async def _has_people_permission(
    repository,
    grantor_user_id: str,
    grantee_user_id: str,
    scope: str,
) -> bool:
    return await repository.has_people_permission(
        grantor_user_id=grantor_user_id,
        grantee_user_id=grantee_user_id,
        scope=scope,
    )


async def _resolve_measurements_view_target(
    repository,
    viewer_user_id: str,
    target_user_id: str = "",
) -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = (
        _as_uuid(target_user_id, "target_user_id")
        if str(target_user_id or "").strip()
        else viewer
    )
    delegated = target != viewer

    if delegated:
        if os.getenv("LIFESWITCH_DELEGATED_READS_ENABLED", "0") != "1":
            raise HTTPException(status_code=403, detail="delegated_access_disabled")
        allowed = await _has_people_permission(
            repository,
            target,
            viewer,
            "measurements:view",
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail="measurements:view permission required",
            )

    return target, delegated


@router.get("/entries")
async def list_measurement_entries(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(90, ge=1, le=500),
    include_inactive: int = Query(0, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    viewer = await require_actor(req, owner_user_id)
    async with lifeswitch_measurements_repository(req) as measurements:
        owner, delegated = await _resolve_measurements_view_target(
            measurements,
            viewer,
            target_user_id,
        )
        rows = await measurements.list_entries(
            owner_user_id=owner,
            limit=limit,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])


@router.post("/entries/create")
async def create_measurement_entry(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    local_date: str = Body(...),
    measured_at: str | None = Body(None),
    weight_value: float | None = Body(None),
    weight_unit: str = Body("lb"),
    waist_value: float | None = Body(None),
    abdomen_value: float | None = Body(None),
    neck_value: float | None = Body(None),
    chest_value: float | None = Body(None),
    hip_value: float | None = Body(None),
    left_arm_value: float | None = Body(None),
    right_arm_value: float | None = Body(None),
    left_thigh_value: float | None = Body(None),
    right_thigh_value: float | None = Body(None),
    left_calf_value: float | None = Body(None),
    right_calf_value: float | None = Body(None),
    body_fat_percent: float | None = Body(None),
    body_fat_method: str | None = Body(None),
    measurement_unit: str = Body("in"),
    source: str = Body("manual"),
    entry_kind: str = Body("general"),
    notes: str = Body(""),
    skinfolds_json: dict | None = Body(None),
    scan_json: dict | None = Body(None),
):
    owner = await require_actor(req, owner_user_id)
    day = _clean_date(local_date)

    measured_at_val = None
    if measured_at is not None and str(measured_at).strip():
        try:
            measured_at_val = _dt.datetime.fromisoformat(
                str(measured_at).strip().replace("Z", "+00:00")
            )
        except Exception:
            raise HTTPException(status_code=400, detail="invalid measured_at")

    weight_unit = _clean_text(weight_unit, 20) or "lb"
    measurement_unit = _clean_text(measurement_unit, 20) or "in"
    source = _clean_text(source, 80) or "manual"
    entry_kind = _clean_text(entry_kind, 80) or "general"
    body_fat_method = _clean_text(body_fat_method, 80) or None
    notes = _clean_text(notes, 2000)

    value = MeasurementEntryWrite(
        owner_user_id=owner,
        local_date=day,
        measured_at=measured_at_val,
        weight_value=weight_value,
        weight_unit=weight_unit,
        waist_value=waist_value,
        abdomen_value=abdomen_value,
        neck_value=neck_value,
        chest_value=chest_value,
        hip_value=hip_value,
        left_arm_value=left_arm_value,
        right_arm_value=right_arm_value,
        left_thigh_value=left_thigh_value,
        right_thigh_value=right_thigh_value,
        left_calf_value=left_calf_value,
        right_calf_value=right_calf_value,
        body_fat_percent=body_fat_percent,
        body_fat_method=body_fat_method,
        measurement_unit=measurement_unit,
        source=source,
        entry_kind=entry_kind,
        notes=notes,
        skinfolds_json=_json_param(skinfolds_json),
        scan_json=_json_param(scan_json),
    )
    async with lifeswitch_measurements_repository(req) as measurements:
        row = await measurements.create_entry(value)
    return JSONResponse(_row_to_jsonable(row))


@router.post("/entries/{measurement_entry_id}/deactivate")
async def deactivate_measurement_entry(
    measurement_entry_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    entry_id = _as_uuid(measurement_entry_id, "measurement_entry_id")
    owner = await require_actor(req, owner_user_id)

    async with lifeswitch_measurements_repository(req) as measurements:
        row = await measurements.deactivate_entry(
            measurement_entry_id=entry_id,
            owner_user_id=owner,
        )
    if not row:
        raise HTTPException(status_code=404, detail="measurement entry not found")
    return JSONResponse(_row_to_jsonable(row))
