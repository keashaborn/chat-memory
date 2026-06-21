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

SCHEMA = os.getenv("LIFESWITCH_PLAN_SCHEMA", "lifeswitch_plan")

JSON_FIELDS = {
    "body_state",
    "nutrition_targets",
    "training_targets",
    "conditioning_targets",
    "activity_targets",
    "recovery_targets",
    "monitoring_rules",
    "snapshot",
}

VALID_PHASES = {"cut", "maintenance", "lean_gain", "recomp", "other"}


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
    out = {}
    for k, v in d.items():
        v = _json_safe(v)
        if k in JSON_FIELDS and isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                pass
        out[k] = v
    return out


def _as_uuid(s: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _json_param(v) -> str:
    if v is None:
        v = {}
    if not isinstance(v, (dict, list)):
        raise HTTPException(status_code=400, detail="json sections must be objects or arrays")
    return json.dumps(v)


def _clean_text(v, max_len: int | None = None) -> str:
    s = str(v or "").strip()
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def _clean_date(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return _dt.date.fromisoformat(str(v).strip())
    except Exception:
        raise HTTPException(status_code=400, detail="invalid date; expected YYYY-MM-DD")


async def _db():
    return await asyncpg.connect(DSN)


async def _fetch_profile(conn, owner: str):
    return await conn.fetchrow(
        f"""
        select
          plan_profile_id, owner_user_id,
          phase, phase_label, primary_goal,
          start_date, review_date, review_cadence,
          body_state, nutrition_targets, training_targets,
          conditioning_targets, activity_targets, recovery_targets,
          monitoring_rules, coach_notes,
          is_active, created_at, updated_at
        from {SCHEMA}.plan_profile
        where owner_user_id=$1::uuid
          and is_active=true
        limit 1
        """,
        owner,
    )


@router.get("/profile")
async def get_plan_profile(
    owner_user_id: str = Query(..., min_length=1),
    create_if_missing: int = Query(1, ge=0, le=1),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        row = await _fetch_profile(conn, owner)
        if not row and create_if_missing:
            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.plan_profile (owner_user_id)
                values ($1::uuid)
                on conflict (owner_user_id) do update
                  set is_active=true,
                      updated_at=now()
                returning
                  plan_profile_id, owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets, recovery_targets,
                  monitoring_rules, coach_notes,
                  is_active, created_at, updated_at
                """,
                owner,
            )
        return JSONResponse(_row_to_jsonable(row) if row else None)
    finally:
        await conn.close()


@router.post("/profile/upsert")
async def upsert_plan_profile(
    owner_user_id: str = Query(..., min_length=1),
    snapshot_reason: str = Query("manual_update", max_length=120),

    phase: str = Body("maintenance"),
    phase_label: str = Body(""),
    primary_goal: str = Body(""),
    start_date: str | None = Body(None),
    review_date: str | None = Body(None),
    review_cadence: str = Body("weekly"),

    body_state: dict = Body(default_factory=dict),
    nutrition_targets: dict = Body(default_factory=dict),
    training_targets: dict = Body(default_factory=dict),
    conditioning_targets: dict = Body(default_factory=dict),
    activity_targets: dict = Body(default_factory=dict),
    recovery_targets: dict = Body(default_factory=dict),
    monitoring_rules: dict = Body(default_factory=dict),

    coach_notes: str = Body(""),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")

    phase = _clean_text(phase, 40)
    if phase not in VALID_PHASES:
        raise HTTPException(status_code=400, detail="phase must be cut|maintenance|lean_gain|recomp|other")

    conn = await _db()
    try:
        async with conn.transaction():
            existing = await _fetch_profile(conn, owner)

            if existing:
                existing_snapshot = json.dumps(_row_to_jsonable(existing))
                await conn.execute(
                    f"""
                    insert into {SCHEMA}.plan_profile_history
                      (plan_profile_id, owner_user_id, snapshot_reason, snapshot)
                    values
                      ($1::uuid, $2::uuid, $3, $4::jsonb)
                    """,
                    str(existing["plan_profile_id"]),
                    owner,
                    _clean_text(snapshot_reason, 120) or "manual_update",
                    existing_snapshot,
                )

            row = await conn.fetchrow(
                f"""
                insert into {SCHEMA}.plan_profile (
                  owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets, recovery_targets,
                  monitoring_rules, coach_notes,
                  is_active
                )
                values (
                  $1::uuid,
                  $2, $3, $4,
                  $5::date, $6::date, $7,
                  $8::jsonb, $9::jsonb, $10::jsonb,
                  $11::jsonb, $12::jsonb, $13::jsonb,
                  $14::jsonb, $15,
                  true
                )
                on conflict (owner_user_id) do update
                  set phase=excluded.phase,
                      phase_label=excluded.phase_label,
                      primary_goal=excluded.primary_goal,
                      start_date=excluded.start_date,
                      review_date=excluded.review_date,
                      review_cadence=excluded.review_cadence,
                      body_state=excluded.body_state,
                      nutrition_targets=excluded.nutrition_targets,
                      training_targets=excluded.training_targets,
                      conditioning_targets=excluded.conditioning_targets,
                      activity_targets=excluded.activity_targets,
                      recovery_targets=excluded.recovery_targets,
                      monitoring_rules=excluded.monitoring_rules,
                      coach_notes=excluded.coach_notes,
                      is_active=true,
                      updated_at=now()
                returning
                  plan_profile_id, owner_user_id,
                  phase, phase_label, primary_goal,
                  start_date, review_date, review_cadence,
                  body_state, nutrition_targets, training_targets,
                  conditioning_targets, activity_targets, recovery_targets,
                  monitoring_rules, coach_notes,
                  is_active, created_at, updated_at
                """,
                owner,
                phase,
                _clean_text(phase_label, 120),
                _clean_text(primary_goal, 500),
                _clean_date(start_date),
                _clean_date(review_date),
                _clean_text(review_cadence, 80) or "weekly",
                _json_param(body_state),
                _json_param(nutrition_targets),
                _json_param(training_targets),
                _json_param(conditioning_targets),
                _json_param(activity_targets),
                _json_param(recovery_targets),
                _json_param(monitoring_rules),
                _clean_text(coach_notes, 10000),
            )

        return JSONResponse(_row_to_jsonable(row) if row else {"error": "upsert_failed"})
    finally:
        await conn.close()


@router.get("/profile/history")
async def list_plan_profile_history(
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    owner = _as_uuid(owner_user_id, "owner_user_id")
    conn = await _db()
    try:
        rows = await conn.fetch(
            f"""
            select
              plan_profile_history_id, plan_profile_id, owner_user_id,
              snapshot_reason, snapshot, created_at
            from {SCHEMA}.plan_profile_history
            where owner_user_id=$1::uuid
            order by created_at desc
            limit $2
            """,
            owner,
            int(limit),
        )
        return JSONResponse([_row_to_jsonable(r) for r in rows])
    finally:
        await conn.close()
