from __future__ import annotations

"""Separate final-answer binding for LifeSwitch structured context."""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_prompt_integration_v1 import AssembledPromptV2


LIFESWITCH_ANSWER_RECORD_REF_VERSION = "lifeswitch_answer_record_ref_v1"
LIFESWITCH_ANSWER_BINDING_VERSION = "final_answer_lifeswitch_binding_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
        revalidate_instances="always",
    )


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


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(type(value).__name__)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class LifeSwitchAnswerRecordRefV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_ANSWER_RECORD_REF_VERSION] = (
        LIFESWITCH_ANSWER_RECORD_REF_VERSION
    )
    ordinal: int = Field(ge=0, le=7)
    projection: str = Field(min_length=1, max_length=80)
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE"]
    record_count: int = Field(ge=0, le=500)
    payload_sha256: str

    @field_validator("payload_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value


class FinalAnswerLifeSwitchBindingV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_ANSWER_BINDING_VERSION] = (
        LIFESWITCH_ANSWER_BINDING_VERSION
    )
    authenticated_actor_user_id: UUID = Field(repr=False)
    owner_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    answer_id: UUID
    request_id_sha256: str
    conversation_snapshot_sha256: str
    source_assembly_sha256: str
    envelope_sha256: str
    rendered_content_sha256: str
    answer_model_exposed: bool
    record_count: int = Field(ge=0, le=500)
    rendered_tokens: int = Field(ge=0, le=1000)
    record_refs: tuple[LifeSwitchAnswerRecordRefV1, ...] = Field(max_length=8)
    created_at: datetime
    binding_manifest_sha256: str

    @field_validator(
        "request_id_sha256",
        "conversation_snapshot_sha256",
        "source_assembly_sha256",
        "envelope_sha256",
        "rendered_content_sha256",
        "binding_manifest_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LifeSwitch binding time must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def exact_binding(self) -> "FinalAnswerLifeSwitchBindingV1":
        if self.authenticated_actor_user_id != self.owner_user_id:
            raise ValueError("LifeSwitch answer binding is owner-only")
        if not self.answer_model_exposed:
            raise ValueError("persisted LifeSwitch binding requires model exposure")
        if self.record_count != sum(item.record_count for item in self.record_refs):
            raise ValueError("LifeSwitch binding record counts do not reconcile")
        payload = self.model_dump(mode="json", exclude={"binding_manifest_sha256"})
        if self.binding_manifest_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch answer binding hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        assembly: AssembledPromptV2,
        authenticated_actor_user_id: UUID,
        answer_id: UUID,
        created_at: datetime,
    ) -> "FinalAnswerLifeSwitchBindingV1 | None":
        source = assembly.source_request
        envelope = source.lifeswitch_envelope
        rendered = source.lifeswitch_rendered
        if envelope is None or rendered is None:
            return None
        if not any(
            block.block_id == "lifeswitch_domain_context_v1"
            for block in assembly.context_blocks
        ):
            raise ValueError("LifeSwitch context was not exposed to the answer model")
        if authenticated_actor_user_id != envelope.owner_user_id:
            raise ValueError("LifeSwitch binding actor differs from owner")
        refs = tuple(
            LifeSwitchAnswerRecordRefV1(
                ordinal=index,
                projection=section.projection,
                status=section.status,
                record_count=section.record_count,
                payload_sha256=section.payload_sha256,
            )
            for index, section in enumerate(envelope.sections)
        )
        payload = {
            "contract_version": LIFESWITCH_ANSWER_BINDING_VERSION,
            "authenticated_actor_user_id": authenticated_actor_user_id,
            "owner_user_id": envelope.owner_user_id,
            "thread_id": envelope.thread_id,
            "answer_id": answer_id,
            "request_id_sha256": _text_sha256(envelope.request_id),
            "conversation_snapshot_sha256": envelope.conversation_snapshot_sha256,
            "source_assembly_sha256": assembly.manifest.assembly_sha256,
            "envelope_sha256": envelope.envelope_sha256,
            "rendered_content_sha256": rendered.content_sha256,
            "answer_model_exposed": True,
            "record_count": sum(item.record_count for item in refs),
            "rendered_tokens": rendered.estimated_tokens,
            "record_refs": refs,
            "created_at": created_at.astimezone(timezone.utc),
        }
        return cls(**payload, binding_manifest_sha256=_sha256(payload))


__all__ = [
    "FinalAnswerLifeSwitchBindingV1",
    "LifeSwitchAnswerRecordRefV1",
]
