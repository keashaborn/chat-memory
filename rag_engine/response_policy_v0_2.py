from __future__ import annotations

"""Pure, fail-closed response-policy decisions for RESSE.

This module accepts only typed inputs. It does not assemble prompts, retrieve
Memory V1 records, query a corpus, call a model, read configuration, log, or
mutate state. A future server adapter is responsible for constructing the
trusted signals after authentication and input-safety assessment.
"""

import hashlib
import json
import re
import unicodedata
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


POLICY_VERSION = "response_policy_v0_2"
POLICY_INPUT_VERSION = "response_policy_input_v0_2"
POLICY_SIGNALS_VERSION = "response_policy_signals_v0_2"
ASSISTANT_PROFILE_ID = "RESSE"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,159}$")

LEGACY_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "assistant_profile_id",
        "available_sources",
        "definition_overlay",
        "fm_lens",
        "fm_level",
        "high_stakes",
        "limits",
        "memory_intent",
        "memory_owner",
        "mix",
        "personalization",
        "pragmatics",
        "response_mode",
        "roleplay",
        "routing",
        "vantage_id",
    }
)


class ResponseMode(str, Enum):
    HIGH_STAKES = "HIGH_STAKES"
    TECHNICAL = "TECHNICAL"
    FM_EXPLICIT = "FM_EXPLICIT"
    COACHING = "COACHING"
    ORDINARY = "ORDINARY"


MODE_PRECEDENCE: tuple[ResponseMode, ...] = (
    ResponseMode.HIGH_STAKES,
    ResponseMode.TECHNICAL,
    ResponseMode.FM_EXPLICIT,
    ResponseMode.COACHING,
    ResponseMode.ORDINARY,
)


class FMLevel(str, Enum):
    OFF = "OFF"
    LIGHT = "LIGHT"
    EXPLICIT = "EXPLICIT"


class GateState(str, Enum):
    PASS = "pass"
    TRIGGERED = "triggered"
    UNCERTAIN = "uncertain"


class Closure(str, Enum):
    COMPLETE = "complete"
    EXPLICIT_NEXT_STEP = "explicit_next_step"
    TECHNICAL_PROCEDURE = "technical_procedure"
    MATERIAL_CLARIFICATION = "material_clarification"
    CONSENTED_COACHING = "consented_coaching"
    SAFETY_ACTION = "safety_action"


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _wire_revalidate(model_type: type[StrictFrozenModel], value: StrictFrozenModel):
    """Reparse JSON so model_construct/model_copy cannot bypass validators."""

    if not isinstance(value, model_type):
        raise TypeError(f"expected {model_type.__name__}")
    return model_type.model_validate_json(_canonical_json_bytes(value))


class ResponsePolicyInputV0_2(StrictFrozenModel):
    contract_version: Literal[POLICY_INPUT_VERSION] = POLICY_INPUT_VERSION
    message: str = Field(max_length=100_000)
    requested_assistant_profile_id: str | None = Field(default=None, max_length=160)
    request_field_names: tuple[str, ...] = ()

    @field_validator("request_field_names")
    @classmethod
    def sorted_unique_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not FIELD_NAME_RE.fullmatch(item) for item in value):
            raise ValueError("request_field_names contains an invalid field name")
        if value != tuple(sorted(set(value))):
            raise ValueError("request_field_names must be sorted and unique")
        return value


class ResponsePolicySignalsV0_2(StrictFrozenModel):
    """Trusted server-side signals; never populate directly from request JSON."""

    contract_version: Literal[POLICY_SIGNALS_VERSION] = POLICY_SIGNALS_VERSION
    high_stakes: bool | None = None
    high_stakes_uncertain: bool = False
    technical: bool | None = None
    fm_explicit: bool | None = None
    coaching: bool | None = None
    fm_application_gate: GateState = GateState.PASS
    ordinary_fm_relevant: bool = False
    user_fm_opt_out: bool = False
    safety_action_required: bool | None = None
    technical_procedure_requested: bool | None = None
    coaching_consent: bool | None = None
    material_clarification_required: bool | None = None
    explicit_next_step_requested: bool | None = None

    @model_validator(mode="after")
    def consistent_high_stakes_signal(self) -> "ResponsePolicySignalsV0_2":
        if self.high_stakes_uncertain and self.high_stakes is not None:
            raise ValueError(
                "high_stakes_uncertain cannot accompany a definitive high_stakes signal"
            )
        return self


