"""Versioned request, response, and cross-capability contracts."""

from .conversation import WEB_ASSISTANT_SOURCE, WEB_USER_SOURCE
from .voice_language import (
    AUTO_VOICE_LANGUAGE,
    DEFAULT_VOICE_LANGUAGE,
    SUPPORTED_VOICE_LANGUAGE_IDS,
    VOICE_LANGUAGE_CONTRACT_VERSION,
    VOICE_LANGUAGE_HEADER,
    VOICE_LANGUAGES,
    normalize_voice_language,
    require_voice_language,
    response_language_instruction,
    transcription_prompt,
    voice_language_from_request,
)

__all__ = [
    "AUTO_VOICE_LANGUAGE",
    "DEFAULT_VOICE_LANGUAGE",
    "SUPPORTED_VOICE_LANGUAGE_IDS",
    "VOICE_LANGUAGE_CONTRACT_VERSION",
    "VOICE_LANGUAGE_HEADER",
    "VOICE_LANGUAGES",
    "WEB_ASSISTANT_SOURCE",
    "WEB_USER_SOURCE",
    "normalize_voice_language",
    "require_voice_language",
    "response_language_instruction",
    "transcription_prompt",
    "voice_language_from_request",
]
