from __future__ import annotations

"""Pure transcript-integrity contract shared by conversation variants.

This contract owns backend-authored assistant transcript provenance. It has no
database, provider, retrieval, compatibility, or Memory authority.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.contracts.conversation_provenance import (
    ACCEPTED_ASSISTANT_TRANSCRIPT_SOURCES,
    CANONICAL_ASSISTANT_TRANSCRIPT_SOURCE_V2,
)

ASSISTANT_ATTESTATION_VERSION = "assistant_transcript_attestation_v1"
ATTESTED_ASSISTANT_SOURCE = CANONICAL_ASSISTANT_TRANSCRIPT_SOURCE_V2
ATTESTED_ASSISTANT_SOURCES = ACCEPTED_ASSISTANT_TRANSCRIPT_SOURCES
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AssistantOutputKind(str, Enum):
    CONTENT = "content"
    REFUSAL = "refusal"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
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


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("assistant attestation time must be timezone-aware")
    return value.astimezone(timezone.utc)


class AssistantTranscriptAttestationV1(_StrictFrozenModel):
    """Append-only proof for one backend-authored assistant transcript row."""

    contract_version: Literal[ASSISTANT_ATTESTATION_VERSION] = (
        ASSISTANT_ATTESTATION_VERSION
    )
    authenticated_actor_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    answer_id: UUID
    request_id_sha256: str
    conversation_snapshot_sha256: str
    trusted_plan_sha256: str
    provider_request_sha256: str
    provider_response_sha256: str
    provider_response_id: str = Field(min_length=1)
    output_kind: AssistantOutputKind
    assistant_text_sha256: str
    created_at: datetime
    attestation_sha256: str

    @field_validator(
        "request_id_sha256",
        "conversation_snapshot_sha256",
        "trusted_plan_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "assistant_text_sha256",
        "attestation_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("assistant attestation hash must be lowercase SHA-256")
        return value

    @field_validator("provider_response_id")
    @classmethod
    def valid_provider_response_id(cls, value: str) -> str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("provider response id must be UTF-8") from exc
        if not 1 <= len(encoded) <= 240:
            raise ValueError("provider response id exceeds the UTF-8 byte limit")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def exact_manifest(self) -> "AssistantTranscriptAttestationV1":
        payload = self.model_dump(mode="json", exclude={"attestation_sha256"})
        if self.attestation_sha256 != _sha256(payload):
            raise ValueError("assistant attestation manifest hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        authenticated_actor_user_id: UUID,
        thread_id: UUID,
        answer_id: UUID,
        request_id_sha256: str,
        conversation_snapshot_sha256: str,
        trusted_plan_sha256: str,
        provider_request_sha256: str,
        provider_response_sha256: str,
        provider_response_id: str,
        output_kind: AssistantOutputKind,
        assistant_text_sha256: str,
        created_at: datetime,
    ) -> "AssistantTranscriptAttestationV1":
        payload = {
            "contract_version": ASSISTANT_ATTESTATION_VERSION,
            "authenticated_actor_user_id": authenticated_actor_user_id,
            "thread_id": thread_id,
            "answer_id": answer_id,
            "request_id_sha256": request_id_sha256,
            "conversation_snapshot_sha256": conversation_snapshot_sha256,
            "trusted_plan_sha256": trusted_plan_sha256,
            "provider_request_sha256": provider_request_sha256,
            "provider_response_sha256": provider_response_sha256,
            "provider_response_id": provider_response_id,
            "output_kind": output_kind,
            "assistant_text_sha256": assistant_text_sha256,
            "created_at": _utc(created_at),
        }
        return cls(**payload, attestation_sha256=_sha256(payload))


def validate_assistant_transcript_attestation_row_v1(
    row: Mapping[str, object],
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    chat_log_id: UUID,
    request_id: str,
    assistant_text: str,
    chat_created_at: datetime,
) -> AssistantTranscriptAttestationV1:
    """Reconstruct the complete manifest and bind it to one transcript row."""

    attestation = AssistantTranscriptAttestationV1(
        authenticated_actor_user_id=UUID(str(row["attestation_owner_user_id"])),
        thread_id=UUID(str(row["attestation_thread_id"])),
        answer_id=UUID(str(row["attestation_answer_id"])),
        request_id_sha256=str(row["attestation_request_id_sha256"]),
        conversation_snapshot_sha256=str(
            row["attestation_conversation_snapshot_sha256"]
        ),
        trusted_plan_sha256=str(row["attestation_trusted_plan_sha256"]),
        provider_request_sha256=str(
            row["attestation_provider_request_sha256"]
        ),
        provider_response_sha256=str(
            row["attestation_provider_response_sha256"]
        ),
        provider_response_id=str(row["attestation_provider_response_id"]),
        output_kind=AssistantOutputKind(str(row["attestation_output_kind"])),
        assistant_text_sha256=str(row["attestation_assistant_text_sha256"]),
        created_at=row["attestation_created_at"],
        attestation_sha256=str(row["attestation_sha256"]),
    )
    if (
        attestation.authenticated_actor_user_id != owner_user_id
        or attestation.thread_id != thread_id
        or attestation.answer_id != chat_log_id
        or UUID(str(row["attestation_chat_log_id"])) != chat_log_id
        or attestation.request_id_sha256 != text_sha256(request_id)
        or attestation.assistant_text_sha256 != text_sha256(assistant_text)
        or attestation.created_at != _utc(chat_created_at)
    ):
        raise ValueError("assistant attestation differs from transcript")
    return attestation


__all__ = [
    "ASSISTANT_ATTESTATION_VERSION",
    "ATTESTED_ASSISTANT_SOURCE",
    "ATTESTED_ASSISTANT_SOURCES",
    "AssistantOutputKind",
    "AssistantTranscriptAttestationV1",
    "text_sha256",
    "validate_assistant_transcript_attestation_row_v1",
]
