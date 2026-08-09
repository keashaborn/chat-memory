from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Mapping, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from rag_engine.memory_v1_evidence_context_v1 import (
    MemoryEvidenceContextEnvelopeV1,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    MemoryEvidenceContextEnvelopeV2,
)
from rag_engine.memory_v1_personal_evidence_exchange_v2 import (
    CONTRACT_VERSION as EXCHANGE_CONTRACT_VERSION,
    POLICY_SHA256 as EXCHANGE_POLICY_SHA256,
    POLICY_VERSION as EXCHANGE_POLICY_VERSION,
    PersonalEvidenceExchangeResultV2,
    classify_personal_evidence_exchange_v2,
    is_external_exchange_reason,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    CONTRACT_VERSION as PREFILTER_CONTRACT_VERSION,
    POLICY_SHA256 as PREFILTER_POLICY_SHA256,
    POLICY_VERSION as PREFILTER_POLICY_VERSION,
    TRUSTED_SOURCE_ROLE,
    PersonalEvidencePrefilterResultV1,
    classify_personal_evidence_v1,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderEntityMention,
    ProviderObservation,
    ProviderPacket,
    ProviderTemporal,
)


CONTRACT_VERSION = "memory_v1_openai_v5_2_semantic_tasks_v1"
EXPECTED_BINDINGS_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_expected_bindings_v1"
)
PUBLIC_BINDING_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_public_binding_v1"
)
CANONICAL_EXTRACTION_RESULT_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_canonical_extraction_result_v1"
)
EXTRACTION_PAYLOAD_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_extraction_payload_v1"
)
ENTITY_VALIDATION_PAYLOAD_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_entity_validation_payload_v1"
)
ENTAILMENT_PAYLOAD_CONTRACT_VERSION = (
    "memory_v1_openai_v5_2_entailment_payload_v1"
)
SOURCE_OFFSET_CONTRACT_VERSION = "python_unicode_codepoint_offsets_v1"
CANONICAL_JSON_CONTRACT_VERSION = (
    "python_json_utf8_sort_keys_compact_no_nan_v1"
)

MAX_SOURCE_CHARS = 200_000
MAX_SELECTED_SPANS = 64
MAX_CONTEXT_ITEMS = 32
MAX_CONTEXT_CHARS = 16_000
MAX_STRUCTURED_INPUT_BYTES = 65_536
MAX_OUTBOUND_PAYLOAD_BYTES = 262_144
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 20_000
MAX_JSON_KEY_CHARS = 200
MAX_JSON_STRING_CHARS = 200_000
MAX_STRICT_JSON_BYTES = 262_144

Task = Literal["memory_extraction", "entity_validation", "observation_entailment"]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticTaskContractError(ValueError):
    """Fail-closed error at the provider-neutral semantic-task boundary."""


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


class OpenAIProviderTemporalV1(ProviderTemporal):
    """Provider-owned temporal value; trusted server anchoring is forbidden."""

    anchored_to_source_time: Literal[False]


class OpenAIProviderObservationV1(ProviderObservation):
    temporal: OpenAIProviderTemporalV1


