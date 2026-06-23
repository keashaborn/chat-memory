from __future__ import annotations

import os
import uuid
import json
import decimal
import datetime as _dt
import asyncpg

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN") or ""
if not DSN:
    raise RuntimeError("POSTGRES_DSN missing")


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


async def _db():
    return await asyncpg.connect(DSN)


@router.get("/entries")
async def list_measurement_entries(
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(90, ge=1, le=500),
    include_inactive: int = Query(0, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    where_active = "" if include_inactive else "and is_active=true"

    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              measurement_entry_id,
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              notes,
              skinfolds_json,
              scan_json,

              is_active,
              created_at,
              updated_at
            from public.lifeswitch_measurement_entries
            where owner_user_id=$1
              {where_active}
            order by local_date desc, created_at desc
            limit $2
            """,
            owner,
            limit,
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()


@router.post("/entries/create")
async def create_measurement_entry(
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

    notes: str = Body(""),
    skinfolds_json: dict | None = Body(None),
    scan_json: dict | None = Body(None),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    day = _clean_date(local_date)

    measured_at_val = None
    if measured_at is not None and str(measured_at).strip():
        try:
            measured_at_val = _dt.datetime.fromisoformat(str(measured_at).strip().replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="invalid measured_at")

    weight_unit = _clean_text(weight_unit, 20) or "lb"
    measurement_unit = _clean_text(measurement_unit, 20) or "in"
    source = _clean_text(source, 80) or "manual"
    body_fat_method = _clean_text(body_fat_method, 80) or None
    notes = _clean_text(notes, 2000)

    skinfolds = _json_param(skinfolds_json)
    scan = _json_param(scan_json)

    conn = await _db()
    try:
        row = await conn.fetchrow(
            """
            insert into public.lifeswitch_measurement_entries (
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              notes,
              skinfolds_json,
              scan_json,

              is_active
            )
            values (
              $1,
              $2::date,
              $3::timestamptz,

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
              $15,
              $16,

              $17,
              $18,

              $19,
              $20,
              $21,
              $22::jsonb,
              $23::jsonb,

              true
            )
            on conflict (owner_user_id, local_date, source)
              where is_active=true
            do update set
              measured_at=excluded.measured_at,

              weight_value=excluded.weight_value,
              weight_unit=excluded.weight_unit,

              waist_value=excluded.waist_value,
              abdomen_value=excluded.abdomen_value,
              neck_value=excluded.neck_value,
              chest_value=excluded.chest_value,
              hip_value=excluded.hip_value,

              left_arm_value=excluded.left_arm_value,
              right_arm_value=excluded.right_arm_value,
              left_thigh_value=excluded.left_thigh_value,
              right_thigh_value=excluded.right_thigh_value,
              left_calf_value=excluded.left_calf_value,
              right_calf_value=excluded.right_calf_value,

              body_fat_percent=excluded.body_fat_percent,
              body_fat_method=excluded.body_fat_method,

              measurement_unit=excluded.measurement_unit,
              notes=excluded.notes,
              skinfolds_json=excluded.skinfolds_json,
              scan_json=excluded.scan_json,

              updated_at=now()
            returning
              measurement_entry_id,
              owner_user_id,
              local_date,
              measured_at,

              weight_value,
              weight_unit,

              waist_value,
              abdomen_value,
              neck_value,
              chest_value,
              hip_value,

              left_arm_value,
              right_arm_value,
              left_thigh_value,
              right_thigh_value,
              left_calf_value,
              right_calf_value,

              body_fat_percent,
              body_fat_method,

              measurement_unit,
              source,
              notes,
              skinfolds_json,
              scan_json,

              is_active,
              created_at,
              updated_at
            """,
            owner,
            day,
            measured_at_val,

            weight_value,
            weight_unit,

            waist_value,
            abdomen_value,
            neck_value,
            chest_value,
            hip_value,

            left_arm_value,
            right_arm_value,
            left_thigh_value,
            right_thigh_value,
            left_calf_value,
            right_calf_value,

            body_fat_percent,
            body_fat_method,

            measurement_unit,
            source,
            notes,
            skinfolds,
            scan,
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
