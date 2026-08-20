from __future__ import annotations

"""Privacy-safe synthetic probe for the governed voice response path."""

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from seebx.contracts.conversation_provenance import (
    CANONICAL_CONVERSATION_RESPONSE_RUNTIME_V1,
)


CONTRACT_VERSION = "voice_synthetic_canary_v1_4"
TRACE_CONTRACT_VERSION = "voice_turn_trace_v1"
VOICE_SESSION_HEADER = "x-vs-voice-session-id"
SPEECH_TO_FIRST_AUDIO_BASIS = "synthetic_turn_start_v1"
SYNTHETIC_PHRASE = "Operational voice canary."
SYNTHETIC_EXPECTED_WORDS = frozenset({"operational", "voice", "canary"})
MAX_RESPONSE_TTS_CHARACTERS = 1_000
MAX_AUDIO_BYTES = 8 * 1024 * 1024
PCM_SAMPLE_RATE = 24_000
PCM_SAMPLE_WIDTH = 2
NO_STORE = "no-store"


class CanaryFailure(RuntimeError):
    def __init__(self, stage: str, code: str) -> None:
        super().__init__(code)
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class CanaryConfig:
    base_url: str
    actor_user_id: str
    service_token: str
    timeout_seconds: float = 90.0
    alert_webhook_url: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1_000))


