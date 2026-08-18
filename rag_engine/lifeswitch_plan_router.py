from __future__ import annotations

import os
import uuid
import json
import decimal
import datetime as _dt
from fastapi import APIRouter, HTTPException, Query, Body, Request
from seebx.core.ownership import require_actor_matches_owner
from rag_engine.lifeswitch_db import connect_lifeswitch
from fastapi.responses import JSONResponse

router = APIRouter()

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


async def _resolve_plan_target(
    conn,
    viewer_user_id: str,
    target_user_id: str = "",
    required_scopes: list[str] | None = None,
) -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = _as_uuid(target_user_id, "target_user_id") if str(target_user_id or "").strip() else viewer
    delegated = target != viewer

    if delegated:
        raise HTTPException(status_code=403, detail="owner-only Plan access required")

    return viewer, False


@router.get("/profile")
async def get_plan_profile(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    create_if_missing: int = Query(1, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    viewer = require_actor_matches_owner(req, owner_user_id)

    conn = await connect_lifeswitch(req)
    try:
        target, delegated = await _resolve_plan_target(conn, viewer, target_user_id, ["plan:view"])
        if delegated:
            create_if_missing = 0

        row = await _fetch_profile(conn, target)
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
                target,
            )

        if not row:
            return JSONResponse(None)

        out = _row_to_jsonable(row)
        out["_viewer_user_id"] = viewer
        out["_target_user_id"] = target
        out["_delegated_view"] = delegated
        return JSONResponse(out)
    finally:
        await conn.close()


@router.post("/profile/upsert")
async def upsert_plan_profile(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    snapshot_reason: str = Query("manual_update", max_length=120),
    target_user_id: str = Query("", max_length=80),

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
    viewer = require_actor_matches_owner(req, owner_user_id)

    phase = _clean_text(phase, 40)
    if phase not in VALID_PHASES:
        raise HTTPException(status_code=400, detail="phase must be cut|maintenance|lean_gain|recomp|other")

    conn = await connect_lifeswitch(req)
    try:
        owner, delegated = await _resolve_plan_target(conn, viewer, target_user_id, ["plan:edit"])

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

        out = _row_to_jsonable(row) if row else {"error": "upsert_failed"}
        if isinstance(out, dict):
            out["_viewer_user_id"] = viewer
            out["_target_user_id"] = owner
            out["_delegated_view"] = delegated
        return JSONResponse(out)
    finally:
        await conn.close()


@router.get("/profile/comments")
async def list_plan_comments(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
    limit: int = Query(50, ge=1, le=200),
):
    viewer = require_actor_matches_owner(req, owner_user_id)
    conn = await connect_lifeswitch(req)
    try:
        target, delegated = await _resolve_plan_target(
            conn,
            viewer,
            target_user_id,
            ["plan:view", "plan:comment", "plan:edit"],
        )

        profile = await _fetch_profile(conn, target)
        if not profile:
            return JSONResponse([])

        rows = await conn.fetch(
            f"""
            select
              c.plan_comment_id,
              c.plan_profile_id,
              c.target_user_id,
              c.author_user_id,
              c.author_user_id::text as author_display_name,
              c.comment_text,
              c.comment_kind,
              c.is_active,
              c.resolved_at,
              c.created_at,
              c.updated_at
            from {SCHEMA}.plan_comment c
            where c.plan_profile_id=$1::uuid
              and c.target_user_id=$2::uuid
              and c.is_active=true
            order by c.created_at desc
            limit $3
            """,
            str(profile["plan_profile_id"]),
            target,
            int(limit),
        )

        out = [_row_to_jsonable(r) for r in rows]
        return JSONResponse(out)
    finally:
        await conn.close()


@router.post("/profile/comments/create")
async def create_plan_comment(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
    comment_text: str = Body(...),
    comment_kind: str = Body("comment"),
):
    viewer = require_actor_matches_owner(req, owner_user_id)
    text = _clean_text(comment_text, 4000)
    kind = _clean_text(comment_kind, 80) or "comment"

    if not text:
        raise HTTPException(status_code=400, detail="comment_text required")

    conn = await connect_lifeswitch(req)
    try:
        target, delegated = await _resolve_plan_target(
            conn,
            viewer,
            target_user_id,
            ["plan:comment", "plan:edit"],
        )

        profile = await _fetch_profile(conn, target)
        if not profile:
            raise HTTPException(status_code=404, detail="target plan not found")

        row = await conn.fetchrow(
            f"""
            insert into {SCHEMA}.plan_comment
              (plan_profile_id, target_user_id, author_user_id, comment_text, comment_kind)
            values
              ($1::uuid, $2::uuid, $3::uuid, $4, $5)
            returning
              plan_comment_id,
              plan_profile_id,
              target_user_id,
              author_user_id,
              comment_text,
              comment_kind,
              is_active,
              resolved_at,
              created_at,
              updated_at
            """,
            str(profile["plan_profile_id"]),
            target,
            viewer,
            text,
            kind,
        )

        out = _row_to_jsonable(row)
        out["_viewer_user_id"] = viewer
        out["_target_user_id"] = target
        out["_delegated_view"] = delegated
        return JSONResponse(out)
    finally:
        await conn.close()


@router.get("/profile/history")
async def list_plan_profile_history(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    conn = await connect_lifeswitch(req)
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
