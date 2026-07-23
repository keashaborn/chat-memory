from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import os, json, uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

router = APIRouter()

DSN = os.environ["POSTGRES_DSN"]
TELEMETRY_NO_STORE_HEADERS = {
    "cache-control": "private, no-store, max-age=0, must-revalidate",
    "pragma": "no-cache",
    "expires": "0",
}
VOICE_SLO_CONTRACT_VERSION = "voice_slo_v1"
VOICE_SLO_MINIMUM_SAMPLES = 30
VOICE_SLO_TARGETS = {
    "turn_success_rate": 0.99,
    "transcription_ms_p95": 3_000.0,
    "response_ms_p95": 10_000.0,
    "tts_first_audio_ms_p95": 3_000.0,
    "end_of_speech_to_first_audio_ms_p95": 15_000.0,
}


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


def _require_actor(req: Request) -> str:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw:
        raise ValueError("missing_actor_user_id")
    try:
        return str(uuid.UUID(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_actor_user_id") from exc


async def _set_actor(conn: asyncpg.Connection, actor_user_id: str) -> None:
    await conn.execute(
        "SELECT set_config('app.user_id',$1,true)",
        actor_user_id,
    )


def _voice_slo_check(
    *,
    actual: float | None,
    target: float,
    minimum_met: bool,
    higher_is_better: bool = False,
) -> dict[str, Any]:
    if not minimum_met or actual is None:
        status = "insufficient_data"
    elif higher_is_better:
        status = "pass" if actual >= target else "fail"
    else:
        status = "pass" if actual <= target else "fail"
    return {
        "actual": actual,
        "target": target,
        "operator": ">=" if higher_is_better else "<=",
        "status": status,
    }


def _optional_float(row: Any, key: str) -> float | None:
    value = row[key]
    return float(value) if value is not None else None


def _voice_slo_payload(row: Any, window_days: int) -> dict[str, Any]:
    total = int(row["total"] or 0)
    completed = int(row["completed"] or 0)
    failed = int(row["failed"] or 0)
    cancelled = int(row["cancelled"] or 0)
    evaluated_turns = completed + failed
    success_rate = (
        completed / evaluated_turns if evaluated_turns else None
    )
    reliability_ready = evaluated_turns >= VOICE_SLO_MINIMUM_SAMPLES
    latency_ready = completed >= VOICE_SLO_MINIMUM_SAMPLES

    checks = {
        "turn_success_rate": _voice_slo_check(
            actual=success_rate,
            target=VOICE_SLO_TARGETS["turn_success_rate"],
            minimum_met=reliability_ready,
            higher_is_better=True,
        ),
        "transcription_ms_p95": _voice_slo_check(
            actual=_optional_float(row, "transcription_ms_p95"),
            target=VOICE_SLO_TARGETS["transcription_ms_p95"],
            minimum_met=latency_ready,
        ),
        "response_ms_p95": _voice_slo_check(
            actual=_optional_float(row, "response_ms_p95"),
            target=VOICE_SLO_TARGETS["response_ms_p95"],
            minimum_met=latency_ready,
        ),
        "tts_first_audio_ms_p95": _voice_slo_check(
            actual=_optional_float(row, "tts_first_audio_ms_p95"),
            target=VOICE_SLO_TARGETS["tts_first_audio_ms_p95"],
            minimum_met=latency_ready,
        ),
        "end_of_speech_to_first_audio_ms_p95": _voice_slo_check(
            actual=_optional_float(
                row,
                "end_of_speech_to_first_audio_ms_p95",
            ),
            target=VOICE_SLO_TARGETS[
                "end_of_speech_to_first_audio_ms_p95"
            ],
            minimum_met=latency_ready,
        ),
    }
    statuses = {check["status"] for check in checks.values()}
    overall_status = (
        "insufficient_data"
        if "insufficient_data" in statuses
        else "fail"
        if "fail" in statuses
        else "pass"
    )

    return {
        "contract_version": VOICE_SLO_CONTRACT_VERSION,
        "window_days": window_days,
        "minimum_samples": VOICE_SLO_MINIMUM_SAMPLES,
        "overall_status": overall_status,
        "sample": {
            "total": total,
            "evaluated_turns": evaluated_turns,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "transcription_failures": int(
                row["transcription_failures"] or 0
            ),
            "response_failures": int(row["response_failures"] or 0),
            "tts_failures": int(row["tts_failures"] or 0),
        },
        "latency_ms": {
            "transcription": {
                "p50": _optional_float(row, "transcription_ms_p50"),
                "p95": _optional_float(row, "transcription_ms_p95"),
                "p99": _optional_float(row, "transcription_ms_p99"),
            },
            "response": {
                "p50": _optional_float(row, "response_ms_p50"),
                "p95": _optional_float(row, "response_ms_p95"),
                "p99": _optional_float(row, "response_ms_p99"),
            },
            "tts_first_audio": {
                "p50": _optional_float(row, "tts_first_audio_ms_p50"),
                "p95": _optional_float(row, "tts_first_audio_ms_p95"),
                "p99": _optional_float(row, "tts_first_audio_ms_p99"),
            },
            "end_of_speech_to_first_audio": {
                "p50": _optional_float(
                    row,
                    "end_of_speech_to_first_audio_ms_p50",
                ),
                "p95": _optional_float(
                    row,
                    "end_of_speech_to_first_audio_ms_p95",
                ),
                "p99": _optional_float(
                    row,
                    "end_of_speech_to_first_audio_ms_p99",
                ),
            },
        },
        "checks": checks,
    }


@router.post("/telemetry/event")
async def telemetry_event(req: Request):
    """
    Write-only telemetry sink. Idempotent by event_id.
    """
    try:
        actor_user_id = _require_actor(req)
    except ValueError as exc:
        status = 401 if str(exc) == "missing_actor_user_id" else 400
        return JSONResponse(
            {"accepted": 0, "rejected": 0, "errors": [{"reason": str(exc)}]},
            status_code=status,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

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
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

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
        async with conn.transaction():
            await _set_actor(conn, actor_user_id)
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

                await conn.execute(
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

    return JSONResponse(
        {"accepted": accepted, "rejected": rejected, "errors": errors},
        headers=TELEMETRY_NO_STORE_HEADERS,
    )


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
    try:
        actor_user_id = _require_actor(req)
    except ValueError as exc:
        status = 401 if str(exc) == "missing_actor_user_id" else 400
        return JSONResponse(
            {"error": str(exc)},
            status_code=status,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    bucket = (bucket or "day").strip().lower()
    if bucket not in ("hour", "day"):
        return JSONResponse(
            {"error": "bucket must be 'hour' or 'day'"},
            status_code=400,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    start = _parse_ts(from_ts)
    end = _parse_ts(to_ts)
    if not start or not end:
        return JSONResponse(
            {"error": "invalid from/to ISO timestamps"},
            status_code=400,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    try:
        expr, default_where = _metric_expr(metric_key)
    except KeyError:
        return JSONResponse(
            {"error": f"unknown metric_key '{metric_key}'"},
            status_code=400,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    conn = await _connect()
    try:
        async with conn.transaction():
            await _set_actor(conn, actor_user_id)
            wh = [
                "subject_type=$1",
                "subject_id=$2",
                "occurred_at >= $3",
                "occurred_at < $4",
                default_where,
                "actor_user_id=$5",
            ]
            params: List[Any] = [
                subject_type,
                subject_id,
                start,
                end,
                actor_user_id,
            ]

            idx = 6
            if target_model_id:
                wh.append(f"target_model_id=${idx}")
                params.append(target_model_id)

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
            seq = []
            if base:
                seq.append(base)
            seq.extend(within)

            for seq_index, row in enumerate(seq):
                cid = row["condition_id"]
                st = row["occurred_at"]
                nxt = (
                    seq[seq_index + 1]["occurred_at"]
                    if seq_index + 1 < len(seq)
                    else None
                )
                payload = row["payload"] or {}
                label = payload.get("label") or payload.get("phase") or cid
                phases.append({
                    "condition_id": cid,
                    "label": label,
                    "start_ts": st.isoformat(),
                    "end_ts": nxt.isoformat() if nxt else None,
                })

            return JSONResponse(
                {
                    "metric_key": metric_key,
                    "subject": {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                    },
                    "points": points,
                    "phases": phases,
                },
                headers=TELEMETRY_NO_STORE_HEADERS,
            )

    finally:
        await conn.close()


@router.get("/metrics/voice-slo")
async def voice_slo(
    req: Request,
    window_days: int = Query(30, ge=1, le=30),
):
    try:
        actor_user_id = _require_actor(req)
    except ValueError as exc:
        status = 401 if str(exc) == "missing_actor_user_id" else 400
        return JSONResponse(
            {"error": str(exc)},
            status_code=status,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    conn = await _connect()
    try:
        async with conn.transaction():
            await _set_actor(conn, actor_user_id)
            row = await conn.fetchrow(
                """
                WITH voice AS (
                  SELECT
                    payload->>'status' AS status,
                    payload->>'failure_stage' AS failure_stage,
                    CASE
                      WHEN payload->>'transcription_ms' ~ '^[0-9]+$'
                      THEN (payload->>'transcription_ms')::double precision
                    END AS transcription_ms,
                    CASE
                      WHEN payload->>'response_ms' ~ '^[0-9]+$'
                      THEN (payload->>'response_ms')::double precision
                    END AS response_ms,
                    CASE
                      WHEN payload->>'tts_first_audio_ms' ~ '^[0-9]+$'
                      THEN (payload->>'tts_first_audio_ms')::double precision
                    END AS tts_first_audio_ms,
                    CASE
                      WHEN payload->>'speech_to_first_audio_ms' ~ '^[0-9]+$'
                       AND payload->>'speech_ms' ~ '^[0-9]+$'
                      THEN greatest(
                        (payload->>'speech_to_first_audio_ms')::double precision
                        - (payload->>'speech_ms')::double precision,
                        0
                      )
                    END AS end_of_speech_to_first_audio_ms
                  FROM public.telemetry_event
                  WHERE event_type='voice.turn.trace'
                    AND actor_user_id=$1
                    AND occurred_at >=
                      clock_timestamp()-make_interval(days => $2)
                )
                SELECT
                  count(*)::bigint AS total,
                  count(*) FILTER (
                    WHERE status='completed'
                  )::bigint AS completed,
                  count(*) FILTER (
                    WHERE status='failed'
                  )::bigint AS failed,
                  count(*) FILTER (
                    WHERE status='cancelled'
                  )::bigint AS cancelled,
                  count(*) FILTER (
                    WHERE failure_stage='transcription'
                  )::bigint AS transcription_failures,
                  count(*) FILTER (
                    WHERE failure_stage='response'
                  )::bigint AS response_failures,
                  count(*) FILTER (
                    WHERE failure_stage='tts'
                  )::bigint AS tts_failures,
                  percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY transcription_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND transcription_ms IS NOT NULL
                  ) AS transcription_ms_p50,
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY transcription_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND transcription_ms IS NOT NULL
                  ) AS transcription_ms_p95,
                  percentile_cont(0.99) WITHIN GROUP (
                    ORDER BY transcription_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND transcription_ms IS NOT NULL
                  ) AS transcription_ms_p99,
                  percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY response_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND response_ms IS NOT NULL
                  ) AS response_ms_p50,
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY response_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND response_ms IS NOT NULL
                  ) AS response_ms_p95,
                  percentile_cont(0.99) WITHIN GROUP (
                    ORDER BY response_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND response_ms IS NOT NULL
                  ) AS response_ms_p99,
                  percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY tts_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND tts_first_audio_ms IS NOT NULL
                  ) AS tts_first_audio_ms_p50,
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY tts_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND tts_first_audio_ms IS NOT NULL
                  ) AS tts_first_audio_ms_p95,
                  percentile_cont(0.99) WITHIN GROUP (
                    ORDER BY tts_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND tts_first_audio_ms IS NOT NULL
                  ) AS tts_first_audio_ms_p99,
                  percentile_cont(0.50) WITHIN GROUP (
                    ORDER BY end_of_speech_to_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND end_of_speech_to_first_audio_ms IS NOT NULL
                  ) AS end_of_speech_to_first_audio_ms_p50,
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY end_of_speech_to_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND end_of_speech_to_first_audio_ms IS NOT NULL
                  ) AS end_of_speech_to_first_audio_ms_p95,
                  percentile_cont(0.99) WITHIN GROUP (
                    ORDER BY end_of_speech_to_first_audio_ms
                  ) FILTER (
                    WHERE status='completed'
                      AND end_of_speech_to_first_audio_ms IS NOT NULL
                  ) AS end_of_speech_to_first_audio_ms_p99
                FROM voice
                """,
                actor_user_id,
                window_days,
            )
    finally:
        await conn.close()

    return JSONResponse(
        _voice_slo_payload(row, window_days),
        headers=TELEMETRY_NO_STORE_HEADERS,
    )