def _validate_config(config: CanaryConfig) -> CanaryConfig:
    try:
        actor = uuid.UUID(config.actor_user_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise CanaryFailure("configuration", "invalid_canary_actor") from exc
    if actor.int == 0:
        raise CanaryFailure("configuration", "invalid_canary_actor")
    if not config.service_token.strip():
        raise CanaryFailure("configuration", "missing_service_token")
    if not config.base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise CanaryFailure("configuration", "canary_base_url_must_be_local")
    return CanaryConfig(
        base_url=config.base_url.rstrip("/"),
        actor_user_id=str(actor),
        service_token=config.service_token.strip(),
        timeout_seconds=max(5.0, min(float(config.timeout_seconds), 180.0)),
        alert_webhook_url=config.alert_webhook_url.strip(),
    )


def _headers(
    config: CanaryConfig,
    voice_turn_id: str,
    voice_session_id: str,
) -> dict[str, str]:
    return {
        "x-vs-service-token": config.service_token,
        "x-vs-actor-user-id": config.actor_user_id,
        "x-vs-owner-user-id": config.actor_user_id,
        "x-vs-voice-turn-id": voice_turn_id,
        VOICE_SESSION_HEADER: voice_session_id,
        "x-request-id": f"voice-canary-{voice_turn_id}",
    }


def _require_no_store(response: httpx.Response, stage: str) -> None:
    cache_control = response.headers.get("cache-control", "").lower()
    if NO_STORE not in cache_control:
        raise CanaryFailure(stage, "missing_no_store_header")


def _require_success(response: httpx.Response, stage: str) -> None:
    if response.status_code < 200 or response.status_code >= 300:
        raise CanaryFailure(stage, f"upstream_http_{response.status_code}")
    _require_no_store(response, stage)


async def _voice_session_command(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    voice_session_id: str,
    action: str,
) -> None:
    response = await client.post(
        f"/voice/session/{action}",
        headers=headers,
        json={"session_id": voice_session_id},
    )
    _require_success(response, "session")
    body = response.json()
    if body.get("session_id") != voice_session_id:
        raise CanaryFailure("session", "voice_session_identity_mismatch")
    expected_flag = {
        "acquire": "acquired",
        "heartbeat": "renewed",
        "release": "released",
    }[action]
    if body.get(expected_flag) is not True:
        raise CanaryFailure(
            "session",
            f"voice_session_{action}_failed",
        )


def _pcm_to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(PCM_SAMPLE_WIDTH)
        wav.setframerate(PCM_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def _pcm_duration_ms(pcm: bytes) -> int:
    bytes_per_second = PCM_SAMPLE_RATE * PCM_SAMPLE_WIDTH
    return max(1, round(len(pcm) / bytes_per_second * 1_000))


def _bounded_tts_text(answer: str) -> str:
    clean = " ".join(str(answer or "").split())
    if not clean:
        raise CanaryFailure("response", "empty_governed_answer")
    if len(clean) <= MAX_RESPONSE_TTS_CHARACTERS:
        return clean
    clipped = clean[:MAX_RESPONSE_TTS_CHARACTERS]
    return clipped.rsplit(" ", 1)[0] or clipped


def _synthetic_transcript_matches(transcript: str) -> bool:
    words = set(re.findall(r"[a-z]+", str(transcript or "").lower()))
    return len(words & SYNTHETIC_EXPECTED_WORDS) >= 2


async def _stream_tts(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    text: str,
    stage: str,
) -> tuple[bytes, int, int]:
    started = time.perf_counter()
    first_audio_ms: int | None = None
    chunks: list[bytes] = []
    total_bytes = 0
    async with client.stream(
        "POST",
        "/voice/tts",
        headers=headers,
        json={
            "text": text,
            "voice": "marin",
            "conversation_style": "direct",
        },
    ) as response:
        _require_success(response, stage)
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if first_audio_ms is None:
                first_audio_ms = _elapsed_ms(started)
            total_bytes += len(chunk)
            if total_bytes > MAX_AUDIO_BYTES:
                raise CanaryFailure(stage, "audio_response_too_large")
            chunks.append(chunk)
    if first_audio_ms is None:
        raise CanaryFailure(stage, "empty_audio_response")
    return b"".join(chunks), first_audio_ms, _elapsed_ms(started)


async def _post_trace(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    voice_turn_id: str,
    status: str,
    failure_stage: str,
    failure_code: str,
    metrics: dict[str, Any],
) -> None:
    payload = {
        "contract_version": TRACE_CONTRACT_VERSION,
        "synthetic": True,
        "canary_contract_version": CONTRACT_VERSION,
        "status": status,
        "failure_stage": failure_stage,
        "failure_code": failure_code or None,
        **metrics,
    }
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "voice.turn.trace",
        "subject_type": "voice_turn",
        "subject_id": voice_turn_id,
        "thread_id": None,
        "turn_id": voice_turn_id,
        "target_model_id": metrics.get("tts_model"),
        "payload": payload,
        "occurred_at": _now_iso(),
    }
    response = await client.post(
        "/telemetry/event",
        headers=headers,
        json={"events": [event]},
    )
    _require_success(response, "telemetry")
    body = response.json()
    if body.get("accepted") != 1 or body.get("rejected") != 0:
        raise CanaryFailure("telemetry", "telemetry_event_rejected")


async def _get_slo(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.get(
        "/metrics/voice-slo",
        headers=headers,
        params={"window_days": 30},
    )
    _require_success(response, "slo")
    payload = response.json()
    if payload.get("contract_version") != "voice_slo_v1":
        raise CanaryFailure("slo", "invalid_slo_contract")
    return payload


def _safe_slo_summary(payload: dict[str, Any]) -> dict[str, Any]:
    sample = payload.get("sample") if isinstance(payload.get("sample"), dict) else {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    return {
        "overall_status": payload.get("overall_status"),
        "sample": {
            "evaluated_turns": sample.get("evaluated_turns"),
            "completed": sample.get("completed"),
            "failed": sample.get("failed"),
        },
        "checks": {
            key: value.get("status")
            for key, value in checks.items()
            if isinstance(value, dict)
        },
    }


async def _send_alert(
    config: CanaryConfig,
    *,
    stage: str,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if not config.alert_webhook_url:
        return "unconfigured"
    payload = {
        "text": "Verbal Sage governed voice canary failed",
        "contract_version": CONTRACT_VERSION,
        "stage": stage,
        "code": code,
        "occurred_at": _now_iso(),
    }
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            transport=transport,
        ) as client:
            response = await client.post(config.alert_webhook_url, json=payload)
            if 200 <= response.status_code < 300:
                return "delivered"
            return f"http_{response.status_code}"
    except Exception:
        return "delivery_failed"


async def run_canary(
    config: CanaryConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    alert_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], int]:
    config = _validate_config(config)
    voice_turn_id = str(uuid.uuid4())
    voice_session_id = str(uuid.uuid4())
    headers = _headers(config, voice_turn_id, voice_session_id)
    metrics: dict[str, Any] = {
        "speech_ms": None,
        "audio_bytes": None,
        "transcription_ms": None,
        "response_ms": None,
        "tts_first_audio_ms": None,
        "speech_to_first_audio_ms": None,
        "speech_to_first_audio_basis": SPEECH_TO_FIRST_AUDIO_BASIS,
        "tts_total_ms": None,
        "total_turn_ms": None,
        "tts_segment_count": 0,
        "transcription_provider": None,
        "transcription_model": None,
        "transcription_language": None,
        "tts_provider": "openai",
        "tts_model": "gpt-4o-mini-tts",
        "tts_voice": "marin",
    }
    started = time.perf_counter()
    status = "failed"
    failure_stage = "configuration"
    failure_code = "unknown_failure"
    slo_payload: dict[str, Any] | None = None
    telemetry_recorded = False
    session_acquired = False

    async with httpx.AsyncClient(
        base_url=config.base_url,
        timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),
        transport=transport,
    ) as client:
        try:
            await _voice_session_command(
                client,
                headers=headers,
                voice_session_id=voice_session_id,
                action="acquire",
            )
            session_acquired = True
            seed_pcm, _, _ = await _stream_tts(
                client,
                headers=headers,
                text=SYNTHETIC_PHRASE,
                stage="seed_tts",
            )
            metrics["speech_ms"] = _pcm_duration_ms(seed_pcm)
            wav_audio = _pcm_to_wav(seed_pcm)
            metrics["audio_bytes"] = len(wav_audio)

            await _voice_session_command(
                client,
                headers=headers,
                voice_session_id=voice_session_id,
                action="heartbeat",
            )
            transcription_started = time.perf_counter()
            transcription = await client.post(
                "/voice/openai/transcribe",
                headers={**headers, "content-type": "audio/wav"},
                content=wav_audio,
            )
            metrics["transcription_ms"] = _elapsed_ms(transcription_started)
            _require_success(transcription, "transcription")
            transcription_body = transcription.json()
            transcript = str(transcription_body.get("transcript") or "").strip()
            if not transcript:
                raise CanaryFailure("transcription", "empty_transcript")
            if not _synthetic_transcript_matches(transcript):
                raise CanaryFailure(
                    "transcription",
                    "synthetic_phrase_mismatch",
                )
            metrics["transcription_provider"] = transcription_body.get("provider")
            metrics["transcription_model"] = transcription_body.get("model")
            metrics["transcription_language"] = transcription_body.get("language")

            await _voice_session_command(
                client,
                headers=headers,
                voice_session_id=voice_session_id,
                action="heartbeat",
            )
            response_started = time.perf_counter()
            governed = await client.post(
                "/response/query",
                headers=headers,
                json={
                    "user_id": config.actor_user_id,
                    "message": transcript,
                    "no_store": True,
                    "include_inspection": False,
                },
            )
            metrics["response_ms"] = _elapsed_ms(response_started)
            _require_success(governed, "response")
            governed_body = governed.json()
            if (
                governed_body.get("runtime")
                != CANONICAL_CONVERSATION_RESPONSE_RUNTIME_V1
            ):
                raise CanaryFailure("response", "unexpected_response_runtime")
            answer = _bounded_tts_text(governed_body.get("answer"))

            await _voice_session_command(
                client,
                headers=headers,
                voice_session_id=voice_session_id,
                action="heartbeat",
            )
            _, first_audio_ms, tts_total_ms = await _stream_tts(
                client,
                headers=headers,
                text=answer,
                stage="tts",
            )
            metrics["tts_first_audio_ms"] = first_audio_ms
            metrics["tts_total_ms"] = tts_total_ms
            metrics["tts_segment_count"] = 1
            metrics["speech_to_first_audio_ms"] = sum(
                int(metrics[key] or 0)
                for key in (
                    "speech_ms",
                    "transcription_ms",
                    "response_ms",
                    "tts_first_audio_ms",
                )
            )
            metrics["total_turn_ms"] = _elapsed_ms(started)
            status = "completed"
            failure_stage = "none"
            failure_code = ""
        except CanaryFailure as exc:
            failure_stage = (
                "tts" if exc.stage in {"seed_tts", "tts"} else exc.stage
            )
            failure_code = exc.code
            metrics["total_turn_ms"] = _elapsed_ms(started)
        except Exception:
            failure_stage = "internal"
            failure_code = "unexpected_internal_error"
            metrics["total_turn_ms"] = _elapsed_ms(started)
        finally:
            if session_acquired:
                try:
                    await _voice_session_command(
                        client,
                        headers=headers,
                        voice_session_id=voice_session_id,
                        action="release",
                    )
                except CanaryFailure as exc:
                    if status == "completed":
                        status = "failed"
                        failure_stage = exc.stage
                        failure_code = exc.code

        try:
            await _post_trace(
                client,
                headers=headers,
                voice_turn_id=voice_turn_id,
                status=status,
                failure_stage=failure_stage,
                failure_code=failure_code,
                metrics=metrics,
            )
            telemetry_recorded = True
        except CanaryFailure as exc:
            if status == "completed":
                status = "failed"
                failure_stage = exc.stage
                failure_code = exc.code

        try:
            slo_payload = await _get_slo(client, headers=headers)
        except CanaryFailure as exc:
            if status == "completed":
                status = "failed"
                failure_stage = exc.stage
                failure_code = exc.code

    overall_slo = (
        slo_payload.get("overall_status")
        if isinstance(slo_payload, dict)
        else None
    )
    if status == "completed" and overall_slo == "fail":
        status = "failed"
        failure_stage = "slo"
        failure_code = "voice_slo_threshold_failed"

    alert_delivery = "not_needed"
    if status != "completed":
        alert_delivery = await _send_alert(
            config,
            stage=failure_stage,
            code=failure_code,
            transport=alert_transport,
        )

    report = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "failure_stage": failure_stage,
        "failure_code": failure_code or None,
        "telemetry_recorded": telemetry_recorded,
        "alert_delivery": alert_delivery,
        "metrics_ms": {
            key: metrics[key]
            for key in (
                "speech_ms",
                "transcription_ms",
                "response_ms",
                "tts_first_audio_ms",
                "speech_to_first_audio_ms",
                "tts_total_ms",
                "total_turn_ms",
            )
        },
        "slo": _safe_slo_summary(slo_payload) if slo_payload else None,
        "occurred_at": _now_iso(),
    }
    return report, 0 if status == "completed" else 2