class _ResponsePolicyDecisionPayloadV0_2(StrictFrozenModel):
    policy_version: Literal[POLICY_VERSION]
    assistant_profile_id: Literal[ASSISTANT_PROFILE_ID]
    response_mode: ResponseMode
    closure: Closure
    mode_reasons: tuple[str, ...]
    high_stakes_gate: GateState
    fm_application_gate: GateState
    fm_default_level: FMLevel
    fm_effective_level: FMLevel
    fm_gate_reasons: tuple[str, ...]
    user_opt_out_applied: bool
    governed_memory_allowed: Literal[True]
    structured_data_allowed: Literal[True]
    ignored_legacy_request_fields: tuple[str, ...]

    @field_validator(
        "mode_reasons", "fm_gate_reasons", "ignored_legacy_request_fields"
    )
    @classmethod
    def sorted_unique_codes(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError(f"{info.field_name} must be sorted and unique")
        return value

    @model_validator(mode="after")
    def fm_level_invariants(self) -> "_ResponsePolicyDecisionPayloadV0_2":
        expected_defaults = {
            ResponseMode.HIGH_STAKES: (FMLevel.OFF,),
            ResponseMode.TECHNICAL: (FMLevel.OFF,),
            ResponseMode.FM_EXPLICIT: (FMLevel.EXPLICIT,),
            ResponseMode.COACHING: (FMLevel.LIGHT,),
            ResponseMode.ORDINARY: (FMLevel.OFF, FMLevel.LIGHT),
        }
        if self.fm_default_level not in expected_defaults[self.response_mode]:
            raise ValueError("FM default level is incompatible with response mode")
        if (
            self.high_stakes_gate is not GateState.PASS
            and self.response_mode is not ResponseMode.HIGH_STAKES
        ):
            raise ValueError("a high-stakes gate requires HIGH_STAKES mode")
        if (
            self.response_mode is ResponseMode.HIGH_STAKES
            and self.high_stakes_gate is GateState.PASS
        ):
            raise ValueError("HIGH_STAKES mode requires a non-pass gate")
        if (
            self.fm_application_gate is not GateState.PASS
            or self.user_opt_out_applied
        ) and self.fm_effective_level is not FMLevel.OFF:
            raise ValueError("an FM veto requires effective FM OFF")
        if (
            self.fm_application_gate is GateState.PASS
            and not self.user_opt_out_applied
            and self.fm_effective_level is not self.fm_default_level
        ):
            raise ValueError("effective FM must equal the default when no veto applies")
        return self


class ResponsePolicyDecisionV0_2(_ResponsePolicyDecisionPayloadV0_2):
    decision_sha256: str

    @field_validator("decision_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("decision_sha256 must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def verify_hash(self) -> "ResponsePolicyDecisionV0_2":
        payload = self.model_dump(mode="json", exclude={"decision_sha256"})
        if self.decision_sha256 != _sha256(payload):
            raise ValueError("response-policy decision hash mismatch")
        return self


def _normalized(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", " ", normalized).strip()


_LOCAL_HIGH_STAKES_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "acute_self_harm",
        re.compile(
            r"\b(?:i (?:have )?a plan to (?:kill myself|end my life)|"
            r"i (?:want|plan|intend) to (?:kill myself|die)|"
            r"i (?:do not|don't) want to live|"
            r"kill myself|end my life|suicid(?:e|al)|self[- ]harm)\b"
        ),
    ),
    (
        "immediate_violence_or_abuse",
        re.compile(
            r"\b(?:partner|spouse|parent|someone) (?:is |keeps )?"
            r"(?:hitting|beating|strangling|threatening) me\b|"
            r"\bthreatening to break in\b|\bimmediate danger\b|"
            r"\b(?:gun|knife) (?:is )?(?:pointed at|beside) me\b"
        ),
    ),
    (
        "acute_medical",
        re.compile(
            r"\bcrushing chest pain\b|\bshortness of breath\b|"
            r"\b(?:cannot|can't) breathe\b|\boverdosed?\b|"
            r"\bunconscious\b|\bfainted twice\b|"
            r"\b(?:cannot|can't) bear weight\b.*\bdeform(?:ed|ity)\b"
        ),
    ),
    (
        "dangerous_restriction_or_eating_disorder",
        re.compile(
            r"\b500 calories\b|\bstarving myself\b|\bpurging (?:every day|daily)\b|"
            r"\bfaint(?:ed|ing) (?:during|after) (?:a )?workout\b|"
            r"\b(?:have not|haven't) eaten (?:in|for) \d+ days\b"
        ),
    ),
    (
        "dangerous_intoxication_or_withdrawal",
        re.compile(
            r"\bshaking and seeing things\b|\balcohol withdrawal\b|"
            r"\bstopped drinking\b.*\b(?:shaking|hallucinating|seeing things)\b|"
            r"\bdangerously intoxicated\b"
        ),
    ),
    (
        "impaired_reality_testing",
        re.compile(
            r"\b(?:i am|i'm|im|i feel|i'm feeling) "
            r"(?:manic|psychotic|paranoid|delusional|dissociating|derealizing)\b|"
            r"\bvoices are telling me\b|\beveryone can hear my thoughts\b|"
            r"\b(?:have not|haven't) slept (?:in|for) \d+ days\b.*\binvincible\b"
        ),
    ),
    (
        "unsafe_medication_change",
        re.compile(
            r"\b(?:tell me|show me) (?:exactly )?how to taper\b|"
            r"\bstop (?:my )?medication (?:tonight|immediately)\b"
        ),
    ),
    (
        "child_safety",
        re.compile(
            r"\bmy child (?:is |was )?(?:missing|unsafe|being abused|abducted)\b|"
            r"\ba child is in immediate danger\b"
        ),
    ),
    (
        "material_legal_decision",
        re.compile(
            r"\bcustody hearing (?:is )?(?:tomorrow|today)\b|"
            r"\bwhat legal claims should i make\b.*\bguarantee\b"
        ),
    ),
    (
        "material_financial_decision",
        re.compile(
            r"\b(?:invest|bet|transfer) (?:all of )?my (?:retirement|life savings)\b|"
            r"\bguarantee (?:a )?(?:profit|return)\b"
        ),
    ),
)

_LOCAL_APPLICATION_BOUNDARY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "acute_grief_or_loss",
        re.compile(
            r"\b(?:i am|i'm|we are|we're) grieving\b|"
            r"\bmy (?:wife|husband|partner|child|parent|mother|father) "
            r"(?:just )?(?:died|passed away)\b|\bacute grief\b"
        ),
    ),
    (
        "trauma_or_abuse",
        re.compile(
            r"\b(?:i was|my friend was|they were) assaulted (?:today|yesterday|recently)\b|"
            r"\b(?:active|ongoing) abuse\b"
        ),
    ),
    (
        "consent_boundary",
        re.compile(
            r"\b(?:covert|without telling (?:them|the user)|without consent)\b"
        ),
    ),
    (
        "urgent_practical_need",
        re.compile(
            r"\b(?:urgent|immediate) practical (?:need|action)\b|"
            r"\b(?:safety|medical care|legal deadline) comes first\b"
        ),
    ),
)

