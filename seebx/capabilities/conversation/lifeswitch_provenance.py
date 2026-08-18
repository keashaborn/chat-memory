from __future__ import annotations

"""Content-free provenance receipt for LifeSwitch-backed final answers."""

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.capabilities.conversation.lifeswitch_answer_binding import FinalAnswerLifeSwitchBindingV1
from seebx.capabilities.conversation.lifeswitch_context import LifeSwitchPreparedContextV1


LIFESWITCH_PROVENANCE_SOURCE_REF_V1 = "lifeswitch_provenance_source_ref_v1"
LIFESWITCH_PROVENANCE_RECEIPT_V1 = "final_answer_lifeswitch_provenance_receipt_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATEGORY_ORDER = ("plan", "nutrition", "training", "measurements")
_PROJECTION_CATEGORIES: dict[str, tuple[str, ...]] = {
    "current_plan": ("plan",),
    "plan_adherence": ("plan", "nutrition", "training", "measurements"),
    "nutrition_day": ("plan", "nutrition"),
    "nutrition_range": ("plan", "nutrition"),
    "training_summary": ("plan", "training"),
    "training_session": ("training",),
    "training_range": ("training",),
    "daily_status_range": ("plan", "nutrition", "training"),
    "exercise_progression": ("training",),
    "exercise_frequency": ("training",),
    "lifting_progression_summary": ("plan", "training"),
    "measurements_summary": ("measurements",),
}


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
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


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


def categories_for_projection_v1(projection: str) -> tuple[str, ...]:
    try:
        values = _PROJECTION_CATEGORIES[projection]
    except KeyError:
        raise ValueError("unknown LifeSwitch provenance projection") from None
    return tuple(item for item in _CATEGORY_ORDER if item in values)


class LifeSwitchProvenanceSourceRefV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PROVENANCE_SOURCE_REF_V1] = (
        LIFESWITCH_PROVENANCE_SOURCE_REF_V1
    )
    ordinal: int = Field(ge=0, le=7)
    projection: str = Field(min_length=1, max_length=80)
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE"]
    record_count: int = Field(ge=0, le=500)
    window_start_date: date | None = None
    window_end_date: date | None = None
    window_exact: bool
    source_categories: tuple[Literal["plan", "nutrition", "training", "measurements"], ...] = Field(
        min_length=1,
        max_length=4,
    )
    payload_sha256: str
    source_ref_manifest_sha256: str

    @field_validator("payload_sha256", "source_ref_manifest_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_ref(self) -> "LifeSwitchProvenanceSourceRefV1":
        if (self.window_start_date is None) != (self.window_end_date is None):
            raise ValueError("LifeSwitch provenance window must be paired")
        if self.window_start_date and self.window_end_date < self.window_start_date:
            raise ValueError("LifeSwitch provenance window is reversed")
        if self.window_exact != (self.window_start_date is not None):
            raise ValueError("LifeSwitch provenance window exactness differs")
        if self.source_categories != categories_for_projection_v1(self.projection):
            raise ValueError("LifeSwitch provenance categories differ from projection")
        payload = self.model_dump(mode="json", exclude={"source_ref_manifest_sha256"})
        if self.source_ref_manifest_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch provenance source ref hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        ordinal: int,
        projection: str,
        status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE"],
        record_count: int,
        window_start_date: date | None,
        window_end_date: date | None,
        payload_sha256: str,
    ) -> "LifeSwitchProvenanceSourceRefV1":
        payload = {
            "contract_version": LIFESWITCH_PROVENANCE_SOURCE_REF_V1,
            "ordinal": ordinal,
            "projection": projection,
            "status": status,
            "record_count": record_count,
            "window_start_date": window_start_date,
            "window_end_date": window_end_date,
            "window_exact": window_start_date is not None,
            "source_categories": categories_for_projection_v1(projection),
            "payload_sha256": payload_sha256,
        }
        return cls(**payload, source_ref_manifest_sha256=_sha256(payload))


