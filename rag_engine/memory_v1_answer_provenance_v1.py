from __future__ import annotations

"""Bounded user-safe provenance derived from the final answer binding."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rag_engine.memory_v1_selection_envelope import (
    FinalAnswerMemoryBindingV1,
    MemoryLane,
)


CONTRACT_VERSION = "memory_v1_answer_provenance_v1"
SHA256_CHARACTERS = frozenset("0123456789abcdef")


class GovernedMemoryAnswerReferenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    lane: MemoryLane
    record_id: UUID
    revision_id: UUID | None
    rank: int = Field(ge=1, le=8)
    model_exposed: Literal[True] = True


class GovernedMemoryAnswerProvenanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    provenance_basis: Literal[
        "final_answer_binding_model_exposure_not_semantic_use"
    ] = "final_answer_binding_model_exposure_not_semantic_use"
    binding_outcome: Literal[
        "no_memory_binding",
        "no_memory_selected",
        "selected_not_injected",
        "injected_not_exposed",
        "exposed",
    ]
    binding_manifest_sha256: str | None
    selected_count: int = Field(ge=0, le=8)
    injected_count: int = Field(ge=0, le=8)
    model_exposed_count: int = Field(ge=0, le=8)
    applied_control_count: int = Field(ge=0, le=8)
    semantic_use_verified: Literal[False] = False
    references: tuple[GovernedMemoryAnswerReferenceV1, ...] = Field(max_length=8)

    @field_validator("binding_manifest_sha256")
    @classmethod
    def optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(ch not in SHA256_CHARACTERS for ch in value)
        ):
            raise ValueError("binding manifest hash is invalid")
        return value


def build_governed_memory_answer_provenance_v1(
    binding: FinalAnswerMemoryBindingV1 | None,
) -> GovernedMemoryAnswerProvenanceV1:
    if binding is None:
        return GovernedMemoryAnswerProvenanceV1(
            binding_outcome="no_memory_binding",
            binding_manifest_sha256=None,
            selected_count=0,
            injected_count=0,
            model_exposed_count=0,
            applied_control_count=0,
            references=(),
        )

    items = tuple(binding.items)
    if binding.selected_count != len(items):
        raise ValueError("answer binding selected count is inconsistent")
    injected_count = sum(bool(item.injected) for item in items)
    exposed = tuple(item for item in items if item.answer_model_exposed)
    if binding.injected_count != injected_count:
        raise ValueError("answer binding injected count is inconsistent")
    if binding.exposed_count != len(exposed):
        raise ValueError("answer binding exposed count is inconsistent")
    if binding.applied_control_count > binding.selected_control_count:
        raise ValueError("answer binding control counts are inconsistent")

    seen: set[tuple[MemoryLane, UUID]] = set()
    references: list[GovernedMemoryAnswerReferenceV1] = []
    for item in exposed:
        record = item.record
        if record.owner_user_id != binding.owner_user_id:
            raise ValueError("answer binding contains a cross-owner record")
        key = (record.lane, record.record_id)
        if key in seen:
            raise ValueError("answer binding contains a duplicate exposed record")
        seen.add(key)
        references.append(
            GovernedMemoryAnswerReferenceV1(
                lane=record.lane,
                record_id=record.record_id,
                revision_id=record.revision_id,
                rank=record.rank,
            )
        )

    return GovernedMemoryAnswerProvenanceV1(
        binding_outcome=binding.outcome,
        binding_manifest_sha256=binding.binding_manifest_sha256,
        selected_count=binding.selected_count,
        injected_count=binding.injected_count,
        model_exposed_count=binding.exposed_count,
        applied_control_count=binding.applied_control_count,
        references=tuple(references),
    )


__all__ = [
    "CONTRACT_VERSION",
    "GovernedMemoryAnswerProvenanceV1",
    "GovernedMemoryAnswerReferenceV1",
    "build_governed_memory_answer_provenance_v1",
]