_LOCAL_TECHNICAL_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:python|sql|api|backend|frontend|qdrant|postgres|docker|"
        r"kubernetes|systemctl|journalctl|nginx|git|worktree|unit test|"
        r"function|class|database|server|service)\b"
    ),
    re.compile(r"\b(?:debug|implement|deploy|restart|patch|trace|compile)\b"),
)

_LOCAL_FM_EXPLICIT_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfractal monism\b"),
    re.compile(r"\bfm (?:lens|view|philosophy|framework|idea)\b"),
    re.compile(r"\bapply (?:the )?fm lens\b"),
)

_LOCAL_COACHING_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhelp me (?:stop|start|change|build|track|improve)\b"),
    re.compile(r"\bi (?:want|need) to (?:change|track|test|improve|stop|start)\b"),
    re.compile(r"\bi keep (?:missing|avoiding|forgetting|doing)\b"),
    re.compile(r"\b(?:build a habit|let'?s track|run an experiment)\b"),
)


def _matched_codes(
    text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]
) -> tuple[str, ...]:
    return tuple(sorted(code for code, pattern in rules if pattern.search(text)))


def _matches_any(text: str, rules: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in rules)


def _select_mode(
    text: str,
    signals: ResponsePolicySignalsV0_2,
    local_high_stakes: tuple[str, ...],
) -> tuple[ResponseMode, GateState, tuple[str, ...]]:
    # Local high-stakes detection is a hard veto: a false upstream signal cannot
    # disable a concrete local safety rule.
    if local_high_stakes:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.TRIGGERED,
            tuple(f"local_high_stakes:{code}" for code in local_high_stakes),
        )
    if signals.high_stakes is True:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.TRIGGERED,
            ("trusted_high_stakes_signal",),
        )
    if signals.high_stakes_uncertain:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.UNCERTAIN,
            ("trusted_high_stakes_uncertain",),
        )

    checks = (
        (
            ResponseMode.TECHNICAL,
            signals.technical,
            _matches_any(text, _LOCAL_TECHNICAL_RULES),
        ),
        (
            ResponseMode.FM_EXPLICIT,
            signals.fm_explicit,
            _matches_any(text, _LOCAL_FM_EXPLICIT_RULES),
        ),
        (
            ResponseMode.COACHING,
            signals.coaching,
            _matches_any(text, _LOCAL_COACHING_RULES),
        ),
    )
    for mode, trusted, detected in checks:
        if trusted is True:
            return mode, GateState.PASS, (f"trusted_{mode.value.lower()}_signal",)
        if trusted is None and detected:
            return mode, GateState.PASS, (f"local_{mode.value.lower()}_signal",)
    return ResponseMode.ORDINARY, GateState.PASS, ("ordinary_default",)