class FinalAnswerLifeSwitchProvenanceReceiptV1(_StrictFrozenModel):
    contract_version: Literal[LIFESWITCH_PROVENANCE_RECEIPT_V1] = (
        LIFESWITCH_PROVENANCE_RECEIPT_V1
    )
    authenticated_actor_user_id: UUID = Field(repr=False)
    owner_user_id: UUID = Field(repr=False)
    thread_id: UUID = Field(repr=False)
    answer_id: UUID
    request_id_sha256: str
    conversation_snapshot_sha256: str
    lifeswitch_prepared_context_manifest_sha256: str
    data_plan_sha256: str
    lifeswitch_binding_manifest_sha256: str
    source_assembly_sha256: str
    envelope_sha256: str
    assistant_text_sha256: str
    attestation_sha256: str
    answer_model_exposed: Literal[True] = True
    source_refs: tuple[LifeSwitchProvenanceSourceRefV1, ...] = Field(
        min_length=1,
        max_length=8,
    )
    created_at: datetime
    receipt_manifest_sha256: str

    @field_validator(
        "request_id_sha256",
        "conversation_snapshot_sha256",
        "lifeswitch_prepared_context_manifest_sha256",
        "data_plan_sha256",
        "lifeswitch_binding_manifest_sha256",
        "source_assembly_sha256",
        "envelope_sha256",
        "assistant_text_sha256",
        "attestation_sha256",
        "receipt_manifest_sha256",
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
            raise ValueError("LifeSwitch provenance receipt time must be aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def exact_receipt(self) -> "FinalAnswerLifeSwitchProvenanceReceiptV1":
        if self.authenticated_actor_user_id != self.owner_user_id:
            raise ValueError("LifeSwitch provenance receipt is owner-only")
        if tuple(item.ordinal for item in self.source_refs) != tuple(range(len(self.source_refs))):
            raise ValueError("LifeSwitch provenance refs are not ordered")
        if len({item.projection for item in self.source_refs}) != len(self.source_refs):
            raise ValueError("LifeSwitch provenance projections are duplicated")
        payload = self.model_dump(mode="json", exclude={"receipt_manifest_sha256"})
        if self.receipt_manifest_sha256 != _sha256(payload):
            raise ValueError("LifeSwitch provenance receipt hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        prepared_context: LifeSwitchPreparedContextV1,
        binding: FinalAnswerLifeSwitchBindingV1,
        assistant_text_sha256: str,
        attestation_sha256: str,
    ) -> "FinalAnswerLifeSwitchProvenanceReceiptV1":
        envelope = prepared_context.envelope
        if envelope is None or prepared_context.rendered is None:
            raise ValueError("LifeSwitch receipt requires exposed structured context")
        if binding.owner_user_id != envelope.owner_user_id:
            raise ValueError("LifeSwitch receipt owner differs from binding")
        if binding.thread_id != envelope.thread_id or binding.answer_id is None:
            raise ValueError("LifeSwitch receipt binding differs from context")
        if binding.envelope_sha256 != envelope.envelope_sha256:
            raise ValueError("LifeSwitch receipt envelope differs from binding")
        refs = tuple(
            LifeSwitchProvenanceSourceRefV1.create(
                ordinal=index,
                projection=section.projection,
                status=section.status,
                record_count=section.record_count,
                window_start_date=(section.window.start_date if section.window else None),
                window_end_date=(section.window.end_date if section.window else None),
                payload_sha256=section.payload_sha256,
            )
            for index, section in enumerate(envelope.sections)
        )
        payload = {
            "contract_version": LIFESWITCH_PROVENANCE_RECEIPT_V1,
            "authenticated_actor_user_id": binding.authenticated_actor_user_id,
            "owner_user_id": binding.owner_user_id,
            "thread_id": binding.thread_id,
            "answer_id": binding.answer_id,
            "request_id_sha256": binding.request_id_sha256,
            "conversation_snapshot_sha256": binding.conversation_snapshot_sha256,
            "lifeswitch_prepared_context_manifest_sha256": prepared_context.manifest_sha256,
            "data_plan_sha256": prepared_context.data_plan.plan_sha256,
            "lifeswitch_binding_manifest_sha256": binding.binding_manifest_sha256,
            "source_assembly_sha256": binding.source_assembly_sha256,
            "envelope_sha256": binding.envelope_sha256,
            "assistant_text_sha256": assistant_text_sha256,
            "attestation_sha256": attestation_sha256,
            "answer_model_exposed": True,
            "source_refs": refs,
            "created_at": binding.created_at,
        }
        return cls(**payload, receipt_manifest_sha256=_sha256(payload))


__all__ = [
    "FinalAnswerLifeSwitchProvenanceReceiptV1",
    "LifeSwitchProvenanceSourceRefV1",
    "categories_for_projection_v1",
]
