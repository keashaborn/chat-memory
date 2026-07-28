from __future__ import annotations

"""Trusted, side-effect-free composition of the RESSE response plan.

This module is not imported by the live route.  It is the backend-owned seam
between authenticated request handling, safety assessment, governed Memory,
canonical FM selection, and provider-neutral prompt assembly.  Browser values
can be represented only by their field names for legacy-audit purposes; their
values have no input path here.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.response_conversation_snapshot_v1 import ConversationSnapshotV1
from rag_engine.fm_selection_envelope_v0_2 import (
    FMSelectionEnvelopeV02,
    FMSelectionRequestV02,
    select_fm_v0_2,
)
from rag_engine.memory_prompt_renderer_v1 import MemoryPromptApplicationResultV1
from rag_engine.memory_v1_selection_envelope import MemoryPromptAssemblyInputV1
from rag_engine.prompt_assembler_v1 import (
    AssembledPromptV1,
    PromptAssemblyRequestV1,
    assemble_prompt,
)
from rag_engine.response_policy_prompt_v0_2 import (
    ResponsePolicyPromptV0_2,
    render_response_policy_prompt_v0_2,
)
from rag_engine.response_policy_v0_2 import (
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyDecisionV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)
from rag_engine.search_capability_manifest_v1 import SearchCapabilityManifestV1
from rag_engine.voice_language_v1 import (
    DEFAULT_VOICE_LANGUAGE,
    SUPPORTED_VOICE_LANGUAGE_IDS,
)


TRUSTED_REQUEST_VERSION = "trusted_response_request_v0_2"
TRUSTED_PLAN_VERSION = "trusted_response_plan_v0_2"
TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION = (
    "trusted_response_policy_signals_envelope_v0_2"
)
SHADOW_TRACE_VERSION = "resse_response_shadow_trace_v0_3"
ORCHESTRATOR_VERSION = "resse_response_orchestrator_v0_2"
TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2 = (
    "openai_moderation_adapter_v0_2",
)
TRUSTED_POLICY_SIGNAL_COMPONENTS_V0_2 = (
    "server_response_signal_classifier_v0_2",
)


class ResponseOrchestrationError(RuntimeError):
    """Generic fail-closed error at the trusted orchestration boundary."""


class _StrictFrozenModel(BaseModel):
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


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class SafetyAssessmentProviderV0_2(Protocol):
    def assess(
        self, request: ResponsePolicyInputV0_2
    ) -> SafetyAssessmentV0_2 | Awaitable[SafetyAssessmentV0_2]: ...


class TrustedPolicySignalsEnvelopeV0_2(_StrictFrozenModel):
    """Server-classified policy signals bound to one transcript snapshot."""

    contract_version: Literal[TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION] = (
        TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION
    )
    conversation_snapshot_sha256: str
    source_components: tuple[str, ...]
    signals: ResponsePolicySignalsV0_2
    envelope_sha256: str

    @field_validator("conversation_snapshot_sha256", "envelope_sha256")
    @classmethod
    def hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("policy-signal envelope hashes must be lowercase SHA-256")
        return value

    @field_validator("source_components")
    @classmethod
    def exact_source_components(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != TRUSTED_POLICY_SIGNAL_COMPONENTS_V0_2:
            raise ValueError("policy-signal envelope uses an untrusted source bundle")
        return value

    @model_validator(mode="after")
    def exact_manifest(self) -> "TrustedPolicySignalsEnvelopeV0_2":
        payload = self.model_dump(mode="json", exclude={"envelope_sha256"})
        if self.envelope_sha256 != _sha256(payload):
            raise ValueError("policy-signal envelope manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        conversation_snapshot: ConversationSnapshotV1,
        signals: ResponsePolicySignalsV0_2 | None = None,
    ) -> "TrustedPolicySignalsEnvelopeV0_2":
        try:
            snapshot = ConversationSnapshotV1.model_validate_json(
                conversation_snapshot.model_dump_json()
            )
            verified_signals = ResponsePolicySignalsV0_2.model_validate_json(
                (
                    signals
                    if signals is not None
                    else ResponsePolicySignalsV0_2()
                ).model_dump_json()
            )
        except Exception:
            raise ResponseOrchestrationError(
                "policy-signal envelope requires trusted typed inputs"
            ) from None
        payload: dict[str, Any] = {
            "contract_version": TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION,
            "conversation_snapshot_sha256": snapshot.snapshot_sha256,
            "source_components": TRUSTED_POLICY_SIGNAL_COMPONENTS_V0_2,
            "signals": verified_signals.model_dump(mode="json"),
        }
        payload["envelope_sha256"] = _sha256(payload)
        return cls.model_validate_json(_canonical_json_bytes(payload))


class TrustedResponseRequestV0_2(_StrictFrozenModel):
    """Inputs available only after authenticated owner and thread checks."""

    contract_version: Literal[TRUSTED_REQUEST_VERSION] = TRUSTED_REQUEST_VERSION
    authenticated_actor_user_id: UUID = Field(repr=False)
    conversation_snapshot: ConversationSnapshotV1 = Field(
        repr=False,
    )
    legacy_request_field_names: tuple[str, ...] = ()
    trusted_policy_signals_envelope: TrustedPolicySignalsEnvelopeV0_2 = Field(
        repr=False,
    )
    memory_input: MemoryPromptAssemblyInputV1 | None = Field(
        default=None,
        repr=False,
    )
    memory_application: MemoryPromptApplicationResultV1 | None = Field(
        default=None,
        repr=False,
    )
    fm_token_budget: int | None = Field(default=None, ge=0, le=1600)
    search_capability_manifest: SearchCapabilityManifestV1 | None = Field(
        default=None,
        repr=False,
    )
    response_language: str = DEFAULT_VOICE_LANGUAGE

    @field_validator("legacy_request_field_names")
    @classmethod
    def sorted_unique_field_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("legacy request field names must be sorted and unique")
        return value

    @field_validator("response_language")
    @classmethod
    def valid_response_language(cls, value: str) -> str:
        if value not in SUPPORTED_VOICE_LANGUAGE_IDS:
            raise ValueError("response language is unsupported")
        return value

    @model_validator(mode="after")
    def owner_and_source_pairing(self) -> "TrustedResponseRequestV0_2":
        if (
            self.conversation_snapshot.authenticated_actor_user_id
            != self.authenticated_actor_user_id
        ):
            raise ValueError(
                "conversation snapshot actor differs from authenticated request actor"
            )
        if (
            self.trusted_policy_signals_envelope.conversation_snapshot_sha256
            != self.conversation_snapshot.snapshot_sha256
        ):
            raise ValueError(
                "policy-signal envelope differs from the conversation snapshot"
            )
        if (self.memory_input is None) != (self.memory_application is None):
            raise ValueError("Memory input and application must be supplied together")
        if self.memory_input is not None:
            context = self.memory_input.context
            if context.authenticated_actor_user_id != self.authenticated_actor_user_id:
                raise ValueError("Memory actor differs from authenticated request actor")
            if context.owner_user_id != self.authenticated_actor_user_id:
                raise ValueError("Memory owner differs from authenticated request actor")
            if context.thread_id != self.thread_id:
                raise ValueError("Memory thread differs from trusted request thread")
            if context.request_id_sha256 != _text_sha256(self.request_id):
                raise ValueError("Memory request differs from trusted response request")
            current_message_sha256 = _text_sha256(self.conversation[-1].content)
            if context.query_sha256 != current_message_sha256:
                raise ValueError("Memory query differs from the current user message")
        return self

    @property
    def thread_id(self) -> UUID:
        return self.conversation_snapshot.thread_id

    @property
    def conversation_snapshot_sha256(self) -> str:
        return self.conversation_snapshot.snapshot_sha256

    @property
    def request_id(self) -> str:
        return self.conversation_snapshot.current_request_id

    @property
    def conversation(
        self,
    ) -> tuple[ResponsePolicyConversationMessageV0_2, ...]:
        return self.conversation_snapshot.messages

    @property
    def trusted_policy_signals(self) -> ResponsePolicySignalsV0_2:
        return self.trusted_policy_signals_envelope.signals

    @classmethod
    def create_from_snapshot(
        cls,
        *,
        authenticated_actor_user_id: UUID,
        conversation_snapshot: ConversationSnapshotV1,
        request_field_names: tuple[str, ...] = (),
        trusted_policy_signals_envelope: (
            TrustedPolicySignalsEnvelopeV0_2 | None
        ) = None,
        memory_input: MemoryPromptAssemblyInputV1 | None = None,
        memory_application: MemoryPromptApplicationResultV1 | None = None,
        fm_token_budget: int | None = None,
        search_capability_manifest: SearchCapabilityManifestV1 | None = None,
        response_language: str = DEFAULT_VOICE_LANGUAGE,
    ) -> "TrustedResponseRequestV0_2":
        """Create from trusted values; request values are deliberately absent."""

        if not isinstance(conversation_snapshot, ConversationSnapshotV1):
            raise ResponseOrchestrationError(
                "trusted request requires ConversationSnapshotV1"
            )
        try:
            snapshot = ConversationSnapshotV1.model_validate_json(
                conversation_snapshot.model_dump_json()
            )
        except Exception:
            raise ResponseOrchestrationError(
                "trusted request requires a valid conversation snapshot"
            ) from None
        if trusted_policy_signals_envelope is None:
            signal_envelope = TrustedPolicySignalsEnvelopeV0_2.create(
                conversation_snapshot=snapshot,
            )
        else:
            try:
                signal_envelope = TrustedPolicySignalsEnvelopeV0_2.model_validate_json(
                    trusted_policy_signals_envelope.model_dump_json()
                )
            except Exception:
                raise ResponseOrchestrationError(
                    "trusted request requires a valid policy-signal envelope"
                ) from None
        return cls(
            authenticated_actor_user_id=authenticated_actor_user_id,
            conversation_snapshot=snapshot,
            legacy_request_field_names=tuple(sorted(set(request_field_names))),
            trusted_policy_signals_envelope=signal_envelope,
            memory_input=memory_input,
            memory_application=memory_application,
            fm_token_budget=fm_token_budget,
            search_capability_manifest=search_capability_manifest,
            response_language=response_language,
        )


class SanitizedResponseShadowTraceV0_2(_StrictFrozenModel):
    """Content-independent shadow artifact for bounded observability.

    Integrity hashes that transitively include request, conversation, Memory,
    FM-query, or prompt content belong only to ``TrustedResponsePlanV0_2``.
    They are deliberately absent here so exporting this model cannot create a
    dictionary-recoverable or cross-trace-linkable content identifier.
    """

    contract_version: Literal[SHADOW_TRACE_VERSION] = SHADOW_TRACE_VERSION
    orchestrator_version: Literal[ORCHESTRATOR_VERSION] = ORCHESTRATOR_VERSION
    occurred_at: datetime
    correlation_id: UUID
    response_mode: str
    interaction_version: str
    interaction: str
    interaction_reason_codes: tuple[str, ...]
    interaction_instruction_sha256: str
    closure_instruction_sha256: str
    closure: str
    high_stakes_gate: str
    safety_action_required: bool
    safety_reason_codes: tuple[str, ...]
    mode_reason_codes: tuple[str, ...]
    fm_level: str
    fm_gate_reason_codes: tuple[str, ...]
    fm_selection_status: str
    fm_selected_record_count: int = Field(ge=0, le=8)
    memory_present: bool
    context_block_count: int = Field(ge=0, le=2)
    total_input_bytes: int = Field(ge=1)
    total_input_tokens: int = Field(ge=1)
    ignored_legacy_request_fields: tuple[str, ...]
    trace_sha256: str

    @field_validator("occurred_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("shadow trace time must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator(
        "trace_sha256",
        "interaction_instruction_sha256",
        "closure_instruction_sha256",
    )
    @classmethod
    def hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("shadow trace hashes must be lowercase SHA-256")
        return value

    @field_validator(
        "safety_reason_codes",
        "mode_reason_codes",
        "interaction_reason_codes",
        "fm_gate_reason_codes",
        "ignored_legacy_request_fields",
    )
    @classmethod
    def sorted_unique_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("shadow trace code arrays must be sorted and unique")
        return value

    @model_validator(mode="after")
    def exact_manifest(self) -> "SanitizedResponseShadowTraceV0_2":
        payload = self.model_dump(mode="json", exclude={"trace_sha256"})
        if self.trace_sha256 != _sha256(payload):
            raise ValueError("shadow trace manifest hash mismatch")
        return self

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TrustedResponsePlanV0_2(_StrictFrozenModel):
    """Internal plan. It contains private prompt material and must not be logged."""

    contract_version: Literal[TRUSTED_PLAN_VERSION] = TRUSTED_PLAN_VERSION
    authenticated_actor_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    conversation_snapshot_sha256: str
    source_snapshot: ConversationSnapshotV1 = Field(repr=False)
    policy_input: ResponsePolicyInputV0_2 = Field(repr=False)
    safety_assessment: SafetyAssessmentV0_2 = Field(repr=False)
    policy_signals_envelope: TrustedPolicySignalsEnvelopeV0_2 = Field(
        repr=False,
    )
    policy_decision: ResponsePolicyDecisionV0_2 = Field(repr=False)
    policy_prompt: ResponsePolicyPromptV0_2 = Field(repr=False)
    fm_selection: FMSelectionEnvelopeV02 = Field(repr=False)
    assembled_prompt: AssembledPromptV1 = Field(repr=False)
    shadow_trace: SanitizedResponseShadowTraceV0_2
    plan_sha256: str

    @field_validator("conversation_snapshot_sha256", "plan_sha256")
    @classmethod
    def plan_hash_shape(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("trusted plan hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def exact_plan_bindings(self) -> "TrustedResponsePlanV0_2":
        manifest = self.assembled_prompt.manifest
        if (
            self.source_snapshot.authenticated_actor_user_id
            != self.authenticated_actor_user_id
            or self.source_snapshot.thread_id != self.thread_id
            or self.source_snapshot.snapshot_sha256
            != self.conversation_snapshot_sha256
            or self.source_snapshot.current_request_id != self.policy_input.request_id
            or self.source_snapshot.messages != self.policy_input.conversation
            or self.policy_signals_envelope.conversation_snapshot_sha256
            != self.source_snapshot.snapshot_sha256
        ):
            raise ValueError("trusted plan differs from its conversation snapshot")
        checks = (
            (self.policy_input.request_sha256, manifest.request_sha256),
            (self.safety_assessment.assessment_sha256, manifest.safety_assessment_sha256),
            (self.policy_decision.decision_sha256, manifest.policy_decision_sha256),
            (self.policy_prompt.content_sha256, manifest.policy_prompt_sha256),
            (self.fm_selection.selection_sha256, manifest.fm_selection_sha256),
        )
        if any(actual != expected for actual, expected in checks):
            raise ValueError("trusted plan bindings do not reconcile")
        if self.policy_signals != self.assembled_prompt.source_request.policy_signals:
            raise ValueError("trusted plan signals differ from prompt assembly")
        expected_trace_values = (
            (self.shadow_trace.response_mode, self.policy_decision.response_mode.value),
            (
                self.shadow_trace.interaction_version,
                self.policy_prompt.interaction_version,
            ),
            (
                self.shadow_trace.interaction,
                self.policy_decision.interaction.value,
            ),
            (
                self.shadow_trace.interaction_reason_codes,
                tuple(sorted(self.policy_decision.interaction_reasons)),
            ),
            (
                self.shadow_trace.interaction_instruction_sha256,
                self.policy_prompt.interaction_instruction_sha256,
            ),
            (
                self.shadow_trace.closure_instruction_sha256,
                self.policy_prompt.closure_instruction_sha256,
            ),
            (self.shadow_trace.closure, self.policy_decision.closure.value),
            (
                self.shadow_trace.high_stakes_gate,
                self.policy_decision.high_stakes_gate.value,
            ),
            (
                self.shadow_trace.safety_action_required,
                self.safety_assessment.safety_action_required
                or self.policy_signals.domain_safety_action_required,
            ),
            (
                self.shadow_trace.safety_reason_codes,
                _combined_safety_reason_codes(
                    self.safety_assessment, self.policy_signals
                ),
            ),
            (
                self.shadow_trace.mode_reason_codes,
                tuple(sorted(self.policy_decision.mode_reasons)),
            ),
            (self.shadow_trace.fm_level, self.policy_decision.fm_effective_level.value),
            (
                self.shadow_trace.fm_gate_reason_codes,
                tuple(sorted(self.policy_decision.fm_gate_reasons)),
            ),
            (self.shadow_trace.fm_selection_status, self.fm_selection.status),
            (
                self.shadow_trace.fm_selected_record_count,
                len(self.fm_selection.selected_record_ids),
            ),
            (
                self.shadow_trace.memory_present,
                self.assembled_prompt.source_request.memory_input is not None,
            ),
            (
                self.shadow_trace.context_block_count,
                manifest.context_block_count,
            ),
            (self.shadow_trace.total_input_bytes, manifest.total_input_bytes),
            (self.shadow_trace.total_input_tokens, manifest.total_input_tokens),
            (
                self.shadow_trace.ignored_legacy_request_fields,
                tuple(sorted(self.policy_decision.ignored_legacy_request_fields)),
            ),
        )
        if any(actual != expected for actual, expected in expected_trace_values):
            raise ValueError("trusted plan shadow trace does not reconcile")
        payload = {
            "contract_version": self.contract_version,
            "actor_user_id_sha256": _text_sha256(self.authenticated_actor_user_id),
            "thread_id_sha256": _text_sha256(self.thread_id),
            "conversation_snapshot_sha256": self.conversation_snapshot_sha256,
            "request_sha256": self.policy_input.request_sha256,
            "safety_assessment_sha256": self.safety_assessment.assessment_sha256,
            "policy_signals_envelope_sha256": (
                self.policy_signals_envelope.envelope_sha256
            ),
            "policy_decision_sha256": self.policy_decision.decision_sha256,
            "policy_prompt_sha256": self.policy_prompt.content_sha256,
            "fm_selection_sha256": self.fm_selection.selection_sha256,
            "assembly_sha256": manifest.assembly_sha256,
            "shadow_trace_sha256": self.shadow_trace.trace_sha256,
        }
        if self.plan_sha256 != _sha256(payload):
            raise ValueError("trusted plan manifest hash mismatch")
        return self

    @property
    def policy_signals(self) -> ResponsePolicySignalsV0_2:
        return self.policy_signals_envelope.signals


def _plan_sha256(
    *,
    actor: UUID,
    thread_id: UUID,
    conversation_snapshot_sha256: str,
    policy_input: ResponsePolicyInputV0_2,
    safety: SafetyAssessmentV0_2,
    signals_envelope: TrustedPolicySignalsEnvelopeV0_2,
    decision: ResponsePolicyDecisionV0_2,
    prompt: ResponsePolicyPromptV0_2,
    fm: FMSelectionEnvelopeV02,
    assembled: AssembledPromptV1,
    trace: SanitizedResponseShadowTraceV0_2,
) -> str:
    return _sha256(
        {
            "contract_version": TRUSTED_PLAN_VERSION,
            "actor_user_id_sha256": _text_sha256(actor),
            "thread_id_sha256": _text_sha256(thread_id),
            "conversation_snapshot_sha256": conversation_snapshot_sha256,
            "request_sha256": policy_input.request_sha256,
            "safety_assessment_sha256": safety.assessment_sha256,
            "policy_signals_envelope_sha256": signals_envelope.envelope_sha256,
            "policy_decision_sha256": decision.decision_sha256,
            "policy_prompt_sha256": prompt.content_sha256,
            "fm_selection_sha256": fm.selection_sha256,
            "assembly_sha256": assembled.manifest.assembly_sha256,
            "shadow_trace_sha256": trace.trace_sha256,
        }
    )


def _combined_safety_reason_codes(
    safety: SafetyAssessmentV0_2,
    signals: ResponsePolicySignalsV0_2,
) -> tuple[str, ...]:
    domain = (
        tuple(f"domain_risk:{item}" for item in signals.domain_risk_reason_codes)
        if signals.domain_risk_gate.value != "pass"
        else ()
    )
    return tuple(sorted(set((*safety.reason_codes, *domain))))


def _shadow_trace(
    *,
    request: TrustedResponseRequestV0_2,
    safety: SafetyAssessmentV0_2,
    decision: ResponsePolicyDecisionV0_2,
    fm: FMSelectionEnvelopeV02,
    assembled: AssembledPromptV1,
    occurred_at: datetime,
    correlation_id: UUID,
) -> SanitizedResponseShadowTraceV0_2:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ResponseOrchestrationError("shadow trace clock must be timezone-aware")
    occurred_at = occurred_at.astimezone(timezone.utc)
    manifest = assembled.manifest
    values: dict[str, Any] = {
        "contract_version": SHADOW_TRACE_VERSION,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "occurred_at": occurred_at,
        "correlation_id": correlation_id,
        "response_mode": decision.response_mode.value,
        "interaction_version": assembled.source_request.policy_prompt.interaction_version,
        "interaction": decision.interaction.value,
        "interaction_reason_codes": tuple(sorted(decision.interaction_reasons)),
        "interaction_instruction_sha256": (
            assembled.source_request.policy_prompt.interaction_instruction_sha256
        ),
        "closure_instruction_sha256": (
            assembled.source_request.policy_prompt.closure_instruction_sha256
        ),
        "closure": decision.closure.value,
        "high_stakes_gate": decision.high_stakes_gate.value,
        "safety_action_required": (
            safety.safety_action_required
            or request.trusted_policy_signals.domain_safety_action_required
        ),
        "safety_reason_codes": _combined_safety_reason_codes(
            safety, request.trusted_policy_signals
        ),
        "mode_reason_codes": tuple(sorted(decision.mode_reasons)),
        "fm_level": decision.fm_effective_level.value,
        "fm_gate_reason_codes": tuple(sorted(decision.fm_gate_reasons)),
        "fm_selection_status": fm.status,
        "fm_selected_record_count": len(fm.selected_record_ids),
        "memory_present": request.memory_input is not None,
        "context_block_count": manifest.context_block_count,
        "total_input_bytes": manifest.total_input_bytes,
        "total_input_tokens": manifest.total_input_tokens,
        "ignored_legacy_request_fields": tuple(
            sorted(decision.ignored_legacy_request_fields)
        ),
    }
    serializable = SanitizedResponseShadowTraceV0_2.model_construct(
        **values,
        trace_sha256="0" * 64,
    ).model_dump(mode="json", exclude={"trace_sha256"})
    return SanitizedResponseShadowTraceV0_2(
        **values,
        trace_sha256=_sha256(serializable),
    )


async def _await_if_needed(
    value: SafetyAssessmentV0_2 | Awaitable[SafetyAssessmentV0_2],
) -> SafetyAssessmentV0_2:
    if hasattr(value, "__await__"):
        return await value  # type: ignore[misc]
    return value


class TrustedResponseOrchestratorV0_2:
    """Build a private typed plan and a content-free shadow trace."""

    def __init__(
        self,
        safety_provider: SafetyAssessmentProviderV0_2,
        *,
        clock: Callable[[], datetime] | None = None,
        correlation_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._safety_provider = safety_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._correlation_id_factory = correlation_id_factory or uuid4

    async def build_plan(
        self,
        request: TrustedResponseRequestV0_2,
    ) -> TrustedResponsePlanV0_2:
        try:
            if not isinstance(request, TrustedResponseRequestV0_2):
                raise TypeError("trusted response request type mismatch")
            request = TrustedResponseRequestV0_2.model_validate_json(
                _canonical_json_bytes(request)
            )
            policy_input = ResponsePolicyInputV0_2.create(
                request_id=request.request_id,
                conversation=request.conversation,
                requested_assistant_profile_id=None,
                request_field_names=request.legacy_request_field_names,
            )
            safety = await _await_if_needed(
                self._safety_provider.assess(policy_input)
            )
            safety = SafetyAssessmentV0_2.model_validate_json(
                safety.model_dump_json()
            )
            if (
                safety.assessor_components
                != TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2
            ):
                raise ResponseOrchestrationError(
                    "safety assessment uses an untrusted assessor bundle"
                )
            decision = decide_response_policy_v0_2(
                policy_input,
                safety_assessment=safety,
                signals=request.trusted_policy_signals,
            )
            prompt = render_response_policy_prompt_v0_2(decision)
            fm = select_fm_v0_2(
                FMSelectionRequestV02(
                    policy_decision=decision,
                    query_text=policy_input.current_message.content,
                    token_budget=request.fm_token_budget,
                )
            )
            assembled = assemble_prompt(
                PromptAssemblyRequestV1(
                    policy_input=policy_input,
                    safety_assessment=safety,
                    policy_signals=request.trusted_policy_signals,
                    policy_decision=decision,
                    policy_prompt=prompt,
                    memory_input=request.memory_input,
                    memory_application=request.memory_application,
                    fm_selection=fm,
                    search_capability_manifest=(
                        request.search_capability_manifest
                    ),
                    response_language=request.response_language,
                )
            )
            occurred_at = self._clock()
            correlation_id = self._correlation_id_factory()
            trace = _shadow_trace(
                request=request,
                safety=safety,
                decision=decision,
                fm=fm,
                assembled=assembled,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
            )
            return TrustedResponsePlanV0_2(
                authenticated_actor_user_id=request.authenticated_actor_user_id,
                thread_id=request.thread_id,
                conversation_snapshot_sha256=(
                    request.conversation_snapshot_sha256
                ),
                source_snapshot=request.conversation_snapshot,
                policy_input=policy_input,
                safety_assessment=safety,
                policy_signals_envelope=(
                    request.trusted_policy_signals_envelope
                ),
                policy_decision=decision,
                policy_prompt=prompt,
                fm_selection=fm,
                assembled_prompt=assembled,
                shadow_trace=trace,
                plan_sha256=_plan_sha256(
                    actor=request.authenticated_actor_user_id,
                    thread_id=request.thread_id,
                    conversation_snapshot_sha256=(
                        request.conversation_snapshot_sha256
                    ),
                    policy_input=policy_input,
                    safety=safety,
                    signals_envelope=(
                        request.trusted_policy_signals_envelope
                    ),
                    decision=decision,
                    prompt=prompt,
                    fm=fm,
                    assembled=assembled,
                    trace=trace,
                ),
            )
        except ResponseOrchestrationError:
            raise
        except Exception:
            raise ResponseOrchestrationError(
                "trusted response orchestration failed"
            ) from None


__all__ = [
    "ORCHESTRATOR_VERSION",
    "ResponseOrchestrationError",
    "SafetyAssessmentProviderV0_2",
    "SanitizedResponseShadowTraceV0_2",
    "TRUSTED_POLICY_SIGNAL_COMPONENTS_V0_2",
    "TRUSTED_POLICY_SIGNALS_ENVELOPE_VERSION",
    "TRUSTED_SAFETY_ASSESSOR_COMPONENTS_V0_2",
    "TrustedPolicySignalsEnvelopeV0_2",
    "TrustedResponseOrchestratorV0_2",
    "TrustedResponsePlanV0_2",
    "TrustedResponseRequestV0_2",
]