def _select_closure(
    mode: ResponseMode,
    text: str,
    signals: ResponsePolicySignalsV0_2,
    local_high_stakes: tuple[str, ...],
) -> Closure:
    if mode is ResponseMode.HIGH_STAKES:
        local_action_categories = {
            "acute_medical",
            "acute_self_harm",
            "child_safety",
            "dangerous_intoxication_or_withdrawal",
            "dangerous_restriction_or_eating_disorder",
            "immediate_violence_or_abuse",
            "impaired_reality_testing",
        }
        if signals.safety_action_required is True or local_action_categories.intersection(
            local_high_stakes
        ):
            return Closure.SAFETY_ACTION
        return Closure.COMPLETE

    if mode is ResponseMode.TECHNICAL:
        if signals.technical_procedure_requested is True:
            return Closure.TECHNICAL_PROCEDURE
        if signals.technical_procedure_requested is None and re.search(
            r"\b(?:implement|exact patch|one command at a time|walk me through|restart)\b",
            text,
        ):
            return Closure.TECHNICAL_PROCEDURE

    if mode is ResponseMode.COACHING:
        local_consent = bool(
            re.search(
                r"\b(?:yes[,.;:]? (?:let us|let's) track|i want to test a change|"
                r"i consent to tracking)\b",
                text,
            )
        )
        if signals.coaching_consent is True or (
            signals.coaching_consent is None and local_consent
        ):
            return Closure.CONSENTED_COACHING
        local_clarification = bool(
            re.search(r"\b(?:can you help me|help me (?:stop|start|change))\b", text)
        )
        if signals.material_clarification_required is True or (
            signals.material_clarification_required is None and local_clarification
        ):
            return Closure.MATERIAL_CLARIFICATION

    local_next_step = bool(
        re.search(r"\b(?:give me next steps|make (?:me )?a plan|what should i do next)\b", text)
    )
    if signals.explicit_next_step_requested is True or (
        signals.explicit_next_step_requested is None and local_next_step
    ):
        return Closure.EXPLICIT_NEXT_STEP
    return Closure.COMPLETE


def _default_fm_level(
    mode: ResponseMode, signals: ResponsePolicySignalsV0_2
) -> FMLevel:
    if mode in {ResponseMode.HIGH_STAKES, ResponseMode.TECHNICAL}:
        return FMLevel.OFF
    if mode is ResponseMode.FM_EXPLICIT:
        return FMLevel.EXPLICIT
    if mode is ResponseMode.COACHING:
        return FMLevel.LIGHT
    if signals.ordinary_fm_relevant:
        return FMLevel.LIGHT
    return FMLevel.OFF


