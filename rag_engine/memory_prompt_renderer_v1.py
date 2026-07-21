"""Pure rendering and control application for governed Memory V1."""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal
from uuid import UUID

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
MEMORY_CONTROL_APPLICATION_DECISION_VERSION = (
    "memory_control_application_decision_v1"
)
MEMORY_PROMPT_APPLICATION_RESULT_VERSION = "memory_prompt_application_result_v1"
MEMORY_PROMPT_CONTENT_FORMAT = "canonical_json_lines_v1"
MEMORY_PROMPT_AUTHORITY = "governed_memory_reference_data"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MemoryPromptRendererError(MemorySelectionContractError):
    """Fail-closed error at the response-policy-owned Memory boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_json_text(value: Any) -> str:
    return _canonical_json_bytes(value).decode("utf-8")


def _manifest_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fragment_prompt_tokens(content: str) -> int:
    """Pinned V1 accounting: ceil(exact canonical UTF-8 bytes / 4)."""

    byte_count = len(content.encode("utf-8"))
    return (byte_count + 3) // 4


def _validate_sha256(value: str, field_name: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _canonicalize_wire_json(value: str | bytes) -> bytes:
    parsed = json.loads(
        value,
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    return _canonical_json_bytes(parsed)


class SelectedMemoryControlV1(_StrictFrozenModel):
    """Selected zero-token control metadata; this is not yet applied."""

    control: MemoryControlRefV1
    action: ResponseControlAction
    surface_policy: SurfacePolicy
    sensitivity: Sensitivity
    scope_sha256: str
    content_tokens: Literal[0] = 0

    @field_validator("scope_sha256")
    @classmethod
    def scope_hash(cls, value: str) -> str:
        return _validate_sha256(value, "scope_sha256")


class RenderedMemoryFragmentV1(_StrictFrozenModel):
    """Exact candidate bytes associated with one selected record."""

    record: MemoryRecordRefV1
    content: str = Field(min_length=1, repr=False)
    actual_prompt_tokens: int = Field(ge=1, le=HARD_MAX_TOKENS)
    rendered_fragment_sha256: str

    @field_validator("rendered_fragment_sha256")
    @classmethod
    def fragment_hash(cls, value: str) -> str:
        return _validate_sha256(value, "rendered_fragment_sha256")

    @model_validator(mode="after")
    def exact_hash_and_token_accounting(self) -> "RenderedMemoryFragmentV1":
        if self.rendered_fragment_sha256 != _content_sha256(self.content):
            raise ValueError("rendered fragment hash differs from exact content")
        if self.actual_prompt_tokens != _fragment_prompt_tokens(self.content):
            raise ValueError("rendered fragment token count differs from exact content")
        return self

    def as_injected_record(self) -> InjectedMemoryRecordV1:
        return InjectedMemoryRecordV1(
            record=self.record,
            actual_prompt_tokens=self.actual_prompt_tokens,
            rendered_fragment_sha256=self.rendered_fragment_sha256,
        )


class MemoryPromptRenderResultV1(_StrictFrozenModel):
    """Manifest-bound selected and rendered candidates, never injection."""

    contract_version: Literal[MEMORY_PROMPT_RENDER_RESULT_VERSION]
    kind: Literal["memory"]
    authority: Literal[MEMORY_PROMPT_AUTHORITY]
    renderer_version: Literal[MEMORY_PROMPT_RENDERER_VERSION]
    content_format: Literal[MEMORY_PROMPT_CONTENT_FORMAT]
    selection_trace_id: UUID
    assembly_context_sha256: str
    assembly_input_sha256: str
    envelope_sha256: str
    request_binding_sha256: str
    request_id_sha256: str
    thread_id_sha256: str
    query_sha256: str
    max_memory_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    rendered_content: str = Field(repr=False)
    rendered_content_sha256: str
    rendered_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    selected_record_refs: tuple[MemoryRecordRefV1, ...]
    rendered_fragments: tuple[RenderedMemoryFragmentV1, ...]
    rendered_record_refs: tuple[MemoryRecordRefV1, ...]
    selected_controls: tuple[SelectedMemoryControlV1, ...]
    selected_control_refs: tuple[MemoryControlRefV1, ...]
    render_manifest_sha256: str

    @field_validator(
        "assembly_context_sha256",
        "assembly_input_sha256",
        "envelope_sha256",
        "request_binding_sha256",
        "request_id_sha256",
        "thread_id_sha256",
        "query_sha256",
        "rendered_content_sha256",
        "render_manifest_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @model_validator(mode="after")
    def reconcile(self) -> "MemoryPromptRenderResultV1":
        fragment_refs = tuple(item.record for item in self.rendered_fragments)
        control_refs = tuple(item.control for item in self.selected_controls)
        if self.rendered_record_refs != fragment_refs:
            raise ValueError("rendered refs differ from rendered fragments")
        if self.selected_record_refs != fragment_refs:
            raise ValueError("all selected Memory records must be rendered")
        if self.selected_control_refs != control_refs:
            raise ValueError("selected control refs differ from control metadata")
        ranks = [item.rank for item in self.selected_record_refs]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("rendered Memory records must preserve canonical rank")
        record_keys = [
            (
                item.owner_user_id,
                item.lane,
                item.record_id,
                item.revision_id,
                item.source_content_sha256,
                item.rank,
            )
            for item in self.rendered_record_refs
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
            for item in self.selected_control_refs
        ]
        if len(control_keys) != len(set(control_keys)):
            raise ValueError("render result contains duplicate controls")
        exact_content = "".join(item.content for item in self.rendered_fragments)
        if self.rendered_content != exact_content:
            raise ValueError("rendered content differs from ordered fragments")
        if self.rendered_content_sha256 != _content_sha256(exact_content):
            raise ValueError("rendered content hash mismatch")
        exact_tokens = sum(
            item.actual_prompt_tokens for item in self.rendered_fragments
        )
        if self.rendered_prompt_tokens != exact_tokens:
            raise ValueError("rendered token total does not reconcile")
        if self.rendered_prompt_tokens > self.max_memory_prompt_tokens:
            raise ValueError("rendered Memory content exceeds the assembly token cap")
        payload = self.model_dump(
            mode="json",
            exclude={"render_manifest_sha256"},
        )
        if self.render_manifest_sha256 != _manifest_sha256(payload):
            raise ValueError("Memory render manifest hash mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    @classmethod
    def from_wire_json(
        cls,
        value: str | bytes,
    ) -> "MemoryPromptRenderResultV1":
        return cls.model_validate_json(_canonicalize_wire_json(value))

    def sanitized_report(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind,
            "authority": self.authority,
            "renderer_version": self.renderer_version,
            "content_format": self.content_format,
            "selection_trace_id": str(self.selection_trace_id),
            "assembly_context_sha256": self.assembly_context_sha256,
            "assembly_input_sha256": self.assembly_input_sha256,
            "envelope_sha256": self.envelope_sha256,
            "request_binding_sha256": self.request_binding_sha256,
            "request_id_sha256": self.request_id_sha256,
            "thread_id_sha256": self.thread_id_sha256,
            "query_sha256": self.query_sha256,
            "max_memory_prompt_tokens": self.max_memory_prompt_tokens,
            "rendered_content_sha256": self.rendered_content_sha256,
            "rendered_prompt_tokens": self.rendered_prompt_tokens,
            "selected_record_count": len(self.selected_record_refs),
            "rendered_record_count": len(self.rendered_record_refs),
            "selected_control_count": len(self.selected_control_refs),
            "render_manifest_sha256": self.render_manifest_sha256,
        }


class MemoryControlApplicationDecisionV1(_StrictFrozenModel):
    """Trusted direct-relevance decision bound to one exact render/input."""

    contract_version: Literal[MEMORY_CONTROL_APPLICATION_DECISION_VERSION]
    assembly_input_sha256: str
    render_manifest_sha256: str
    direct_relevance_confirmed: bool
    decision_manifest_sha256: str

    @field_validator(
        "assembly_input_sha256",
        "render_manifest_sha256",
        "decision_manifest_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @model_validator(mode="after")
    def verify_manifest(self) -> "MemoryControlApplicationDecisionV1":
        payload = self.model_dump(
            mode="json",
            exclude={"decision_manifest_sha256"},
        )
        if self.decision_manifest_sha256 != _manifest_sha256(payload):
            raise ValueError("Memory control decision manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        render_result: MemoryPromptRenderResultV1,
        direct_relevance_confirmed: bool,
    ) -> "MemoryControlApplicationDecisionV1":
        verified = _strict_reparse_render_result(render_result)
        payload = {
            "contract_version": MEMORY_CONTROL_APPLICATION_DECISION_VERSION,
            "assembly_input_sha256": verified.assembly_input_sha256,
            "render_manifest_sha256": verified.render_manifest_sha256,
            "direct_relevance_confirmed": direct_relevance_confirmed,
        }
        return cls(
            **payload,
            decision_manifest_sha256=_manifest_sha256(payload),
        )

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    @classmethod
    def from_wire_json(
        cls,
        value: str | bytes,
    ) -> "MemoryControlApplicationDecisionV1":
        return cls.model_validate_json(_canonicalize_wire_json(value))


class MemoryPromptApplicationResultV1(_StrictFrozenModel):
    """Only this post-control result may claim injection or application."""

    contract_version: Literal[MEMORY_PROMPT_APPLICATION_RESULT_VERSION]
    kind: Literal["memory"]
    authority: Literal[MEMORY_PROMPT_AUTHORITY]
    renderer_version: Literal[MEMORY_PROMPT_RENDERER_VERSION]
    content_format: Literal[MEMORY_PROMPT_CONTENT_FORMAT]
    selection_trace_id: UUID
    assembly_context_sha256: str
    assembly_input_sha256: str
    envelope_sha256: str
    request_binding_sha256: str
    request_id_sha256: str
    thread_id_sha256: str
    query_sha256: str
    render_manifest_sha256: str
    control_decision_manifest_sha256: str
    max_memory_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    render_result: MemoryPromptRenderResultV1 = Field(repr=False)
    decision: MemoryControlApplicationDecisionV1
    direct_relevance_gate_required: bool
    memory_content_included: bool
    outcome: Literal[
        "included",
        "suppressed_by_direct_relevance_control",
        "no_rendered_content",
    ]
    content: str = Field(repr=False)
    content_sha256: str
    actual_prompt_tokens: int = Field(ge=0, le=HARD_MAX_TOKENS)
    fragments: tuple[RenderedMemoryFragmentV1, ...]
    injected_records: tuple[InjectedMemoryRecordV1, ...]
    injected_record_refs: tuple[MemoryRecordRefV1, ...]
    applied_control_refs: tuple[MemoryControlRefV1, ...]
    application_manifest_sha256: str

    @field_validator(
        "assembly_context_sha256",
        "assembly_input_sha256",
        "envelope_sha256",
        "request_binding_sha256",
        "request_id_sha256",
        "thread_id_sha256",
        "query_sha256",
        "render_manifest_sha256",
        "control_decision_manifest_sha256",
        "content_sha256",
        "application_manifest_sha256",
    )
    @classmethod
    def hashes(cls, value: str, info: Any) -> str:
        return _validate_sha256(value, info.field_name)

    @model_validator(mode="after")
    def reconcile(self) -> "MemoryPromptApplicationResultV1":
        render = self.render_result
        decision = self.decision
        copied_bindings = (
            (self.selection_trace_id, render.selection_trace_id),
            (self.assembly_context_sha256, render.assembly_context_sha256),
            (self.assembly_input_sha256, render.assembly_input_sha256),
            (self.envelope_sha256, render.envelope_sha256),
            (self.request_binding_sha256, render.request_binding_sha256),
            (self.request_id_sha256, render.request_id_sha256),
            (self.thread_id_sha256, render.thread_id_sha256),
            (self.query_sha256, render.query_sha256),
            (self.render_manifest_sha256, render.render_manifest_sha256),
            (
                self.control_decision_manifest_sha256,
                decision.decision_manifest_sha256,
            ),
            (self.max_memory_prompt_tokens, render.max_memory_prompt_tokens),
        )
        if any(actual != expected for actual, expected in copied_bindings):
            raise ValueError("Memory application binding differs from render")
        if decision.assembly_input_sha256 != render.assembly_input_sha256:
            raise ValueError("Memory control decision input binding mismatch")
        if decision.render_manifest_sha256 != render.render_manifest_sha256:
            raise ValueError("Memory control decision render binding mismatch")
        if self.fragments != render.rendered_fragments:
            raise ValueError("Memory application fragments differ from render")
        expected_gate = any(
            item.action == ResponseControlAction.REQUIRE_DIRECT_RELEVANCE
            for item in render.selected_controls
        )
        if self.direct_relevance_gate_required != expected_gate:
            raise ValueError("direct-relevance gate state does not reconcile")
        expected_applied = render.selected_control_refs
        if self.applied_control_refs != expected_applied:
            raise ValueError("all selected controls must be recorded as applied")
        has_content = bool(render.rendered_fragments)
        expected_included = has_content and (
            not expected_gate or decision.direct_relevance_confirmed
        )
        if self.memory_content_included != expected_included:
            raise ValueError("Memory content inclusion does not match control decision")
        if not has_content:
            expected_outcome = "no_rendered_content"
        elif expected_gate and not decision.direct_relevance_confirmed:
            expected_outcome = "suppressed_by_direct_relevance_control"
        else:
            expected_outcome = "included"
        if self.outcome != expected_outcome:
            raise ValueError("Memory application outcome does not reconcile")
        expected_injected = (
            tuple(item.as_injected_record() for item in render.rendered_fragments)
            if expected_included
            else ()
        )
        expected_refs = tuple(item.record for item in expected_injected)
        if self.injected_records != expected_injected:
            raise ValueError("injected records do not match applied render")
        if self.injected_record_refs != expected_refs:
            raise ValueError("injected refs do not match applied render")
        expected_content = render.rendered_content if expected_included else ""
        if self.content != expected_content:
            raise ValueError("application content does not match inclusion decision")
        if self.content_sha256 != _content_sha256(expected_content):
            raise ValueError("Memory application content hash mismatch")
        expected_tokens = (
            render.rendered_prompt_tokens if expected_included else 0
        )
        if self.actual_prompt_tokens != expected_tokens:
            raise ValueError("Memory application token count mismatch")
        if self.actual_prompt_tokens > self.max_memory_prompt_tokens:
            raise ValueError("applied Memory content exceeds token cap")
        payload = self.model_dump(
            mode="json",
            exclude={"application_manifest_sha256"},
        )
        if self.application_manifest_sha256 != _manifest_sha256(payload):
            raise ValueError("Memory application manifest hash mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self)

    @classmethod
    def from_wire_json(
        cls,
        value: str | bytes,
    ) -> "MemoryPromptApplicationResultV1":
        return cls.model_validate_json(_canonicalize_wire_json(value))

    def sanitized_report(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind,
            "renderer_version": self.renderer_version,
            "selection_trace_id": str(self.selection_trace_id),
            "assembly_context_sha256": self.assembly_context_sha256,
            "assembly_input_sha256": self.assembly_input_sha256,
            "envelope_sha256": self.envelope_sha256,
            "request_binding_sha256": self.request_binding_sha256,
            "request_id_sha256": self.request_id_sha256,
            "thread_id_sha256": self.thread_id_sha256,
            "query_sha256": self.query_sha256,
            "render_manifest_sha256": self.render_manifest_sha256,
            "control_decision_manifest_sha256": (
                self.control_decision_manifest_sha256
            ),
            "application_manifest_sha256": self.application_manifest_sha256,
            "direct_relevance_gate_required": (
                self.direct_relevance_gate_required
            ),
            "direct_relevance_confirmed": (
                self.decision.direct_relevance_confirmed
            ),
            "memory_content_included": self.memory_content_included,
            "outcome": self.outcome,
            "content_sha256": self.content_sha256,
            "actual_prompt_tokens": self.actual_prompt_tokens,
            "selected_record_count": len(self.render_result.selected_record_refs),
            "rendered_record_count": len(self.render_result.rendered_record_refs),
            "injected_record_count": len(self.injected_record_refs),
            "selected_control_count": len(
                self.render_result.selected_control_refs
            ),
            "applied_control_count": len(self.applied_control_refs),
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


def _strict_reparse_input(
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


def _strict_reparse_render_result(
    render_result: MemoryPromptRenderResultV1,
) -> MemoryPromptRenderResultV1:
    if not isinstance(render_result, MemoryPromptRenderResultV1):
        raise MemoryPromptRendererError(
            "expected MemoryPromptRenderResultV1"
        )
    try:
        return MemoryPromptRenderResultV1.from_wire_json(
            render_result.canonical_json_bytes()
        )
    except Exception as exc:
        raise MemoryPromptRendererError(
            "invalid MemoryPromptRenderResultV1"
        ) from exc


def _strict_reparse_decision(
    decision: MemoryControlApplicationDecisionV1,
) -> MemoryControlApplicationDecisionV1:
    if not isinstance(decision, MemoryControlApplicationDecisionV1):
        raise MemoryPromptRendererError(
            "expected MemoryControlApplicationDecisionV1"
        )
    try:
        return MemoryControlApplicationDecisionV1.from_wire_json(
            decision.canonical_json_bytes()
        )
    except Exception as exc:
        raise MemoryPromptRendererError(
            "invalid MemoryControlApplicationDecisionV1"
        ) from exc


def render_governed_memory_v1(
    *,
    memory_input: MemoryPromptAssemblyInputV1,
) -> MemoryPromptRenderResultV1:
    """Render selected candidates without claiming injection or control use."""

    verified = _strict_reparse_input(memory_input)
    context = verified.context
    envelope = verified.envelope
    if context.renderer_version != MEMORY_PROMPT_RENDERER_VERSION:
        raise MemoryPromptRendererError("Memory renderer version mismatch")

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
            record=ref,
            content=content,
            actual_prompt_tokens=tokens,
            rendered_fragment_sha256=_content_sha256(content),
        )
        for content, ref, tokens in zip(exact_fragments, refs, token_counts)
    )
    selected_controls: list[SelectedMemoryControlV1] = []
    for control in envelope.controls:
        if control.action != ResponseControlAction.REQUIRE_DIRECT_RELEVANCE:
            raise MemoryPromptRendererError(
                "unsupported Memory response-control action"
            )
        selected_controls.append(
            SelectedMemoryControlV1(
                control=MemoryControlRefV1.from_control(control),
                action=control.action,
                surface_policy=control.surface_policy,
                sensitivity=control.sensitivity,
                scope_sha256=control.scope_sha256,
                content_tokens=0,
            )
        )
    selected = tuple(selected_controls)
    rendered_content = "".join(exact_fragments)
    payload = {
        "contract_version": MEMORY_PROMPT_RENDER_RESULT_VERSION,
        "kind": "memory",
        "authority": MEMORY_PROMPT_AUTHORITY,
        "renderer_version": MEMORY_PROMPT_RENDERER_VERSION,
        "content_format": MEMORY_PROMPT_CONTENT_FORMAT,
        "selection_trace_id": envelope.selection_trace_id,
        "assembly_context_sha256": context.context_sha256,
        "assembly_input_sha256": verified.assembly_input_sha256,
        "envelope_sha256": envelope.envelope_sha256,
        "request_binding_sha256": context.request_binding_sha256,
        "request_id_sha256": context.request_id_sha256,
        "thread_id_sha256": context.thread_id_sha256,
        "query_sha256": context.query_sha256,
        "max_memory_prompt_tokens": context.max_memory_prompt_tokens,
        "rendered_content": rendered_content,
        "rendered_content_sha256": _content_sha256(rendered_content),
        "rendered_prompt_tokens": total_tokens,
        "selected_record_refs": refs,
        "rendered_fragments": fragments,
        "rendered_record_refs": refs,
        "selected_controls": selected,
        "selected_control_refs": tuple(item.control for item in selected),
    }
    return MemoryPromptRenderResultV1(
        **payload,
        render_manifest_sha256=_manifest_sha256(payload),
    )


def apply_memory_control_decision_v1(
    *,
    render_result: MemoryPromptRenderResultV1,
    decision: MemoryControlApplicationDecisionV1,
) -> MemoryPromptApplicationResultV1:
    """Apply the selected zero-token controls as one atomic pure operation."""

    render = _strict_reparse_render_result(render_result)
    verified_decision = _strict_reparse_decision(decision)
    if verified_decision.assembly_input_sha256 != render.assembly_input_sha256:
        raise MemoryPromptRendererError(
            "Memory control decision input binding mismatch"
        )
    if verified_decision.render_manifest_sha256 != render.render_manifest_sha256:
        raise MemoryPromptRendererError(
            "Memory control decision render binding mismatch"
        )
    gate_required = any(
        item.action == ResponseControlAction.REQUIRE_DIRECT_RELEVANCE
        for item in render.selected_controls
    )
    has_content = bool(render.rendered_fragments)
    include = has_content and (
        not gate_required or verified_decision.direct_relevance_confirmed
    )
    if not has_content:
        outcome = "no_rendered_content"
    elif gate_required and not verified_decision.direct_relevance_confirmed:
        outcome = "suppressed_by_direct_relevance_control"
    else:
        outcome = "included"
    injected = (
        tuple(item.as_injected_record() for item in render.rendered_fragments)
        if include
        else ()
    )
    content = render.rendered_content if include else ""
    payload = {
        "contract_version": MEMORY_PROMPT_APPLICATION_RESULT_VERSION,
        "kind": "memory",
        "authority": MEMORY_PROMPT_AUTHORITY,
        "renderer_version": render.renderer_version,
        "content_format": render.content_format,
        "selection_trace_id": render.selection_trace_id,
        "assembly_context_sha256": render.assembly_context_sha256,
        "assembly_input_sha256": render.assembly_input_sha256,
        "envelope_sha256": render.envelope_sha256,
        "request_binding_sha256": render.request_binding_sha256,
        "request_id_sha256": render.request_id_sha256,
        "thread_id_sha256": render.thread_id_sha256,
        "query_sha256": render.query_sha256,
        "render_manifest_sha256": render.render_manifest_sha256,
        "control_decision_manifest_sha256": (
            verified_decision.decision_manifest_sha256
        ),
        "max_memory_prompt_tokens": render.max_memory_prompt_tokens,
        "render_result": render,
        "decision": verified_decision,
        "direct_relevance_gate_required": gate_required,
        "memory_content_included": include,
        "outcome": outcome,
        "content": content,
        "content_sha256": _content_sha256(content),
        "actual_prompt_tokens": (
            render.rendered_prompt_tokens if include else 0
        ),
        "fragments": render.rendered_fragments,
        "injected_records": injected,
        "injected_record_refs": tuple(item.record for item in injected),
        "applied_control_refs": render.selected_control_refs,
    }
    return MemoryPromptApplicationResultV1(
        **payload,
        application_manifest_sha256=_manifest_sha256(payload),
    )


__all__ = [
    "MEMORY_CONTROL_APPLICATION_DECISION_VERSION",
    "MEMORY_PROMPT_APPLICATION_RESULT_VERSION",
    "MEMORY_PROMPT_AUTHORITY",
    "MEMORY_PROMPT_CONTENT_FORMAT",
    "MEMORY_PROMPT_RENDERER_VERSION",
    "MEMORY_PROMPT_RENDER_RESULT_VERSION",
    "MemoryControlApplicationDecisionV1",
    "MemoryPromptApplicationResultV1",
    "MemoryPromptRenderResultV1",
    "MemoryPromptRendererError",
    "RenderedMemoryFragmentV1",
    "SelectedMemoryControlV1",
    "apply_memory_control_decision_v1",
    "render_governed_memory_v1",
]
