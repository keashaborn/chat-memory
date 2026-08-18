from __future__ import annotations

"""OpenAI HTTP adapter for governed voice transcription."""

import os
from dataclasses import dataclass
from typing import Any

import httpx

from seebx.adapters.openai_chat import safety_identifier_v1
from seebx.contracts.voice_language import (
    AUTO_VOICE_LANGUAGE,
    transcription_prompt,
)


OPENAI_TRANSCRIPTION_URL = (
    os.getenv("OPENAI_TRANSCRIPTION_URL")
    or "https://api.openai.com/v1/audio/transcriptions"
).strip()
ALLOWED_TRANSCRIPTION_MODELS = {
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
}
_configured_model = (os.getenv("OPENAI_TRANSCRIPTION_MODEL") or "").strip()
DEFAULT_TRANSCRIPTION_MODEL = (
    _configured_model
    if _configured_model in ALLOWED_TRANSCRIPTION_MODELS
    else "gpt-4o-transcribe"
)


class OpenAITranscriptionConfigurationError(RuntimeError):
    pass


class OpenAITranscriptionInvalidResponseError(RuntimeError):
    pass


class OpenAITranscriptionTimeoutError(RuntimeError):
    pass


class OpenAITranscriptionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenAITranscriptionResult:
    status_code: int
    provider_request_id: str | None
    model: str
    payload: dict[str, Any] | None


async def transcribe_audio_with_openai(
    *,
    owner_user_id: str,
    filename: str,
    raw_audio: bytes,
    content_type: str,
    language: str,
) -> OpenAITranscriptionResult:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise OpenAITranscriptionConfigurationError("missing_openai_key")

    data = {
        "model": DEFAULT_TRANSCRIPTION_MODEL,
        "response_format": "json",
        "temperature": "0",
        "include[]": "logprobs",
        "prompt": transcription_prompt(language),
    }
    if language != AUTO_VOICE_LANGUAGE:
        data["language"] = language

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        ) as client:
            upstream = await client.post(
                OPENAI_TRANSCRIPTION_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": safety_identifier_v1(
                        owner_user_id
                    ),
                },
                files={"file": (filename, raw_audio, content_type)},
                data=data,
            )
    except httpx.TimeoutException as exc:
        raise OpenAITranscriptionTimeoutError(
            "openai_transcription_timeout"
        ) from exc
    except Exception as exc:
        raise OpenAITranscriptionUnavailableError(
            "openai_transcription_unreachable"
        ) from exc

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )
    payload: dict[str, Any] | None = None
    if upstream.status_code < 400:
        try:
            raw_payload = upstream.json()
            if not isinstance(raw_payload, dict):
                raise TypeError("transcription response is not an object")
            payload = raw_payload
        except Exception as exc:
            raise OpenAITranscriptionInvalidResponseError(
                "invalid_openai_transcription_response"
            ) from exc

    return OpenAITranscriptionResult(
        status_code=upstream.status_code,
        provider_request_id=provider_request_id,
        model=DEFAULT_TRANSCRIPTION_MODEL,
        payload=payload,
    )


__all__ = [
    "OpenAITranscriptionConfigurationError",
    "OpenAITranscriptionInvalidResponseError",
    "OpenAITranscriptionResult",
    "OpenAITranscriptionTimeoutError",
    "OpenAITranscriptionUnavailableError",
    "transcribe_audio_with_openai",
]