def _application_gate(
    high_stakes_gate: GateState,
    local_boundaries: tuple[str, ...],
    signals: ResponsePolicySignalsV0_2,
) -> tuple[GateState, tuple[str, ...]]:
    if high_stakes_gate is GateState.TRIGGERED:
        return GateState.TRIGGERED, ("high_stakes_application_boundary",)
    if high_stakes_gate is GateState.UNCERTAIN:
        return GateState.UNCERTAIN, ("uncertain_high_stakes_application_boundary",)
    if local_boundaries:
        return (
            GateState.TRIGGERED,
            tuple(f"fm_ag_001:{code}" for code in local_boundaries),
        )
    if signals.fm_application_gate is GateState.TRIGGERED:
        return GateState.TRIGGERED, ("trusted_fm_application_boundary",)
    if signals.fm_application_gate is GateState.UNCERTAIN:
        return GateState.UNCERTAIN, ("trusted_fm_application_boundary_uncertain",)
    return GateState.PASS, ()


def decide_response_policy_v0_2(
    request: ResponsePolicyInputV0_2,
    *,
    signals: ResponsePolicySignalsV0_2 | None = None,
) -> ResponsePolicyDecisionV0_2:
    """Return one deterministic decision without performing a side effect."""

    request = _wire_revalidate(ResponsePolicyInputV0_2, request)
    trusted = _wire_revalidate(
        ResponsePolicySignalsV0_2, signals or ResponsePolicySignalsV0_2()
    )
    text = _normalized(request.message)
    local_high_stakes = _matched_codes(text, _LOCAL_HIGH_STAKES_RULES)
    local_boundaries = _matched_codes(text, _LOCAL_APPLICATION_BOUNDARY_RULES)

    mode, high_stakes_gate, mode_reasons = _select_mode(
        text, trusted, local_high_stakes
    )
    closure = _select_closure(mode, text, trusted, local_high_stakes)
    default_fm = _default_fm_level(mode, trusted)
    application_gate, application_reasons = _application_gate(
        high_stakes_gate, local_boundaries, trusted
    )

    fm_gate_reasons = list(application_reasons)
    if mode is ResponseMode.HIGH_STAKES:
        fm_gate_reasons.append("fm_off_by_high_stakes_mode")
    elif mode is ResponseMode.TECHNICAL:
        fm_gate_reasons.append("fm_off_by_technical_mode")
    elif default_fm is FMLevel.OFF:
        fm_gate_reasons.append("fm_off_by_default")
    if trusted.user_fm_opt_out:
        fm_gate_reasons.append("fm_off_by_user_opt_out")

    effective_fm = default_fm
    if application_gate is not GateState.PASS or trusted.user_fm_opt_out:
        effective_fm = FMLevel.OFF

    reasons = list(mode_reasons)
    requested_profile = (request.requested_assistant_profile_id or "").strip()
    if requested_profile and requested_profile.upper() != ASSISTANT_PROFILE_ID:
        reasons.append("requested_assistant_profile_rejected")

    ignored = tuple(
        sorted(LEGACY_REQUEST_FIELDS.intersection(request.request_field_names))
    )
    if ignored:
        reasons.append("legacy_request_fields_ignored")

    payload = _ResponsePolicyDecisionPayloadV0_2(
        policy_version=POLICY_VERSION,
        assistant_profile_id=ASSISTANT_PROFILE_ID,
        response_mode=mode,
        closure=closure,
        mode_reasons=tuple(sorted(set(reasons))),
        high_stakes_gate=high_stakes_gate,
        fm_application_gate=application_gate,
        fm_default_level=default_fm,
        fm_effective_level=effective_fm,
        fm_gate_reasons=tuple(sorted(set(fm_gate_reasons))),
        user_opt_out_applied=trusted.user_fm_opt_out,
        governed_memory_allowed=True,
        structured_data_allowed=True,
        ignored_legacy_request_fields=ignored,
    )
    payload_python = payload.model_dump()
    payload_json = payload.model_dump(mode="json")
    return ResponsePolicyDecisionV0_2(
        **payload_python,
        decision_sha256=_sha256(payload_json),
    )


__all__ = [
    "ASSISTANT_PROFILE_ID",
    "LEGACY_REQUEST_FIELDS",
    "MODE_PRECEDENCE",
    "POLICY_INPUT_VERSION",
    "POLICY_SIGNALS_VERSION",
    "POLICY_VERSION",
    "Closure",
    "FMLevel",
    "GateState",
    "ResponseMode",
    "ResponsePolicyDecisionV0_2",
    "ResponsePolicyInputV0_2",
    "ResponsePolicySignalsV0_2",
    "decide_response_policy_v0_2",
]
