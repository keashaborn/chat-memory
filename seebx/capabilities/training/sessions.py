from __future__ import annotations

import datetime as _dt
import math

from fastapi import Body, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from seebx.adapters.lifeswitch_training_sessions_postgres import lifeswitch_training_sessions_repository
from seebx.adapters.lifeswitch_training_writes_postgres import TrainingWriterError
from seebx.core.identity import require_actor

from .access import _resolve_training_view_target
from .common import _as_uuid, _clean_text, _require_idempotency_key, _row_to_jsonable
from .write_errors import training_writer_http_error


async def complete_training_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    payload: dict = Body(...),
):
    owner = await require_actor(req, owner_user_id)
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
    try:
        async with lifeswitch_training_sessions_repository(req) as repository:
            session = await repository.complete_session(
                owner=owner,
                intent=intent,
                idempotency_key=write_key,
            )
        result = _row_to_jsonable(session)
        result["set_count"] = len(normalized_sets)
        return JSONResponse(result)
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error

async def create_training_session(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="session creation moved to atomic /sessions/complete",
    )

async def list_training_sessions(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    day: str | None = Query(None),
    include_inactive: int = Query(0, ge=0, le=1),
    limit: int = Query(100, ge=1, le=500),
    target_user_id: str = Query("", max_length=80),
):
    viewer = await require_actor(req, owner_user_id)

    day_val = None
    if day:
        try:
            day_val = _dt.date.fromisoformat(day)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid day")

    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_sessions_repository(req) as repository:
        rows = await repository.list_sessions(
            owner=owner,
            delegated=delegated,
            day_val=day_val,
            include_inactive=bool(include_inactive),
            limit=limit,
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def get_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    viewer = await require_actor(req, owner_user_id)

    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_sessions_repository(req) as repository:
        row = await repository.get_session(
            sid=sid,
            owner=owner,
            delegated=delegated,
        )
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(_row_to_jsonable(row))

async def list_strength_progression(
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    start_day: str = Query(..., min_length=10, max_length=10),
    end_day: str = Query(..., min_length=10, max_length=10),
    limit: int = Query(2000, ge=1, le=5000),
    target_user_id: str = Query("", max_length=80),
):
    viewer = await require_actor(req, owner_user_id)

    try:
        start_date = _dt.date.fromisoformat(start_day)
        end_date = _dt.date.fromisoformat(end_day)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid progression date range")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_day must be on or after start_day")
    if (end_date - start_date).days > 366:
        raise HTTPException(status_code=400, detail="progression range cannot exceed 367 days")

    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_sessions_repository(req) as repository:
        rows = await repository.list_strength_progression(
            owner=owner,
            delegated=delegated,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def deactivate_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    reason: str = Body("user_deleted", embed=True),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = await require_actor(req, owner_user_id)

    try:
        async with lifeswitch_training_sessions_repository(req) as repository:
            voided_id = await repository.deactivate_session(
                sid=sid,
                owner=owner,
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

async def correct_training_session(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    payload: dict = Body(...),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    owner = await require_actor(req, owner_user_id)
    write_key = _require_idempotency_key(idempotency_key)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")

    try:
        async with lifeswitch_training_sessions_repository(req) as repository:
            row = await repository.correct_session(
                sid=sid,
                owner=owner,
                intent=payload,
                idempotency_key=write_key,
            )
        if not row:
            raise HTTPException(
                status_code=500, detail="training correction unavailable"
            )
        result = _row_to_jsonable(row)
        result["set_count"] = len(payload.get("sets") or [])
        return JSONResponse(result)
    except TrainingWriterError as error:
        raise training_writer_http_error(error) from error

async def list_training_session_sets(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    include_inactive: int = Query(0, ge=0, le=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    viewer = await require_actor(req, owner_user_id)

    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_sessions_repository(req) as repository:
        rows = await repository.list_session_sets(
            sid=sid,
            owner=owner,
            delegated=delegated,
            include_inactive=bool(include_inactive),
        )
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def add_training_set_log(
    training_session_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )

async def update_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )

async def list_training_set_log_segments(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
    target_user_id: str = Query("", max_length=80),
):
    sid = _as_uuid(training_session_id, "training_session_id")
    setid = _as_uuid(training_set_log_id, "training_set_log_id")
    viewer = await require_actor(req, owner_user_id)
    owner, delegated = await _resolve_training_view_target(
        req, viewer, target_user_id
    )
    async with lifeswitch_training_sessions_repository(req) as repository:
        rows = await repository.list_set_segments(
            sid=sid,
            setid=setid,
            owner=owner,
            delegated=delegated,
        )
    if rows is None:
        raise HTTPException(status_code=404, detail="set not found")
    return JSONResponse([_row_to_jsonable(row) for row in rows])

async def add_training_set_log_segment(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )

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
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )

async def delete_training_set_log(
    training_session_id: str,
    training_set_log_id: str,
    req: Request,
    owner_user_id: str = Query(..., min_length=1),
):
    _as_uuid(training_session_id, "training_session_id")
    _as_uuid(training_set_log_id, "training_set_log_id")
    await require_actor(req, owner_user_id)
    raise HTTPException(
        status_code=410,
        detail="completed sessions are immutable; submit an aggregate correction",
    )
