from __future__ import annotations

"""Closed response contracts owned by the governed-Memory successor.

The installable successor package must not import host-chat models.  The host
application validates its own snapshot and policy contracts, then translates
them into these bounded request and assembly DTOs.
"""

from dataclasses import dataclass
import hashlib
import math
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    require_sha256,
    require_uuid,
)


SUCCESSOR_RESPONSE_REQUEST_CONTRACT = (
    "governed-memory-successor-response-request-v1"
)
SUCCESSOR_CONTEXT_BLOCK_VERSION = "prompt_reference_context_block_v1"
SUCCESSOR_CONTEXT_FRAGMENT_VERSION = "prompt_reference_fragment_v1"
SUCCESSOR_TOKEN_ESTIMATOR_VERSION = "utf8_bytes_div4_v1"
SUCCESSOR_MEMORY_BLOCK_ID = "governed_memory_successor_v1"
MAX_RESPONSE_QUERY_BYTES = 32_768
MAX_CONTEXT_BYTES = 96_000
MAX_CONTEXT_TOKENS = 32_000


@dataclass(frozen=True, slots=True, kw_only=True)
class SuccessorResponseActorBinding:
    owner_user_id: UUID
    session_id: UUID
    authentication_manifest_sha256: str
    request_id: str
    thread_id: UUID
    eligible: bool

    def __post_init__(self) -> None:
        require_uuid(self.owner_user_id, "invalid_response_memory_owner")
        require_uuid(self.session_id, "invalid_response_memory_session")
        require_sha256(
            self.authentication_manifest_sha256,
            "invalid_response_memory_authentication_manifest",
        )
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or len(self.request_id.encode("utf-8")) > 200
        ):
            raise ContractViolation("invalid_response_memory_request")
        require_uuid(self.thread_id, "invalid_response_memory_thread")
        if type(self.eligible) is not bool:
            raise ContractViolation("invalid_response_memory_eligibility")


@dataclass(frozen=True, slots=True, kw_only=True)
class SuccessorResponseRequestV1:
    authenticated_actor_user_id: UUID
    thread_id: UUID
    request_id: str
    current_message: str
    conversation_snapshot_sha256: str
    trusted_policy_signals_sha256: str
    contract_version: str = SUCCESSOR_RESPONSE_REQUEST_CONTRACT

    def __post_init__(self) -> None:
        if self.contract_version != SUCCESSOR_RESPONSE_REQUEST_CONTRACT:
            raise ContractViolation("invalid_response_memory_request_contract")
        require_uuid(
            self.authenticated_actor_user_id,
            "invalid_response_memory_authenticated_actor",
        )
        require_uuid(self.thread_id, "invalid_response_memory_thread")
        if (
            not isinstance(self.request_id, str)
            or not self.request_id
            or len(self.request_id.encode("utf-8")) > 200
        ):
            raise ContractViolation("invalid_response_memory_request")
        if (
            not isinstance(self.current_message, str)
            or not self.current_message
            or len(self.current_message.encode("utf-8")) > MAX_RESPONSE_QUERY_BYTES
        ):
            raise ContractViolation("invalid_response_memory_query")
        require_sha256(
            self.conversation_snapshot_sha256,
            "invalid_response_memory_snapshot",
        )
        require_sha256(
            self.trusted_policy_signals_sha256,
            "invalid_response_memory_policy_signals",
        )


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _require_hash(value: str, field_name: str) -> str:
    try:
        return require_sha256(value, f"invalid_response_memory_{field_name}")
    except ContractViolation as exc:
        raise ValueError(field_name) from exc


class SuccessorPromptReferenceFragmentV1(_StrictFrozenModel):
    contract_version: Literal[SUCCESSOR_CONTEXT_FRAGMENT_VERSION] = (
        SUCCESSOR_CONTEXT_FRAGMENT_VERSION
    )
    ordinal: int = Field(ge=0)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(ge=1)
    content_sha256: str
    estimated_tokens: int = Field(ge=1)

    @field_validator("content_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        return _require_hash(value, "content_sha256")


class SuccessorPromptReferenceContextBlockV1(_StrictFrozenModel):
    contract_version: Literal[SUCCESSOR_CONTEXT_BLOCK_VERSION] = (
        SUCCESSOR_CONTEXT_BLOCK_VERSION
    )
    block_id: Literal[SUCCESSOR_MEMORY_BLOCK_ID] = SUCCESSOR_MEMORY_BLOCK_ID
    kind: Literal["memory"] = "memory"
    authority: Literal["reference_data"] = "reference_data"
    source_contract_version: str = Field(min_length=1, max_length=160)
    source_manifest_sha256: str
    request_id_sha256: str
    query_sha256: str
    content: str = Field(min_length=1, repr=False)
    content_sha256: str
    content_bytes: int = Field(ge=1, le=MAX_CONTEXT_BYTES)
    estimated_tokens: int = Field(ge=1, le=MAX_CONTEXT_TOKENS)
    token_estimator_version: Literal[SUCCESSOR_TOKEN_ESTIMATOR_VERSION] = (
        SUCCESSOR_TOKEN_ESTIMATOR_VERSION
    )
    fragments: tuple[SuccessorPromptReferenceFragmentV1, ...] = Field(
        min_length=1
    )

    @field_validator(
        "source_manifest_sha256",
        "request_id_sha256",
        "query_sha256",
        "content_sha256",
    )
    @classmethod
    def valid_hashes(cls, value: str, info: Any) -> str:
        return _require_hash(value, info.field_name)

    @model_validator(mode="after")
    def exact_content_manifest(self) -> "SuccessorPromptReferenceContextBlockV1":
        raw = self.content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if self.content_sha256 != digest:
            raise ValueError("context content hash mismatch")
        if self.content_bytes != len(raw):
            raise ValueError("context byte count mismatch")
        if self.estimated_tokens != math.ceil(len(raw) / 4):
            raise ValueError("context token count mismatch")
        offset = 0
        for ordinal, fragment in enumerate(self.fragments):
            if fragment.ordinal != ordinal or fragment.byte_offset != offset:
                raise ValueError("Memory fragment ordering is not contiguous")
            end = offset + fragment.byte_length
            chunk = raw[offset:end]
            if len(chunk) != fragment.byte_length:
                raise ValueError("Memory fragment extends beyond context content")
            if fragment.content_sha256 != hashlib.sha256(chunk).hexdigest():
                raise ValueError("Memory fragment hash mismatch")
            if fragment.estimated_tokens != math.ceil(len(chunk) / 4):
                raise ValueError("Memory fragment token count mismatch")
            offset = end
        if offset != len(raw):
            raise ValueError("Memory fragment manifests do not cover context")
        return self


class SuccessorMemoryAssemblyV1(_StrictFrozenModel):
    successor_memory_context_block: (
        SuccessorPromptReferenceContextBlockV1 | None
    ) = Field(default=None, repr=False, exclude_if=lambda value: value is None)


__all__ = [
    "MAX_RESPONSE_QUERY_BYTES",
    "SUCCESSOR_CONTEXT_BLOCK_VERSION",
    "SUCCESSOR_CONTEXT_FRAGMENT_VERSION",
    "SUCCESSOR_MEMORY_BLOCK_ID",
    "SUCCESSOR_RESPONSE_REQUEST_CONTRACT",
    "SUCCESSOR_TOKEN_ESTIMATOR_VERSION",
    "SuccessorMemoryAssemblyV1",
    "SuccessorPromptReferenceContextBlockV1",
    "SuccessorPromptReferenceFragmentV1",
    "SuccessorResponseActorBinding",
    "SuccessorResponseRequestV1",
]
