from __future__ import annotations

"""OpenAI HTTP and stream-lifetime adapter for voice synthesis."""

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


OPENAI_TTS_URL = (
    os.getenv("OPENAI_TTS_URL")
    or "https://api.openai.com/v1/audio/speech"
)
DEFAULT_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE") or "marin"


class OpenAITTSConfigurationError(RuntimeError):
    pass


class OpenAITTSTimeoutError(RuntimeError):
    pass


class OpenAITTSUnavailableError(RuntimeError):
    pass


class OpenAITTSUpstreamError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        provider_request_id: str | None,
    ) -> None:
        super().__init__("openai_tts_error")
        self.status_code = status_code
        self.provider_request_id = provider_request_id


@dataclass(slots=True)
class OpenAITTSAudioStream:
    client: httpx.AsyncClient
    upstream: httpx.Response
    provider_request_id: str | None
    model: str

    async def iter_audio(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await self.close()

    async def close(self) -> None:
        try:
            await self.upstream.aclose()
        finally:
            await self.client.aclose()


def _safety_identifier(actor_user_id: str) -> str:
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()


async def open_tts_audio_stream(
    *,
    actor_user_id: str,
    model: str,
    voice: str,
    text: str,
    speed: float,
    instructions: str,
) -> OpenAITTSAudioStream:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise OpenAITTSConfigurationError("missing_openai_key")

    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "stream_format": "audio",
        "speed": speed,
    }
    if model == "gpt-4o-mini-tts":
        payload["instructions"] = instructions

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0)
    )
    try:
        request = client.build_request(
            "POST",
            OPENAI_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "OpenAI-Safety-Identifier": _safety_identifier(
                    actor_user_id
                ),
            },
            json=payload,
        )
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise OpenAITTSTimeoutError("openai_tts_timeout") from exc
    except Exception as exc:
        await client.aclose()
        raise OpenAITTSUnavailableError("openai_tts_unreachable") from exc

    provider_request_id = (
        upstream.headers.get("x-request-id")
        or upstream.headers.get("openai-request-id")
    )
    if upstream.status_code >= 400:
        status_code = upstream.status_code
        try:
            await upstream.aread()
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()
        raise OpenAITTSUpstreamError(
            status_code=status_code,
            provider_request_id=provider_request_id,
        )

    return OpenAITTSAudioStream(
        client=client,
        upstream=upstream,
        provider_request_id=provider_request_id,
        model=model,
    )


__all__ = [
    "DEFAULT_TTS_VOICE",
    "OpenAITTSAudioStream",
    "OpenAITTSConfigurationError",
    "OpenAITTSTimeoutError",
    "OpenAITTSUnavailableError",
    "OpenAITTSUpstreamError",
    "open_tts_audio_stream",
]
