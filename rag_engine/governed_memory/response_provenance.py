from __future__ import annotations

"""Bounded public provenance for one successor answer dispatch lifecycle."""

from enum import Enum
from typing import Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_sha256,
    require_exact_int,
    require_sha256,
    require_uuid,
    sha256_bytes,
)


SUCCESSOR_PROVENANCE_CONTRACT_VERSION = (
    "governed_memory_successor_answer_provenance_v1"
)
SUCCESSOR_PROVENANCE_DOMAIN = "governed_memory.response_provenance.v1"


class SuccessorMemoryNotApplicableReason(str, Enum):
    NO_STORE = "no_store"
    ATTACHMENT = "attachment"
    VOICE = "voice"
    WEB_SEARCH = "web_search"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


class SuccessorMemoryAnswerReferenceV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )

    claim_id: UUID
    revision_id: UUID
    rank: int = Field(ge=1, le=8)
    model_exposed: Literal[True] = True


class SuccessorMemoryAnswerProvenanceV1(BaseModel):
    """Content-free proof of exposure, never proof of semantic answer use."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )

    contract_version: Literal[SUCCESSOR_PROVENANCE_CONTRACT_VERSION] = (
        SUCCESSOR_PROVENANCE_CONTRACT_VERSION
    )
    provenance_basis: Literal[
        "successor_dispatch_receipt_model_exposure_not_semantic_use"
    ] = "successor_dispatch_receipt_model_exposure_not_semantic_use"
    binding_outcome: Literal["not_applicable", "no_memory_selected", "exposed"]
    not_applicable_reason: SuccessorMemoryNotApplicableReason | None
    answer_id: UUID
    prompt_sha256: str
    outbound_request_sha256: str
    binding_manifest_sha256: str | None
    selection_manifest_sha256: str | None
    injection_manifest_sha256: str | None
    selected_count: int = Field(ge=0, le=8)
    injected_count: int = Field(ge=0, le=8)
    model_exposed_count: int = Field(ge=0, le=8)
    semantic_use_verified: Literal[False] = False
    references: tuple[SuccessorMemoryAnswerReferenceV1, ...] = Field(
        max_length=8
    )
    provenance_sha256: str

    @field_validator(
        "prompt_sha256",
        "outbound_request_sha256",
        "provenance_sha256",
    )
    @classmethod
    def required_hash(cls, value: str) -> str:
        try:
            return require_sha256(value, "invalid_successor_provenance_sha256")
        except ContractViolation as exc:
            raise ValueError(exc.code) from None

    @field_validator(
        "binding_manifest_sha256",
        "selection_manifest_sha256",
        "injection_manifest_sha256",
    )
    @classmethod
    def optional_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return require_sha256(value, "invalid_successor_provenance_sha256")
        except ContractViolation as exc:
            raise ValueError(exc.code) from None

    def _hash_material(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"provenance_sha256"})

    @model_validator(mode="after")
    def exact_receipt(self) -> "SuccessorMemoryAnswerProvenanceV1":
        references = tuple(self.references)
        if tuple(item.rank for item in references) != tuple(
            range(1, len(references) + 1)
        ):
            raise ValueError("successor provenance ranks are not contiguous")
        if len({item.claim_id for item in references}) != len(references):
            raise ValueError("successor provenance references are duplicated")
        if len({item.revision_id for item in references}) != len(references):
            raise ValueError("successor provenance revisions are duplicated")
        if self.binding_outcome in {"not_applicable", "no_memory_selected"}:
            if (
                self.binding_manifest_sha256 is not None
                or self.selection_manifest_sha256 is not None
                or self.injection_manifest_sha256 is not None
                or self.selected_count != 0
                or self.injected_count != 0
                or self.model_exposed_count != 0
                or references
            ):
                raise ValueError("no-selection successor provenance is not empty")
            if (
                self.binding_outcome == "not_applicable"
                and self.not_applicable_reason is None
            ) or (
                self.binding_outcome == "no_memory_selected"
                and self.not_applicable_reason is not None
            ):
                raise ValueError("successor provenance reason is inconsistent")
        elif (
            self.not_applicable_reason is not None
            or self.binding_manifest_sha256 is None
            or self.selection_manifest_sha256 is None
            or self.injection_manifest_sha256 is None
            or self.binding_manifest_sha256 != self.injection_manifest_sha256
            or not 1 <= self.selected_count <= 8
            or self.selected_count < self.injected_count
            or self.injected_count != self.model_exposed_count
            or self.model_exposed_count != len(references)
        ):
            raise ValueError("exposed successor provenance is inconsistent")
        expected = canonical_sha256(
            SUCCESSOR_PROVENANCE_DOMAIN,
            self._hash_material(),
        )
        if self.provenance_sha256 != expected:
            raise ValueError("successor provenance hash mismatch")
        return self


def _create_provenance(
    material: dict[str, object],
) -> SuccessorMemoryAnswerProvenanceV1:
    hash_material = {
        "contract_version": SUCCESSOR_PROVENANCE_CONTRACT_VERSION,
        "provenance_basis": (
            "successor_dispatch_receipt_model_exposure_not_semantic_use"
        ),
        **material,
        "semantic_use_verified": False,
        "references": [
            (
                item.model_dump(mode="json")
                if isinstance(item, SuccessorMemoryAnswerReferenceV1)
                else item
            )
            for item in material["references"]  # type: ignore[union-attr]
        ],
    }
    return SuccessorMemoryAnswerProvenanceV1(
        **material,
        provenance_sha256=canonical_sha256(
            SUCCESSOR_PROVENANCE_DOMAIN,
            hash_material,
        ),
    )


def build_successor_no_memory_selected_provenance_v1(
    *,
    answer_id: UUID,
    prompt_sha256: str,
    outbound_request_bytes: bytes,
) -> SuccessorMemoryAnswerProvenanceV1:
    answer = require_uuid(answer_id, "invalid_successor_provenance_answer")
    prompt = require_sha256(
        prompt_sha256,
        "invalid_successor_provenance_prompt_sha256",
    )
    if (
        not isinstance(outbound_request_bytes, bytes)
        or not 1 <= len(outbound_request_bytes) <= 1_048_576
    ):
        raise ContractViolation("invalid_successor_provenance_outbound_request")
    return _create_provenance(
        {
            "binding_outcome": "no_memory_selected",
            "not_applicable_reason": None,
            "answer_id": answer,
            "prompt_sha256": prompt,
            "outbound_request_sha256": sha256_bytes(outbound_request_bytes),
            "binding_manifest_sha256": None,
            "selection_manifest_sha256": None,
            "injection_manifest_sha256": None,
            "selected_count": 0,
            "injected_count": 0,
            "model_exposed_count": 0,
            "references": (),
        }
    )


def build_successor_not_applicable_provenance_v1(
    *,
    reason: SuccessorMemoryNotApplicableReason,
    answer_id: UUID,
    prompt_sha256: str,
    outbound_request_bytes: bytes,
) -> SuccessorMemoryAnswerProvenanceV1:
    if not isinstance(reason, SuccessorMemoryNotApplicableReason):
        raise ContractViolation("invalid_successor_not_applicable_reason")
    value = build_successor_no_memory_selected_provenance_v1(
        answer_id=answer_id,
        prompt_sha256=prompt_sha256,
        outbound_request_bytes=outbound_request_bytes,
    )
    material = value.model_dump(
        mode="python",
        exclude={
            "contract_version",
            "provenance_basis",
            "provenance_sha256",
            "semantic_use_verified",
        },
    )
    material["binding_outcome"] = "not_applicable"
    material["not_applicable_reason"] = reason
    return _create_provenance(material)


def build_successor_exposed_provenance_v1(
    dispatched_binding: Mapping[str, object],
) -> SuccessorMemoryAnswerProvenanceV1:
    if not isinstance(dispatched_binding, Mapping):
        raise ContractViolation("invalid_successor_dispatched_binding")
    if (
        dispatched_binding.get("dispatch_state") != "dispatched"
        or dispatched_binding.get("outcome") != "exposed"
    ):
        raise ContractViolation("invalid_successor_dispatched_binding")
    try:
        answer = UUID(str(dispatched_binding.get("response_id")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContractViolation("invalid_successor_provenance_answer") from exc
    require_uuid(answer, "invalid_successor_provenance_answer")
    selected_count = require_exact_int(
        dispatched_binding.get("selected_count"),
        code="invalid_successor_provenance_selected_count",
        minimum=1,
        maximum=8,
    )
    injected_count = require_exact_int(
        dispatched_binding.get("injected_count"),
        code="invalid_successor_provenance_injected_count",
        minimum=1,
        maximum=8,
    )
    exposed_count = require_exact_int(
        dispatched_binding.get("model_exposed_count"),
        code="invalid_successor_provenance_exposed_count",
        minimum=1,
        maximum=8,
    )
    raw_claim_ids = dispatched_binding.get("injected_claim_ids", ())
    raw_revision_ids = dispatched_binding.get("injected_revision_ids", ())
    if (
        isinstance(raw_claim_ids, (str, bytes, bytearray))
        or not isinstance(raw_claim_ids, (list, tuple))
        or isinstance(raw_revision_ids, (str, bytes, bytearray))
        or not isinstance(raw_revision_ids, (list, tuple))
    ):
        raise ContractViolation("invalid_successor_provenance_lineage")
    claim_ids = tuple(raw_claim_ids)
    revision_ids = tuple(raw_revision_ids)
    if (
        selected_count < injected_count
        or injected_count != exposed_count
        or len(claim_ids) != exposed_count
        or len(revision_ids) != exposed_count
    ):
        raise ContractViolation("invalid_successor_provenance_lineage")
    try:
        references = tuple(
            SuccessorMemoryAnswerReferenceV1(
                claim_id=UUID(str(claim_id)),
                revision_id=UUID(str(revision_id)),
                rank=rank,
            )
            for rank, (claim_id, revision_id) in enumerate(
                zip(claim_ids, revision_ids, strict=True),
                start=1,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ContractViolation("invalid_successor_provenance_lineage") from exc
    binding_manifest = require_sha256(
        dispatched_binding.get("binding_sha256"),
        "invalid_successor_provenance_binding_sha256",
    )
    injection_manifest = require_sha256(
        dispatched_binding.get("injection_manifest_sha256"),
        "invalid_successor_provenance_injection_sha256",
    )
    if binding_manifest != injection_manifest:
        raise ContractViolation("successor_provenance_binding_hash_mismatch")
    return _create_provenance(
        {
            "binding_outcome": "exposed",
            "not_applicable_reason": None,
            "answer_id": answer,
            "prompt_sha256": require_sha256(
                dispatched_binding.get("prompt_sha256"),
                "invalid_successor_provenance_prompt_sha256",
            ),
            "outbound_request_sha256": require_sha256(
                dispatched_binding.get("outbound_request_sha256"),
                "invalid_successor_provenance_outbound_sha256",
            ),
            "binding_manifest_sha256": binding_manifest,
            "selection_manifest_sha256": require_sha256(
                dispatched_binding.get("selection_manifest_sha256"),
                "invalid_successor_provenance_selection_sha256",
            ),
            "injection_manifest_sha256": injection_manifest,
            "selected_count": selected_count,
            "injected_count": injected_count,
            "model_exposed_count": exposed_count,
            "references": references,
        }
    )


__all__ = [
    "SUCCESSOR_PROVENANCE_CONTRACT_VERSION",
    "SUCCESSOR_PROVENANCE_DOMAIN",
    "SuccessorMemoryAnswerProvenanceV1",
    "SuccessorMemoryAnswerReferenceV1",
    "SuccessorMemoryNotApplicableReason",
    "build_successor_exposed_provenance_v1",
    "build_successor_no_memory_selected_provenance_v1",
    "build_successor_not_applicable_provenance_v1",
]
