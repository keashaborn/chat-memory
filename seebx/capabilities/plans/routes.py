from __future__ import annotations

import datetime as _dt
import uuid

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_plan_postgres import (
    PlanProfileWrite,
    lifeswitch_plan_repository,
    plan_row_to_jsonable,
)
from seebx.core.ownership import require_actor_matches_owner


router = APIRouter()

VALID_PHASES = {"cut", "maintenance", "lean_gain", "recomp", "other"}


def _as_uuid(value: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"invalid {name}")


def _clean_text(value, max_len: int | None = None) -> str:
    result = str(value or "").strip()
    if max_len is not None and len(result) > max_len:
        result = result[:max_len]
    return result


def _clean_date(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return _dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid date; expected YYYY-MM-DD")


async def _resolve_plan_target(
    viewer_user_id: str,
    target_user_id: str = "",
) -> tuple[str, bool]:
    viewer = _as_uuid(viewer_user_id, "owner_user_id")
    target = (
        _as_uuid(target_user_id, "target_user_id")
        if str(target_user_id or "").strip()
        else viewer
    )
    if target != viewer:
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
    target, delegated = await _resolve_plan_target(viewer, target_user_id)
    async with lifeswitch_plan_repository(req) as repository:
        row = await repository.get_or_create_profile(
            owner_user_id=target,
            create_if_missing=bool(create_if_missing),
        )
    if row is None:
        return JSONResponse(None)
    result = plan_row_to_jsonable(row)
    result.update(
        _viewer_user_id=viewer,
        _target_user_id=target,
        _delegated_view=delegated,
    )
    return JSONResponse(result)


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
    cleaned_phase = _clean_text(phase, 40)
    if cleaned_phase not in VALID_PHASES:
        raise HTTPException(
            status_code=400,
            detail="phase must be cut|maintenance|lean_gain|recomp|other",
        )
    owner, delegated = await _resolve_plan_target(viewer, target_user_id)
    value = PlanProfileWrite(
        owner_user_id=owner,
        phase=cleaned_phase,
        phase_label=_clean_text(phase_label, 120),
        primary_goal=_clean_text(primary_goal, 500),
        start_date=_clean_date(start_date),
        review_date=_clean_date(review_date),
        review_cadence=_clean_text(review_cadence, 80) or "weekly",
        body_state=body_state,
        nutrition_targets=nutrition_targets,
        training_targets=training_targets,
        conditioning_targets=conditioning_targets,
        activity_targets=activity_targets,
        recovery_targets=recovery_targets,
        monitoring_rules=monitoring_rules,
        coach_notes=_clean_text(coach_notes, 10000),
    )
    async with lifeswitch_plan_repository(req) as repository:
        row = await repository.upsert_profile(
            value,
            snapshot_reason=_clean_text(snapshot_reason, 120) or "manual_update",
        )
    result = plan_row_to_jsonable(row) if row is not None else {"error": "upsert_failed"}
    result.update(
        _viewer_user_id=viewer,
        _target_user_id=owner,
        _delegated_view=delegated,
    )
    return JSONResponse(result)


@router.get("/profile/comments")
async def list_plan_comments(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
    limit: int = Query(50, ge=1, le=200),
):
    viewer = require_actor_matches_owner(req, owner_user_id)
    target, _ = await _resolve_plan_target(viewer, target_user_id)
    async with lifeswitch_plan_repository(req) as repository:
        rows = await repository.list_comments(owner_user_id=target, limit=int(limit))
    return JSONResponse([plan_row_to_jsonable(row) for row in rows])


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
    target, delegated = await _resolve_plan_target(viewer, target_user_id)
    async with lifeswitch_plan_repository(req) as repository:
        row = await repository.create_comment(
            owner_user_id=target,
            author_user_id=viewer,
            comment_text=text,
            comment_kind=kind,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="target plan not found")
    result = plan_row_to_jsonable(row)
    result.update(
        _viewer_user_id=viewer,
        _target_user_id=target,
        _delegated_view=delegated,
    )
    return JSONResponse(result)


@router.get("/profile/history")
async def list_plan_profile_history(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    owner = require_actor_matches_owner(req, owner_user_id)
    async with lifeswitch_plan_repository(req) as repository:
        rows = await repository.list_history(owner_user_id=owner, limit=int(limit))
    return JSONResponse([plan_row_to_jsonable(row) for row in rows])
