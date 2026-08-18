from __future__ import annotations

"""Runtime secret boundary for realtime voice preview."""

import os
from dataclasses import dataclass


class VoiceRealtimeConfigurationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VoiceRealtimeSecrets:
    openai_api_key: str
    service_token: str


def load_voice_realtime_secrets() -> VoiceRealtimeSecrets:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise VoiceRealtimeConfigurationError("missing_openai_key")

    service_token = (os.getenv("VS_SERVICE_TOKEN") or "").strip()
    if not service_token:
        raise VoiceRealtimeConfigurationError("missing_service_token")

    return VoiceRealtimeSecrets(
        openai_api_key=api_key,
        service_token=service_token,
    )


__all__ = [
    "VoiceRealtimeConfigurationError",
    "VoiceRealtimeSecrets",
    "load_voice_realtime_secrets",
]
