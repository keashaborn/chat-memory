from __future__ import annotations

"""Pure RESSE user-preference and profile-context selection.

This module validates user-controlled presentation preferences and selects only
the fields a trusted application context marks relevant. It performs no prompt
assembly, retrieval, memory access, database access, HTTP calls, or side effect.
"""

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata
from typing import Any, Mapping, TypeVar

from rag_engine.resse_runtime_policy import (
    ASSISTANT_PROFILE_ID,
    MEMORY_INTENT_OWNER,
    ResponseMode,
)


PREFERENCE_POLICY_VERSION = "resse_user_preference_v0_1"

MAX_NICKNAME_CHARS = 64
MAX_OCCUPATION_CHARS = 160
MAX_MORE_ABOUT_YOU_CHARS = 2_000
MAX_CUSTOM_INSTRUCTIONS_CHARS = 1_200

ALLOWED_TOP_LEVEL_FIELDS = frozenset({"preferences", "profile"})
ALLOWED_PREFERENCE_FIELDS = frozenset(
    {
        "response_length",
        "technical_depth",
        "format",
        "encouragement",
        "custom_instructions",
    }
)
ALLOWED_PROFILE_FIELDS = frozenset({"nickname", "occupation", "more_about_you"})


class ResponseLength(str, Enum):
    CONCISE = "concise"
    BALANCED = "balanced"
    DETAILED = "detailed"


class TechnicalDepth(str, Enum):
    PLAIN = "plain"
    BALANCED = "balanced"
    EXPERT = "expert"


class ResponseFormat(str, Enum):
    AUTO = "auto"
    PROSE = "prose"
    BULLETS = "bullets"
    STEPS = "steps"


class Encouragement(str, Enum):
    MINIMAL = "minimal"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class UserPreferences:
    response_length: ResponseLength = ResponseLength.BALANCED
    technical_depth: TechnicalDepth = TechnicalDepth.BALANCED
    response_format: ResponseFormat = ResponseFormat.AUTO
    encouragement: Encouragement = Encouragement.NEUTRAL
    custom_instructions: str = ""


@dataclass(frozen=True)
class UserProfileContext:
    nickname: str = ""
    occupation: str = ""
    more_about_you: str = ""


@dataclass(frozen=True)
class ParsedPreferencePayload:
    preferences: UserPreferences
    profile: UserProfileContext
    ignored_fields: tuple[str, ...] = ()
    validation_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreferenceSelectionContext:
    """Trusted selector inputs; never populate directly from request JSON."""

    response_mode: ResponseMode
    nickname_relevant: bool = False
    occupation_relevant: bool = False
    more_about_you_relevant: bool = False
    custom_instructions_relevant: bool = False


@dataclass(frozen=True)
class ResolvedPreferenceEnvelope:
    response_mode: ResponseMode
    response_length: ResponseLength
    technical_depth: TechnicalDepth
    response_format: ResponseFormat
    encouragement: Encouragement
    nickname: str | None
    occupation: str | None
    more_about_you: str | None
    custom_instructions: str | None
    suppressed_fields: tuple[str, ...]
    ignored_fields: tuple[str, ...]
    validation_notes: tuple[str, ...]
    assistant_profile_id: str = ASSISTANT_PROFILE_ID
    memory_intent_owner: str = MEMORY_INTENT_OWNER
    policy_version: str = PREFERENCE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "assistant_profile_id": self.assistant_profile_id,
            "response_mode": self.response_mode.value,
            "presentation": {
                "response_length": self.response_length.value,
                "technical_depth": self.technical_depth.value,
                "format": self.response_format.value,
                "encouragement": self.encouragement.value,
            },
            "selected_profile_context": {
                key: value
                for key, value in (
                    ("nickname", self.nickname),
                    ("occupation", self.occupation),
                    ("more_about_you", self.more_about_you),
                )
                if value is not None
            },
            "untrusted_custom_instructions": self.custom_instructions,
            "suppressed_fields": list(self.suppressed_fields),
            "ignored_fields": list(self.ignored_fields),
            "validation_notes": list(self.validation_notes),
            "invariants": {
                "memory_intent_owner": self.memory_intent_owner,
                "user_text_is_system_policy": False,
                "may_change_assistant_identity": False,
                "may_change_response_mode": False,
                "may_change_fm_routing": False,
                "may_change_memory_ownership": False,
                "may_change_safety_policy": False,
            },
        }


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONTROL_LANGUAGE = (
    "ignore previous instructions",
    "ignore all previous",
    "override the system",
    "override safety",
    "system prompt",
    "developer message",
    "you are now",
    "act as a different assistant",
    "change memory owner",
    "memory_intent_owner",
    "assistant_profile_id",
    "vantage_id",
    "<system>",
    "[system]",
)


