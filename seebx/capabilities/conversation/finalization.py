from __future__ import annotations

"""Pure final-answer binding for one trusted provider response.

This module performs no database writes. It creates the neutral assistant
transcript attestation accepted by the append-only conversation persistence
layer. Response-memory provenance is owned by the separate Zep lifecycle and
is never embedded in this host-chat DTO.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seebx.contracts.transcript_integrity import (
    ASSISTANT_ATTESTATION_VERSION,
    AssistantOutputKind,
    AssistantTranscriptAttestationV1,
)
from seebx.adapters.openai_chat import (
    OpenAIChatGenerationConfigV1,
    OpenAIChatRequestV1,
    OpenAIChatResponseV1,
)
from seebx.capabilities.conversation.orchestration import TrustedResponsePlanV0_2
from seebx.capabilities.search.output_validation import (
    validate_search_capability_output_v1,
)


FINALIZED_RESPONSE_VERSION = "finalized_trusted_response_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ResponseFinalizationError(RuntimeError):
    pass


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
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ResponseFinalizationError("finalization clock must be timezone-aware")
    return value.astimezone(timezone.utc)


class FinalizedTrustedResponseV1(_StrictFrozenModel):
    contract_version: Literal[FINALIZED_RESPONSE_VERSION] = FINALIZED_RESPONSE_VERSION
    answer_id: UUID
    output_kind: AssistantOutputKind
    assistant_text: str = Field(min_length=1, max_length=500_000, repr=False)
    attestation: AssistantTranscriptAttestationV1
    finalization_sha256: str

    @field_validator("finalization_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("finalization hash must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def exact_finalization(self) -> "FinalizedTrustedResponseV1":
        if self.attestation.answer_id != self.answer_id:
            raise ValueError("finalization answer differs from attestation")
        if self.attestation.output_kind is not self.output_kind:
            raise ValueError("finalization output kind differs from attestation")
        if self.attestation.assistant_text_sha256 != _text_sha256(self.assistant_text):
            raise ValueError("finalization text differs from attestation")
        payload = {
            "contract_version": self.contract_version,
            "answer_id": self.answer_id,
            "output_kind": self.output_kind.value,
            "assistant_text_sha256": _text_sha256(self.assistant_text),
            "attestation_sha256": self.attestation.attestation_sha256,
        }
        if self.finalization_sha256 != _sha256(payload):
            raise ValueError("finalized response manifest hash mismatch")
        return self


def finalize_trusted_response_v1(
    *,
    trusted_plan: TrustedResponsePlanV0_2,
    provider_response: OpenAIChatResponseV1,
    generation_config: OpenAIChatGenerationConfigV1 | None = None,
    answer_id: UUID,
    created_at: datetime,
) -> FinalizedTrustedResponseV1:
    """Bind provider output and exact Memory exposure to one answer ID."""

    try:
        plan = TrustedResponsePlanV0_2.model_validate_json(
            trusted_plan.model_dump_json()
        )
        response = OpenAIChatResponseV1.model_validate_json(
            provider_response.model_dump_json()
        )
        if not isinstance(answer_id, UUID):
            raise TypeError("answer id type mismatch")
        occurred_at = _utc(created_at)
        expected_provider_request = OpenAIChatRequestV1.create(
            trusted_plan=plan,
            generation_config=generation_config,
        )
        if response.provider_request_sha256 != expected_provider_request.request_sha256:
            raise ValueError("provider response is not bound to the trusted plan")

        if response.content is not None:
            output_kind = AssistantOutputKind.CONTENT
            text = response.content
            text_hash = response.content_sha256
        else:
            output_kind = AssistantOutputKind.REFUSAL
            text = response.refusal
            text_hash = response.refusal_sha256
        if text is None or text_hash is None:
            raise ValueError("provider response has no attestable output")

        source = plan.assembled_prompt.source_request
        validate_search_capability_output_v1(
            text,
            source.search_capability_manifest,
        )
        attestation = AssistantTranscriptAttestationV1.create(
            authenticated_actor_user_id=plan.authenticated_actor_user_id,
            thread_id=plan.thread_id,
            answer_id=answer_id,
            request_id_sha256=_text_sha256(plan.policy_input.request_id),
            conversation_snapshot_sha256=plan.conversation_snapshot_sha256,
            trusted_plan_sha256=plan.plan_sha256,
            provider_request_sha256=response.provider_request_sha256,
            provider_response_sha256=response.response_sha256,
            provider_response_id=response.response_id,
            output_kind=output_kind,
            assistant_text_sha256=text_hash,
            created_at=occurred_at,
        )
        final_payload = {
            "contract_version": FINALIZED_RESPONSE_VERSION,
            "answer_id": answer_id,
            "output_kind": output_kind.value,
            "assistant_text_sha256": text_hash,
            "attestation_sha256": attestation.attestation_sha256,
        }
        return FinalizedTrustedResponseV1(
            answer_id=answer_id,
            output_kind=output_kind,
            assistant_text=text,
            attestation=attestation,
            finalization_sha256=_sha256(final_payload),
        )
    except ResponseFinalizationError:
        raise
    except Exception:
        raise ResponseFinalizationError("trusted response finalization failed") from None


__all__ = [
    "ASSISTANT_ATTESTATION_VERSION",
    "FINALIZED_RESPONSE_VERSION",
    "AssistantOutputKind",
    "AssistantTranscriptAttestationV1",
    "FinalizedTrustedResponseV1",
    "ResponseFinalizationError",
    "finalize_trusted_response_v1",
]
