from __future__ import annotations

"""Pure response-policy renderer for the governed Memory V1 boundary.

The renderer accepts only a manifest-bound MemoryPromptAssemblyInputV1.
It performs no retrieval, owner resolution, I/O, provider call, or policy
selection. Selected records are rendered atomically in canonical rank order.
Selected controls remain zero-token typed metadata and never enter prompt
content.
"""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.memory_v1_selection_envelope import (
    HARD_MAX_TOKENS,
    ClaimSelectionV1,
    InjectedMemoryRecordV1,
    LifePreferenceSelectionV1,
    MemoryControlRefV1,
    MemoryPromptAssemblyInputV1,
    MemoryRecordRefV1,
    MemorySelectionContractError,
    ProjectKnowledgeSelectionV1,
    ResponseControlAction,
    Sensitivity,
    SurfacePolicy,
)


MEMORY_PROMPT_RENDERER_VERSION = "resse_memory_prompt_renderer_v1"
MEMORY_PROMPT_RENDER_RESULT_VERSION = "memory_prompt_render_result_v1"
MEMORY_PROMPT_CONTENT_FORMAT = "canonical_json_lines_v1"
MEMORY_PROMPT_AUTHORITY = "governed_memory_reference_data"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MemoryPromptRendererError(MemorySelectionContractError):
    """Fail-closed error at the response-policy-owned Memory renderer."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_json_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fragment_prompt_tokens(content: str) -> int:
    """The pinned V1 accounting rule for an exact rendered fragment."""

    byte_count = len(content.encode("utf-8"))
    return (byte_count + 3) // 4


class AppliedMemoryControlV1(_StrictFrozenModel):
    """A selected response control applied as non-prompt policy metadata."""

    control: MemoryControlRefV1
    action: ResponseControlAction
    surface_policy: SurfacePolicy
    sensitivity: Sensitivity
    scope_sha256: str
    content_tokens: Literal[0] = 0

    @field_validator("scope_sha256")
    @classmethod
    def scope_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("scope_sha256 must be a lowercase SHA-256")
        return value


class RenderedMemoryFragmentV1(_StrictFrozenModel):
    """Exact model-facing bytes and their Memory V1 injection report."""

    content: str = Field(min_length=1)
    injected_record: InjectedMemoryRecordV1

    @model_validator(mode="after")
    def exact_hash_and_token_accounting(self) -> "RenderedMemoryFragmentV1":
        if (
            self.injected_record.rendered_fragment_sha256
            != _content_sha256(self.content)
        ):
            raise ValueError("rendered fragment hash differs from exact content")
        if (
            self.injected_record.actual_prompt_tokens
            != _fragment_prompt_tokens(self.content)
        ):
            raise ValueError("rendered fragment token count differs from exact content")
        return self


class MemoryPromptRenderResultV1(_StrictFrozenModel):
    """Private typed contribution plus exact Memory finalizer inputs."""

    contract_version: Literal[MEMORY_PROMPT_RENDER_RESULT_VERSION]
    kind: Literal["memory"]
    authority: Literal[MEMORY_PROMPT_AUTHORITY]
    renderer_version: Literal[MEMORY_PROMPT_RENDERER_VERSION]
    content_format: Literal[MEMORY_PROMPT_CONTENT_FORMAT]
    assembly_context_sha256: str
    assembly_input_sha256: str
    envelope_sha256: str
    max_memory_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    content: str
    content_sha256: str
    actual_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    selected_record_refs: tuple[MemoryRecordRefV1, ...]
    fragments: tuple[RenderedMemoryFragmentV1, ...]
    injected_records: tuple[InjectedMemoryRecordV1, ...]
    injected_record_refs: tuple[MemoryRecordRefV1, ...]
    applied_controls: tuple[AppliedMemoryControlV1, ...]
    applied_control_refs: tuple[MemoryControlRefV1, ...]

    @field_validator(
        "assembly_context_sha256",
        "assembly_input_sha256",
        "envelope_sha256",
        "content_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def reconcile(self) -> "MemoryPromptRenderResultV1":
        expected_refs = tuple(
            fragment.injected_record.record for fragment in self.fragments
        )
        expected_injected = tuple(
            fragment.injected_record for fragment in self.fragments
        )
        expected_control_refs = tuple(
            item.control for item in self.applied_controls
        )
        if self.injected_records != expected_injected:
            raise ValueError("injected records differ from rendered fragments")
        if self.injected_record_refs != expected_refs:
            raise ValueError("injected refs differ from rendered fragments")
        if self.selected_record_refs != expected_refs:
            raise ValueError("selected Memory records must be rendered atomically")
        ranks = [item.rank for item in self.selected_record_refs]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("rendered Memory records must preserve canonical rank")
        if self.applied_control_refs != expected_control_refs:
            raise ValueError("applied control refs differ from control metadata")
        record_keys = [
            (
                item.owner_user_id,
                item.lane,
                item.record_id,
                item.revision_id,
                item.source_content_sha256,
                item.rank,
            )
            for item in self.injected_record_refs
        ]
        if len(record_keys) != len(set(record_keys)):
            raise ValueError("render result contains duplicate records")
        control_keys = [
            (
                item.owner_user_id,
                item.record_id,
                item.revision_id,
                item.source_content_sha256,
                item.control_sha256,
            )
            for item in self.applied_control_refs
        ]
        if len(control_keys) != len(set(control_keys)):
            raise ValueError("render result contains duplicate controls")
        exact_content = "".join(fragment.content for fragment in self.fragments)
        if self.content != exact_content:
            raise ValueError("rendered content differs from ordered fragments")
        if self.content_sha256 != _content_sha256(self.content):
            raise ValueError("content hash differs from exact rendered content")
        exact_tokens = sum(
            fragment.injected_record.actual_prompt_tokens
            for fragment in self.fragments
        )
        if self.actual_prompt_tokens != exact_tokens:
            raise ValueError("rendered token total does not reconcile")
        if self.actual_prompt_tokens > self.max_memory_prompt_tokens:
            raise ValueError("rendered Memory content exceeds the assembly token cap")
        return self

    def sanitized_report(self) -> dict[str, Any]:
        """Return no prompt prose, owner IDs, or stable record/control handles."""

        return {
            "contract_version": self.contract_version,
            "kind": self.kind,
            "authority": self.authority,
            "renderer_version": self.renderer_version,
            "content_format": self.content_format,
            "assembly_context_sha256": self.assembly_context_sha256,
            "assembly_input_sha256": self.assembly_input_sha256,
            "envelope_sha256": self.envelope_sha256,
            "max_memory_prompt_tokens": self.max_memory_prompt_tokens,
            "content_sha256": self.content_sha256,
            "actual_prompt_tokens": self.actual_prompt_tokens,
            "selected_record_count": len(self.selected_record_refs),
            "injected_record_count": len(self.injected_records),
            "applied_control_count": len(self.applied_controls),
        }


def _common_record_payload(record: Any) -> dict[str, Any]:
    return {
        "authority": MEMORY_PROMPT_AUTHORITY,
        "lane": record.lane.value,
        "policy": {
            "sensitivity": record.sensitivity.value,
            "surface_policy": record.surface_policy.value,
            "use_instruction": record.use_instruction.value,
        },
        "rank": record.rank,
    }


def _record_payload(record: Any) -> dict[str, Any]:
    common = _common_record_payload(record)
    if isinstance(record, ClaimSelectionV1):
        return {
            **common,
            "record": {
                "component_key": record.component_key,
                "epistemic_status": record.epistemic_status.value,
                "predicate": record.predicate,
                "project_key": record.project_key,
                "text": record.text,
                "type": "claim",
            },
        }
    if isinstance(record, LifePreferenceSelectionV1):
        return {
            **common,
            "record": {
                "canonical_value": json.loads(record.canonical_value_json),
                "polarity": record.polarity.value,
                "preference_class": record.preference_class,
                "preference_domain": record.preference_domain,
                "preference_key": record.preference_key,
                "stability": record.stability.value,
                "type": "life_preference",
            },
        }
    if isinstance(record, ProjectKnowledgeSelectionV1):
        return {
            **common,
            "record": {
                "authority_level": record.authority_level.value,
                "component_key": record.component_key,
                "document_state": record.document_state.value,
                "knowledge_key": record.knowledge_key,
                "knowledge_kind": record.knowledge_kind.value,
                "project_key": record.project_key,
                "text": record.text,
                "type": "project_knowledge",
            },
        }
    raise MemoryPromptRendererError("unsupported selected Memory record type")


def _strict_reparse(
    memory_input: MemoryPromptAssemblyInputV1,
) -> MemoryPromptAssemblyInputV1:
    if not isinstance(memory_input, MemoryPromptAssemblyInputV1):
        raise MemoryPromptRendererError(
            "renderer requires MemoryPromptAssemblyInputV1"
        )
    try:
        return MemoryPromptAssemblyInputV1.from_wire_json(
            memory_input.canonical_json_bytes()
        )
    except Exception as exc:
        raise MemoryPromptRendererError(
            "invalid MemoryPromptAssemblyInputV1"
        ) from exc


def render_governed_memory_v1(
    *,
    memory_input: MemoryPromptAssemblyInputV1,
) -> MemoryPromptRenderResultV1:
    """Render one verified Memory input without side effects or fallback."""

    verified = _strict_reparse(memory_input)
    context = verified.context
    envelope = verified.envelope
    if context.renderer_version != MEMORY_PROMPT_RENDERER_VERSION:
        raise MemoryPromptRendererError(
            "Memory renderer version mismatch"
        )

    refs = tuple(MemoryRecordRefV1.from_record(item) for item in envelope.records)
    canonical_lines = tuple(
        _canonical_json_text(_record_payload(item)) for item in envelope.records
    )
    exact_fragments = tuple(
        line + ("\n" if index + 1 < len(canonical_lines) else "")
        for index, line in enumerate(canonical_lines)
    )
    token_counts = tuple(
        _fragment_prompt_tokens(fragment) for fragment in exact_fragments
    )
    total_tokens = sum(token_counts)
    if total_tokens > context.max_memory_prompt_tokens:
        raise MemoryPromptRendererError(
            "rendered Memory content exceeds the assembly token cap"
        )
    if any(item > HARD_MAX_TOKENS for item in token_counts):
        raise MemoryPromptRendererError(
            "rendered Memory fragment exceeds the hard token cap"
        )

    fragments = tuple(
        RenderedMemoryFragmentV1(
            content=content,
            injected_record=InjectedMemoryRecordV1(
                record=ref,
                actual_prompt_tokens=tokens,
                rendered_fragment_sha256=_content_sha256(content),
            ),
        )
        for content, ref, tokens in zip(exact_fragments, refs, token_counts)
    )
    injected = tuple(item.injected_record for item in fragments)

    applied_controls: list[AppliedMemoryControlV1] = []
    for control in envelope.controls:
        if control.action != ResponseControlAction.REQUIRE_DIRECT_RELEVANCE:
            raise MemoryPromptRendererError(
                "unsupported Memory response-control action"
            )
        applied_controls.append(
            AppliedMemoryControlV1(
                control=MemoryControlRefV1.from_control(control),
                action=control.action,
                surface_policy=control.surface_policy,
                sensitivity=control.sensitivity,
                scope_sha256=control.scope_sha256,
                content_tokens=0,
            )
        )
    applied = tuple(applied_controls)
    content = "".join(exact_fragments)
    return MemoryPromptRenderResultV1(
        contract_version=MEMORY_PROMPT_RENDER_RESULT_VERSION,
        kind="memory",
        authority=MEMORY_PROMPT_AUTHORITY,
        renderer_version=MEMORY_PROMPT_RENDERER_VERSION,
        content_format=MEMORY_PROMPT_CONTENT_FORMAT,
        assembly_context_sha256=context.context_sha256,
        assembly_input_sha256=verified.assembly_input_sha256,
        envelope_sha256=envelope.envelope_sha256,
        max_memory_prompt_tokens=context.max_memory_prompt_tokens,
        content=content,
        content_sha256=_content_sha256(content),
        actual_prompt_tokens=total_tokens,
        selected_record_refs=refs,
        fragments=fragments,
        injected_records=injected,
        injected_record_refs=refs,
        applied_controls=applied,
        applied_control_refs=tuple(item.control for item in applied),
    )


__all__ = [
    "AppliedMemoryControlV1",
    "MEMORY_PROMPT_AUTHORITY",
    "MEMORY_PROMPT_CONTENT_FORMAT",
    "MEMORY_PROMPT_RENDERER_VERSION",
    "MEMORY_PROMPT_RENDER_RESULT_VERSION",
    "MemoryPromptRenderResultV1",
    "MemoryPromptRendererError",
    "RenderedMemoryFragmentV1",
    "render_governed_memory_v1",
]
