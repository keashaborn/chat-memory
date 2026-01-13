from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os, json, uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.getenv("POSTGRES_DSN", "postgres://sage:strongpassword@localhost:5432/memory")


def _parse_uuid(s: Any) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(s))
    except Exception:
        return None


def _parse_ts(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        t = str(s).strip()
        if not t:
            return None
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def _connect() -> asyncpg.Connection:
    conn = await asyncpg.connect(DSN)
    await conn.set_type_codec(
        "json",
        encoder=lambda v: json.dumps(v),
        decoder=lambda v: json.loads(v),
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v),
        decoder=lambda v: json.loads(v),
        schema="pg_catalog",
    )
    return conn


@router.post("/telemetry/event")
async def telemetry_event(req: Request):
    """
    Write-only telemetry sink. Idempotent by event_id.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}

    req_request_id = getattr(req.state, "request_id", None)

    events = (body or {}).get("events")
    if not isinstance(events, list) or not events:
        return JSONResponse(
            {"accepted": 0, "rejected": 0, "errors": [{"reason": "missing events[]"}]},
            status_code=400,
        )

    actor_user_id = (req.headers.get("x-vs-actor-user-id") or "").strip() or None
    if actor_user_id:
        actor_user_id = actor_user_id[:128]

    conn = await _connect()
    accepted = 0
    rejected = 0
    errors: List[Dict[str, Any]] = []

    sql = """
        INSERT INTO telemetry_event (
        event_id, event_type,
        subject_type, subject_id,
        target_model_id, target_model_version,
        judge_model_id, judge_model_version,
        vantage_id, condition_id,
        thread_id, turn_id,
        actor_user_id,
        payload, occurred_at
        )
        VALUES (
        $1,$2,
        $3,$4,
        $5,$6,
        $7,$8,
        $9,$10,
        $11,$12,
        $13,
        $14,$15
        )
        ON CONFLICT (event_id) DO NOTHING
    """

    try:
        for i, e in enumerate(events):
            if not isinstance(e, dict):
                rejected += 1
                errors.append({"index": i, "reason": "event not object"})
                continue

            event_id = _parse_uuid(e.get("event_id"))
            if not event_id:
                rejected += 1
                errors.append({"index": i, "reason": "invalid/missing event_id (uuid)"})
                continue

            event_type = str(e.get("event_type") or "").strip()
            subject_type = str(e.get("subject_type") or "").strip()
            subject_id = str(e.get("subject_id") or "").strip()
            if not event_type or not subject_type or not subject_id:
                rejected += 1
                errors.append({"index": i, "reason": "missing event_type/subject_type/subject_id"})
                continue

            occurred_at = _parse_ts(e.get("occurred_at")) or _parse_ts(e.get("created_at")) or datetime.now(timezone.utc)

            payload = e.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if req_request_id and "request_id" not in payload:
                payload["request_id"] = str(req_request_id)

            target_model_id = (e.get("target_model_id") or None)
            target_model_version = (e.get("target_model_version") or None)
            judge_model_id = (e.get("judge_model_id") or None)
            judge_model_version = (e.get("judge_model_version") or None)
            vantage_id = (e.get("vantage_id") or None)
            condition_id = (e.get("condition_id") or None)
            thread_id = (e.get("thread_id") or None)
            turn_id = (e.get("turn_id") or None)

            r = await conn.execute(
                sql,
                event_id, event_type,
                subject_type, subject_id,
                target_model_id, target_model_version,
                judge_model_id, judge_model_version,
                vantage_id, condition_id,
                thread_id, turn_id,
                actor_user_id,
                payload, occurred_at
            )

            accepted += 1

    finally:
        await conn.close()

    return {"accepted": accepted, "rejected": rejected, "errors": errors}


def _metric_expr(metric_key: str) -> Tuple[str, str]:
    defs: Dict[str, Tuple[str, str]] = {
        "probe_overall": (
            "NULLIF(payload->'scores'->>'overall','')::double precision",
            "event_type IN ('probe.response','chat.response')",
        ),
        "hallucination_rate": (
            "CASE WHEN (payload->'flags'->>'hallucination')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'hallucination')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "concession_rate": (
            "CASE WHEN (payload->'flags'->>'concession')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'concession')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "clarification_rate": (
            "CASE WHEN (payload->'flags'->>'clarification')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'clarification')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
        "style_drift": (
            "NULLIF(payload->'scores'->>'style_drift','')::double precision",
            "event_type IN ('probe.response','chat.response')",
        ),
        "refusal_rate": (
            "CASE WHEN (payload->'flags'->>'refusal')='true' THEN 1.0 "
            "WHEN (payload->'flags'->>'refusal')='false' THEN 0.0 "
            "ELSE NULL END",
            "event_type IN ('probe.response','chat.response')",
        ),
    }
    if metric_key not in defs:
        raise KeyError(metric_key)
    return defs[metric_key]


@router.get("/metrics/timeseries")
async def metrics_timeseries(
    req: Request,
    metric_key: str = Query(...),
    subject_type: str = Query(...),
    subject_id: str = Query(...),
    from_ts: str = Query(..., alias="from"),
    to_ts: str = Query(..., alias="to"),
    bucket: str = Query("day"),
    target_model_id: Optional[str] = Query(None),
):
    bucket = (bucket or "day").strip().lower()
    if bucket not in ("hour", "day"):
        return JSONResponse({"error": "bucket must be 'hour' or 'day'"} , status_code=400)

    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    if not start or not end:
        return JSONResponse({"error": "invalid from/to ISO timestamps"} , status_code=400)

    try:
        expr, default_where = _metric_expr(metric_key)
    except KeyError:
        return JSONResponse({"error": f"unknown metric_key '{metric_key}'"}, status_code=400)

    actor_user_id = (req.headers.get("x-vs-actor-user-id") or "").strip() or None
    if actor_user_id:
        actor_user_id = actor_user_id[:128]

    conn = await _connect()
    try:
        wh = ["subject_type=$1", "subject_id=$2", "occurred_at >= $3", "occurred_at < $4", default_where]
        params: List[Any] = [subject_type, subject_id, start, end]

        idx = 5
        if target_model_id:
            wh.append(f"target_model_id=${idx}")
            params.append(target_model_id)
            idx += 1

        if actor_user_id:
            wh.append(f"actor_user_id=${idx}")
            params.append(actor_user_id)
            idx += 1

        where_sql = " AND ".join(f"({w})" for w in wh)
        dt_unit_param = "day" if bucket == "day" else "hour"

        q = f"""
            SELECT
              date_trunc('{dt_unit_param}', occurred_at) AS t,
              AVG({expr}) AS v,
              COUNT({expr}) AS n
            FROM telemetry_event
            WHERE {where_sql}
            GROUP BY 1
            ORDER BY 1
        """

        rows = await conn.fetch(q, *params)

        points = []
        for r in rows:
            points.append({
                "t": r["t"].isoformat(),
                "v": float(r["v"]) if r["v"] is not None else None,
                "n": int(r["n"]) if r["n"] is not None else 0,
                "meta": {"method": "v0_jsonb_expr"},
            })

        phases = []

        if actor_user_id:
            base = await conn.fetchrow(
                """
                SELECT condition_id, occurred_at, payload
                FROM telemetry_event
                WHERE subject_type=$1 AND subject_id=$2 AND actor_user_id=$3
                AND event_type='condition.set' AND occurred_at < $4
                ORDER BY occurred_at DESC
                LIMIT 1
                """,
                subject_type, subject_id, actor_user_id, start
            )
            within = await conn.fetch(
                """
                SELECT condition_id, occurred_at, payload
                FROM telemetry_event
                WHERE subject_type=$1 AND subject_id=$2 AND actor_user_id=$3
                AND event_type='condition.set'
                AND occurred_at >= $4 AND occurred_at < $5
                ORDER BY occurred_at ASC
                """,
                subject_type, subject_id, actor_user_id, start, end
            )
        else:
            base = await conn.fetchrow(
                """
                SELECT condition_id, occurred_at, payload
                FROM telemetry_event
                WHERE subject_type=$1 AND subject_id=$2 AND event_type='condition.set' AND occurred_at < $3
                ORDER BY occurred_at DESC
                LIMIT 1
                """,
                subject_type, subject_id, start
            )
            within = await conn.fetch(
                """
                SELECT condition_id, occurred_at, payload
                FROM telemetry_event
                WHERE subject_type=$1 AND subject_id=$2 AND event_type='condition.set'
                AND occurred_at >= $3 AND occurred_at < $4
                ORDER BY occurred_at ASC
                """,
                subject_type, subject_id, start, end
            )

        seq = []
        if base:
            seq.append(base)
        seq.extend(within)

        for idx, row in enumerate(seq):
            cid = row["condition_id"]
            st = row["occurred_at"]
            nxt = seq[idx + 1]["occurred_at"] if idx + 1 < len(seq) else None
            payload = row["payload"] or {}
            label = payload.get("label") or payload.get("phase") or cid
            phases.append({
                "condition_id": cid,
                "label": label,
                "start_ts": st.isoformat(),
                "end_ts": nxt.isoformat() if nxt else None,
            })

        return {
            "metric_key": metric_key,
            "subject": {"subject_type": subject_type, "subject_id": subject_id},
            "points": points,
            "phases": phases,
        }

    finally:
        await conn.close()
