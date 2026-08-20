from __future__ import annotations

"""Pure, fail-closed response-policy decisions for SeeBx conversations.

This module accepts only typed inputs. It does not assemble prompts, retrieve
memory records, query a corpus, call a model, read configuration, log, or
mutate state. The canonical conversation composition layer constructs trusted
signals after authentication and input-safety assessment.
"""

import hashlib
import json
import re
import unicodedata
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.contracts.conversation_provenance import (
    CANONICAL_CONVERSATION_SAFETY_ASSESSOR_V1,
    CANONICAL_DEFAULT_RESPONSE_PROFILE_V1,
)

POLICY_VERSION = "response_policy_v0_2"
POLICY_DECISION_VERSION = "response_policy_decision_v0_4"
POLICY_INPUT_VERSION = "response_policy_input_v0_2"
POLICY_SIGNALS_VERSION = "response_policy_signals_v0_3"
SAFETY_ASSESSMENT_VERSION = "safety_assessment_v0_2"
SAFETY_ASSESSOR_VERSION = CANONICAL_CONVERSATION_SAFETY_ASSESSOR_V1
DEFAULT_SAFETY_COMPONENT = "server_safety_assessment_v0_2"
ASSISTANT_PROFILE_ID = CANONICAL_DEFAULT_RESPONSE_PROFILE_V1
DOMAIN_CLASSIFIER_UNAVAILABLE_REASON = "domain_classifier_unavailable"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,159}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")

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


class Interaction(str, Enum):
    DIRECT = "DIRECT"
    GUIDED_REFLECTION = "GUIDED_REFLECTION"
    BEHAVIORAL_INTERVENTION = "BEHAVIORAL_INTERVENTION"
    CONVERSATIONAL = "CONVERSATIONAL"


class QuestionPolicy(str, Enum):
    FORBIDDEN = "FORBIDDEN"
    OPTIONAL_ONE_NON_LEADING = "OPTIONAL_ONE_NON_LEADING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ControllingPolicyDisposition(str, Enum):
    NONE = "NONE"
    DEFER_TO_CONTROLLING_POLICY = "DEFER_TO_CONTROLLING_POLICY"
    FM_APPLICATION_VETO = "FM_APPLICATION_VETO"


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
    GUIDED_REFLECTION = "guided_reflection"
    SAFETY_ACTION = "safety_action"


class ConversationRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ResponsePolicyContractError(RuntimeError):
    """Fail-closed error at the response-policy wire boundary."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
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


class ResponsePolicyConversationMessageV0_2(StrictFrozenModel):
    role: ConversationRole
    content: str = Field(min_length=1, max_length=100_000, repr=False)


class ResponsePolicyInputV0_2(StrictFrozenModel):
    contract_version: Literal[POLICY_INPUT_VERSION] = POLICY_INPUT_VERSION
    request_id: str
    conversation: tuple[ResponsePolicyConversationMessageV0_2, ...] = Field(
        min_length=1,
        max_length=256,
        repr=False,
    )
    current_message_sha256: str
    conversation_sha256: str
    request_sha256: str
    requested_assistant_profile_id: str | None = Field(default=None, max_length=160)
    request_field_names: tuple[str, ...] = ()

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value

    @field_validator("current_message_sha256", "conversation_sha256", "request_sha256")
    @classmethod
    def valid_request_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("request binding must be a lowercase SHA-256")
        return value

    @field_validator("request_field_names")
    @classmethod
    def sorted_unique_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not FIELD_NAME_RE.fullmatch(item) for item in value):
            raise ValueError("request_field_names contains an invalid field name")
        if value != tuple(sorted(set(value))):
            raise ValueError("request_field_names must be sorted and unique")
        return value

    @model_validator(mode="after")
    def exact_request_manifest(self) -> "ResponsePolicyInputV0_2":
        if self.conversation[-1].role is not ConversationRole.USER:
            raise ValueError("the current conversation message must have user role")
        if sum(len(item.content) for item in self.conversation) > 200_000:
            raise ValueError("conversation content exceeds the policy limit")
        current_hash = hashlib.sha256(
            self.conversation[-1].content.encode("utf-8")
        ).hexdigest()
        conversation_payload = [
            item.model_dump(mode="json") for item in self.conversation
        ]
        if self.current_message_sha256 != current_hash:
            raise ValueError("current-message hash mismatch")
        if self.conversation_sha256 != _sha256(conversation_payload):
            raise ValueError("conversation hash mismatch")
        payload = self.model_dump(mode="json", exclude={"request_sha256"})
        if self.request_sha256 != _sha256(payload):
            raise ValueError("request manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        conversation: tuple[ResponsePolicyConversationMessageV0_2, ...],
        requested_assistant_profile_id: str | None = None,
        request_field_names: tuple[str, ...] = (),
    ) -> "ResponsePolicyInputV0_2":
        conversation_payload = [item.model_dump(mode="json") for item in conversation]
        payload: dict[str, Any] = {
            "contract_version": POLICY_INPUT_VERSION,
            "request_id": request_id,
            "conversation": conversation_payload,
            "current_message_sha256": hashlib.sha256(
                conversation[-1].content.encode("utf-8")
            ).hexdigest()
            if conversation
            else "",
            "conversation_sha256": _sha256(conversation_payload),
            "requested_assistant_profile_id": requested_assistant_profile_id,
            "request_field_names": request_field_names,
        }
        payload["request_sha256"] = _sha256(payload)
        return cls.model_validate_json(_canonical_json_bytes(payload))

    @property
    def current_message(self) -> ResponsePolicyConversationMessageV0_2:
        return self.conversation[-1]


class SafetyAssessmentV0_2(StrictFrozenModel):
    contract_version: Literal[SAFETY_ASSESSMENT_VERSION] = SAFETY_ASSESSMENT_VERSION
    assessor_version: Literal[SAFETY_ASSESSOR_VERSION] = SAFETY_ASSESSOR_VERSION
    request_id: str
    request_sha256: str
    current_message_sha256: str
    conversation_sha256: str
    assessed_scope: Literal["full_user_conversation"]
    assessed_user_message_sha256s: tuple[str, ...] = Field(
        min_length=1,
        max_length=128,
    )
    assessor_components: tuple[str, ...] = Field(min_length=1, max_length=16)
    assessment_complete: Literal[True]
    high_stakes_gate: GateState
    safety_action_required: bool
    reason_codes: tuple[str, ...] = ()
    assessment_sha256: str

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value

    @field_validator(
        "request_sha256",
        "current_message_sha256",
        "conversation_sha256",
        "assessment_sha256",
    )
    @classmethod
    def valid_assessment_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("safety binding must be a lowercase SHA-256")
        return value

    @field_validator("reason_codes")
    @classmethod
    def sorted_unique_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("safety reason codes must be sorted and unique")
        return value

    @field_validator("assessed_user_message_sha256s")
    @classmethod
    def assessed_message_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not SHA256_RE.fullmatch(item) for item in value):
            raise ValueError("assessed message hashes must be lowercase SHA-256 values")
        return value

    @field_validator("assessor_components")
    @classmethod
    def sorted_unique_assessor_components(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not FIELD_NAME_RE.fullmatch(item) for item in value):
            raise ValueError("assessor_components contains an invalid component")
        if value != tuple(sorted(set(value))):
            raise ValueError("assessor_components must be sorted and unique")
        return value

    @model_validator(mode="after")
    def exact_assessment_manifest(self) -> "SafetyAssessmentV0_2":
        if self.high_stakes_gate is not GateState.PASS and not self.reason_codes:
            raise ValueError("a non-pass safety assessment requires a reason code")
        if self.safety_action_required and self.high_stakes_gate is GateState.PASS:
            raise ValueError("a safety action requires a non-pass high-stakes gate")
        payload = self.model_dump(mode="json", exclude={"assessment_sha256"})
        if self.assessment_sha256 != _sha256(payload):
            raise ValueError("safety-assessment manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        request: ResponsePolicyInputV0_2,
        *,
        high_stakes_gate: GateState = GateState.PASS,
        safety_action_required: bool = False,
        reason_codes: tuple[str, ...] = (),
        assessor_components: tuple[str, ...] = (DEFAULT_SAFETY_COMPONENT,),
    ) -> "SafetyAssessmentV0_2":
        verified = _wire_revalidate(ResponsePolicyInputV0_2, request)
        assessed_user_hashes = tuple(
            hashlib.sha256(item.content.encode("utf-8")).hexdigest()
            for item in verified.conversation
            if item.role is ConversationRole.USER
        )
        payload: dict[str, Any] = {
            "contract_version": SAFETY_ASSESSMENT_VERSION,
            "assessor_version": SAFETY_ASSESSOR_VERSION,
            "request_id": verified.request_id,
            "request_sha256": verified.request_sha256,
            "current_message_sha256": verified.current_message_sha256,
            "conversation_sha256": verified.conversation_sha256,
            "assessed_scope": "full_user_conversation",
            "assessed_user_message_sha256s": assessed_user_hashes,
            "assessor_components": tuple(sorted(set(assessor_components))),
            "assessment_complete": True,
            "high_stakes_gate": high_stakes_gate.value,
            "safety_action_required": safety_action_required,
            "reason_codes": tuple(sorted(set(reason_codes))),
        }
        payload["assessment_sha256"] = _sha256(payload)
        return cls.model_validate_json(_canonical_json_bytes(payload))


class ResponsePolicySignalsV0_2(StrictFrozenModel):
    """Trusted server-side signals; never populate directly from request JSON."""

    contract_version: Literal[POLICY_SIGNALS_VERSION] = POLICY_SIGNALS_VERSION
    technical: bool | None = None
    fm_explicit: bool | None = None
    coaching: bool | None = None
    fm_application_gate: GateState = GateState.PASS
    ordinary_fm_relevant: bool = False
    user_fm_opt_out: bool = False
    technical_procedure_requested: bool | None = None
    coaching_consent: bool | None = None
    specific_experiment_consent: bool | None = None
    experiment_reversible_and_proportionate: bool | None = None
    experiment_measurement_defined: bool | None = None
    experiment_adverse_indicators_defined: bool | None = None
    experiment_stop_rule_defined: bool | None = None
    direct_response_requested: bool | None = None
    guided_reflection_requested: bool | None = None
    behavioral_intervention_requested: bool | None = None
    user_declines_questions: bool | None = None
    material_clarification_required: bool | None = None
    explicit_next_step_requested: bool | None = None
    domain_risk_gate: GateState = GateState.PASS
    domain_safety_action_required: bool = False
    domain_risk_reason_codes: tuple[str, ...] = ()

    @field_validator("domain_risk_reason_codes")
    @classmethod
    def sorted_unique_domain_risk_codes(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not FIELD_NAME_RE.fullmatch(item) for item in value):
            raise ValueError("domain risk reason codes contain an invalid code")
        if value != tuple(sorted(set(value))):
            raise ValueError("domain risk reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def domain_risk_invariants(self) -> "ResponsePolicySignalsV0_2":
        if (
            self.domain_risk_gate is not GateState.PASS
            and not self.domain_risk_reason_codes
        ):
            raise ValueError("a non-pass domain risk gate requires reason codes")
        if (
            self.domain_risk_gate is GateState.PASS
            and self.domain_risk_reason_codes
        ):
            raise ValueError("a passing domain risk gate cannot claim risk reasons")
        if (
            self.domain_safety_action_required
            and self.domain_risk_gate is not GateState.TRIGGERED
        ):
            raise ValueError("a domain safety action requires a triggered risk gate")
        return self



class _ResponsePolicyDecisionPayloadV0_2(StrictFrozenModel):
    contract_version: Literal[POLICY_DECISION_VERSION] = POLICY_DECISION_VERSION
    policy_version: Literal[POLICY_VERSION]
    assistant_profile_id: Literal[ASSISTANT_PROFILE_ID]
    request_id: str
    request_sha256: str
    current_message_sha256: str
    conversation_sha256: str
    safety_assessment_sha256: str
    response_mode: ResponseMode
    interaction: Interaction
    question_policy: QuestionPolicy
    closure: Closure
    mode_reasons: tuple[str, ...]
    interaction_reasons: tuple[str, ...]
    intervention_authorized: bool
    fm_ir_020_eligible: bool
    controlling_policy_disposition: ControllingPolicyDisposition
    high_stakes_gate: GateState
    fm_application_gate: GateState
    fm_default_level: FMLevel
    fm_effective_level: FMLevel
    fm_gate_reasons: tuple[str, ...]
    user_opt_out_applied: bool
    governed_memory_allowed: Literal[True]
    structured_data_allowed: Literal[True]
    ignored_legacy_request_fields: tuple[str, ...]

    @field_validator("request_id")
    @classmethod
    def valid_request_id(cls, value: str) -> str:
        if not REQUEST_ID_RE.fullmatch(value):
            raise ValueError("request_id is invalid")
        return value

    @field_validator(
        "request_sha256",
        "current_message_sha256",
        "conversation_sha256",
        "safety_assessment_sha256",
    )
    @classmethod
    def valid_binding_hash(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("decision binding must be a lowercase SHA-256")
        return value

    @field_validator(
        "mode_reasons",
        "interaction_reasons",
        "fm_gate_reasons",
        "ignored_legacy_request_fields",
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
        if self.interaction is Interaction.GUIDED_REFLECTION:
            if self.closure is not Closure.GUIDED_REFLECTION:
                raise ValueError("guided reflection requires its dedicated closure")
            if self.question_policy not in {
                QuestionPolicy.FORBIDDEN,
                QuestionPolicy.OPTIONAL_ONE_NON_LEADING,
            }:
                raise ValueError("guided reflection requires a bounded question policy")
            if self.fm_ir_020_eligible:
                raise ValueError("guided reflection cannot authorize FM-IR-020")
        elif self.question_policy not in {
            QuestionPolicy.FORBIDDEN,
            QuestionPolicy.NOT_APPLICABLE,
        }:
            raise ValueError(
                "non-reflective interaction can only forbid questions globally"
            )
        if (
            self.fm_ir_020_eligible
            and self.closure is not Closure.CONSENTED_COACHING
        ):
            raise ValueError("FM-IR-020 requires a consented coaching closure")
        if (
            self.intervention_authorized
            and self.closure is not Closure.CONSENTED_COACHING
        ):
            raise ValueError("an authorized intervention requires consented coaching")
        if (
            self.interaction is not Interaction.BEHAVIORAL_INTERVENTION
            and (self.intervention_authorized or self.fm_ir_020_eligible)
        ):
            raise ValueError(
                "intervention authorization and FM-IR-020 require "
                "behavioral intervention"
            )
        if self.fm_ir_020_eligible and not self.intervention_authorized:
            raise ValueError("FM-IR-020 requires an authorized intervention")
        if (
            self.fm_ir_020_eligible
            and self.fm_effective_level is FMLevel.OFF
        ):
            raise ValueError("FM-IR-020 cannot be eligible while FM is off")
        if (
            self.response_mode is ResponseMode.HIGH_STAKES
            and self.interaction is not Interaction.DIRECT
        ):
            raise ValueError("HIGH_STAKES forces direct interaction")
        if (
            self.fm_application_gate is not GateState.PASS
            and self.interaction is Interaction.BEHAVIORAL_INTERVENTION
        ):
            raise ValueError("FM application veto forbids behavioral intervention")
        expected_disposition = (
            ControllingPolicyDisposition.DEFER_TO_CONTROLLING_POLICY
            if self.response_mode is ResponseMode.HIGH_STAKES
            else (
                ControllingPolicyDisposition.FM_APPLICATION_VETO
                if self.fm_application_gate is not GateState.PASS
                else ControllingPolicyDisposition.NONE
            )
        )
        if self.controlling_policy_disposition is not expected_disposition:
            raise ValueError("controlling policy disposition is inconsistent")
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
            r"i (?:want|plan|intend|am going) to (?:hurt|harm) myself|"
            r"i (?:might|may) (?:hurt|harm) myself|"
            r"i am thinking (?:about|of) (?:hurting|harming) myself|"
            r"i (?:do not|don't) want to live|"
            r"i (?:am|feel) suicidal|i (?:am going to|may) kill myself|"
            r"i (?:am|have been) self[- ]harming)\b"
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
            r"\bi (?:have|am having) crushing chest pain\b|"
            r"\bi (?:have|am having) shortness of breath\b|"
            r"\bi (?:cannot|can't) breathe\b|\bi (?:have )?overdosed\b|"
            r"\b(?:i am|someone is) unconscious\b|\bi fainted twice\b|"
            r"\bi (?:cannot|can't) bear weight\b.*\bdeform(?:ed|ity)\b"
        ),
    ),
    (
        "dangerous_restriction_or_eating_disorder",
        re.compile(
            r"\bi\b[^.?!]{0,160}\b(?:500 calories|starving myself|"
            r"purging (?:every day|daily)|"
            r"faint(?:ed|ing) (?:during|after) (?:a )?workout)\b|"
            r"\bi (?:have not|haven't) eaten (?:in|for) \d+ days\b"
        ),
    ),
    (
        "dangerous_intoxication_or_withdrawal",
        re.compile(
            r"\bi (?:am|have been) shaking and seeing things\b|"
            r"\bi stopped drinking\b.*\b(?:shaking|hallucinating|seeing things)\b|"
            r"\bi am hallucinating after (?:stopping|quitting) alcohol\b|"
            r"\bi am dangerously intoxicated\b"
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
            r"\bmy (?:wife|husband|partner|child|parent|mother|father|"
            r"friend|sibling|brother|sister) "
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
        r"\b(?:python|sql|api|software|backend|frontend|qdrant|postgres|docker|"
        r"kubernetes|systemctl|journalctl|nginx|git|worktree|unit test|"
        r"function|database|server)\b"
    ),
    re.compile(
        r"\b(?:python|typescript|javascript|java|ruby|c\+\+) class\b|"
        r"\b(?:systemd|backend|frontend|web|api) service\b"
    ),
    re.compile(r"\b(?:debug|implement|deploy|restart|patch|trace|compile)\b"),
)

_LOCAL_FM_EXPLICIT_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brelational monism\b"),
    re.compile(r"\brm (?:lens|view|philosophy|framework|idea)\b"),
    re.compile(r"\bapply (?:the )?rm lens\b"),
)

_LOCAL_COACHING_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhelp me (?:stop|start|change|build|track|improve)\b"),
    re.compile(r"\bi (?:want|need) to (?:change|track|test|improve|stop|start)\b"),
    re.compile(r"\bi keep (?:missing|avoiding|forgetting|doing)\b"),
    re.compile(r"\b(?:build a habit|let'?s track|run an experiment)\b"),
)

_LOCAL_DIRECT_RESPONSE_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:give me your recommendation|tell me which|"
        r"tell me what (?:i|we) should do|what do you recommend|"
        r"answer (?:the question |me )?directly)\b"
    ),
)

_LOCAL_RESPONSE_REQUEST_SHAPE_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\?"),
    re.compile(
        r"^(?:please )?(?:(?:can|could|would|will|should) "
        r"(?:you|we|i|this|that|it)|"
        r"(?:do|does|did|is|are|am|was|were|what|why|how|when|where|who|which))\b"
    ),
    re.compile(
        r"^(?:please )?(?:answer|tell|explain|describe|summarize|compare|"
        r"define|review|analyze|show|give|write|create|make|set|use|find|check|"
        r"fix|implement|deploy|restart|run|list|identify|recommend|advise|"
        r"calculate|continue|help)\b"
    ),
    re.compile(
        r"^i (?:want|need|would like) "
        r"(?:you to|your help|help(?: with| to)?)\b"
    ),
    re.compile(
        r"\b(?:let us|let's) "
        r"(?:continue|implement|design|build|make|set|try|test|track|"
        r"review|analyze)\b"
    ),
)

_LOCAL_GUIDED_REFLECTION_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:help me|i want to|can we) "
        r"(?:think|reflect|talk|work) (?:this |it )?through\b"
    ),
    re.compile(r"\bthink (?:this|it) through with me\b"),
    re.compile(r"\bhelp me understand why\b"),
    re.compile(r"\bhelp me reflect (?:on|about)\b"),
    re.compile(
        r"\b(?:ask|guide) me (?:with )?(?:one|1) "
        r"(?:useful |non-leading )?question\b"
    ),
    re.compile(r"\bhelp me get unstuck (?:creatively|without advice)\b"),
)

_LOCAL_BEHAVIORAL_INTERVENTION_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bhelp me (?:design|build|make|set up) "
        r"(?:a |an |one )?(?:plan|change|experiment|tracker|protocol)\b"
    ),
    re.compile(r"\b(?:run|design|track) (?:a |an )?(?:experiment|change|intervention)\b"),
    re.compile(r"\btrack whether (?:it|this|the change) helps\b"),
)

_LOCAL_DECLINES_QUESTIONS_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:do not|don't) ask (?:me )?(?:any |further )?questions\b"),
    re.compile(r"\bwithout asking (?:me )?(?:a |any )?questions?\b"),
)

_LOCAL_TECHNICAL_PROCEDURE_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:implement|apply|patch|deploy|restart|install|configure|"
        r"migrate|run|repair|fix|edit|change|build|create) "
        r"(?:this|the|an?|my)\b"
    ),
    re.compile(
        r"\b(?:walk me through|one command at a time|exact patch|"
        r"verification command)\b"
    ),
)

_LOCAL_TECHNICAL_EXPLANATION_RULES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bexplain (?:why|how)\b"),
    re.compile(r"\b(?:analyze|diagnose|review) (?:why|how|the)\b"),
)


def _matched_codes(
    text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]
) -> tuple[str, ...]:
    return tuple(sorted(code for code, pattern in rules if pattern.search(text)))


def _matches_any(text: str, rules: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in rules)


def _local_behavioral_intervention_requested(text: str) -> bool:
    """Match explicit intervention requests without activating negated phrases."""

    for pattern in _LOCAL_BEHAVIORAL_INTERVENTION_RULES:
        for match in pattern.finditer(text):
            prefix = text[: match.start()]
            clause_start = max(
                prefix.rfind("."),
                prefix.rfind("?"),
                prefix.rfind("!"),
                prefix.rfind(";"),
                prefix.rfind(":"),
            )
            clause_prefix = prefix[clause_start + 1 :]
            adversative = tuple(re.finditer(r"\b(?:but|however)\b", clause_prefix))
            if adversative:
                clause_prefix = clause_prefix[adversative[-1].end() :]
            if re.search(
                r"\b(?:do not|don't|never|"
                r"(?:i am|i'm) not asking (?:you )?to|"
                r"not asking (?:you )?to)\b",
                clause_prefix,
            ):
                continue
            return True
    return False


def _select_mode(
    text: str,
    signals: ResponsePolicySignalsV0_2,
    safety_assessment: SafetyAssessmentV0_2,
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
    if safety_assessment.high_stakes_gate is GateState.TRIGGERED:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.TRIGGERED,
            tuple(
                f"safety_assessment:{code}"
                for code in safety_assessment.reason_codes
            )
            or ("safety_assessment:high_stakes",),
        )
    if safety_assessment.high_stakes_gate is GateState.UNCERTAIN:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.UNCERTAIN,
            tuple(
                f"safety_assessment:{code}"
                for code in safety_assessment.reason_codes
            )
            or ("safety_assessment:uncertain",),
        )
    if signals.domain_risk_gate is GateState.TRIGGERED:
        return (
            ResponseMode.HIGH_STAKES,
            GateState.TRIGGERED,
            tuple(
                f"domain_risk:{code}"
                for code in signals.domain_risk_reason_codes
            ),
        )
    classifier_unavailable = (
        signals.domain_risk_gate is GateState.UNCERTAIN
        and signals.domain_risk_reason_codes
        == (DOMAIN_CLASSIFIER_UNAVAILABLE_REASON,)
    )
    if (
        signals.domain_risk_gate is GateState.UNCERTAIN
        and not classifier_unavailable
    ):
        return (
            ResponseMode.HIGH_STAKES,
            GateState.UNCERTAIN,
            tuple(
                f"domain_risk:{code}"
                for code in signals.domain_risk_reason_codes
            ),
        )

    degraded_reasons = (
        (DOMAIN_CLASSIFIER_UNAVAILABLE_REASON,)
        if classifier_unavailable
        else ()
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
            return (
                mode,
                GateState.PASS,
                tuple(
                    sorted(
                        (*degraded_reasons, f"trusted_{mode.value.lower()}_signal")
                    )
                ),
            )
        if trusted is None and detected:
            return (
                mode,
                GateState.PASS,
                tuple(
                    sorted(
                        (*degraded_reasons, f"local_{mode.value.lower()}_signal")
                    )
                ),
            )
    return (
        ResponseMode.ORDINARY,
        GateState.PASS,
        tuple(sorted((*degraded_reasons, "ordinary_default"))),
    )


def _select_closure(
    mode: ResponseMode,
    interaction: Interaction,
    intervention_authorized: bool,
    text: str,
    signals: ResponsePolicySignalsV0_2,
    safety_assessment: SafetyAssessmentV0_2,
    local_high_stakes: tuple[str, ...],
) -> Closure:
    local_next_step = bool(
        re.search(
            r"\b(?:give me next steps|make (?:me )?a plan|what should i do next)\b",
            text,
        )
    )
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
        if (
            safety_assessment.safety_action_required
            or signals.domain_safety_action_required
            or local_action_categories.intersection(local_high_stakes)
        ):
            return Closure.SAFETY_ACTION
        return Closure.COMPLETE

    if interaction is Interaction.GUIDED_REFLECTION:
        return Closure.GUIDED_REFLECTION

    if interaction is Interaction.BEHAVIORAL_INTERVENTION:
        if intervention_authorized:
            return Closure.CONSENTED_COACHING
        return Closure.COMPLETE

    if (
        interaction is Interaction.DIRECT
        and _matches_any(text, _LOCAL_DIRECT_RESPONSE_RULES)
        and not local_next_step
    ):
        return Closure.COMPLETE

    if mode is ResponseMode.TECHNICAL:
        local_procedure = _matches_any(
            text, _LOCAL_TECHNICAL_PROCEDURE_RULES
        )
        local_explanation = _matches_any(
            text, _LOCAL_TECHNICAL_EXPLANATION_RULES
        )
        if local_explanation and not local_procedure:
            return Closure.COMPLETE
        if local_procedure:
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

    if signals.explicit_next_step_requested is True or (
        signals.explicit_next_step_requested is None and local_next_step
    ):
        return Closure.EXPLICIT_NEXT_STEP
    return Closure.COMPLETE


def _trusted_or_local(
    trusted: bool | None,
    detected: bool,
) -> bool:
    if trusted is not None:
        return trusted
    return detected


def _fm_ir_020_prerequisites_satisfied(
    signals: ResponsePolicySignalsV0_2,
) -> bool:
    return all(
        value is True
        for value in (
            signals.specific_experiment_consent,
            signals.experiment_reversible_and_proportionate,
            signals.experiment_measurement_defined,
            signals.experiment_adverse_indicators_defined,
            signals.experiment_stop_rule_defined,
        )
    )


def _select_interaction(
    mode: ResponseMode,
    text: str,
    signals: ResponsePolicySignalsV0_2,
    application_gate: GateState,
) -> tuple[Interaction, QuestionPolicy, tuple[str, ...], bool]:
    classifier_unavailable = (
        signals.domain_risk_gate is GateState.UNCERTAIN
        and signals.domain_risk_reason_codes
        == (DOMAIN_CLASSIFIER_UNAVAILABLE_REASON,)
    )
    declines_questions = _trusted_or_local(
        signals.user_declines_questions,
        _matches_any(text, _LOCAL_DECLINES_QUESTIONS_RULES),
    )
    if mode is ResponseMode.HIGH_STAKES:
        return (
            Interaction.DIRECT,
            QuestionPolicy.NOT_APPLICABLE,
            ("high_stakes_forces_direct",),
            False,
        )
    if application_gate is not GateState.PASS and not classifier_unavailable:
        reasons = ["fm_application_boundary_forces_direct"]
        if declines_questions:
            reasons.append("user_declined_questions")
        return (
            Interaction.DIRECT,
            (
                QuestionPolicy.FORBIDDEN
                if declines_questions
                else QuestionPolicy.NOT_APPLICABLE
            ),
            tuple(sorted(reasons)),
            False,
        )

    direct = _trusted_or_local(
        signals.direct_response_requested,
        _matches_any(text, _LOCAL_DIRECT_RESPONSE_RULES),
    )
    local_intervention = _local_behavioral_intervention_requested(text)
    if mode is ResponseMode.TECHNICAL:
        local_intervention = False
    intervention = _trusted_or_local(
        signals.behavioral_intervention_requested,
        local_intervention,
    )
    if classifier_unavailable:
        intervention = False
    reflection = _trusted_or_local(
        signals.guided_reflection_requested,
        _matches_any(text, _LOCAL_GUIDED_REFLECTION_RULES),
    )

    if direct:
        reasons = ["direct_response_requested"]
        if declines_questions:
            reasons.append("user_declined_questions")
        return (
            Interaction.DIRECT,
            (
                QuestionPolicy.FORBIDDEN
                if declines_questions
                else QuestionPolicy.NOT_APPLICABLE
            ),
            tuple(sorted(reasons)),
            False,
        )
    if intervention:
        reasons = ["behavioral_intervention_requested"]
        if declines_questions:
            reasons.append("user_declined_questions")
        intervention_authorized = _fm_ir_020_prerequisites_satisfied(signals)
        if signals.specific_experiment_consent is True:
            reasons.append("specific_experiment_consent_confirmed")
        else:
            reasons.append("specific_experiment_consent_missing")
        if intervention_authorized:
            reasons.append("intervention_authorized")
            reasons.append("fm_ir_020_prerequisites_satisfied")
        else:
            reasons.append("fm_ir_020_prerequisites_incomplete")
        return (
            Interaction.BEHAVIORAL_INTERVENTION,
            (
                QuestionPolicy.FORBIDDEN
                if declines_questions
                else QuestionPolicy.NOT_APPLICABLE
            ),
            tuple(sorted(reasons)),
            intervention_authorized,
        )
    if reflection:
        reasons = ["guided_reflection_requested"]
        if declines_questions:
            reasons.append("user_declined_questions")
        return (
            Interaction.GUIDED_REFLECTION,
            (
                QuestionPolicy.FORBIDDEN
                if declines_questions
                else QuestionPolicy.OPTIONAL_ONE_NON_LEADING
            ),
            tuple(sorted(reasons)),
            False,
        )
    if _matches_any(text, _LOCAL_RESPONSE_REQUEST_SHAPE_RULES):
        reasons = ["direct_request_shape"]
        if declines_questions:
            reasons.append("user_declined_questions")
        return (
            Interaction.DIRECT,
            (
                QuestionPolicy.FORBIDDEN
                if declines_questions
                else QuestionPolicy.NOT_APPLICABLE
            ),
            tuple(sorted(reasons)),
            False,
        )
    reasons = ["conversational_update_default"]
    if declines_questions:
        reasons.append("user_declined_questions")
    return (
        Interaction.CONVERSATIONAL,
        (
            QuestionPolicy.FORBIDDEN
            if declines_questions
            else QuestionPolicy.NOT_APPLICABLE
        ),
        tuple(sorted(reasons)),
        False,
    )


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
    safety_assessment: SafetyAssessmentV0_2 | None = None,
    signals: ResponsePolicySignalsV0_2 | None = None,
) -> ResponsePolicyDecisionV0_2:
    """Return one deterministic decision without performing a side effect."""

    if safety_assessment is None:
        raise ResponsePolicyContractError("a completed safety assessment is required")
    try:
        request = _wire_revalidate(ResponsePolicyInputV0_2, request)
        safety_assessment = _wire_revalidate(
            SafetyAssessmentV0_2, safety_assessment
        )
        trusted = _wire_revalidate(
            ResponsePolicySignalsV0_2, signals or ResponsePolicySignalsV0_2()
        )
    except (TypeError, ValueError) as exc:
        raise ResponsePolicyContractError("invalid response-policy input manifest") from exc

    request_bindings = (
        request.request_id,
        request.request_sha256,
        request.current_message_sha256,
        request.conversation_sha256,
    )
    assessment_bindings = (
        safety_assessment.request_id,
        safety_assessment.request_sha256,
        safety_assessment.current_message_sha256,
        safety_assessment.conversation_sha256,
    )
    if assessment_bindings != request_bindings:
        raise ResponsePolicyContractError(
            "safety assessment does not bind to the policy request"
        )
    expected_assessed_user_hashes = tuple(
        hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        for item in request.conversation
        if item.role is ConversationRole.USER
    )
    if (
        safety_assessment.assessed_scope != "full_user_conversation"
        or safety_assessment.assessed_user_message_sha256s
        != expected_assessed_user_hashes
    ):
        raise ResponsePolicyContractError(
            "safety assessment does not cover the full user conversation"
        )

    text = _normalized(request.current_message.content)
    user_texts = (
        _normalized(item.content)
        for item in request.conversation
        if item.role is ConversationRole.USER
    )
    local_high_stakes = tuple(
        sorted(
            {
                code
                for user_text in user_texts
                for code in _matched_codes(user_text, _LOCAL_HIGH_STAKES_RULES)
            }
        )
    )
    user_texts = (
        _normalized(item.content)
        for item in request.conversation
        if item.role is ConversationRole.USER
    )
    local_boundaries = tuple(
        sorted(
            {
                code
                for user_text in user_texts
                for code in _matched_codes(user_text, _LOCAL_APPLICATION_BOUNDARY_RULES)
            }
        )
    )

    mode, high_stakes_gate, mode_reasons = _select_mode(
        text, trusted, safety_assessment, local_high_stakes
    )
    default_fm = _default_fm_level(mode, trusted)
    application_gate, application_reasons = _application_gate(
        high_stakes_gate, local_boundaries, trusted
    )
    interaction, question_policy, interaction_reasons, intervention_authorized = (
        _select_interaction(mode, text, trusted, application_gate)
    )
    closure = _select_closure(
        mode,
        interaction,
        intervention_authorized,
        text,
        trusted,
        safety_assessment,
        local_high_stakes,
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
    fm_ir_020_eligible = (
        intervention_authorized and effective_fm is not FMLevel.OFF
    )
    controlling_policy_disposition = (
        ControllingPolicyDisposition.DEFER_TO_CONTROLLING_POLICY
        if mode is ResponseMode.HIGH_STAKES
        else (
            ControllingPolicyDisposition.FM_APPLICATION_VETO
            if application_gate is not GateState.PASS
            else ControllingPolicyDisposition.NONE
        )
    )

    reasons = list(mode_reasons)
    requested_profile = (request.requested_assistant_profile_id or "").strip()
    if (
        requested_profile
        and requested_profile.casefold() != ASSISTANT_PROFILE_ID.casefold()
    ):
        reasons.append("requested_assistant_profile_rejected")

    ignored = tuple(
        sorted(LEGACY_REQUEST_FIELDS.intersection(request.request_field_names))
    )
    if ignored:
        reasons.append("legacy_request_fields_ignored")

    payload = _ResponsePolicyDecisionPayloadV0_2(
        contract_version=POLICY_DECISION_VERSION,
        policy_version=POLICY_VERSION,
        assistant_profile_id=ASSISTANT_PROFILE_ID,
        request_id=request.request_id,
        request_sha256=request.request_sha256,
        current_message_sha256=request.current_message_sha256,
        conversation_sha256=request.conversation_sha256,
        safety_assessment_sha256=safety_assessment.assessment_sha256,
        response_mode=mode,
        interaction=interaction,
        question_policy=question_policy,
        closure=closure,
        mode_reasons=tuple(sorted(set(reasons))),
        interaction_reasons=tuple(sorted(set(interaction_reasons))),
        intervention_authorized=intervention_authorized,
        fm_ir_020_eligible=fm_ir_020_eligible,
        controlling_policy_disposition=controlling_policy_disposition,
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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ResponsePolicyContractError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _wire_document(value: str | bytes) -> bytes:
    try:
        document = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ResponsePolicyContractError(f"invalid JSON constant: {constant}")
            ),
        )
    except ResponsePolicyContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ResponsePolicyContractError("invalid response-policy JSON") from exc
    return _canonical_json_bytes(document)


def parse_response_policy_input_v0_2(
    value: str | bytes,
) -> ResponsePolicyInputV0_2:
    try:
        return ResponsePolicyInputV0_2.model_validate_json(_wire_document(value))
    except ResponsePolicyContractError:
        raise
    except ValueError as exc:
        raise ResponsePolicyContractError("invalid response-policy input") from exc


def parse_safety_assessment_v0_2(value: str | bytes) -> SafetyAssessmentV0_2:
    try:
        return SafetyAssessmentV0_2.model_validate_json(_wire_document(value))
    except ResponsePolicyContractError:
        raise
    except ValueError as exc:
        raise ResponsePolicyContractError("invalid safety assessment") from exc


def parse_response_policy_decision_v0_2(
    value: str | bytes,
) -> ResponsePolicyDecisionV0_2:
    try:
        return ResponsePolicyDecisionV0_2.model_validate_json(_wire_document(value))
    except ResponsePolicyContractError:
        raise
    except ValueError as exc:
        raise ResponsePolicyContractError("invalid response-policy decision") from exc


__all__ = [
    "ASSISTANT_PROFILE_ID",
    "DEFAULT_SAFETY_COMPONENT",
    "DOMAIN_CLASSIFIER_UNAVAILABLE_REASON",
    "LEGACY_REQUEST_FIELDS",
    "MODE_PRECEDENCE",
    "POLICY_INPUT_VERSION",
    "POLICY_DECISION_VERSION",
    "POLICY_SIGNALS_VERSION",
    "POLICY_VERSION",
    "SAFETY_ASSESSMENT_VERSION",
    "SAFETY_ASSESSOR_VERSION",
    "Closure",
    "ConversationRole",
    "ControllingPolicyDisposition",
    "FMLevel",
    "GateState",
    "Interaction",
    "QuestionPolicy",
    "ResponseMode",
    "ResponsePolicyContractError",
    "ResponsePolicyConversationMessageV0_2",
    "ResponsePolicyDecisionV0_2",
    "ResponsePolicyInputV0_2",
    "ResponsePolicySignalsV0_2",
    "SafetyAssessmentV0_2",
    "decide_response_policy_v0_2",
    "parse_response_policy_decision_v0_2",
    "parse_response_policy_input_v0_2",
    "parse_safety_assessment_v0_2",
]
