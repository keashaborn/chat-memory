from __future__ import annotations

"""Bounded lower-authority provenance for prior LifeSwitch-backed answers."""

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.capabilities.conversation.lifeswitch_answer_binding import (
    FinalAnswerLifeSwitchBindingV1,
    LifeSwitchAnswerRecordRefV1,
)
from seebx.capabilities.conversation.lifeswitch_provenance import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
    LifeSwitchProvenanceSourceRefV1,
)
from seebx.capabilities.conversation.snapshot import (
    ATTESTED_ASSISTANT_SOURCE,
    ConversationSnapshotOutcome,
    ConversationSnapshotV1,
)


PRIOR_LIFESWITCH_PROVENANCE_V1 = "prior_lifeswitch_provenance_v1"
MAX_PROVENANCE_CANDIDATES = 9
MAX_PROVENANCE_RESPONSES = 3
MAX_SOURCE_REFS_PER_RESPONSE = 8
MAX_PROVENANCE_CONTENT_BYTES = 8_192
MAX_PROVENANCE_CONTENT_TOKENS = 2_048
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RE = re.compile(
    r"\b(where|how|source|sourced|pulled|retrieved|obtained|access|accessed|"
    r"record|records|data|numbers?|information|came from|got)\b",
    re.IGNORECASE,
)
_PRIOR_RE = re.compile(
    r"\b(you|your|that|those|this|these|earlier|previous|prior|last|"
    r"answer|response|numbers?|figures?|information|data|records?)\b",
    re.IGNORECASE,
)
_PERSONAL_DOMAIN_RE = re.compile(
    r"\b(my|mine|lifeswitch|macro|nutrition|protein|calorie|training|workout|"
    r"exercise|lift|weight|measurement|waist|body fat|plan|goal)\b",
    re.IGNORECASE,
)
_DIRECT_ACCESS_RE = re.compile(
    r"\b(?:did|do|can|could|have)\s+you\b.{0,80}\b(?:access|read|retrieve|"
    r"pull|use|look at|see)\b",
    re.IGNORECASE,
)
_ANAPHORIC_SOURCE_RES = (
    re.compile(
        r"^(?:and\s+)?where(?:\s+did|['’]d)\s+you\s+"
        r"(?:get|find|pull|retrieve|obtain)\s+(?:all\s+)?(?:of\s+)?"
        r"(?:(?:that|those|these|the)\s+)?"
        r"(?:numbers?|figures?|values?|data|information|records?)\s*"
        r"(?:from)?[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:and\s+)?where\s+did\s+(?:that|those|these|the)\s+"
        r"(?:numbers?|figures?|values?|data|information|records?)\s+"
        r"come\s+from[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:and\s+)?how\s+(?:do|did|can|could)\s+you\s+know"
        r"(?:\s+(?:that|this|those|these))?[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:and\s+)?what\s+(?:was|is|were|are)\s+"
        r"(?:(?:that|this|those|these)(?:\s+(?:answer|response|numbers?|"
        r"figures?|values?|data|information))?|the\s+(?:answer|response|"
        r"numbers?|figures?|values?|data|information))\s+based\s+on[?.!]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:and\s+)?(?:what|which)\s+(?:records?|sources?|data)\s+"
        r"(?:supported?|backed)\s+(?:(?:that|this|those|these)(?:\s+"
        r"(?:answer|response|numbers?|figures?|values?))?|the\s+"
        r"(?:answer|response|numbers?|figures?|values?))[?.!]*$",
        re.IGNORECASE,
    ),
)


class PriorLifeSwitchProvenanceError(RuntimeError):
    pass


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


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _tokens(value: str) -> int:
    return (len(value.encode("utf-8")) + 3) // 4


