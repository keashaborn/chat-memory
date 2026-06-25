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

PEOPLE_SCHEMA = os.getenv("LIFESWITCH_PEOPLE_SCHEMA", "lifeswitch_people")


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


async def _resolve_measurements_view_target(conn, viewer_user_id: str, target_user_id: str = "") -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        allowed = await _has_people_permission(conn, target, viewer, "measurements:view")
        if not allowed:
            raise HTTPException(status_code=403, detail="measurements:view permission required")

    return target, delegated


@router.get("/entries")
async def list_measurement_entries(
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(90, ge=1, le=500),
    include_inactive: int = Query(0, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    viewer = _as_uuid(owner_user_id, "owner_user_id")
    where_active = "" if include_inactive else "and is_active=true"

    conn = await _db()
    try:
        owner, delegated = await _resolve_measurements_view_target(conn, viewer, target_user_id)

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
              entry_kind,
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
    entry_kind: str = Body("general"),

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
    entry_kind = _clean_text(entry_kind, 80) or "general"
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
              entry_kind,
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
              $22,
              $23::jsonb,
              $24::jsonb,

              true
            )
            on conflict (owner_user_id, local_date, source, entry_kind)
              where is_active=true
            do update set
              measured_at=coalesce(excluded.measured_at, public.lifeswitch_measurement_entries.measured_at),

              weight_value=coalesce(excluded.weight_value, public.lifeswitch_measurement_entries.weight_value),
              weight_unit=excluded.weight_unit,

              waist_value=coalesce(excluded.waist_value, public.lifeswitch_measurement_entries.waist_value),
              abdomen_value=coalesce(excluded.abdomen_value, public.lifeswitch_measurement_entries.abdomen_value),
              neck_value=coalesce(excluded.neck_value, public.lifeswitch_measurement_entries.neck_value),
              chest_value=coalesce(excluded.chest_value, public.lifeswitch_measurement_entries.chest_value),
              hip_value=coalesce(excluded.hip_value, public.lifeswitch_measurement_entries.hip_value),

              left_arm_value=coalesce(excluded.left_arm_value, public.lifeswitch_measurement_entries.left_arm_value),
              right_arm_value=coalesce(excluded.right_arm_value, public.lifeswitch_measurement_entries.right_arm_value),
              left_thigh_value=coalesce(excluded.left_thigh_value, public.lifeswitch_measurement_entries.left_thigh_value),
              right_thigh_value=coalesce(excluded.right_thigh_value, public.lifeswitch_measurement_entries.right_thigh_value),
              left_calf_value=coalesce(excluded.left_calf_value, public.lifeswitch_measurement_entries.left_calf_value),
              right_calf_value=coalesce(excluded.right_calf_value, public.lifeswitch_measurement_entries.right_calf_value),

              body_fat_percent=coalesce(excluded.body_fat_percent, public.lifeswitch_measurement_entries.body_fat_percent),
              body_fat_method=coalesce(excluded.body_fat_method, public.lifeswitch_measurement_entries.body_fat_method),

              measurement_unit=excluded.measurement_unit,
              notes=case when excluded.notes <> '' then excluded.notes else public.lifeswitch_measurement_entries.notes end,
              skinfolds_json=coalesce(excluded.skinfolds_json, public.lifeswitch_measurement_entries.skinfolds_json),
              scan_json=coalesce(excluded.scan_json, public.lifeswitch_measurement_entries.scan_json),

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
              entry_kind,
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
            entry_kind,
            notes,
            skinfolds,
            scan,
        )

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()


@router.post("/entries/{measurement_entry_id}/deactivate")
async def deactivate_measurement_entry(
    measurement_entry_id: str,
    owner_user_id: str = Query(..., min_length=1),
):
    entry_id = _as_uuid(measurement_entry_id, "measurement_entry_id")
    owner = _as_uuid(owner_user_id, "owner_user_id")

    conn = await _db()
    try:
        row = await conn.fetchrow(
            """
            update public.lifeswitch_measurement_entries
               set is_active=false,
                   updated_at=now()
             where measurement_entry_id=$1::uuid
               and owner_user_id=$2
               and is_active=true
            returning
              measurement_entry_id,
              owner_user_id,
              local_date,
              entry_kind,
              source,
              is_active,
              created_at,
              updated_at
            """,
            entry_id,
            owner,
        )

        if not row:
            raise HTTPException(status_code=404, detail="measurement entry not found")

        return JSONResponse(_row_to_jsonable(row))
    finally:
        await conn.close()
