from __future__ import annotations

"""Backend-owned language catalog and validation shared across capabilities."""

from typing import Any

from fastapi import HTTPException, Request


VOICE_LANGUAGE_HEADER = "x-vs-voice-language"
DEFAULT_VOICE_LANGUAGE = "en"
AUTO_VOICE_LANGUAGE = "auto"
VOICE_LANGUAGE_CONTRACT_VERSION = "voice_language_v1"

VOICE_LANGUAGES: tuple[dict[str, str], ...] = (
    {"id": "auto", "label": "Auto detect"},
    {"id": "af", "label": "Afrikaans"},
    {"id": "ar", "label": "العربية · Arabic"},
    {"id": "hy", "label": "Հայերեն · Armenian"},
    {"id": "az", "label": "azərbaycanca · Azerbaijani"},
    {"id": "be", "label": "беларуская · Belarusian"},
    {"id": "bs", "label": "bosanski · Bosnian"},
    {"id": "bg", "label": "български · Bulgarian"},
    {"id": "ca", "label": "català · Catalan"},
    {"id": "zh", "label": "中文 · Chinese"},
    {"id": "hr", "label": "hrvatski · Croatian"},
    {"id": "cs", "label": "čeština · Czech"},
    {"id": "da", "label": "dansk · Danish"},
    {"id": "nl", "label": "Nederlands · Dutch"},
    {"id": "en", "label": "English"},
    {"id": "et", "label": "eesti · Estonian"},
    {"id": "fi", "label": "suomi · Finnish"},
    {"id": "fr", "label": "français · French"},
    {"id": "gl", "label": "galego · Galician"},
    {"id": "de", "label": "Deutsch · German"},
    {"id": "el", "label": "Ελληνικά · Greek"},
    {"id": "he", "label": "עברית · Hebrew"},
    {"id": "hi", "label": "हिन्दी · Hindi"},
    {"id": "hu", "label": "magyar · Hungarian"},
    {"id": "is", "label": "íslenska · Icelandic"},
    {"id": "id", "label": "Bahasa Indonesia · Indonesian"},
    {"id": "it", "label": "italiano · Italian"},
    {"id": "ja", "label": "日本語 · Japanese"},
    {"id": "kn", "label": "ಕನ್ನಡ · Kannada"},
    {"id": "kk", "label": "қазақ тілі · Kazakh"},
    {"id": "ko", "label": "한국어 · Korean"},
    {"id": "lv", "label": "latviešu · Latvian"},
    {"id": "lt", "label": "lietuvių · Lithuanian"},
    {"id": "mk", "label": "македонски · Macedonian"},
    {"id": "ms", "label": "Bahasa Melayu · Malay"},
    {"id": "mr", "label": "मराठी · Marathi"},
    {"id": "mi", "label": "te reo Māori · Maori"},
    {"id": "ne", "label": "नेपाली · Nepali"},
    {"id": "no", "label": "norsk · Norwegian"},
    {"id": "fa", "label": "فارسی · Persian"},
    {"id": "pl", "label": "polski · Polish"},
    {"id": "pt", "label": "português · Portuguese"},
    {"id": "ro", "label": "română · Romanian"},
    {"id": "ru", "label": "русский · Russian"},
    {"id": "sr", "label": "српски · Serbian"},
    {"id": "sk", "label": "slovenčina · Slovak"},
    {"id": "sl", "label": "slovenščina · Slovenian"},
    {"id": "es", "label": "español · Spanish"},
    {"id": "sw", "label": "Kiswahili · Swahili"},
    {"id": "sv", "label": "svenska · Swedish"},
    {"id": "tl", "label": "Filipino · Tagalog"},
    {"id": "ta", "label": "தமிழ் · Tamil"},
    {"id": "th", "label": "ไทย · Thai"},
    {"id": "tr", "label": "Türkçe · Turkish"},
    {"id": "uk", "label": "українська · Ukrainian"},
    {"id": "ur", "label": "اردو · Urdu"},
    {"id": "vi", "label": "Tiếng Việt · Vietnamese"},
    {"id": "cy", "label": "Cymraeg · Welsh"},
)

VOICE_LANGUAGE_LABELS = {
    item["id"]: item["label"] for item in VOICE_LANGUAGES
}
SUPPORTED_VOICE_LANGUAGE_IDS = frozenset(VOICE_LANGUAGE_LABELS)


def normalize_voice_language(
    value: Any,
    *,
    default: str = DEFAULT_VOICE_LANGUAGE,
) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_VOICE_LANGUAGE_IDS else default


def require_voice_language(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_VOICE_LANGUAGE_IDS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_voice_language",
                "field": "language",
                "value": normalized[:32],
            },
        )
    return normalized


def voice_language_from_request(
    req: Request,
    *,
    default: str = DEFAULT_VOICE_LANGUAGE,
) -> str:
    raw = req.headers.get(VOICE_LANGUAGE_HEADER)
    if raw is None or not raw.strip():
        return require_voice_language(default)
    return require_voice_language(raw)


def response_language_instruction(language: str) -> str:
    selected = require_voice_language(language)
    if selected == AUTO_VOICE_LANGUAGE:
        return (
            "Reply in the language used in the user's current message. If that "
            "message is too short to identify a language, use the language "
            "established by the recent conversation; otherwise use English."
        )
    label = VOICE_LANGUAGE_LABELS[selected].split(" · ")[-1]
    return (
        f"Reply in {label}. Preserve proper nouns, product names, citations, "
        "and code in their natural form. Language choice never weakens safety, "
        "consent, evidence, or qualified-care boundaries."
    )


def transcription_prompt(language: str) -> str:
    selected = require_voice_language(language)
    language_text = (
        "the language naturally spoken by the user"
        if selected == AUTO_VOICE_LANGUAGE
        else VOICE_LANGUAGE_LABELS[selected].split(" · ")[-1]
    )
    return (
        f"Natural conversational {language_text} in LifeSwitch with the Verbal "
        "Sage assistant. Preserve short questions and incomplete phrases "
        "exactly; do not complete or reinterpret them. Proper names may include "
        "Relational Monism v0.4, RM v0.4, Sage, Governed Memory V1, Qdrant, "
        "OpenAI, and Supabase."
    )


__all__ = [
    "AUTO_VOICE_LANGUAGE",
    "DEFAULT_VOICE_LANGUAGE",
    "SUPPORTED_VOICE_LANGUAGE_IDS",
    "VOICE_LANGUAGE_CONTRACT_VERSION",
    "VOICE_LANGUAGE_HEADER",
    "VOICE_LANGUAGES",
    "normalize_voice_language",
    "require_voice_language",
    "response_language_instruction",
    "transcription_prompt",
    "voice_language_from_request",
]
