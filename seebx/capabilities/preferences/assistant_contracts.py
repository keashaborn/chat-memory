from __future__ import annotations

"""Typed contracts for owner-scoped assistant response preferences."""

from datetime import datetime
from enum import Enum
import hashlib
import json
import re
import unicodedata
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ASSISTANT_PREFERENCES_VERSION = "assistant_response_preferences_v1"
PREFERENCE_CANDIDATE_VERSION = "assistant_preference_compilation_candidate_v1"
PREFERENCE_COMPILER_VERSION = "assistant_preference_compiler_v3"
MAX_PREFERENCE_NARRATIVE_CHARS = 8_000
MAX_COMPILED_RULES = 12
COMPILED_RULE_MARKER_PREFIX = "assistant-preference-plan-v1:"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALLOWED_NAME_PUNCTUATION = frozenset({" ", "'", "’", "-", "."})


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


class ConversationStyle(str, Enum):
    DIRECT = "direct"
    NATURAL = "natural"
    WARM = "warm"


class CompiledPreferenceRule(str, Enum):
    DIRECT_ANSWERS_FIRST = "direct_answers_first"
    RESTRAINED_REASSURANCE = "restrained_reassurance"
    EVIDENCE_BASED_CHALLENGE = "evidence_based_challenge"
    NO_UNSOLICITED_CLOSING_OFFERS = "no_unsolicited_closing_offers"
    MINIMAL_PARAPHRASE = "minimal_paraphrase"
    NO_GENERIC_PRAISE = "no_generic_praise"
    PRACTICAL_FOCUS = "practical_focus"
    QUESTION_RESTRAINT = "question_restraint"
    CANDID_UNCERTAINTY = "candid_uncertainty"
    CONTEXTUAL_PLAYFULNESS = "contextual_playfulness"
    PRECISE_PLAIN_LANGUAGE = "precise_plain_language"
    EVIDENCE_FIRST_CONCLUSIONS = "evidence_first_conclusions"
    INFORMATION_DENSE = "information_dense"
    CALM_PATIENT_TONE = "calm_patient_tone"
    CONTEXTUAL_POETIC_LANGUAGE = "contextual_poetic_language"


class PreferenceRejectionReason(str, Enum):
    CANNOT_CHANGE_SAFETY = "cannot_change_safety"
    CANNOT_CHANGE_FACTUAL_STANDARDS = "cannot_change_factual_standards"
    CANNOT_FORCE_AGREEMENT = "cannot_force_agreement"
    CANNOT_EXPOSE_HIDDEN_PROMPTS = "cannot_expose_hidden_prompts"
    CANNOT_OVERRIDE_DOMAIN_POLICY = "cannot_override_domain_policy"
    CANNOT_CONTROL_TOOLS_OR_MEMORY = "cannot_control_tools_or_memory"
    UNSUPPORTED_STYLE_REQUEST = "unsupported_style_request"
    AMBIGUOUS_REQUEST = "ambiguous_request"


class PreferenceCompilationStatus(str, Enum):
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CLEAR = "clear"


class PreferenceSource(str, Enum):
    DEFAULTS = "defaults"
    POSTGRES = "postgres"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def normalize_text(value: Any, *, multiline: bool) -> str | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    if multiline:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize_assistant_name(value: Any) -> str | None:
    normalized = normalize_text(value, multiline=False)
    if normalized is None:
        return None
    if len(normalized) > 40 or len(normalized.split(" ")) > 4:
        raise ValueError("assistant name is outside the supported boundary")
    for character in normalized:
        if character in _ALLOWED_NAME_PUNCTUATION:
            continue
        if unicodedata.category(character)[0] not in {"L", "M", "N"}:
            raise ValueError("assistant name contains unsupported characters")
    return normalized


class AssistantPreferencesUpdate(_StrictFrozenModel):
    expected_revision: int = Field(ge=0)
    assistant_name: str | None = Field(default=None, max_length=40)
    nickname: str | None = Field(default=None, max_length=64)
    occupation: str | None = Field(default=None, max_length=160)
    more_about_you: str | None = Field(default=None, max_length=2_000)
    # Wire compatibility: the frontend calls the reviewed narrative
    # custom_instructions. It is stored as preference_narrative and is never
    # projected directly into an answer prompt.
    custom_instructions: str | None = Field(
        default=None,
        max_length=MAX_PREFERENCE_NARRATIVE_CHARS,
    )
    response_length: ResponseLength = ResponseLength.BALANCED
    technical_depth: TechnicalDepth = TechnicalDepth.BALANCED
    response_format: ResponseFormat = ResponseFormat.AUTO
    conversation_style: ConversationStyle = ConversationStyle.NATURAL

    @field_validator("assistant_name", mode="before")
    @classmethod
    def canonical_name(cls, value: Any) -> str | None:
        return normalize_assistant_name(value)

    @field_validator("nickname", "occupation", mode="before")
    @classmethod
    def canonical_single_line(cls, value: Any) -> str | None:
        return normalize_text(value, multiline=False)

    @field_validator("more_about_you", "custom_instructions", mode="before")
    @classmethod
    def canonical_multiline(cls, value: Any) -> str | None:
        return normalize_text(value, multiline=True)

    @field_validator(
        "response_length",
        "technical_depth",
        "response_format",
        "conversation_style",
        mode="before",
    )
    @classmethod
    def enums_from_wire(cls, value: Any, info: Any) -> Any:
        enum_type = {
            "response_length": ResponseLength,
            "technical_depth": TechnicalDepth,
            "response_format": ResponseFormat,
            "conversation_style": ConversationStyle,
        }[info.field_name]
        return enum_type(value) if isinstance(value, str) else value


