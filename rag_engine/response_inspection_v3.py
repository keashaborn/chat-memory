from __future__ import annotations

"""Content-free LifeSwitch extension for the response inspector.

This is a new contract rather than a silent field addition to response trace V2.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_engine.lifeswitch_answer_binding_v1 import FinalAnswerLifeSwitchBindingV1
from rag_engine.lifeswitch_prompt_integration_v1 import AssembledPromptV2
from rag_engine.lifeswitch_response_context_provider_v1 import (
    LifeSwitchPreparedContextV1,
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


class LifeSwitchBeforeOpenAIInspectionV1(_StrictFrozenModel):
    status: Literal[
        "OFF",
        "TIMEZONE_UNAVAILABLE",
        "EMPTY",
        "SELECTED",
        "PARTIAL",
    ]
    intent: str
    reason_codes: tuple[str, ...]
    database_accessed: bool
    timezone_source: str
    included: bool
    record_count: int = Field(ge=0, le=500)
    estimated_tokens: int = Field(ge=0, le=1000)
    projections: tuple[str, ...]
    source_contract_version: str | None = None


class LifeSwitchAfterOpenAIInspectionV1(_StrictFrozenModel):
    answer_binding: Literal["bound", "none"]
    binding_contract_version: str | None = None


class ResponseInspectionV3(_StrictFrozenModel):
    contract_version: Literal[RESPONSE_INSPECTION_V3] = RESPONSE_INSPECTION_V3
    before_openai: LifeSwitchBeforeOpenAIInspectionV1
    after_openai: LifeSwitchAfterOpenAIInspectionV1


def build_response_inspection_v3(
    *,
    prepared: LifeSwitchPreparedContextV1,
    assembled: AssembledPromptV2,
    binding: FinalAnswerLifeSwitchBindingV1 | None,
) -> ResponseInspectionV3:
    included = any(
        block.block_id == "lifeswitch_domain_context_v1"
        for block in assembled.context_blocks
    )
    envelope = prepared.envelope
    assembled_envelope = assembled.source_request.lifeswitch_envelope
    if (envelope is None) != (assembled_envelope is None):
        raise ValueError("LifeSwitch inspector differs from selected envelope")
    if (
        envelope is not None
        and assembled_envelope is not None
        and envelope.envelope_sha256 != assembled_envelope.envelope_sha256
    ):
        raise ValueError("LifeSwitch inspector envelope differs from assembly")
    if included != (prepared.rendered is not None):
        raise ValueError("LifeSwitch inspector differs from prompt exposure")
    if binding is not None:
        if not included:
            raise ValueError("LifeSwitch binding exists without prompt exposure")
        if binding.source_assembly_sha256 != assembled.manifest.assembly_sha256:
            raise ValueError("LifeSwitch binding differs from inspected assembly")
        if envelope is None or binding.envelope_sha256 != envelope.envelope_sha256:
            raise ValueError("LifeSwitch binding differs from selected envelope")
    projections = (
        tuple(section.projection for section in envelope.sections)
        if envelope is not None
        else ()
    )
    record_count = (
        sum(section.record_count for section in envelope.sections)
        if envelope is not None
        else 0
    )
    return ResponseInspectionV3(
        before_openai=LifeSwitchBeforeOpenAIInspectionV1(
            status=prepared.status,
            intent=prepared.data_plan.intent,
            reason_codes=prepared.data_plan.reason_codes,
            database_accessed=prepared.database_accessed,
            timezone_source=prepared.timezone_source,
            included=included,
            record_count=record_count,
            estimated_tokens=(
                prepared.rendered.estimated_tokens if prepared.rendered else 0
            ),
            projections=projections,
            source_contract_version=(
                envelope.contract_version if envelope is not None else None
            ),
        ),
        after_openai=LifeSwitchAfterOpenAIInspectionV1(
            answer_binding="bound" if binding is not None else "none",
            binding_contract_version=(
                binding.contract_version if binding is not None else None
            ),
        ),
    )


__all__ = [
    "LifeSwitchAfterOpenAIInspectionV1",
    "LifeSwitchBeforeOpenAIInspectionV1",
    "ResponseInspectionV3",
    "build_response_inspection_v3",
]