class OpenAIExtractionResultV1(ProviderPacket):
    """ProviderPacket-compatible output with the transport's frozen contract."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )

    observations: list[OpenAIProviderObservationV1] = Field(max_length=32)

    @model_validator(mode="after")
    def _json_safe(self) -> "OpenAIExtractionResultV1":
        _validate_json_tree(self.model_dump(mode="python"))
        return self


class OpenAIEntityValidationResultV1(StrictFrozenModel):
    decision: Literal["supported", "contradicted", "ambiguous"]
    confidence: Literal["high", "medium", "low"]


class OpenAIEntailmentResultV1(StrictFrozenModel):
    decision: Literal["entailed", "contradicted", "ambiguous"]
    confidence: Literal["high", "medium", "low"]


class ExpectedSemanticTaskBindingsV1(StrictFrozenModel):
    contract_version: Literal[
        "memory_v1_openai_v5_2_expected_bindings_v1"
    ] = EXPECTED_BINDINGS_CONTRACT_VERSION
    task: Task
    owner_binding_sha256: Sha256
    source_sha256: Sha256
    context_binding_sha256: Sha256 | None
    profile_sha256: Sha256

    def validate_exact(
        self,
        *,
        task: Task,
        owner_binding_sha256: str,
        source_sha256: str,
        context_binding_sha256: str | None,
        profile_sha256: str,
    ) -> None:
        expected = (
            self.task,
            self.owner_binding_sha256,
            self.source_sha256,
            self.context_binding_sha256,
            self.profile_sha256,
        )
        observed = (
            task,
            owner_binding_sha256,
            source_sha256,
            context_binding_sha256,
            profile_sha256,
        )
        if expected != observed:
            names = (
                "task",
                "owner",
                "source",
                "context",
                "profile",
            )
            differing = [
                name
                for name, expected_item, observed_item in zip(
                    names,
                    expected,
                    observed,
                    strict=True,
                )
                if expected_item != observed_item
            ]
            raise SemanticTaskContractError(
                "semantic task binding mismatch: " + ",".join(differing)
            )


class SemanticTaskPublicBindingV1(StrictFrozenModel):
    contract_version: Literal[
        "memory_v1_openai_v5_2_public_binding_v1"
    ] = PUBLIC_BINDING_CONTRACT_VERSION
    task: Task
    canonical_json_contract_version: Literal[
        "python_json_utf8_sort_keys_compact_no_nan_v1"
    ] = CANONICAL_JSON_CONTRACT_VERSION
    owner_binding_sha256: Sha256
    source_sha256: Sha256
    context_binding_sha256: Sha256 | None
    profile_sha256: Sha256
    gate_policy_sha256: Sha256
    gate_result_sha256: Sha256
    selected_spans_sha256: Sha256
    structured_input_sha256: Sha256 | None
    output_schema_sha256: Sha256
    outbound_payload_sha256: Sha256
    source_char_count: int = Field(ge=1, le=MAX_SOURCE_CHARS)
    selected_span_count: int = Field(ge=1, le=MAX_SELECTED_SPANS)
    context_item_count: int = Field(ge=0, le=MAX_CONTEXT_ITEMS)
    outbound_payload_bytes: int = Field(ge=1, le=MAX_OUTBOUND_PAYLOAD_BYTES)
    binding_sha256: Sha256

    def content_free_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CanonicalExtractionResultEnvelopeV1(StrictFrozenModel):
    contract_version: Literal[
        "memory_v1_openai_v5_2_canonical_extraction_result_v1"
    ] = CANONICAL_EXTRACTION_RESULT_CONTRACT_VERSION
    task: Literal["memory_extraction"] = "memory_extraction"
    compiled_binding_sha256: Sha256
    owner_binding_sha256: Sha256
    source_sha256: Sha256
    source_char_count: int = Field(ge=1, le=MAX_SOURCE_CHARS)
    gate_result_sha256: Sha256
    selected_spans_sha256: Sha256
    canonical_result_json: str = Field(
        min_length=2,
        max_length=MAX_STRICT_JSON_BYTES,
        repr=False,
    )
    canonical_result_sha256: Sha256
    canonical_result_bytes: int = Field(ge=2, le=MAX_STRICT_JSON_BYTES)

    @model_validator(mode="after")
    def _validate_envelope(self) -> "CanonicalExtractionResultEnvelopeV1":
        parsed = strict_json_loads(self.canonical_result_json)
        if canonical_json(parsed) != self.canonical_result_json:
            raise ValueError("extraction result JSON is not canonical")
        encoded = self.canonical_result_json.encode("utf-8")
        if len(encoded) != self.canonical_result_bytes:
            raise ValueError("extraction result byte count mismatch")
        if _sha256_bytes(encoded) != self.canonical_result_sha256:
            raise ValueError("extraction result hash mismatch")
        return self


@dataclass(frozen=True)
class CompiledSemanticTaskV1:
    task: Task
    source_text: str = field(repr=False)
    expected_bindings: ExpectedSemanticTaskBindingsV1
    outbound_payload_json: str = field(repr=False)
    output_model: type[BaseModel]
    public_binding: SemanticTaskPublicBindingV1
    evidence_context_authority: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ) = field(repr=False)
    extraction_result_authority: (
        CanonicalExtractionResultEnvelopeV1 | None
    ) = field(repr=False)
    extraction_result_ref: str | None

    def __post_init__(self) -> None:
        _validate_compiled_task(self)


def owner_binding_sha256_v1(owner_user_id: str) -> str:
    try:
        owner = str(uuid.UUID(str(owner_user_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SemanticTaskContractError("owner_user_id must be a UUID") from exc
    return _domain_hash("owner", owner)


def build_expected_bindings_v1(
    *,
    task: Task,
    owner_user_id: str,
    source_text: str,
    context_binding_sha256: str | None,
    profile_sha256: str,
) -> ExpectedSemanticTaskBindingsV1:
    source = _validate_source(source_text)
    _require_sha256(profile_sha256, "profile_sha256")
    if context_binding_sha256 is not None:
        _require_sha256(
            context_binding_sha256,
            "context_binding_sha256",
        )
    return ExpectedSemanticTaskBindingsV1(
        task=task,
        owner_binding_sha256=owner_binding_sha256_v1(owner_user_id),
        source_sha256=_sha256_text(source),
        context_binding_sha256=context_binding_sha256,
        profile_sha256=profile_sha256,
    )


def extraction_context_binding_sha256_v1(
    evidence_context: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ),
) -> str | None:
    if evidence_context is None:
        return None
    if type(evidence_context) not in (
        MemoryEvidenceContextEnvelopeV1,
        MemoryEvidenceContextEnvelopeV2,
    ):
        raise SemanticTaskContractError("evidence context type is invalid")
    evidence_context.validate_hash()
    return _require_sha256(
        evidence_context.envelope_sha256,
        "evidence_context.envelope_sha256",
    )


def entity_mention_binding_sha256_v1(
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    entity_ref: str,
) -> str:
    mention = _select_extraction_entity(extraction_result, entity_ref)
    return _extraction_reference_binding_sha256(
        extraction_result=extraction_result,
        reference_kind="entity_ref",
        reference=entity_ref,
        structured_value=mention.model_dump(mode="json"),
    )


def observation_binding_sha256_v1(
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    observation_ref: str,
) -> str:
    observation = _select_extraction_observation(
        extraction_result,
        observation_ref,
    )
    return _extraction_reference_binding_sha256(
        extraction_result=extraction_result,
        reference_kind="observation_ref",
        reference=observation_ref,
        structured_value=observation.model_dump(mode="json"),
    )


def compile_extraction_payload_v1(
    *,
    source_text: str,
    owner_user_id: str,
    gate_result: (
        PersonalEvidencePrefilterResultV1
        | PersonalEvidenceExchangeResultV2
    ),
    profile_sha256: str,
    expected_bindings: ExpectedSemanticTaskBindingsV1,
    evidence_context: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ) = None,
) -> CompiledSemanticTaskV1:
    task: Task = "memory_extraction"
    _require_expected_bindings(expected_bindings)
    source, source_sha256, selected_spans, gate_sha256 = (
        _validated_selected_source(
            source_text,
            gate_result,
            evidence_context,
        )
    )
    owner_binding = owner_binding_sha256_v1(owner_user_id)
    (
        context_binding,
        context_items,
        context_header,
    ) = _validated_context(
        source=source,
        source_sha256=source_sha256,
        owner_user_id=owner_user_id,
        evidence_context=evidence_context,
        include_immediate_assistant_question=isinstance(
            gate_result,
            PersonalEvidenceExchangeResultV2,
        ),
    )
    _require_sha256(profile_sha256, "profile_sha256")
    expected_bindings.validate_exact(
        task=task,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=context_binding,
        profile_sha256=profile_sha256,
    )
    selected_spans_sha256 = _selected_spans_binding_sha256(selected_spans)
    payload = {
        "canonical_json_contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "contract_version": EXTRACTION_PAYLOAD_CONTRACT_VERSION,
        "task": task,
        "source_offset_contract": SOURCE_OFFSET_CONTRACT_VERSION,
        "owner_binding_sha256": owner_binding,
        "profile_sha256": profile_sha256,
        "source_char_count": len(source),
        "source_sha256": source_sha256,
        "context_binding_sha256": context_binding,
        "gate_contract_version": gate_result.contract_version,
        "gate_policy_version": gate_result.policy_version,
        "gate_policy_sha256": gate_result.policy_sha256,
        "gate_result_sha256": gate_sha256,
        "selected_spans_sha256": selected_spans_sha256,
        "structured_input_sha256": None,
        "selected_source_spans": selected_spans,
        "context": {
            **context_header,
            "items": context_items,
        },
    }
    return _compile(
        task=task,
        source_text=source,
        expected_bindings=expected_bindings,
        payload=payload,
        output_model=OpenAIExtractionResultV1,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=context_binding,
        profile_sha256=profile_sha256,
        gate_result=gate_result,
        gate_result_sha256=gate_sha256,
        selected_spans=selected_spans,
        structured_input_sha256=None,
        source_char_count=len(source),
        context_item_count=len(context_items),
        evidence_context_authority=evidence_context,
        extraction_result_authority=None,
        extraction_result_ref=None,
    )


def compile_entity_validation_payload_v1(
    *,
    source_text: str,
    owner_user_id: str,
    gate_result: PersonalEvidencePrefilterResultV1,
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    entity_ref: str,
    profile_sha256: str,
    expected_bindings: ExpectedSemanticTaskBindingsV1,
) -> CompiledSemanticTaskV1:
    task: Task = "entity_validation"
    _require_expected_bindings(expected_bindings)
    source, source_sha256, selected_spans, gate_sha256 = (
        _validated_selected_source(source_text, gate_result)
    )
    _validate_extraction_result_binding(
        extraction_result=extraction_result,
        owner_binding_sha256=owner_binding_sha256_v1(owner_user_id),
        source_sha256=source_sha256,
        source_char_count=len(source),
        gate_result_sha256=gate_sha256,
        selected_spans_sha256=_selected_spans_binding_sha256(selected_spans),
    )
    mention = _select_extraction_entity(extraction_result, entity_ref)
    _validate_structured_source_spans(
        source=source,
        selected_spans=selected_spans,
        source_spans=mention.source_spans,
        label="entity mention",
    )
    structured = mention.model_dump(mode="json")
    structured_sha256 = _bounded_structured_input_sha256(structured)
    extraction_reference_sha256 = entity_mention_binding_sha256_v1(
        extraction_result,
        entity_ref,
    )
    owner_binding = owner_binding_sha256_v1(owner_user_id)
    _require_sha256(profile_sha256, "profile_sha256")
    expected_bindings.validate_exact(
        task=task,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=extraction_reference_sha256,
        profile_sha256=profile_sha256,
    )
    selected_spans_sha256 = _selected_spans_binding_sha256(selected_spans)
    payload = {
        "canonical_json_contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "contract_version": ENTITY_VALIDATION_PAYLOAD_CONTRACT_VERSION,
        "task": task,
        "source_offset_contract": SOURCE_OFFSET_CONTRACT_VERSION,
        "owner_binding_sha256": owner_binding,
        "profile_sha256": profile_sha256,
        "source_char_count": len(source),
        "source_sha256": source_sha256,
        "context_binding_sha256": extraction_reference_sha256,
        "gate_contract_version": gate_result.contract_version,
        "gate_policy_version": gate_result.policy_version,
        "gate_policy_sha256": gate_result.policy_sha256,
        "gate_result_sha256": gate_sha256,
        "selected_spans_sha256": selected_spans_sha256,
        "structured_input_sha256": structured_sha256,
        "selected_source_spans": selected_spans,
        "structured_entity_mention": structured,
    }
    return _compile(
        task=task,
        source_text=source,
        expected_bindings=expected_bindings,
        payload=payload,
        output_model=OpenAIEntityValidationResultV1,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=extraction_reference_sha256,
        profile_sha256=profile_sha256,
        gate_result=gate_result,
        gate_result_sha256=gate_sha256,
        selected_spans=selected_spans,
        structured_input_sha256=structured_sha256,
        source_char_count=len(source),
        context_item_count=0,
        evidence_context_authority=None,
        extraction_result_authority=extraction_result,
        extraction_result_ref=entity_ref,
    )


def compile_entailment_payload_v1(
    *,
    source_text: str,
    owner_user_id: str,
    gate_result: PersonalEvidencePrefilterResultV1,
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    observation_ref: str,
    profile_sha256: str,
    expected_bindings: ExpectedSemanticTaskBindingsV1,
) -> CompiledSemanticTaskV1:
    task: Task = "observation_entailment"
    _require_expected_bindings(expected_bindings)
    source, source_sha256, selected_spans, gate_sha256 = (
        _validated_selected_source(source_text, gate_result)
    )
    _validate_extraction_result_binding(
        extraction_result=extraction_result,
        owner_binding_sha256=owner_binding_sha256_v1(owner_user_id),
        source_sha256=source_sha256,
        source_char_count=len(source),
        gate_result_sha256=gate_sha256,
        selected_spans_sha256=_selected_spans_binding_sha256(selected_spans),
    )
    observation = _select_extraction_observation(
        extraction_result,
        observation_ref,
    )
    if observation.sensitivity != "low":
        raise SemanticTaskContractError(
            "only low-sensitivity observations may use external entailment"
        )
    _validate_structured_source_spans(
        source=source,
        selected_spans=selected_spans,
        source_spans=observation.source_spans,
        label="observation",
    )
    structured = observation.model_dump(mode="json")
    structured_sha256 = _bounded_structured_input_sha256(structured)
    extraction_reference_sha256 = observation_binding_sha256_v1(
        extraction_result,
        observation_ref,
    )
    owner_binding = owner_binding_sha256_v1(owner_user_id)
    _require_sha256(profile_sha256, "profile_sha256")
    expected_bindings.validate_exact(
        task=task,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=extraction_reference_sha256,
        profile_sha256=profile_sha256,
    )
    selected_spans_sha256 = _selected_spans_binding_sha256(selected_spans)
    payload = {
        "canonical_json_contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "contract_version": ENTAILMENT_PAYLOAD_CONTRACT_VERSION,
        "task": task,
        "source_offset_contract": SOURCE_OFFSET_CONTRACT_VERSION,
        "owner_binding_sha256": owner_binding,
        "profile_sha256": profile_sha256,
        "source_char_count": len(source),
        "source_sha256": source_sha256,
        "context_binding_sha256": extraction_reference_sha256,
        "gate_contract_version": gate_result.contract_version,
        "gate_policy_version": gate_result.policy_version,
        "gate_policy_sha256": gate_result.policy_sha256,
        "gate_result_sha256": gate_sha256,
        "selected_spans_sha256": selected_spans_sha256,
        "structured_input_sha256": structured_sha256,
        "selected_source_spans": selected_spans,
        "structured_observation": structured,
    }
    return _compile(
        task=task,
        source_text=source,
        expected_bindings=expected_bindings,
        payload=payload,
        output_model=OpenAIEntailmentResultV1,
        owner_binding_sha256=owner_binding,
        source_sha256=source_sha256,
        context_binding_sha256=extraction_reference_sha256,
        profile_sha256=profile_sha256,
        gate_result=gate_result,
        gate_result_sha256=gate_sha256,
        selected_spans=selected_spans,
        structured_input_sha256=structured_sha256,
        source_char_count=len(source),
        context_item_count=0,
        evidence_context_authority=None,
        extraction_result_authority=extraction_result,
        extraction_result_ref=observation_ref,
    )


OutputModelT = TypeVar("OutputModelT", bound=BaseModel)


def parse_extraction_result_v1(
    compiled_task: CompiledSemanticTaskV1,
    value: Any,
) -> CanonicalExtractionResultEnvelopeV1:
    if type(compiled_task) is not CompiledSemanticTaskV1:
        raise SemanticTaskContractError("compiled extraction task type is invalid")
    _validate_compiled_task(compiled_task)
    if (
        compiled_task.task != "memory_extraction"
        or compiled_task.output_model is not OpenAIExtractionResultV1
    ):
        raise SemanticTaskContractError("compiled task is not extraction")
    payload = strict_json_loads(compiled_task.outbound_payload_json)
    selected = payload["selected_source_spans"]
    packet = _validate_model_input(value, OpenAIExtractionResultV1)
    for index, mention in enumerate(packet.entity_mentions):
        _validate_result_source_spans(
            source=compiled_task.source_text,
            selected_spans=selected,
            source_spans=mention.source_spans,
            label=f"entity_mentions[{index}]",
        )
    for index, observation in enumerate(packet.observations):
        _validate_result_source_spans(
            source=compiled_task.source_text,
            selected_spans=selected,
            source_spans=observation.source_spans,
            label=f"observations[{index}]",
        )
    for index, deferral in enumerate(packet.deferrals):
        _validate_result_source_spans(
            source=compiled_task.source_text,
            selected_spans=selected,
            source_spans=deferral.source_spans,
            label=f"deferrals[{index}]",
        )
    result_json = _bounded_canonical_json(
        packet.model_dump(mode="json"),
        MAX_STRICT_JSON_BYTES,
        "canonical extraction result",
    )
    encoded = result_json.encode("utf-8")
    public = compiled_task.public_binding
    return CanonicalExtractionResultEnvelopeV1(
        compiled_binding_sha256=public.binding_sha256,
        owner_binding_sha256=public.owner_binding_sha256,
        source_sha256=public.source_sha256,
        source_char_count=public.source_char_count,
        gate_result_sha256=public.gate_result_sha256,
        selected_spans_sha256=public.selected_spans_sha256,
        canonical_result_json=result_json,
        canonical_result_sha256=_sha256_bytes(encoded),
        canonical_result_bytes=len(encoded),
    )


def parse_entity_validation_result_v1(
    value: Any,
) -> OpenAIEntityValidationResultV1:
    return _validate_model_input(value, OpenAIEntityValidationResultV1)


def parse_entailment_result_v1(value: Any) -> OpenAIEntailmentResultV1:
    return _validate_model_input(value, OpenAIEntailmentResultV1)


def _validated_extraction_packet(
    extraction_result: CanonicalExtractionResultEnvelopeV1,
) -> OpenAIExtractionResultV1:
    if type(extraction_result) is not CanonicalExtractionResultEnvelopeV1:
        raise SemanticTaskContractError("extraction result envelope type is invalid")
    if (
        extraction_result.contract_version
        != CANONICAL_EXTRACTION_RESULT_CONTRACT_VERSION
        or extraction_result.task != "memory_extraction"
    ):
        raise SemanticTaskContractError(
            "extraction result envelope contract is invalid"
        )
    _require_sha256(
        extraction_result.compiled_binding_sha256,
        "extraction result compiled binding",
    )
    _require_sha256(
        extraction_result.owner_binding_sha256,
        "extraction result owner binding",
    )
    _require_sha256(
        extraction_result.source_sha256,
        "extraction result source binding",
    )
    _require_sha256(
        extraction_result.gate_result_sha256,
        "extraction result gate binding",
    )
    _require_sha256(
        extraction_result.selected_spans_sha256,
        "extraction result selected-spans binding",
    )
    result_json = _bounded_canonical_json(
        strict_json_loads(extraction_result.canonical_result_json),
        MAX_STRICT_JSON_BYTES,
        "canonical extraction result",
    )
    if result_json != extraction_result.canonical_result_json:
        raise SemanticTaskContractError(
            "extraction result envelope JSON is not canonical"
        )
    encoded = result_json.encode("utf-8")
    if len(encoded) != extraction_result.canonical_result_bytes:
        raise SemanticTaskContractError(
            "extraction result envelope byte count mismatch"
        )
    if _sha256_bytes(encoded) != extraction_result.canonical_result_sha256:
        raise SemanticTaskContractError(
            "extraction result envelope hash mismatch"
        )
    return _validate_model_input(
        result_json,
        OpenAIExtractionResultV1,
    )


def _extraction_reference_binding_sha256(
    *,
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    reference_kind: Literal["entity_ref", "observation_ref"],
    reference: str,
    structured_value: dict[str, Any],
) -> str:
    return canonical_sha256(
        {
            "canonical_result_sha256": (
                extraction_result.canonical_result_sha256
            ),
            "compiled_binding_sha256": (
                extraction_result.compiled_binding_sha256
            ),
            "reference": reference,
            "reference_kind": reference_kind,
            "structured_input_sha256": canonical_sha256(structured_value),
        }
    )


def _select_extraction_entity(
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    entity_ref: str,
) -> ProviderEntityMention:
    if not isinstance(entity_ref, str) or not re.fullmatch(r"e[0-9]{2}", entity_ref):
        raise SemanticTaskContractError("entity_ref is invalid")
    packet = _validated_extraction_packet(extraction_result)
    matches = [
        item for item in packet.entity_mentions if item.entity_ref == entity_ref
    ]
    if len(matches) != 1:
        raise SemanticTaskContractError("entity_ref is absent or ambiguous")
    return matches[0]


def _select_extraction_observation(
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    observation_ref: str,
) -> ProviderObservation:
    if not isinstance(observation_ref, str) or not re.fullmatch(
        r"o[0-9]{2}",
        observation_ref,
    ):
        raise SemanticTaskContractError("observation_ref is invalid")
    packet = _validated_extraction_packet(extraction_result)
    matches = [
        item
        for item in packet.observations
        if item.observation_ref == observation_ref
    ]
    if len(matches) != 1:
        raise SemanticTaskContractError("observation_ref is absent or ambiguous")
    return matches[0]


def _validate_extraction_result_binding(
    *,
    extraction_result: CanonicalExtractionResultEnvelopeV1,
    owner_binding_sha256: str,
    source_sha256: str,
    source_char_count: int,
    gate_result_sha256: str,
    selected_spans_sha256: str,
) -> None:
    _validated_extraction_packet(extraction_result)
    expected = (
        extraction_result.owner_binding_sha256,
        extraction_result.source_sha256,
        extraction_result.source_char_count,
        extraction_result.gate_result_sha256,
        extraction_result.selected_spans_sha256,
    )
    observed = (
        owner_binding_sha256,
        source_sha256,
        source_char_count,
        gate_result_sha256,
        selected_spans_sha256,
    )
    if expected != observed:
        raise SemanticTaskContractError(
            "extraction result does not match follow-up source binding"
        )


def _validate_result_source_spans(
    *,
    source: str,
    selected_spans: list[dict[str, Any]],
    source_spans: Any,
    label: str,
) -> None:
    if not source_spans:
        raise SemanticTaskContractError(f"{label} source spans are absent")
    if len(source_spans) > 8:
        raise SemanticTaskContractError(f"{label} source span count is invalid")
    previous_end = -1
    for span in source_spans:
        start = span.start
        end = span.end
        if not (0 <= start < end <= len(source)):
            raise SemanticTaskContractError(f"{label} source span bounds are invalid")
        if start < previous_end:
            raise SemanticTaskContractError(f"{label} source spans overlap")
        previous_end = end
        if source[start:end] != span.quote:
            raise SemanticTaskContractError(f"{label} source quote mismatch")
        if not any(
            selected["original_char_start"] <= start
            and end <= selected["original_char_end"]
            for selected in selected_spans
        ):
            raise SemanticTaskContractError(
                f"{label} source span is outside externally eligible evidence"
            )


def strict_json_loads(value: str) -> Any:
    if not isinstance(value, str):
        raise SemanticTaskContractError("strict JSON input must be text")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SemanticTaskContractError("strict JSON contains invalid Unicode") from exc
    if len(raw) > MAX_STRICT_JSON_BYTES:
        raise SemanticTaskContractError("strict JSON input is oversized")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise SemanticTaskContractError("duplicate JSON key")
            result[key] = item
        return result

    def reject_constant(_: str) -> Any:
        raise SemanticTaskContractError("non-finite JSON number")

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except SemanticTaskContractError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticTaskContractError("strict JSON input is invalid") from exc
    _validate_json_tree(parsed)
    return parsed


def canonical_json(value: Any) -> str:
    _validate_json_tree(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SemanticTaskContractError("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return _sha256_text(canonical_json(value))


def _validated_selected_source(
    source_text: str,
    gate_result: (
        PersonalEvidencePrefilterResultV1
        | PersonalEvidenceExchangeResultV2
    ),
    evidence_context: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ) = None,
) -> tuple[str, str, list[dict[str, Any]], str]:
    source = _validate_source(source_text)
    if isinstance(gate_result, PersonalEvidencePrefilterResultV1):
        if gate_result.contract_version != PREFILTER_CONTRACT_VERSION:
            raise SemanticTaskContractError("personal-evidence gate contract mismatch")
        if gate_result.policy_version != PREFILTER_POLICY_VERSION:
            raise SemanticTaskContractError("personal-evidence gate policy mismatch")
        if gate_result.policy_sha256 != PREFILTER_POLICY_SHA256:
            raise SemanticTaskContractError("personal-evidence gate hash mismatch")
        authoritative = classify_personal_evidence_v1(
            source,
            source_role=TRUSTED_SOURCE_ROLE,
        )
        allowed_reason = gate_result.reason_codes == (
            "personal_evidence_selected",
        )
    elif isinstance(gate_result, PersonalEvidenceExchangeResultV2):
        if gate_result.contract_version != EXCHANGE_CONTRACT_VERSION:
            raise SemanticTaskContractError("exchange gate contract mismatch")
        if gate_result.policy_version != EXCHANGE_POLICY_VERSION:
            raise SemanticTaskContractError("exchange gate policy mismatch")
        if gate_result.policy_sha256 != EXCHANGE_POLICY_SHA256:
            raise SemanticTaskContractError("exchange gate hash mismatch")
        if evidence_context is not None and not isinstance(
            evidence_context,
            MemoryEvidenceContextEnvelopeV2,
        ):
            raise SemanticTaskContractError("exchange context type is invalid")
        authoritative = classify_personal_evidence_exchange_v2(
            source,
            source_role=TRUSTED_SOURCE_ROLE,
            evidence_context=evidence_context,
        )
        allowed_reason = (
            len(gate_result.reason_codes) == 1
            and is_external_exchange_reason(gate_result.reason_codes[0])
        )
    else:
        raise SemanticTaskContractError("personal-evidence gate type is invalid")
    if gate_result != authoritative:
        raise SemanticTaskContractError(
            "personal-evidence gate is not authoritative"
        )
    if gate_result.decision != "send_external":
        raise SemanticTaskContractError(
            "personal-evidence source is not externally eligible"
        )
    if not allowed_reason:
        raise SemanticTaskContractError("personal-evidence reason is invalid")
    if not 1 <= len(gate_result.selected_spans) <= MAX_SELECTED_SPANS:
        raise SemanticTaskContractError("selected source span count is invalid")
    selected: list[dict[str, Any]] = []
    cursor = 0
    for ordinal, span in enumerate(gate_result.selected_spans):
        if not (0 <= span.char_start < span.char_end <= len(source)):
            raise SemanticTaskContractError("selected source span bounds are invalid")
        if span.char_start < cursor:
            raise SemanticTaskContractError("selected source spans overlap")
        cursor = span.char_end
        text = source[span.char_start : span.char_end]
        if _sha256_text(text) != span.content_sha256:
            raise SemanticTaskContractError("selected source span hash mismatch")
        if span.sensitivity != "ordinary":
            raise SemanticTaskContractError("sensitive source span is forbidden")
        selected.append(
            {
                "assertion_mode": span.assertion_mode,
                "category": span.category,
                "content": text,
                "content_sha256": span.content_sha256,
                "ordinal": ordinal,
                "original_char_end": span.char_end,
                "original_char_start": span.char_start,
                "sensitivity": span.sensitivity,
                "subject_hint": span.subject_hint,
            }
        )
    gate_sha256 = canonical_sha256(gate_result.public_dict())
    return source, _sha256_text(source), selected, gate_sha256


def _validated_context(
    *,
    source: str,
    source_sha256: str,
    owner_user_id: str,
    evidence_context: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ),
    include_immediate_assistant_question: bool = False,
) -> tuple[str | None, list[dict[str, Any]], dict[str, Any]]:
    if evidence_context is None:
        context_header = {
            "contract_version": None,
            "context_policy": None,
            "envelope_sha256": None,
        }
        return (
            None,
            [],
            context_header,
        )
    if type(evidence_context) not in (
        MemoryEvidenceContextEnvelopeV1,
        MemoryEvidenceContextEnvelopeV2,
    ):
        raise SemanticTaskContractError("evidence context type is invalid")
    try:
        evidence_context.validate_hash()
    except Exception as exc:
        raise SemanticTaskContractError("evidence context hash is invalid") from exc
    try:
        canonical_owner = str(uuid.UUID(str(owner_user_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SemanticTaskContractError("owner_user_id must be a UUID") from exc
    if evidence_context.owner_user_id != canonical_owner:
        raise SemanticTaskContractError("evidence context owner mismatch")
    if evidence_context.target_content_sha256 != source_sha256:
        raise SemanticTaskContractError("evidence context source hash mismatch")
    target_spans = [
        span
        for span in evidence_context.spans
        if span.context_role == "target"
    ]
    if (
        len(target_spans) != 1
        or target_spans[0].content != source
        or target_spans[0].content_sha256 != source_sha256
        or not target_spans[0].assertion_origin_allowed
        or target_spans[0].evidence_use != "target_assertion_source"
        or evidence_context.allowed_assertion_evidence_ids
        != (evidence_context.target_evidence_id,)
    ):
        raise SemanticTaskContractError(
            "evidence context target is not the extraction source"
        )

    authority_items: list[dict[str, Any]] = []
    prior_turns = list(getattr(evidence_context, "prior_turns", ()))
    for turn in sorted(prior_turns, key=lambda item: item.context_distance):
        if (
            turn.assertion_origin_allowed
            or turn.instruction_capability
            or turn.evidence_use != "disambiguating_context_only"
        ):
            raise SemanticTaskContractError(
                "prior-turn context received forbidden authority"
            )
        if not (
            0 <= turn.source_char_start < turn.source_char_end
            <= turn.source_char_count
        ):
            raise SemanticTaskContractError("prior-turn context offsets are invalid")
        if len(turn.content) != turn.source_char_end - turn.source_char_start:
            raise SemanticTaskContractError("prior-turn context length mismatch")
        if _sha256_text(turn.content) != turn.content_sha256:
            raise SemanticTaskContractError("prior-turn context hash mismatch")
        if turn.speaker_role == "assistant" and not (
            include_immediate_assistant_question
            and turn.context_distance == 1
            and "?" in turn.content
        ):
            continue
        authority_items.append(
            {
                "assertion_origin_allowed": False,
                "raw_content": turn.content,
                "content_sha256": turn.content_sha256,
                "context_distance": turn.context_distance,
                "context_role": "before",
                "instruction_capability": False,
                "item_kind": "prior_turn",
                "original_char_end": turn.source_char_end,
                "original_char_start": turn.source_char_start,
                "source_binding_sha256": turn.source_content_sha256,
                "source_id_binding_sha256": _domain_hash(
                    "context_source_id",
                    turn.source_id,
                ),
                "speaker_role": turn.speaker_role,
            }
        )

    context_spans = [
        span
        for span in evidence_context.spans
        if span.context_role != "target"
    ]
    preceding = [span for span in context_spans if span.context_role == "before"]
    following = [span for span in context_spans if span.context_role != "before"]
    ordered_siblings = [
        *[(span, distance) for distance, span in enumerate(reversed(preceding), 1)],
        *[(span, None) for span in following],
    ]
    for span, distance in ordered_siblings:
        if (
            span.assertion_origin_allowed
            or span.evidence_use != "disambiguating_context_only"
        ):
            raise SemanticTaskContractError(
                "sibling context received forbidden authority"
            )
        if _sha256_text(span.content) != span.content_sha256:
            raise SemanticTaskContractError("sibling context hash mismatch")
        if not (
            0 <= span.char_start < span.char_end
            <= evidence_context.source.source_char_count
        ):
            raise SemanticTaskContractError("sibling context offsets are invalid")
        if len(span.content) != span.char_end - span.char_start:
            raise SemanticTaskContractError("sibling context length mismatch")
        authority_items.append(
            {
                "assertion_origin_allowed": False,
                "raw_content": span.content,
                "content_sha256": span.content_sha256,
                "context_distance": distance,
                "context_role": span.context_role,
                "evidence_id_binding_sha256": _domain_hash(
                    "context_evidence_id",
                    span.evidence_id,
                ),
                "instruction_capability": False,
                "item_kind": "sibling_evidence",
                "original_char_end": span.char_end,
                "original_char_start": span.char_start,
                "source_binding_sha256": evidence_context.source.source_content_sha256,
                "speaker_role": "user",
            }
        )
    if len(authority_items) > MAX_CONTEXT_ITEMS:
        raise SemanticTaskContractError("evidence context item count is oversized")
    if sum(len(item["raw_content"]) for item in authority_items) > (
        MAX_CONTEXT_CHARS
    ):
        raise SemanticTaskContractError("evidence context content is oversized")
    context_binding = _require_sha256(
        evidence_context.envelope_sha256,
        "evidence_context.envelope_sha256",
    )
    context_header = {
        "contract_version": evidence_context.contract_version,
        "context_policy": evidence_context.context_policy,
        "envelope_sha256": context_binding,
    }
    context_authority = {**context_header, "items": authority_items}
    items = _compile_context_items_from_authority(
        context_authority,
        include_immediate_assistant_question=(
            include_immediate_assistant_question
        ),
    )
    return (
        context_binding,
        items,
        context_header,
    )


def _compile_context_items_from_authority(
    context_authority: Any,
    *,
    include_immediate_assistant_question: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(context_authority, dict) or set(context_authority) != {
        "context_policy",
        "contract_version",
        "envelope_sha256",
        "items",
    }:
        raise SemanticTaskContractError("context authority envelope is invalid")
    authority_items = context_authority["items"]
    if not isinstance(authority_items, list):
        raise SemanticTaskContractError("context authority must be a list")
    if len(authority_items) > MAX_CONTEXT_ITEMS:
        raise SemanticTaskContractError("context authority item count is oversized")
    result: list[dict[str, Any]] = []
    raw_char_count = 0
    for item in authority_items:
        if not isinstance(item, dict):
            raise SemanticTaskContractError("context authority item is invalid")
        item_kind = item.get("item_kind")
        if item_kind == "prior_turn":
            expected_keys = {
                "assertion_origin_allowed",
                "content_sha256",
                "context_distance",
                "context_role",
                "instruction_capability",
                "item_kind",
                "original_char_end",
                "original_char_start",
                "raw_content",
                "source_binding_sha256",
                "source_id_binding_sha256",
                "speaker_role",
            }
        elif item_kind == "sibling_evidence":
            expected_keys = {
                "assertion_origin_allowed",
                "content_sha256",
                "context_distance",
                "context_role",
                "evidence_id_binding_sha256",
                "instruction_capability",
                "item_kind",
                "original_char_end",
                "original_char_start",
                "raw_content",
                "source_binding_sha256",
                "speaker_role",
            }
        else:
            raise SemanticTaskContractError("context authority kind is invalid")
        if set(item) != expected_keys:
            raise SemanticTaskContractError("context authority shape is invalid")
        if (
            item["assertion_origin_allowed"] is not False
            or item["instruction_capability"] is not False
        ):
            raise SemanticTaskContractError("context authority is elevated")
        speaker_role = item["speaker_role"]
        if item_kind == "prior_turn":
            _require_sha256(
                item["source_id_binding_sha256"],
                "prior-turn source ID binding",
            )
            if speaker_role == "assistant":
                if not (
                    include_immediate_assistant_question
                    and item["context_distance"] == 1
                    and "?" in item["raw_content"]
                ):
                    continue
            elif speaker_role != "user":
                continue
        else:
            _require_sha256(
                item["evidence_id_binding_sha256"],
                "sibling evidence ID binding",
            )
            if speaker_role != "user":
                raise SemanticTaskContractError(
                    "sibling context speaker role is invalid"
                )
        raw_content = item["raw_content"]
        if not isinstance(raw_content, str) or not raw_content:
            raise SemanticTaskContractError("context authority content is invalid")
        raw_char_count += len(raw_content)
        if raw_char_count > MAX_CONTEXT_CHARS:
            raise SemanticTaskContractError("context authority content is oversized")
        if _sha256_text(raw_content) != item["content_sha256"]:
            raise SemanticTaskContractError("context authority hash mismatch")
        base_start = item["original_char_start"]
        base_end = item["original_char_end"]
        if (
            not isinstance(base_start, int)
            or isinstance(base_start, bool)
            or not isinstance(base_end, int)
            or isinstance(base_end, bool)
            or base_start < 0
            or base_end <= base_start
            or base_end - base_start != len(raw_content)
        ):
            raise SemanticTaskContractError("context authority offsets are invalid")
        source_binding = _require_sha256(
            item["source_binding_sha256"],
            "context source binding",
        )
        if speaker_role == "assistant":
            emitted = {
                key: value
                for key, value in item.items()
                if key not in {"raw_content", "content_sha256"}
            }
            emitted.update(
                {
                    "context_text": raw_content,
                    "parent_content_sha256": item["content_sha256"],
                    "selected_context_spans": [],
                    "source_binding_sha256": source_binding,
                }
            )
            result.append(emitted)
            continue
        context_gate = classify_personal_evidence_v1(
            raw_content,
            source_role=TRUSTED_SOURCE_ROLE,
        )
        if context_gate.decision != "send_external":
            continue
        selected: list[dict[str, Any]] = []
        selected_cursor = 0
        for ordinal, span in enumerate(context_gate.selected_spans):
            if not (
                0 <= span.char_start < span.char_end <= len(raw_content)
                and span.char_start >= selected_cursor
            ):
                raise SemanticTaskContractError(
                    "context selected span bounds are invalid"
                )
            selected_cursor = span.char_end
            if (
                span.sensitivity != "ordinary"
                or span.subject_hint != "owner"
            ):
                continue
            content = raw_content[span.char_start : span.char_end]
            if _sha256_text(content) != span.content_sha256:
                raise SemanticTaskContractError(
                    "context selected span hash mismatch"
                )
            selected.append(
                {
                    "assertion_mode": span.assertion_mode,
                    "category": span.category,
                    "content": content,
                    "content_sha256": span.content_sha256,
                    "ordinal": ordinal,
                    "original_char_end": base_start + span.char_end,
                    "original_char_start": base_start + span.char_start,
                    "parent_relative_char_end": span.char_end,
                    "parent_relative_char_start": span.char_start,
                    "sensitivity": span.sensitivity,
                    "subject_hint": span.subject_hint,
                }
            )
        if not selected:
            continue
        emitted = {
            key: value
            for key, value in item.items()
            if key not in {"raw_content", "content_sha256"}
        }
        emitted.update(
            {
                "context_gate_policy_sha256": context_gate.policy_sha256,
                "context_gate_result_sha256": canonical_sha256(
                    context_gate.public_dict()
                ),
                "parent_content_sha256": item["content_sha256"],
                "selected_context_spans": selected,
                "source_binding_sha256": source_binding,
            }
        )
        result.append(emitted)
    return result
def _validate_structured_source_spans(
    *,
    source: str,
    selected_spans: list[dict[str, Any]],
    source_spans: Any,
    label: str,
) -> None:
    _validate_result_source_spans(
        source=source,
        selected_spans=selected_spans,
        source_spans=source_spans,
        label=label,
    )


def _bounded_structured_input_sha256(value: Any) -> str:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_STRUCTURED_INPUT_BYTES:
        raise SemanticTaskContractError("structured semantic input is oversized")
    return _sha256_bytes(encoded)


def _compile(
    *,
    task: Task,
    source_text: str,
    expected_bindings: ExpectedSemanticTaskBindingsV1,
    payload: dict[str, Any],
    output_model: type[BaseModel],
    owner_binding_sha256: str,
    source_sha256: str,
    context_binding_sha256: str | None,
    profile_sha256: str,
    gate_result: PersonalEvidencePrefilterResultV1,
    gate_result_sha256: str,
    selected_spans: list[dict[str, Any]],
    structured_input_sha256: str | None,
    source_char_count: int,
    context_item_count: int,
    evidence_context_authority: (
        MemoryEvidenceContextEnvelopeV1
        | MemoryEvidenceContextEnvelopeV2
        | None
    ),
    extraction_result_authority: (
        CanonicalExtractionResultEnvelopeV1 | None
    ),
    extraction_result_ref: str | None,
) -> CompiledSemanticTaskV1:
    payload_json = canonical_json(payload)
    payload_bytes = payload_json.encode("utf-8")
    if not 1 <= len(payload_bytes) <= MAX_OUTBOUND_PAYLOAD_BYTES:
        raise SemanticTaskContractError("outbound semantic payload is oversized")
    payload_sha256 = _sha256_bytes(payload_bytes)
    output_schema_sha256 = canonical_sha256(output_model.model_json_schema())
    public_base = {
        "canonical_json_contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "contract_version": PUBLIC_BINDING_CONTRACT_VERSION,
        "task": task,
        "owner_binding_sha256": owner_binding_sha256,
        "source_sha256": source_sha256,
        "context_binding_sha256": context_binding_sha256,
        "profile_sha256": profile_sha256,
        "gate_policy_sha256": gate_result.policy_sha256,
        "gate_result_sha256": gate_result_sha256,
        "selected_spans_sha256": _selected_spans_binding_sha256(
            selected_spans
        ),
        "structured_input_sha256": structured_input_sha256,
        "output_schema_sha256": output_schema_sha256,
        "outbound_payload_sha256": payload_sha256,
        "source_char_count": source_char_count,
        "selected_span_count": len(selected_spans),
        "context_item_count": context_item_count,
        "outbound_payload_bytes": len(payload_bytes),
    }
    public_binding = SemanticTaskPublicBindingV1(
        **public_base,
        binding_sha256=canonical_sha256(public_base),
    )
    return CompiledSemanticTaskV1(
        task=task,
        source_text=source_text,
        expected_bindings=expected_bindings,
        outbound_payload_json=payload_json,
        output_model=output_model,
        public_binding=public_binding,
        evidence_context_authority=evidence_context_authority,
        extraction_result_authority=extraction_result_authority,
        extraction_result_ref=extraction_result_ref,
    )


def _validate_compiled_task(compiled: CompiledSemanticTaskV1) -> None:
    _require_expected_bindings(compiled.expected_bindings)
    if type(compiled.public_binding) is not SemanticTaskPublicBindingV1:
        raise SemanticTaskContractError("compiled public binding type is invalid")
    source = _validate_source(compiled.source_text)
    source_sha256 = _sha256_text(source)
    payload = strict_json_loads(compiled.outbound_payload_json)
    if canonical_json(payload) != compiled.outbound_payload_json:
        raise SemanticTaskContractError("compiled payload JSON is not canonical")
    if not isinstance(payload, dict):
        raise SemanticTaskContractError("compiled payload must be an object")
    common_keys = {
        "canonical_json_contract_version",
        "context_binding_sha256",
        "contract_version",
        "gate_contract_version",
        "gate_policy_sha256",
        "gate_policy_version",
        "gate_result_sha256",
        "owner_binding_sha256",
        "profile_sha256",
        "selected_source_spans",
        "selected_spans_sha256",
        "source_char_count",
        "source_offset_contract",
        "source_sha256",
        "structured_input_sha256",
        "task",
    }
    task_config: tuple[str, str, type[BaseModel]]
    if compiled.task == "memory_extraction":
        task_config = (
            EXTRACTION_PAYLOAD_CONTRACT_VERSION,
            "context",
            OpenAIExtractionResultV1,
        )
    elif compiled.task == "entity_validation":
        task_config = (
            ENTITY_VALIDATION_PAYLOAD_CONTRACT_VERSION,
            "structured_entity_mention",
            OpenAIEntityValidationResultV1,
        )
    elif compiled.task == "observation_entailment":
        task_config = (
            ENTAILMENT_PAYLOAD_CONTRACT_VERSION,
            "structured_observation",
            OpenAIEntailmentResultV1,
        )
    else:
        raise SemanticTaskContractError("compiled task is invalid")
    expected_contract, task_key, expected_output_model = task_config
    if set(payload) != common_keys | {task_key}:
        raise SemanticTaskContractError("compiled payload shape is invalid")
    if payload["contract_version"] != expected_contract:
        raise SemanticTaskContractError("compiled payload contract is invalid")
    if (
        payload["canonical_json_contract_version"]
        != CANONICAL_JSON_CONTRACT_VERSION
    ):
        raise SemanticTaskContractError(
            "compiled canonical JSON contract mismatch"
        )
    if payload["task"] != compiled.task:
        raise SemanticTaskContractError("compiled payload task mismatch")
    if payload["source_offset_contract"] != SOURCE_OFFSET_CONTRACT_VERSION:
        raise SemanticTaskContractError("compiled offset contract mismatch")
    if compiled.output_model is not expected_output_model:
        raise SemanticTaskContractError("compiled output model mismatch")
    config = compiled.output_model.model_config
    if (
        config.get("extra") != "forbid"
        or config.get("strict") is not True
        or config.get("frozen") is not True
    ):
        raise SemanticTaskContractError(
            "compiled output model is not strict and frozen"
        )

    gate_context = (
        compiled.evidence_context_authority
        if compiled.task == "memory_extraction"
        else None
    )
    if payload["gate_contract_version"] == EXCHANGE_CONTRACT_VERSION:
        if gate_context is not None and not isinstance(
            gate_context,
            MemoryEvidenceContextEnvelopeV2,
        ):
            raise SemanticTaskContractError(
                "compiled exchange gate context type is invalid"
            )
        authoritative_gate = classify_personal_evidence_exchange_v2(
            source,
            source_role=TRUSTED_SOURCE_ROLE,
            evidence_context=gate_context,
        )
    else:
        authoritative_gate = classify_personal_evidence_v1(
            source,
            source_role=TRUSTED_SOURCE_ROLE,
        )
    _, _, selected_spans, gate_result_sha256 = _validated_selected_source(
        source,
        authoritative_gate,
        gate_context,
    )
    selected_spans_sha256 = _selected_spans_binding_sha256(selected_spans)
    if payload["selected_source_spans"] != selected_spans:
        raise SemanticTaskContractError("compiled selected spans mismatch")
    derived_common = {
        "gate_contract_version": authoritative_gate.contract_version,
        "gate_policy_sha256": authoritative_gate.policy_sha256,
        "gate_policy_version": authoritative_gate.policy_version,
        "gate_result_sha256": gate_result_sha256,
        "selected_spans_sha256": selected_spans_sha256,
        "source_char_count": len(source),
        "source_sha256": source_sha256,
    }
    for key, expected_value in derived_common.items():
        if payload[key] != expected_value:
            raise SemanticTaskContractError(f"compiled {key} mismatch")

    owner_binding_sha256 = _require_sha256(
        payload["owner_binding_sha256"],
        "compiled owner binding",
    )
    profile_sha256 = _require_sha256(
        payload["profile_sha256"],
        "compiled profile binding",
    )
    structured_input_sha256: str | None
    context_item_count = 0
    if compiled.task == "memory_extraction":
        if (
            compiled.extraction_result_authority is not None
            or compiled.extraction_result_ref is not None
        ):
            raise SemanticTaskContractError(
                "extraction task received follow-up authority"
            )
        if payload["structured_input_sha256"] is not None:
            raise SemanticTaskContractError(
                "extraction structured input must be absent"
            )
        structured_input_sha256 = None
        context = payload["context"]
        if not isinstance(context, dict) or set(context) != {
            "context_policy",
            "contract_version",
            "envelope_sha256",
            "items",
        }:
            raise SemanticTaskContractError("compiled context shape is invalid")
        evidence_context = compiled.evidence_context_authority
        if evidence_context is None:
            context_binding_sha256 = None
            context_items: list[dict[str, Any]] = []
            context_header = {
                "context_policy": None,
                "contract_version": None,
                "envelope_sha256": None,
            }
        else:
            if type(evidence_context) not in (
                MemoryEvidenceContextEnvelopeV1,
                MemoryEvidenceContextEnvelopeV2,
            ):
                raise SemanticTaskContractError(
                    "compiled evidence context authority type is invalid"
                )
            if (
                owner_binding_sha256_v1(evidence_context.owner_user_id)
                != owner_binding_sha256
            ):
                raise SemanticTaskContractError(
                    "compiled evidence context owner binding mismatch"
                )
            (
                context_binding_sha256,
                context_items,
                context_header,
            ) = _validated_context(
                source=source,
                source_sha256=source_sha256,
                owner_user_id=evidence_context.owner_user_id,
                evidence_context=evidence_context,
                include_immediate_assistant_question=(
                    payload["gate_contract_version"]
                    == EXCHANGE_CONTRACT_VERSION
                ),
            )
        expected_context = {**context_header, "items": context_items}
        if context != expected_context:
            raise SemanticTaskContractError(
                "compiled context does not match evidence authority"
            )
        context_item_count = len(context_items)
    else:
        if compiled.evidence_context_authority is not None:
            raise SemanticTaskContractError(
                "follow-up task may not carry extraction context authority"
            )
        extraction_result = compiled.extraction_result_authority
        if type(extraction_result) is not CanonicalExtractionResultEnvelopeV1:
            raise SemanticTaskContractError(
                "follow-up task lacks canonical extraction authority"
            )
        reference = compiled.extraction_result_ref
        if not isinstance(reference, str):
            raise SemanticTaskContractError(
                "follow-up extraction reference is invalid"
            )
        structured_key = (
            "structured_entity_mention"
            if compiled.task == "entity_validation"
            else "structured_observation"
        )
        structured_model = (
            ProviderEntityMention
            if compiled.task == "entity_validation"
            else ProviderObservation
        )
        structured_value = _validate_model_input(
            payload[structured_key],
            structured_model,
        )
        _validate_extraction_result_binding(
            extraction_result=extraction_result,
            owner_binding_sha256=owner_binding_sha256,
            source_sha256=source_sha256,
            source_char_count=len(source),
            gate_result_sha256=gate_result_sha256,
            selected_spans_sha256=selected_spans_sha256,
        )
        if compiled.task == "entity_validation":
            authoritative_value = _select_extraction_entity(
                extraction_result,
                reference,
            )
            context_binding_sha256 = entity_mention_binding_sha256_v1(
                extraction_result,
                reference,
            )
        else:
            authoritative_value = _select_extraction_observation(
                extraction_result,
                reference,
            )
            context_binding_sha256 = observation_binding_sha256_v1(
                extraction_result,
                reference,
            )
        if (
            structured_value.model_dump(mode="json")
            != authoritative_value.model_dump(mode="json")
        ):
            raise SemanticTaskContractError(
                "compiled structured input does not match extraction authority"
            )
        if (
            compiled.task == "observation_entailment"
            and structured_value.sensitivity != "low"
        ):
            raise SemanticTaskContractError(
                "compiled entailment observation is not low sensitivity"
            )
        _validate_structured_source_spans(
            source=source,
            selected_spans=selected_spans,
            source_spans=structured_value.source_spans,
            label=structured_key,
        )
        structured_input_sha256 = _bounded_structured_input_sha256(
            structured_value.model_dump(mode="json")
        )
        if payload["structured_input_sha256"] != structured_input_sha256:
            raise SemanticTaskContractError("structured input hash mismatch")
    if payload["context_binding_sha256"] != context_binding_sha256:
        raise SemanticTaskContractError("compiled context binding mismatch")
    compiled.expected_bindings.validate_exact(
        task=compiled.task,
        owner_binding_sha256=owner_binding_sha256,
        source_sha256=source_sha256,
        context_binding_sha256=context_binding_sha256,
        profile_sha256=profile_sha256,
    )

    payload_bytes = compiled.outbound_payload_json.encode("utf-8")
    if not 1 <= len(payload_bytes) <= MAX_OUTBOUND_PAYLOAD_BYTES:
        raise SemanticTaskContractError("compiled payload is oversized")
    payload_sha256 = _sha256_bytes(payload_bytes)
    output_schema_sha256 = canonical_sha256(
        compiled.output_model.model_json_schema()
    )
    public_base = {
        "canonical_json_contract_version": CANONICAL_JSON_CONTRACT_VERSION,
        "contract_version": PUBLIC_BINDING_CONTRACT_VERSION,
        "task": compiled.task,
        "owner_binding_sha256": owner_binding_sha256,
        "source_sha256": source_sha256,
        "context_binding_sha256": context_binding_sha256,
        "profile_sha256": profile_sha256,
        "gate_policy_sha256": authoritative_gate.policy_sha256,
        "gate_result_sha256": gate_result_sha256,
        "selected_spans_sha256": selected_spans_sha256,
        "structured_input_sha256": structured_input_sha256,
        "output_schema_sha256": output_schema_sha256,
        "outbound_payload_sha256": payload_sha256,
        "source_char_count": len(source),
        "selected_span_count": len(selected_spans),
        "context_item_count": context_item_count,
        "outbound_payload_bytes": len(payload_bytes),
    }
    observed_public = compiled.public_binding.model_dump(
        mode="json",
        exclude={"binding_sha256"},
    )
    if observed_public != public_base:
        raise SemanticTaskContractError("compiled public binding fields mismatch")
    if compiled.public_binding.binding_sha256 != canonical_sha256(public_base):
        raise SemanticTaskContractError("public binding self-hash mismatch")


def _validate_source(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticTaskContractError("semantic source must be non-empty text")
    if len(value) > MAX_SOURCE_CHARS:
        raise SemanticTaskContractError("semantic source is oversized")
    if "\x00" in value:
        raise SemanticTaskContractError("semantic source contains NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SemanticTaskContractError(
            "semantic source contains invalid Unicode"
        ) from exc
    return value


def _validate_model_input(
    value: Any,
    model: type[OutputModelT],
) -> OutputModelT:
    if isinstance(value, str):
        parsed = strict_json_loads(value)
    elif isinstance(value, BaseModel):
        parsed = value.model_dump(mode="python")
        _bounded_canonical_json(
            parsed,
            MAX_STRICT_JSON_BYTES,
            "structured BaseModel input",
        )
    elif isinstance(value, Mapping):
        parsed = dict(value)
        _validate_json_tree(parsed)
        _bounded_canonical_json(
            parsed,
            MAX_STRICT_JSON_BYTES,
            "structured mapping input",
        )
    else:
        raise SemanticTaskContractError("structured semantic value is invalid")
    try:
        validated = model.model_validate(parsed, strict=True)
    except Exception as exc:
        raise SemanticTaskContractError(
            "structured semantic value violates its closed schema"
        ) from exc
    validated_value = validated.model_dump(mode="python")
    _validate_json_tree(validated_value)
    _bounded_canonical_json(
        validated_value,
        MAX_STRICT_JSON_BYTES,
        "validated structured input",
    )
    return validated


def _bounded_canonical_json(value: Any, limit: int, label: str) -> str:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > limit:
        raise SemanticTaskContractError(f"{label} is oversized")
    return encoded.decode("utf-8")


def _selected_spans_binding_sha256(
    selected_spans: list[dict[str, Any]],
) -> str:
    return canonical_sha256(
        [
            {
                key: value
                for key, value in span.items()
                if key != "content"
            }
            for span in selected_spans
        ]
    )


def _require_expected_bindings(value: Any) -> None:
    if type(value) is not ExpectedSemanticTaskBindingsV1:
        raise SemanticTaskContractError(
            "expected_bindings must have the exact contract type"
        )


def _validate_json_tree(value: Any) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise SemanticTaskContractError("JSON value exceeds node limit")
        if depth > MAX_JSON_DEPTH:
            raise SemanticTaskContractError("JSON value exceeds depth limit")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if item.bit_length() > 256:
                raise SemanticTaskContractError("JSON integer is oversized")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise SemanticTaskContractError("non-finite JSON number")
            return
        if isinstance(item, str):
            if len(item) > MAX_JSON_STRING_CHARS:
                raise SemanticTaskContractError("JSON string is oversized")
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise SemanticTaskContractError(
                    "JSON string contains invalid Unicode"
                ) from exc
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key:
                    raise SemanticTaskContractError(
                        "JSON object key must be non-empty text"
                    )
                if len(key) > MAX_JSON_KEY_CHARS:
                    raise SemanticTaskContractError("JSON object key is oversized")
                visit(child, depth + 1)
            return
        raise SemanticTaskContractError("value contains a non-JSON type")

    visit(value, 0)


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SemanticTaskContractError(f"{field} must be a lowercase SHA-256")
    return value


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_hash(kind: str, value: str) -> str:
    return _sha256_text(f"{CONTRACT_VERSION}\0{kind}\0{value}")


__all__ = [
    "CANONICAL_EXTRACTION_RESULT_CONTRACT_VERSION",
    "CANONICAL_JSON_CONTRACT_VERSION",
    "CONTRACT_VERSION",
    "CanonicalExtractionResultEnvelopeV1",
    "CompiledSemanticTaskV1",
    "ENTAILMENT_PAYLOAD_CONTRACT_VERSION",
    "ENTITY_VALIDATION_PAYLOAD_CONTRACT_VERSION",
    "EXPECTED_BINDINGS_CONTRACT_VERSION",
    "EXTRACTION_PAYLOAD_CONTRACT_VERSION",
    "ExpectedSemanticTaskBindingsV1",
    "OpenAIEntailmentResultV1",
    "OpenAIEntityValidationResultV1",
    "OpenAIExtractionResultV1",
    "PUBLIC_BINDING_CONTRACT_VERSION",
    "SOURCE_OFFSET_CONTRACT_VERSION",
    "SemanticTaskContractError",
    "SemanticTaskPublicBindingV1",
    "build_expected_bindings_v1",
    "canonical_json",
    "canonical_sha256",
    "compile_entailment_payload_v1",
    "compile_entity_validation_payload_v1",
    "compile_extraction_payload_v1",
    "entity_mention_binding_sha256_v1",
    "extraction_context_binding_sha256_v1",
    "observation_binding_sha256_v1",
    "owner_binding_sha256_v1",
    "parse_entailment_result_v1",
    "parse_entity_validation_result_v1",
    "parse_extraction_result_v1",
    "strict_json_loads",
]