def _config_from_environment() -> CanaryConfig:
    enabled = (os.getenv("VOICE_SYNTHETIC_CANARY_ENABLED") or "").strip()
    if enabled != "authorized":
        raise CanaryFailure("configuration", "canary_not_authorized")
    raw_timeout = (os.getenv("VOICE_CANARY_TIMEOUT_SECONDS") or "90").strip()
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError as exc:
        raise CanaryFailure(
            "configuration",
            "invalid_canary_timeout",
        ) from exc
    return CanaryConfig(
        base_url=(
            os.getenv("VOICE_CANARY_BASE_URL")
            or "http://127.0.0.1:8088"
        ).strip(),
        actor_user_id=(
            os.getenv("VOICE_CANARY_ACTOR_USER_ID") or ""
        ).strip(),
        service_token=(os.getenv("VS_SERVICE_TOKEN") or "").strip(),
        timeout_seconds=timeout_seconds,
        alert_webhook_url=(
            os.getenv("VOICE_CANARY_ALERT_WEBHOOK_URL") or ""
        ).strip(),
    )


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--print-contract",
        action="store_true",
        help="Print the non-secret canary contract without making requests.",
    )
    args = parser.parse_args()
    if args.print_contract:
        print(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "no_store": True,
                    "synthetic_phrase": SYNTHETIC_PHRASE,
                    "creates_thread": False,
                    "requires_dedicated_actor": True,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        config = _config_from_environment()
        report, exit_code = await run_canary(config)
    except CanaryFailure as exc:
        report = {
            "contract_version": CONTRACT_VERSION,
            "status": "failed",
            "failure_stage": exc.stage,
            "failure_code": exc.code,
            "telemetry_recorded": False,
            "alert_delivery": "not_attempted",
            "metrics_ms": {},
            "slo": None,
            "occurred_at": _now_iso(),
        }
        exit_code = 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