class AssistantPreferencesRecord(_StrictFrozenModel):
    owner_user_id: UUID = Field(repr=False)
    revision: int = Field(ge=0)
    source: PreferenceSource
    updated_at: datetime | None = None
    assistant_name: str | None = None
    nickname: str | None = None
    occupation: str | None = None
    more_about_you: str | None = Field(default=None, repr=False)
    compiled_rule_marker: str | None = Field(default=None, repr=False)
    response_length: ResponseLength = ResponseLength.BALANCED
    technical_depth: TechnicalDepth = TechnicalDepth.BALANCED
    response_format: ResponseFormat = ResponseFormat.AUTO
    conversation_style: ConversationStyle = ConversationStyle.NATURAL

    @model_validator(mode="after")
    def consistent_source(self) -> "AssistantPreferencesRecord":
        if self.source is PreferenceSource.DEFAULTS:
            if self.revision != 0 or self.updated_at is not None:
                raise ValueError("default preferences require revision zero")
        elif self.revision < 1 or self.updated_at is None:
            raise ValueError("stored preferences require a revision and timestamp")
        return self


class PreferenceCompilationPublic(_StrictFrozenModel):
    status: Literal["none", "active"]
    summary: tuple[str, ...] = ()
    not_applied: tuple[str, ...] = ()
    compiled_at: datetime | None = None


class AssistantPreferencesPublic(_StrictFrozenModel):
    contract_version: Literal[ASSISTANT_PREFERENCES_VERSION] = ASSISTANT_PREFERENCES_VERSION
    revision: int = Field(ge=0)
    updated_at: datetime | None
    assistant_name: str | None
    nickname: str | None
    occupation: str | None
    more_about_you: str | None
    custom_instructions: str | None
    response_length: ResponseLength
    technical_depth: TechnicalDepth
    response_format: ResponseFormat
    conversation_style: ConversationStyle
    compilation: PreferenceCompilationPublic


class PreferenceCompilationRequest(_StrictFrozenModel):
    expected_revision: int = Field(ge=0)
    narrative: str = Field(max_length=MAX_PREFERENCE_NARRATIVE_CHARS)

    @field_validator("narrative", mode="before")
    @classmethod
    def canonical_narrative(cls, value: Any) -> str:
        return normalize_text(value, multiline=True) or ""


class PreferenceApprovalRequest(_StrictFrozenModel):
    expected_revision: int = Field(ge=0)
    candidate_id: UUID

    @field_validator("candidate_id", mode="before")
    @classmethod
    def uuid_from_wire(cls, value: Any) -> Any:
        return UUID(value) if isinstance(value, str) else value


class PreferenceCompilationCandidate(_StrictFrozenModel):
    contract_version: Literal[PREFERENCE_CANDIDATE_VERSION] = PREFERENCE_CANDIDATE_VERSION
    candidate_id: UUID
    owner_user_id: UUID = Field(repr=False)
    source_revision: int = Field(ge=0)
    source_narrative: str = Field(max_length=MAX_PREFERENCE_NARRATIVE_CHARS, repr=False)
    source_narrative_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_length: ResponseLength | None
    technical_depth: TechnicalDepth | None
    response_format: ResponseFormat | None
    conversation_style: ConversationStyle | None
    rule_ids: tuple[CompiledPreferenceRule, ...] = Field(max_length=MAX_COMPILED_RULES)
    rejected_reason_codes: tuple[PreferenceRejectionReason, ...] = Field(max_length=8)
    compilation_status: PreferenceCompilationStatus
    summary: tuple[str, ...] = Field(max_length=16)
    not_applied: tuple[str, ...] = Field(max_length=8)
    compiler_version: Literal[PREFERENCE_COMPILER_VERSION] = PREFERENCE_COMPILER_VERSION
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_model: str = Field(min_length=1, max_length=120)
    provider_response_id: str | None = Field(default=None, max_length=255, repr=False)
    created_at: datetime
    expires_at: datetime

    def public_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "candidate_id": str(self.candidate_id),
            "source_revision": self.source_revision,
            "status": self.compilation_status.value,
            "summary": list(self.summary),
            "not_applied": list(self.not_applied),
            "proposed": {
                "response_length": self.response_length.value if self.response_length else None,
                "technical_depth": self.technical_depth.value if self.technical_depth else None,
                "format": self.response_format.value if self.response_format else None,
                "conversation_style": self.conversation_style.value if self.conversation_style else None,
            },
            "expires_at": self.expires_at.isoformat(),
        }


def default_preferences(owner_user_id: UUID) -> AssistantPreferencesRecord:
    return AssistantPreferencesRecord(
        owner_user_id=owner_user_id,
        revision=0,
        source=PreferenceSource.DEFAULTS,
    )


def compiled_rule_marker(rule_ids: tuple[CompiledPreferenceRule, ...]) -> str | None:
    unique = tuple(dict.fromkeys(rule_ids))
    if not unique:
        return None
    if len(unique) > MAX_COMPILED_RULES:
        raise ValueError("too many compiled preference rules")
    return COMPILED_RULE_MARKER_PREFIX + ",".join(item.value for item in unique)


def candidate_plan_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
