from __future__ import annotations

from typing import Any, Dict, List, Optional
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse

from seebx.capabilities.observability.telemetry_contracts import (
    TelemetryEventRecordV1,
    TelemetryRepository,
)

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




def _require_actor(req: Request) -> str:
    raw = (req.headers.get("x-vs-actor-user-id") or "").strip()
    if not raw:
        raise ValueError("missing_actor_user_id")
    try:
        return str(uuid.UUID(raw))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid_actor_user_id") from exc




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


def _iso_utc_or_none(value: Any) -> str | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_text(row: Any, key: str) -> str | None:
    value = row[key]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    latest_status = _optional_text(row, "latest_status")
    current_status = (
        "pass"
        if latest_status == "completed"
        else "fail"
        if latest_status == "failed"
        else "insufficient_data"
    )

    return {
        "contract_version": VOICE_SLO_CONTRACT_VERSION,
        "window_days": window_days,
        "minimum_samples": VOICE_SLO_MINIMUM_SAMPLES,
        "overall_status": overall_status,
        "latest_sample_at": _iso_utc_or_none(row["latest_sample_at"]),
        "current": {
            "status": current_status,
            "consecutive_successes": int(
                row["consecutive_successes"] or 0
            ),
            "latest_failure_at": _iso_utc_or_none(
                row["latest_failure_at"]
            ),
            "latest_failure_stage": _optional_text(
                row,
                "latest_failure_stage",
            ),
            "latest_failure_code": _optional_text(
                row,
                "latest_failure_code",
            ),
        },
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

async def execute_telemetry_event(
    req: Request,
    *,
    repository: TelemetryRepository,
):
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
    events = (body or {}).get("events")
    if not isinstance(events, list) or not events:
        return JSONResponse(
            {"accepted": 0, "rejected": 0, "errors": [{"reason": "missing events[]"}]},
            status_code=400,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )

    request_id = getattr(req.state, "request_id", None)
    records: list[TelemetryEventRecordV1] = []
    errors: List[Dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append({"index": index, "reason": "event not object"})
            continue
        event_id = _parse_uuid(event.get("event_id"))
        if not event_id:
            errors.append(
                {"index": index, "reason": "invalid/missing event_id (uuid)"}
            )
            continue
        event_type = str(event.get("event_type") or "").strip()
        subject_type = str(event.get("subject_type") or "").strip()
        subject_id = str(event.get("subject_id") or "").strip()
        if not event_type or not subject_type or not subject_id:
            errors.append(
                {
                    "index": index,
                    "reason": "missing event_type/subject_type/subject_id",
                }
            )
            continue
        occurred_at = (
            _parse_ts(event.get("occurred_at"))
            or _parse_ts(event.get("created_at"))
            or datetime.now(timezone.utc)
        )
        payload = event.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        if request_id and "request_id" not in payload:
            payload["request_id"] = str(request_id)
        records.append(
            TelemetryEventRecordV1(
                event_id=event_id,
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                target_model_id=event.get("target_model_id") or None,
                target_model_version=event.get("target_model_version") or None,
                judge_model_id=event.get("judge_model_id") or None,
                judge_model_version=event.get("judge_model_version") or None,
                condition_id=event.get("condition_id") or None,
                thread_id=event.get("thread_id") or None,
                turn_id=event.get("turn_id") or None,
                actor_user_id=actor_user_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )
    await repository.write_events(
        actor_user_id=actor_user_id,
        events=tuple(records),
    )
    return JSONResponse(
        {
            "accepted": len(records),
            "rejected": len(errors),
            "errors": errors,
        },
        headers=TELEMETRY_NO_STORE_HEADERS,
    )


async def execute_metrics_timeseries(
    req: Request,
    metric_key: str,
    subject_type: str,
    subject_id: str,
    from_ts: str,
    to_ts: str,
    bucket: str,
    target_model_id: Optional[str],
    *,
    repository: TelemetryRepository,
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
        snapshot = await repository.read_timeseries(
            actor_user_id=actor_user_id,
            metric_key=metric_key,
            subject_type=subject_type,
            subject_id=subject_id,
            start=start,
            end=end,
            bucket=bucket,
            target_model_id=target_model_id,
        )
    except KeyError:
        return JSONResponse(
            {"error": f"unknown metric_key '{metric_key}'"},
            status_code=400,
            headers=TELEMETRY_NO_STORE_HEADERS,
        )
    points = [
        {
            "t": row["t"].isoformat(),
            "v": float(row["v"]) if row["v"] is not None else None,
            "n": int(row["n"]) if row["n"] is not None else 0,
            "meta": {"method": "v0_jsonb_expr"},
        }
        for row in snapshot.point_rows
    ]
    phases = []
    conditions = snapshot.condition_rows
    for index, row in enumerate(conditions):
        condition_id = row["condition_id"]
        started_at = row["occurred_at"]
        next_at = (
            conditions[index + 1]["occurred_at"]
            if index + 1 < len(conditions)
            else None
        )
        payload = row["payload"] or {}
        phases.append(
            {
                "condition_id": condition_id,
                "label": payload.get("label") or payload.get("phase") or condition_id,
                "start_ts": started_at.isoformat(),
                "end_ts": next_at.isoformat() if next_at else None,
            }
        )
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


async def execute_voice_slo(
    req: Request,
    window_days: int,
    *,
    repository: TelemetryRepository,
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
    row = await repository.read_voice_slo(
        actor_user_id=actor_user_id,
        window_days=window_days,
    )
    return JSONResponse(
        _voice_slo_payload(row, window_days),
        headers=TELEMETRY_NO_STORE_HEADERS,
    )


def create_telemetry_router(repository: TelemetryRepository) -> APIRouter:
    if repository is None:
        raise ValueError("telemetry_repository_required")
    router = APIRouter()

    @router.post("/telemetry/event")
    async def telemetry_event(req: Request):
        """Write-only telemetry sink. Idempotent by event_id."""
        return await execute_telemetry_event(req, repository=repository)

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
        return await execute_metrics_timeseries(
            req,
            metric_key,
            subject_type,
            subject_id,
            from_ts,
            to_ts,
            bucket,
            target_model_id,
            repository=repository,
        )

    @router.get("/metrics/voice-slo")
    async def voice_slo(
        req: Request,
        window_days: int = Query(30, ge=1, le=30),
    ):
        return await execute_voice_slo(
            req,
            window_days,
            repository=repository,
        )

    return router


__all__ = [
    "TELEMETRY_NO_STORE_HEADERS",
    "create_telemetry_router",
    "execute_metrics_timeseries",
    "execute_telemetry_event",
    "execute_voice_slo",
]
