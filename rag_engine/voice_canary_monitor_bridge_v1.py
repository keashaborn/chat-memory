from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any


CANARY_CONTRACT_VERSION = "voice_synthetic_canary_v1_4"
CANARY_TRACE_CONTRACT_VERSION = "voice_turn_trace_v1"
SLO_TRACE_CONTRACT_VERSION = "voice_slo_monitor_trace_v1"
MONITOR_CONTRACT_VERSION = "ai_operations_monitor_record_v1"
CANARY_MONITOR_NAME = "voice_synthetic_canary"
SLO_MONITOR_NAME = "voice_slo"
SLO_WINDOW_DAYS = 7
SLO_WINDOW_HOURS = SLO_WINDOW_DAYS * 24
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SLO_CHECKS = frozenset(
    {
        "turn_success_rate",
        "transcription_ms_p95",
        "response_ms_p95",
        "tts_first_audio_ms_p95",
        "end_of_speech_to_first_audio_ms_p95",
    }
)


@dataclass(frozen=True)
class VoiceMonitorObservationV1:
    monitor_name: str
    status: str
    severity: str
    reason_codes: tuple[str, ...]
    window_hours: int
    request_count: int
    completed_count: int
    dependency_failure_count: int
    observed_at: datetime


def _bounded_count(raw: Any, field: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"invalid_voice_slo_{field}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_voice_slo_{field}") from exc
    if value < 0 or value > 1_000_000_000:
        raise ValueError(f"invalid_voice_slo_{field}")
    return value


def _parse_canary_observation(
    payload: dict[str, Any],
    occurred_at: datetime,
) -> VoiceMonitorObservationV1:
    if (
        payload.get("contract_version") != CANARY_TRACE_CONTRACT_VERSION
        or payload.get("canary_contract_version")
        != CANARY_CONTRACT_VERSION
    ):
        raise ValueError("invalid_voice_canary_contract")

    status = str(payload.get("status") or "").strip()
    failure_stage = str(payload.get("failure_stage") or "").strip()
    failure_code = str(payload.get("failure_code") or "").strip()
    if status == "completed":
        if failure_stage not in {"", "none"} or failure_code:
            raise ValueError("invalid_voice_canary_pass")
        return VoiceMonitorObservationV1(
            monitor_name=CANARY_MONITOR_NAME,
            status="pass",
            severity="info",
            reason_codes=(),
            window_hours=1,
            request_count=1,
            completed_count=1,
            dependency_failure_count=0,
            observed_at=occurred_at,
        )
    if status != "failed":
        raise ValueError("invalid_voice_canary_status")
    if not _STAGE_PATTERN.fullmatch(failure_stage):
        raise ValueError("invalid_voice_canary_failure_stage")
    if not _CODE_PATTERN.fullmatch(failure_code):
        raise ValueError("invalid_voice_canary_failure_code")
    return VoiceMonitorObservationV1(
        monitor_name=CANARY_MONITOR_NAME,
        status="unavailable",
        severity="critical",
        reason_codes=(failure_code,),
        window_hours=1,
        request_count=1,
        completed_count=0,
        dependency_failure_count=0,
        observed_at=occurred_at,
    )


def _parse_slo_observation(
    payload: dict[str, Any],
    occurred_at: datetime,
) -> VoiceMonitorObservationV1 | None:
    if (
        payload.get("contract_version") != SLO_TRACE_CONTRACT_VERSION
        or payload.get("canary_contract_version")
        != CANARY_CONTRACT_VERSION
        or payload.get("window_days") != SLO_WINDOW_DAYS
    ):
        raise ValueError("invalid_voice_slo_contract")

    overall_status = str(payload.get("overall_status") or "").strip()
    if overall_status not in {
        "pass",
        "fail",
        "insufficient_data",
        "unavailable",
    }:
        raise ValueError("invalid_voice_slo_status")

    sample = payload.get("sample")
    if not isinstance(sample, dict):
        raise ValueError("invalid_voice_slo_sample")
    evaluated = _bounded_count(sample.get("evaluated_turns", 0), "total")
    completed = _bounded_count(sample.get("completed", 0), "completed")
    failed = _bounded_count(sample.get("failed", 0), "failed")
    if evaluated != completed + failed:
        raise ValueError("invalid_voice_slo_sample")

    failed_checks = payload.get("failed_checks")
    if not isinstance(failed_checks, list):
        raise ValueError("invalid_voice_slo_checks")
    if any(not isinstance(check, str) for check in failed_checks):
        raise ValueError("invalid_voice_slo_checks")
    normalized_checks = tuple(sorted(set(failed_checks)))
    if any(check not in _SLO_CHECKS for check in normalized_checks):
        raise ValueError("invalid_voice_slo_checks")

    if overall_status == "insufficient_data":
        if normalized_checks:
            raise ValueError("invalid_voice_slo_checks")
        return None
    if overall_status == "unavailable":
        if evaluated or normalized_checks:
            raise ValueError("invalid_voice_slo_unavailable")
        return VoiceMonitorObservationV1(
            monitor_name=SLO_MONITOR_NAME,
            status="unavailable",
            severity="critical",
            reason_codes=("voice_slo_query_failed",),
            window_hours=SLO_WINDOW_HOURS,
            request_count=0,
            completed_count=0,
            dependency_failure_count=1,
            observed_at=occurred_at,
        )
    if overall_status == "pass":
        if normalized_checks:
            raise ValueError("invalid_voice_slo_pass")
        return VoiceMonitorObservationV1(
            monitor_name=SLO_MONITOR_NAME,
            status="pass",
            severity="info",
            reason_codes=(),
            window_hours=SLO_WINDOW_HOURS,
            request_count=evaluated,
            completed_count=completed,
            dependency_failure_count=0,
            observed_at=occurred_at,
        )
    if not normalized_checks:
        normalized_checks = ("threshold_failed",)
    return VoiceMonitorObservationV1(
        monitor_name=SLO_MONITOR_NAME,
        status="violated",
        severity="critical",
        reason_codes=tuple(
            f"voice_slo_{check}" for check in normalized_checks
        ),
        window_hours=SLO_WINDOW_HOURS,
        request_count=evaluated,
        completed_count=completed,
        dependency_failure_count=0,
        observed_at=occurred_at,
    )


def parse_voice_monitor_observation_v1(
    *,
    event_type: str,
    subject_type: str,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> VoiceMonitorObservationV1 | None:
    if payload.get("synthetic") is not True:
        return None
    if event_type == "voice.turn.trace" and subject_type == "voice_turn":
        return _parse_canary_observation(payload, occurred_at)
    if event_type == "voice.slo.observation" and subject_type == "voice_slo":
        return _parse_slo_observation(payload, occurred_at)
    return None


async def record_voice_monitor_observation_v1(
    connection: Any,
    observation: VoiceMonitorObservationV1,
) -> dict[str, Any]:
    result = await connection.fetchval(
        """
        SELECT ai_operations.record_monitor_observation_v1(
            $1,$2,$3,$4,$5::text[],$6,$7,$8,$9,$10,$11,$12,$13
        )
        """,
        observation.monitor_name,
        observation.status,
        observation.severity,
        False,
        list(observation.reason_codes),
        observation.window_hours,
        observation.request_count,
        observation.completed_count,
        0,
        0,
        observation.dependency_failure_count,
        0.0,
        observation.observed_at,
    )
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise RuntimeError("invalid_voice_monitor_result")
    if result.get("contract_version") != MONITOR_CONTRACT_VERSION:
        raise RuntimeError("invalid_voice_monitor_contract")
    return result
