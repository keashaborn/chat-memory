from __future__ import annotations

"""Content-free inspection of the exact RESSE response execution.

This contract may expose control decisions and counts to authorized operators.
It must never contain prompt text, conversation text, Memory content, FM prose,
provider output, or content-derived hashes.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rag_engine.openai_chat_provider_v1 import OpenAIChatResponseV1
from rag_engine.response_finalization_v1 import FinalizedTrustedResponseV1
from rag_engine.response_orchestration_v0_2 import TrustedResponsePlanV0_2


RESPONSE_INSPECTION_VERSION = "response_inspection_v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class BeforeOpenAIInspectionV1(_StrictFrozenModel):
    response_mode: str
    closure: str
    high_stakes_gate: str
    safety_action_required: bool
    safety_reason_codes: tuple[str, ...]
    mode_reason_codes: tuple[str, ...]
    fm_level: str
    fm_status: str
    fm_record_count: int = Field(ge=0, le=8)
    fm_selected_record_ids: tuple[str, ...]
    fm_estimated_tokens: int = Field(ge=0, le=1600)
    memory_included: bool
    memory_record_count: int = Field(ge=0)
    memory_estimated_tokens: int = Field(ge=0)
    context_block_count: int = Field(ge=0, le=2)
    conversation_message_count: int = Field(ge=1)
    total_message_count: int = Field(ge=2)
    estimated_input_tokens: int = Field(ge=1)
    ignored_legacy_request_fields: tuple[str, ...]


class OpenAIInspectionV1(_StrictFrozenModel):
    response_id: str
    requested_model: str
    returned_model: str
    finish_reason: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AfterOpenAIInspectionV1(_StrictFrozenModel):
    answer_id: UUID
    output_kind: str
    validation: Literal["passed"] = "passed"
    answer_binding: Literal["bound"] = "bound"
    transcript_persistence: Literal["persisted", "skipped"]
    memory_binding: Literal["bound", "none"]


class ResponseInspectionV1(_StrictFrozenModel):
    contract_version: Literal["response_inspection_v1"] = RESPONSE_INSPECTION_VERSION
    before_openai: BeforeOpenAIInspectionV1
    openai: OpenAIInspectionV1
    after_openai: AfterOpenAIInspectionV1


def build_response_inspection_v1(
    *,
    trusted_plan: TrustedResponsePlanV0_2,
    provider_response: OpenAIChatResponseV1,
    finalized: FinalizedTrustedResponseV1,
    transcript_persistence: Literal["persisted", "skipped"],
) -> ResponseInspectionV1:
    """Build a concise audit view from already-validated typed artifacts."""

    plan = TrustedResponsePlanV0_2.model_validate_json(
        trusted_plan.model_dump_json()
    )
    response = OpenAIChatResponseV1.model_validate_json(
        provider_response.model_dump_json()
    )
    result = FinalizedTrustedResponseV1.model_validate_json(
        finalized.model_dump_json()
    )
    attestation = result.attestation
    if attestation.trusted_plan_sha256 != plan.plan_sha256:
        raise ValueError("inspection plan is not bound to the final answer")
    if attestation.provider_response_sha256 != response.response_sha256:
        raise ValueError("inspection provider response is not bound to the final answer")

    trace = plan.shadow_trace
    manifest = plan.assembled_prompt.manifest
    application = plan.assembled_prompt.source_request.memory_application
    memory_included = bool(application and application.memory_content_included)

    return ResponseInspectionV1(
        before_openai=BeforeOpenAIInspectionV1(
            response_mode=trace.response_mode,
            closure=trace.closure,
            high_stakes_gate=trace.high_stakes_gate,
            safety_action_required=trace.safety_action_required,
            safety_reason_codes=trace.safety_reason_codes,
            mode_reason_codes=trace.mode_reason_codes,
            fm_level=trace.fm_level,
            fm_status=trace.fm_selection_status,
            fm_record_count=trace.fm_selected_record_count,
            fm_selected_record_ids=plan.fm_selection.selected_record_ids,
            fm_estimated_tokens=plan.fm_selection.used_tokens,
            memory_included=memory_included,
            memory_record_count=(
                len(application.injected_record_refs) if application else 0
            ),
            memory_estimated_tokens=(
                application.actual_prompt_tokens if application else 0
            ),
            context_block_count=manifest.context_block_count,
            conversation_message_count=manifest.conversation_count,
            total_message_count=manifest.total_message_count,
            estimated_input_tokens=manifest.total_input_tokens,
            ignored_legacy_request_fields=trace.ignored_legacy_request_fields,
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
        after_openai=AfterOpenAIInspectionV1(
            answer_id=result.answer_id,
            output_kind=result.output_kind.value,
            transcript_persistence=transcript_persistence,
            memory_binding="bound" if result.memory_binding is not None else "none",
        ),
    )


__all__ = [
    "RESPONSE_INSPECTION_VERSION",
    "ResponseInspectionV1",
    "build_response_inspection_v1",
]
