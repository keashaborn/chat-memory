from __future__ import annotations

"""Content-free inspection with explicit response-interaction decisions."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from seebx.adapters.openai_chat import OpenAIChatResponseV1
from rag_engine.response_finalization_v1 import FinalizedTrustedResponseV1
from rag_engine.response_inspection_v1 import (
    AfterOpenAIInspectionV1,
    BeforeOpenAIInspectionV1,
    OpenAIInspectionV1,
    VoiceDeliveryInspectionV1,
    build_response_inspection_v1,
)
from seebx.capabilities.conversation.orchestration import TrustedResponsePlanV0_2


RESPONSE_INSPECTION_VERSION = "response_inspection_v2"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


class BeforeOpenAIInspectionV2(BeforeOpenAIInspectionV1):
    interaction_version: str
    interaction: str
    question_policy: str
    interaction_reason_codes: tuple[str, ...]


class ResponseInspectionV2(_StrictFrozenModel):
    contract_version: Literal["response_inspection_v2"] = (
        RESPONSE_INSPECTION_VERSION
    )
    delivery: VoiceDeliveryInspectionV1 | None = None
    before_openai: BeforeOpenAIInspectionV2
    openai: OpenAIInspectionV1
    after_openai: AfterOpenAIInspectionV1


def build_response_inspection_v2(
    *,
    trusted_plan: TrustedResponsePlanV0_2,
    provider_response: OpenAIChatResponseV1,
    finalized: FinalizedTrustedResponseV1,
    transcript_persistence: Literal["persisted", "skipped"],
    voice_turn_id: UUID | None = None,
) -> ResponseInspectionV2:
    """Project validated interaction metadata without private content."""

    plan = TrustedResponsePlanV0_2.model_validate_json(
        trusted_plan.model_dump_json()
    )
    base = build_response_inspection_v1(
        trusted_plan=plan,
        provider_response=provider_response,
        finalized=finalized,
        transcript_persistence=transcript_persistence,
        voice_turn_id=voice_turn_id,
    )
    trace = plan.shadow_trace
    return ResponseInspectionV2(
        delivery=base.delivery,
        before_openai=BeforeOpenAIInspectionV2(
            **base.before_openai.model_dump(mode="python"),
            interaction_version=trace.interaction_version,
            interaction=trace.interaction,
            question_policy=plan.policy_decision.question_policy.value,
            interaction_reason_codes=trace.interaction_reason_codes,
        ),
        openai=base.openai,
        after_openai=base.after_openai,
    )


__all__ = [
    "RESPONSE_INSPECTION_VERSION",
    "ResponseInspectionV2",
    "build_response_inspection_v2",
]