def _utc(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prior LifeSwitch time is not timezone-aware")
    return value.astimezone(timezone.utc)


def prior_lifeswitch_provenance_requested_v1(message: str) -> bool:
    if not isinstance(message, str) or not message.strip():
        return False
    normalized = " ".join(message.split())
    if any(pattern.fullmatch(normalized) for pattern in _ANAPHORIC_SOURCE_RES):
        return True
    return bool(
        _PERSONAL_DOMAIN_RE.search(normalized)
        and (
            (_SOURCE_RE.search(normalized) and _PRIOR_RE.search(normalized))
            or _DIRECT_ACCESS_RE.search(normalized)
        )
    )


def _json_array(value: Any, *, maximum_bytes: int = 65_536) -> list[Any] | None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > maximum_bytes:
            return None
        try:
            value = json.loads(value)
        except Exception:
            return None
    return value if isinstance(value, list) else None


class PriorLifeSwitchResponseV1(_StrictFrozenModel):
    relative_ordinal: int = Field(ge=0, lt=MAX_PROVENANCE_RESPONSES)
    answer_id: UUID = Field(repr=False)
    provenance_status: Literal["exact_receipt", "historical_binding_only"]
    source_refs: tuple[LifeSwitchProvenanceSourceRefV1, ...] = Field(
        min_length=1,
        max_length=MAX_SOURCE_REFS_PER_RESPONSE,
    )
    answer_sha256: str
    attestation_sha256: str

    @field_validator("answer_sha256", "attestation_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_response(self) -> "PriorLifeSwitchResponseV1":
        if tuple(item.ordinal for item in self.source_refs) != tuple(range(len(self.source_refs))):
            raise ValueError("prior LifeSwitch refs are not ordered")
        if self.provenance_status == "historical_binding_only" and any(
            item.window_exact for item in self.source_refs
        ):
            raise ValueError("historical binding cannot assert exact windows")
        return self


class PriorLifeSwitchProvenanceEnvelopeV1(_StrictFrozenModel):
    contract_version: Literal[PRIOR_LIFESWITCH_PROVENANCE_V1] = (
        PRIOR_LIFESWITCH_PROVENANCE_V1
    )
    authenticated_actor_user_id_sha256: str
    thread_id_sha256: str
    conversation_snapshot_sha256: str
    current_request_id_sha256: str
    current_query_sha256: str
    responses: tuple[PriorLifeSwitchResponseV1, ...] = Field(
        min_length=1,
        max_length=MAX_PROVENANCE_RESPONSES,
    )
    content: str = Field(min_length=1, repr=False)
    content_sha256: str
    content_bytes: int = Field(ge=1, le=MAX_PROVENANCE_CONTENT_BYTES)
    estimated_tokens: int = Field(ge=1, le=MAX_PROVENANCE_CONTENT_TOKENS)
    manifest_sha256: str

    @field_validator(
        "authenticated_actor_user_id_sha256",
        "thread_id_sha256",
        "conversation_snapshot_sha256",
        "current_request_id_sha256",
        "current_query_sha256",
        "content_sha256",
        "manifest_sha256",
    )
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("invalid SHA-256")
        return value

    @model_validator(mode="after")
    def exact_envelope(self) -> "PriorLifeSwitchProvenanceEnvelopeV1":
        if tuple(item.relative_ordinal for item in self.responses) != tuple(range(len(self.responses))):
            raise ValueError("prior LifeSwitch responses are not ordered")
        if len({item.answer_id for item in self.responses}) != len(self.responses):
            raise ValueError("prior LifeSwitch responses are duplicated")
        if self.content != _render_content(self.responses):
            raise ValueError("prior LifeSwitch content is not canonical")
        raw = self.content.encode("utf-8")
        if self.content_bytes != len(raw) or self.content_sha256 != _text_sha256(self.content):
            raise ValueError("prior LifeSwitch content manifest differs")
        if self.estimated_tokens != _tokens(self.content):
            raise ValueError("prior LifeSwitch token estimate differs")
        payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.manifest_sha256 != _sha256(payload):
            raise ValueError("prior LifeSwitch envelope hash mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        authenticated_actor_user_id: UUID,
        thread_id: UUID,
        conversation_snapshot_sha256: str,
        current_request_id: str,
        current_query: str,
        responses: tuple[PriorLifeSwitchResponseV1, ...],
    ) -> "PriorLifeSwitchProvenanceEnvelopeV1":
        content = _render_content(responses)
        payload = {
            "contract_version": PRIOR_LIFESWITCH_PROVENANCE_V1,
            "authenticated_actor_user_id_sha256": _text_sha256(authenticated_actor_user_id),
            "thread_id_sha256": _text_sha256(thread_id),
            "conversation_snapshot_sha256": conversation_snapshot_sha256,
            "current_request_id_sha256": _text_sha256(current_request_id),
            "current_query_sha256": _text_sha256(current_query),
            "responses": responses,
            "content": content,
            "content_sha256": _text_sha256(content),
            "content_bytes": len(content.encode("utf-8")),
            "estimated_tokens": _tokens(content),
        }
        return cls(**payload, manifest_sha256=_sha256(payload))


def _render_content(responses: tuple[PriorLifeSwitchResponseV1, ...]) -> str:
    payload = {
        "contract_version": PRIOR_LIFESWITCH_PROVENANCE_V1,
        "instructions": [
            "This is lower-authority server-verified provenance for prior LifeSwitch-backed answers.",
            "It identifies authenticated source categories and projections, not current values.",
            "It does not prove that every prior value or inference was correct.",
            "Use it only to answer where prior personal data came from or whether LifeSwitch records were accessed.",
            "For current values, use fresh LifeSwitch context instead.",
        ],
        "responses": [
            {
                "provenance_status": response.provenance_status,
                "relative_ordinal": response.relative_ordinal,
                "sources": [
                    {
                        "ordinal": ref.ordinal,
                        "projection": ref.projection,
                        "record_count": ref.record_count,
                        "source_categories": ref.source_categories,
                        "status": ref.status,
                        "window_end_date": ref.window_end_date,
                        "window_exact": ref.window_exact,
                        "window_start_date": ref.window_start_date,
                    }
                    for ref in response.source_refs
                ],
            }
            for response in responses
        ],
    }
    content = _canonical_json_bytes(payload).decode("utf-8")
    if len(content.encode("utf-8")) > MAX_PROVENANCE_CONTENT_BYTES:
        raise PriorLifeSwitchProvenanceError("prior LifeSwitch provenance byte budget exceeded")
    if _tokens(content) > MAX_PROVENANCE_CONTENT_TOKENS:
        raise PriorLifeSwitchProvenanceError("prior LifeSwitch provenance token budget exceeded")
    return content


def _binding_from_row(row: dict[str, Any]) -> FinalAnswerLifeSwitchBindingV1:
    refs = _json_array(row.get("binding_record_refs"))
    if refs is None:
        raise ValueError("LifeSwitch binding refs are malformed")
    return FinalAnswerLifeSwitchBindingV1(
        authenticated_actor_user_id=UUID(str(row["binding_actor_user_id"])),
        owner_user_id=UUID(str(row["owner_user_id"])),
        thread_id=UUID(str(row["thread_id"])),
        answer_id=UUID(str(row["answer_id"])),
        request_id_sha256=str(row["binding_request_id_sha256"]),
        conversation_snapshot_sha256=str(row["binding_snapshot_sha256"]),
        source_assembly_sha256=str(row["binding_source_assembly_sha256"]),
        envelope_sha256=str(row["binding_envelope_sha256"]),
        rendered_content_sha256=str(row["binding_rendered_content_sha256"]),
        answer_model_exposed=bool(row["binding_answer_model_exposed"]),
        record_count=int(row["binding_record_count"]),
        rendered_tokens=int(row["binding_rendered_tokens"]),
        record_refs=tuple(
            LifeSwitchAnswerRecordRefV1.model_validate_json(
                _canonical_json_bytes(item)
            )
            for item in refs
        ),
        created_at=_utc(row["created_at"]),
        binding_manifest_sha256=str(row["binding_manifest_sha256"]),
    )


def _receipt_from_row(row: dict[str, Any]) -> FinalAnswerLifeSwitchProvenanceReceiptV1:
    refs = _json_array(row.get("receipt_source_refs"))
    if refs is None:
        raise ValueError("LifeSwitch receipt refs are malformed")
    return FinalAnswerLifeSwitchProvenanceReceiptV1(
        authenticated_actor_user_id=UUID(str(row["receipt_actor_user_id"])),
        owner_user_id=UUID(str(row["owner_user_id"])),
        thread_id=UUID(str(row["thread_id"])),
        answer_id=UUID(str(row["answer_id"])),
        request_id_sha256=str(row["receipt_request_id_sha256"]),
        conversation_snapshot_sha256=str(row["receipt_snapshot_sha256"]),
        lifeswitch_prepared_context_manifest_sha256=str(row["prepared_context_manifest_sha256"]),
        data_plan_sha256=str(row["data_plan_sha256"]),
        lifeswitch_binding_manifest_sha256=str(row["receipt_binding_manifest_sha256"]),
        source_assembly_sha256=str(row["receipt_source_assembly_sha256"]),
        envelope_sha256=str(row["receipt_envelope_sha256"]),
        assistant_text_sha256=str(row["receipt_assistant_text_sha256"]),
        attestation_sha256=str(row["receipt_attestation_sha256"]),
        answer_model_exposed=bool(row["receipt_answer_model_exposed"]),
        source_refs=tuple(
            LifeSwitchProvenanceSourceRefV1.model_validate_json(
                _canonical_json_bytes(item)
            )
            for item in refs
        ),
        created_at=_utc(row["receipt_created_at"]),
        receipt_manifest_sha256=str(row["receipt_manifest_sha256"]),
    )


def _historical_refs(binding: FinalAnswerLifeSwitchBindingV1) -> tuple[LifeSwitchProvenanceSourceRefV1, ...]:
    return tuple(
        LifeSwitchProvenanceSourceRefV1.create(
            ordinal=index,
            projection=ref.projection,
            status=ref.status,
            record_count=ref.record_count,
            window_start_date=None,
            window_end_date=None,
            payload_sha256=ref.payload_sha256,
        )
        for index, ref in enumerate(binding.record_refs)
    )


def _response_from_row(
    row: dict[str, Any],
    *,
    actor: UUID,
    thread_id: UUID,
    ordinal: int,
    cutoff: datetime,
    current_log_id: UUID,
) -> PriorLifeSwitchResponseV1 | None:
    try:
        answer_id = UUID(str(row["answer_id"]))
        created_at = _utc(row["created_at"])
        if UUID(str(row["owner_user_id"])) != actor or UUID(str(row["thread_id"])) != thread_id:
            return None
        if (created_at, answer_id.int) >= (cutoff, current_log_id.int):
            return None
        if UUID(str(row["attestation_answer_id"])) != answer_id:
            return None
        if UUID(str(row["chat_log_id"])) != answer_id:
            return None
        if str(row["chat_source"]) != ATTESTED_ASSISTANT_SOURCE:
            return None
        answer_sha256 = str(row["attestation_assistant_text_sha256"])
        attestation_sha256 = str(row["attestation_sha256"])
        attestation_request_sha256 = str(row["attestation_request_id_sha256"])
        attestation_snapshot_sha256 = str(row["attestation_snapshot_sha256"])
        attestation_created_at = _utc(row["attestation_created_at"])
        if not all(
            _SHA256_RE.fullmatch(value)
            for value in (
                answer_sha256,
                attestation_sha256,
                attestation_request_sha256,
                attestation_snapshot_sha256,
            )
        ):
            return None
        binding = _binding_from_row(row)
        if binding.owner_user_id != actor or binding.thread_id != thread_id or binding.answer_id != answer_id:
            return None
        if binding.request_id_sha256 != attestation_request_sha256:
            return None
        if binding.conversation_snapshot_sha256 != attestation_snapshot_sha256:
            return None
        if binding.created_at != attestation_created_at:
            return None
        receipt_manifest = row.get("receipt_manifest_sha256")
        if receipt_manifest is not None:
            receipt = _receipt_from_row(row)
            if receipt.request_id_sha256 != binding.request_id_sha256:
                return None
            if (
                receipt.conversation_snapshot_sha256
                != binding.conversation_snapshot_sha256
            ):
                return None
            if receipt.lifeswitch_binding_manifest_sha256 != binding.binding_manifest_sha256:
                return None
            if receipt.source_assembly_sha256 != binding.source_assembly_sha256:
                return None
            if receipt.envelope_sha256 != binding.envelope_sha256:
                return None
            if receipt.assistant_text_sha256 != answer_sha256:
                return None
            if receipt.attestation_sha256 != attestation_sha256:
                return None
            if receipt.answer_model_exposed != binding.answer_model_exposed:
                return None
            if receipt.created_at != binding.created_at:
                return None
            if len(receipt.source_refs) != len(binding.record_refs):
                return None
            for receipt_ref, binding_ref in zip(
                receipt.source_refs,
                binding.record_refs,
                strict=True,
            ):
                if (
                    receipt_ref.ordinal != binding_ref.ordinal
                    or receipt_ref.projection != binding_ref.projection
                    or receipt_ref.status != binding_ref.status
                    or receipt_ref.record_count != binding_ref.record_count
                    or receipt_ref.payload_sha256 != binding_ref.payload_sha256
                ):
                    return None
            status: Literal["exact_receipt", "historical_binding_only"] = "exact_receipt"
            refs = receipt.source_refs
        else:
            status = "historical_binding_only"
            refs = _historical_refs(binding)
        return PriorLifeSwitchResponseV1(
            relative_ordinal=ordinal,
            answer_id=answer_id,
            provenance_status=status,
            source_refs=refs,
            answer_sha256=answer_sha256,
            attestation_sha256=attestation_sha256,
        )
    except Exception:
        return None


async def select_prior_lifeswitch_provenance_v1(
    conn: Any,
    *,
    context_id: UUID,
    authenticated_actor_user_id: UUID,
    conversation_snapshot: ConversationSnapshotV1,
) -> PriorLifeSwitchProvenanceEnvelopeV1 | None:
    snapshot = ConversationSnapshotV1.model_validate_json(conversation_snapshot.model_dump_json())
    if snapshot.authenticated_actor_user_id != authenticated_actor_user_id:
        raise PriorLifeSwitchProvenanceError("prior LifeSwitch actor differs from snapshot")
    query = snapshot.messages[-1].content
    if not prior_lifeswitch_provenance_requested_v1(query):
        return None
    if snapshot.outcome is not ConversationSnapshotOutcome.CURRENT_REQUEST_BOUND:
        return None
    if snapshot.cutoff_created_at is None or snapshot.current_log_id is None:
        return None
    try:
        rows = list(
            await conn.fetch(
                """
                select *
                from lifeswitch_chat.read_prior_answer_lifeswitch_provenance_v1
                where context_id=$1 and owner_user_id=$2 and thread_id=$3
                  and (created_at,answer_id)<($4,$5)
                order by created_at desc,answer_id desc
                limit $6
                """,
                context_id,
                authenticated_actor_user_id,
                snapshot.thread_id,
                snapshot.cutoff_created_at,
                snapshot.current_log_id,
                MAX_PROVENANCE_CANDIDATES,
            )
        )
    except Exception:
        raise PriorLifeSwitchProvenanceError("prior LifeSwitch provenance read failed") from None
    selected: list[PriorLifeSwitchResponseV1] = []
    for raw in rows:
        response = _response_from_row(
            dict(raw),
            actor=authenticated_actor_user_id,
            thread_id=snapshot.thread_id,
            ordinal=len(selected),
            cutoff=snapshot.cutoff_created_at,
            current_log_id=snapshot.current_log_id,
        )
        if response is None:
            continue
        candidate = tuple((*selected, response))
        try:
            _render_content(candidate)
        except PriorLifeSwitchProvenanceError:
            break
        selected.append(response)
        if len(selected) == MAX_PROVENANCE_RESPONSES:
            break
    if not selected:
        return None
    return PriorLifeSwitchProvenanceEnvelopeV1.create(
        authenticated_actor_user_id=authenticated_actor_user_id,
        thread_id=snapshot.thread_id,
        conversation_snapshot_sha256=snapshot.snapshot_sha256,
        current_request_id=snapshot.current_request_id,
        current_query=query,
        responses=tuple(selected),
    )


__all__ = [
    "MAX_PROVENANCE_CANDIDATES",
    "MAX_PROVENANCE_CONTENT_BYTES",
    "MAX_PROVENANCE_CONTENT_TOKENS",
    "MAX_PROVENANCE_RESPONSES",
    "PRIOR_LIFESWITCH_PROVENANCE_V1",
    "PriorLifeSwitchProvenanceEnvelopeV1",
    "PriorLifeSwitchProvenanceError",
    "PriorLifeSwitchResponseV1",
    "prior_lifeswitch_provenance_requested_v1",
    "select_prior_lifeswitch_provenance_v1",
]
