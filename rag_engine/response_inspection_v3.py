from __future__ import annotations

"""Content-free inspection of the versioned LifeSwitch response path."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rag_engine.openai_chat_request_v3 import OpenAIChatResponseV3
from rag_engine.response_finalization_v2 import FinalizedTrustedResponseV2
from rag_engine.response_inspection_v1 import (
    OpenAIInspectionV1,
    VoiceDeliveryInspectionV1,
)
from rag_engine.response_inspection_v2 import BeforeOpenAIInspectionV2
from rag_engine.response_lifeswitch_integration_v1 import (
    TrustedLifeSwitchResponsePlanV1,
)


RESPONSE_INSPECTION_V3 = "response_inspection_v3"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class BeforeOpenAIInspectionV3(BeforeOpenAIInspectionV2):
    context_block_count: int = Field(ge=0, le=4)
    lifeswitch_status: Literal[
        "OFF",
        "TIMEZONE_UNAVAILABLE",
        "EMPTY",
        "SELECTED",
        "PARTIAL",
    ]
    lifeswitch_intent: str
    lifeswitch_reason_codes: tuple[str, ...]
    lifeswitch_database_accessed: bool
    lifeswitch_timezone_source: str
    lifeswitch_included: bool
    lifeswitch_record_count: int = Field(ge=0, le=500)
    lifeswitch_estimated_tokens: int = Field(ge=0, le=1000)
    lifeswitch_projections: tuple[str, ...]
    lifeswitch_source_contract_version: str | None = None


class AfterOpenAIInspectionV3(_StrictFrozenModel):
    answer_id: UUID
    output_kind: str
    validation: Literal["passed"] = "passed"
    answer_binding: Literal["bound"] = "bound"
    transcript_persistence: Literal["persisted", "skipped"]
    memory_binding: Literal["bound", "none"]
    lifeswitch_binding: Literal["bound", "none"]
    lifeswitch_binding_contract_version: str | None = None


class ResponseInspectionV3(_StrictFrozenModel):
    contract_version: Literal[RESPONSE_INSPECTION_V3] = RESPONSE_INSPECTION_V3
    delivery: VoiceDeliveryInspectionV1 | None = None
    before_openai: BeforeOpenAIInspectionV3
    openai: OpenAIInspectionV1
    after_openai: AfterOpenAIInspectionV3


def build_response_inspection_v3(
    *,
    trusted_plan: TrustedLifeSwitchResponsePlanV1,
    provider_response: OpenAIChatResponseV3,
    finalized: FinalizedTrustedResponseV2,
    transcript_persistence: Literal["persisted", "skipped"],
    voice_turn_id: UUID | None = None,
) -> ResponseInspectionV3:
    plan = TrustedLifeSwitchResponsePlanV1.model_validate_json(
        trusted_plan.model_dump_json()
    )
    response = OpenAIChatResponseV3.model_validate_json(
        provider_response.model_dump_json()
    )
    result = FinalizedTrustedResponseV2.model_validate_json(
        finalized.model_dump_json()
    )
    if result.attestation.trusted_plan_sha256 != plan.plan_sha256:
        raise ValueError("inspection plan is not bound to the final answer")
    if result.attestation.provider_response_sha256 != response.response_sha256:
        raise ValueError("inspection response is not bound to the final answer")

    base = plan.base_response_plan
    trace = base.shadow_trace
    manifest = plan.assembled_prompt.manifest
    base_source = base.assembled_prompt.source_request
    memory_application = base_source.memory_application
    memory_included = bool(
        memory_application and memory_application.memory_content_included
    )
    provenance = base.prior_web_provenance
    prepared = plan.lifeswitch_context
    envelope = prepared.envelope
    included = any(
        block.block_id == "lifeswitch_domain_context_v1"
        for block in plan.assembled_prompt.context_blocks
    )
    if included != (prepared.rendered is not None):
        raise ValueError("LifeSwitch inspector differs from prompt exposure")
    record_count = (
        sum(section.record_count for section in envelope.sections)
        if envelope is not None
        else 0
    )
    projections = (
        tuple(section.projection for section in envelope.sections)
        if envelope is not None
        else ()
    )
    binding = result.lifeswitch_binding
    if binding is not None:
        if not included or envelope is None:
            raise ValueError("LifeSwitch binding exists without prompt exposure")
        if binding.source_assembly_sha256 != manifest.assembly_sha256:
            raise ValueError("LifeSwitch binding differs from inspected assembly")
        if binding.envelope_sha256 != envelope.envelope_sha256:
            raise ValueError("LifeSwitch binding differs from selected envelope")

    return ResponseInspectionV3(
        delivery=(
            VoiceDeliveryInspectionV1(voice_turn_id=voice_turn_id)
            if voice_turn_id is not None
            else None
        ),
        before_openai=BeforeOpenAIInspectionV3(
            response_mode=trace.response_mode,
            closure=trace.closure,
            high_stakes_gate=trace.high_stakes_gate,
            safety_action_required=trace.safety_action_required,
            safety_reason_codes=trace.safety_reason_codes,
            mode_reason_codes=trace.mode_reason_codes,
            fm_level=trace.fm_level,
            fm_status=trace.fm_selection_status,
            fm_record_count=trace.fm_selected_record_count,
            fm_selected_record_ids=base.fm_selection.selected_record_ids,
            fm_estimated_tokens=base.fm_selection.used_tokens,
            memory_included=memory_included,
            memory_record_count=(
                len(memory_application.injected_record_refs)
                if memory_application is not None
                else 0
            ),
            memory_estimated_tokens=(
                memory_application.actual_prompt_tokens
                if memory_application is not None
                else 0
            ),
            prior_web_provenance_included=provenance is not None,
            prior_web_response_count=(
                len(provenance.responses) if provenance is not None else 0
            ),
            context_block_count=manifest.context_block_count,
            conversation_message_count=len(plan.assembled_prompt.conversation),
            total_message_count=manifest.total_message_count,
            estimated_input_tokens=manifest.total_input_tokens,
            ignored_legacy_request_fields=trace.ignored_legacy_request_fields,
            interaction_version=trace.interaction_version,
            interaction=trace.interaction,
            question_policy=base.policy_decision.question_policy.value,
            interaction_reason_codes=trace.interaction_reason_codes,
            personalization=base.assembled_prompt.manifest.personalization,
            lifeswitch_status=prepared.status,
            lifeswitch_intent=prepared.data_plan.intent,
            lifeswitch_reason_codes=prepared.data_plan.reason_codes,
            lifeswitch_database_accessed=prepared.database_accessed,
            lifeswitch_timezone_source=prepared.timezone_source,
            lifeswitch_included=included,
            lifeswitch_record_count=record_count,
            lifeswitch_estimated_tokens=(
                prepared.rendered.estimated_tokens if prepared.rendered else 0
            ),
            lifeswitch_projections=projections,
            lifeswitch_source_contract_version=(
                envelope.contract_version if envelope is not None else None
            ),
        ),
        openai=OpenAIInspectionV1(
            response_id=response.response_id,
            requested_model=response.requested_model,
            returned_model=response.model,
            finish_reason=response.finish_reason,
            input_tokens=response.provider_input_tokens,
            output_tokens=response.provider_output_tokens,
            total_tokens=response.provider_total_tokens,
        ),
        after_openai=AfterOpenAIInspectionV3(
            answer_id=result.answer_id,
            output_kind=result.output_kind.value,
            transcript_persistence=transcript_persistence,
            memory_binding="bound" if result.memory_binding is not None else "none",
            lifeswitch_binding="bound" if binding is not None else "none",
            lifeswitch_binding_contract_version=(
                binding.contract_version if binding is not None else None
            ),
        ),
    )


__all__ = [
    "AfterOpenAIInspectionV3",
    "BeforeOpenAIInspectionV3",
    "ResponseInspectionV3",
    "build_response_inspection_v3",
]