def _clean_text(value: Any, max_chars: int, *, multiline: bool) -> tuple[str, bool]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    if multiline:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    truncated = len(text) > max_chars
    return text[:max_chars].rstrip(), truncated


EnumValue = TypeVar("EnumValue", bound=Enum)


def _parse_enum(
    enum_type: type[EnumValue],
    value: Any,
    default: EnumValue,
    field: str,
    notes: list[str],
) -> EnumValue:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    try:
        return enum_type(normalized)
    except ValueError:
        notes.append(f"{field}:invalid_defaulted")
        return default


def _contains_control_language(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return any(phrase in normalized for phrase in _CONTROL_LANGUAGE)


def parse_preference_payload(raw: Mapping[str, Any]) -> ParsedPreferencePayload:
    """Validate the whitelisted preference payload without applying it."""

    ignored: set[str] = {str(key) for key in raw.keys() if key not in ALLOWED_TOP_LEVEL_FIELDS}
    notes: list[str] = []

    raw_preferences = raw.get("preferences", {})
    if not isinstance(raw_preferences, Mapping):
        notes.append("preferences:not_an_object_defaulted")
        raw_preferences = {}
    raw_profile = raw.get("profile", {})
    if not isinstance(raw_profile, Mapping):
        notes.append("profile:not_an_object_defaulted")
        raw_profile = {}

    ignored.update(
        f"preferences.{key}"
        for key in raw_preferences.keys()
        if key not in ALLOWED_PREFERENCE_FIELDS
    )
    ignored.update(
        f"profile.{key}" for key in raw_profile.keys() if key not in ALLOWED_PROFILE_FIELDS
    )

    response_length = _parse_enum(
        ResponseLength,
        raw_preferences.get("response_length"),
        ResponseLength.BALANCED,
        "preferences.response_length",
        notes,
    )
    technical_depth = _parse_enum(
        TechnicalDepth,
        raw_preferences.get("technical_depth"),
        TechnicalDepth.BALANCED,
        "preferences.technical_depth",
        notes,
    )
    response_format = _parse_enum(
        ResponseFormat,
        raw_preferences.get("format"),
        ResponseFormat.AUTO,
        "preferences.format",
        notes,
    )
    encouragement = _parse_enum(
        Encouragement,
        raw_preferences.get("encouragement"),
        Encouragement.NEUTRAL,
        "preferences.encouragement",
        notes,
    )

    custom_instructions, truncated = _clean_text(
        raw_preferences.get("custom_instructions"),
        MAX_CUSTOM_INSTRUCTIONS_CHARS,
        multiline=True,
    )
    if truncated:
        notes.append("preferences.custom_instructions:truncated")

    nickname, truncated = _clean_text(
        raw_profile.get("nickname"), MAX_NICKNAME_CHARS, multiline=False
    )
    if truncated:
        notes.append("profile.nickname:truncated")
    occupation, truncated = _clean_text(
        raw_profile.get("occupation"), MAX_OCCUPATION_CHARS, multiline=False
    )
    if truncated:
        notes.append("profile.occupation:truncated")
    more_about_you, truncated = _clean_text(
        raw_profile.get("more_about_you"), MAX_MORE_ABOUT_YOU_CHARS, multiline=True
    )
    if truncated:
        notes.append("profile.more_about_you:truncated")

    return ParsedPreferencePayload(
        preferences=UserPreferences(
            response_length=response_length,
            technical_depth=technical_depth,
            response_format=response_format,
            encouragement=encouragement,
            custom_instructions=custom_instructions,
        ),
        profile=UserProfileContext(
            nickname=nickname,
            occupation=occupation,
            more_about_you=more_about_you,
        ),
        ignored_fields=tuple(sorted(ignored)),
        validation_notes=tuple(sorted(notes)),
    )


def _select_text(
    *,
    field: str,
    value: str,
    relevant: bool,
    high_stakes: bool,
    suppress_control_language: bool,
    suppressed: list[str],
) -> str | None:
    if not value:
        return None
    if high_stakes:
        suppressed.append(f"{field}:high_stakes")
        return None
    if not relevant:
        suppressed.append(f"{field}:not_relevant")
        return None
    if suppress_control_language and _contains_control_language(value):
        suppressed.append(f"{field}:control_language")
        return None
    return value


def resolve_preference_envelope(
    parsed: ParsedPreferencePayload,
    context: PreferenceSelectionContext,
) -> ResolvedPreferenceEnvelope:
    """Select an auditable preference envelope without constructing a prompt."""

    preferences = parsed.preferences
    profile = parsed.profile
    suppressed: list[str] = []
    high_stakes = context.response_mode is ResponseMode.HIGH_STAKES

    if high_stakes:
        response_length = ResponseLength.CONCISE
        technical_depth = TechnicalDepth.PLAIN
        response_format = ResponseFormat.AUTO
        encouragement = Encouragement.NEUTRAL
        if preferences.response_length is not response_length:
            suppressed.append("preferences.response_length:high_stakes_override")
        if preferences.technical_depth is not technical_depth:
            suppressed.append("preferences.technical_depth:high_stakes_override")
        if preferences.response_format is not response_format:
            suppressed.append("preferences.format:high_stakes_override")
        if preferences.encouragement is not encouragement:
            suppressed.append("preferences.encouragement:high_stakes_override")
    else:
        response_length = preferences.response_length
        technical_depth = preferences.technical_depth
        response_format = preferences.response_format
        encouragement = preferences.encouragement

    nickname = _select_text(
        field="profile.nickname",
        value=profile.nickname,
        relevant=context.nickname_relevant,
        high_stakes=high_stakes,
        suppress_control_language=True,
        suppressed=suppressed,
    )
    occupation = _select_text(
        field="profile.occupation",
        value=profile.occupation,
        relevant=context.occupation_relevant,
        high_stakes=high_stakes,
        suppress_control_language=True,
        suppressed=suppressed,
    )
    more_about_you = _select_text(
        field="profile.more_about_you",
        value=profile.more_about_you,
        relevant=context.more_about_you_relevant,
        high_stakes=high_stakes,
        suppress_control_language=True,
        suppressed=suppressed,
    )
    custom_instructions = _select_text(
        field="preferences.custom_instructions",
        value=preferences.custom_instructions,
        relevant=context.custom_instructions_relevant,
        high_stakes=high_stakes,
        suppress_control_language=True,
        suppressed=suppressed,
    )

    return ResolvedPreferenceEnvelope(
        response_mode=context.response_mode,
        response_length=response_length,
        technical_depth=technical_depth,
        response_format=response_format,
        encouragement=encouragement,
        nickname=nickname,
        occupation=occupation,
        more_about_you=more_about_you,
        custom_instructions=custom_instructions,
        suppressed_fields=tuple(sorted(suppressed)),
        ignored_fields=parsed.ignored_fields,
        validation_notes=parsed.validation_notes,
    )


def build_preference_envelope(
    raw: Mapping[str, Any],
    context: PreferenceSelectionContext,
) -> ResolvedPreferenceEnvelope:
    return resolve_preference_envelope(parse_preference_payload(raw), context)
