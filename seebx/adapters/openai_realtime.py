from __future__ import annotations

"""OpenAI HTTP negotiation adapter for realtime voice preview."""

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from seebx.adapters.openai_chat import safety_identifier_v1
from seebx.contracts.voice_language import AUTO_VOICE_LANGUAGE


OPENAI_REALTIME_CALLS_URL = (
    os.getenv("OPENAI_REALTIME_CALLS_URL")
    or "https://api.openai.com/v1/realtime/calls"
).strip()
REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
REALTIME_TRANSCRIPTION_DELAY = "low"
MAX_SDP_RESPONSE_BYTES = 256 * 1024
_CALL_ID_RE = re.compile(r"^rtc_[A-Za-z0-9_-]{1,120}$")


class OpenAIRealtimeTimeoutError(RuntimeError):
    pass


class OpenAIRealtimeUnavailableError(RuntimeError):
    pass


class OpenAIRealtimeUpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        provider_request_id: str | None,
    ) -> None:
        super().__init__("openai_realtime_error")
        self.status_code = status_code
        self.provider_request_id = provider_request_id


class OpenAIRealtimeProtocolError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class OpenAIRealtimeCall:
    answer_sdp: str
    call_id: str
    provider_request_id: str | None


def _call_id_from_location(raw: str | None) -> str:
    location = str(raw or "").strip()
    call_id = location.rstrip("/").rsplit("/", 1)[-1]
    if not _CALL_ID_RE.fullmatch(call_id):
        raise OpenAIRealtimeProtocolError(
            "invalid_openai_realtime_call_id"
        )
    return call_id


def _transcription_session_config(language: str) -> dict[str, Any]:
    transcription: dict[str, Any] = {
        "model": REALTIME_TRANSCRIPTION_MODEL,
        "delay": REALTIME_TRANSCRIPTION_DELAY,
    }
    if language != AUTO_VOICE_LANGUAGE:
        transcription["language"] = language
    return {
        "type": "transcription",
        "audio": {
            "input": {
                "transcription": transcription,
                "turn_detection": None,
            },
        },
    }


async def create_transcription_call(
    *,
    owner_user_id: str,
    offer_sdp: str,
    language: str,
    api_key: str,
) -> OpenAIRealtimeCall:
    session_config = _transcription_session_config(language)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0),
        ) as client:
            upstream = await client.post(
                OPENAI_REALTIME_CALLS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier_v1(
                        owner_user_id
                    ),
                },
                files={
                    "sdp": (None, offer_sdp, "application/sdp"),
                    "session": (
                        None,
                        json.dumps(
                            session_config,
                            separators=(",", ":"),
                        ),
                        "application/json",
                    ),
                },
            )
    except httpx.TimeoutException as exc:
        raise OpenAIRealtimeTimeoutError(
            "openai_realtime_timeout"
        ) from exc
    except Exception as exc:
        raise OpenAIRealtimeUnavailableError(
            "openai_realtime_unreachable"
        ) from exc

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )
    if upstream.status_code >= 400:
        raise OpenAIRealtimeUpstreamError(
            status_code=upstream.status_code,
            provider_request_id=provider_request_id,
        )

    answer_sdp = upstream.text
    if not answer_sdp.strip().startswith("v=0"):
        raise OpenAIRealtimeProtocolError(
            "invalid_openai_realtime_sdp"
        )
    if len(answer_sdp.encode("utf-8")) > MAX_SDP_RESPONSE_BYTES:
        raise OpenAIRealtimeProtocolError(
            "openai_realtime_sdp_too_large"
        )

    return OpenAIRealtimeCall(
        answer_sdp=answer_sdp,
        call_id=_call_id_from_location(upstream.headers.get("location")),
        provider_request_id=provider_request_id,
    )


__all__ = [
    "OpenAIRealtimeCall",
    "OpenAIRealtimeProtocolError",
    "OpenAIRealtimeTimeoutError",
    "OpenAIRealtimeUnavailableError",
    "OpenAIRealtimeUpstreamError",
    "create_transcription_call",
]
