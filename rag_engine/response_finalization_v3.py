from __future__ import annotations

"""Versioned finalization for independently governed LifeSwitch context."""

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_engine.lifeswitch_answer_binding_v1 import (
    FinalAnswerLifeSwitchBindingV1,
)
from rag_engine.lifeswitch_answer_provenance_receipt_v1 import (
    FinalAnswerLifeSwitchProvenanceReceiptV1,
)
from rag_engine.memory_v1_selection_envelope import FinalAnswerMemoryBindingV1
from rag_engine.openai_chat_provider_v1 import OpenAIChatGenerationConfigV1
from rag_engine.openai_chat_request_v4 import (
    OpenAIChatRequestV4,
    OpenAIChatResponseV3,
)
from rag_engine.response_finalization_v1 import (
    ASSISTANT_ATTESTATION_VERSION,
    AssistantOutputKind,
    AssistantTranscriptAttestationV1,
)
from rag_engine.response_lifeswitch_integration_v2 import (
    TrustedLifeSwitchResponsePlanV2,
)
from rag_engine.search_capability_output_validator_v1 import (
    validate_search_capability_output_v1,
)


FINALIZED_LIFESWITCH_RESPONSE_V3_VERSION = "finalized_trusted_response_v3"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LifeSwitchResponseFinalizationError(RuntimeError):
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
    if isinstance(value, Enum):
        return value.value
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


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("finalization clock must be timezone-aware")
    return value.astimezone(timezone.utc)


class FinalizedTrustedResponseV3(_StrictFrozenModel):
    contract_version: Literal[FINALIZED_LIFESWITCH_RESPONSE_V3_VERSION] = (
        FINALIZED_LIFESWITCH_RESPONSE_V3_VERSION
    )
    answer_id: UUID
    output_kind: AssistantOutputKind
    assistant_text: str = Field(min_length=1, max_length=500_000, repr=False)
    attestation: AssistantTranscriptAttestationV1
    memory_binding: FinalAnswerMemoryBindingV1 | None = Field(default=None, repr=False)
    lifeswitch_binding: FinalAnswerLifeSwitchBindingV1 | None = Field(
        default=None,
        repr=False,
    )
    lifeswitch_provenance_receipt: FinalAnswerLifeSwitchProvenanceReceiptV1 | None = Field(
        default=None,
        repr=False,
    )
    finalization_sha256: str

    @field_validator("finalization_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("finalization hash must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def exact_finalization(self) -> "FinalizedTrustedResponseV3":
        if self.attestation.answer_id != self.answer_id:
            raise ValueError("finalization answer differs from attestation")
        if self.attestation.output_kind is not self.output_kind:
            raise ValueError("finalization output kind differs from attestation")
        if self.attestation.assistant_text_sha256 != _text_sha256(self.assistant_text):
            raise ValueError("finalization text differs from attestation")
        for binding in (self.memory_binding, self.lifeswitch_binding):
            if binding is not None and binding.answer_id != self.answer_id:
                raise ValueError("finalization answer differs from context binding")
        receipt = self.lifeswitch_provenance_receipt
        if (receipt is None) != (self.lifeswitch_binding is None):
            raise ValueError("LifeSwitch binding and provenance receipt must be paired")
        if receipt is not None:
            if receipt.answer_id != self.answer_id:
                raise ValueError("provenance receipt answer differs from finalization")
            if receipt.attestation_sha256 != self.attestation.attestation_sha256:
                raise ValueError("provenance receipt differs from attestation")
            if receipt.lifeswitch_binding_manifest_sha256 != self.lifeswitch_binding.binding_manifest_sha256:
                raise ValueError("provenance receipt differs from LifeSwitch binding")
        payload = {
            "contract_version": self.contract_version,
            "answer_id": self.answer_id,
            "output_kind": self.output_kind.value,
            "assistant_text_sha256": _text_sha256(self.assistant_text),
            "attestation_sha256": self.attestation.attestation_sha256,
            "memory_binding_manifest_sha256": (
                self.memory_binding.binding_manifest_sha256
                if self.memory_binding is not None
                else None
            ),
            "lifeswitch_binding_manifest_sha256": (
                self.lifeswitch_binding.binding_manifest_sha256
                if self.lifeswitch_binding is not None
                else None
            ),
            "lifeswitch_provenance_receipt_manifest_sha256": (
                receipt.receipt_manifest_sha256 if receipt is not None else None
            ),
        }
        if self.finalization_sha256 != _sha256(payload):
            raise ValueError("finalized response manifest hash mismatch")
        return self


def finalize_trusted_response_v3(
    *,
    trusted_plan: TrustedLifeSwitchResponsePlanV2,
    provider_response: OpenAIChatResponseV3,
    generation_config: OpenAIChatGenerationConfigV1 | None = None,
    answer_id: UUID,
    created_at: datetime,
) -> FinalizedTrustedResponseV3:
    """Bind one provider output to Memory and LifeSwitch independently."""

    try:
        plan = TrustedLifeSwitchResponsePlanV2.model_validate_json(
            trusted_plan.model_dump_json()
        )
        response = OpenAIChatResponseV3.model_validate_json(
            provider_response.model_dump_json()
        )
        if not isinstance(answer_id, UUID):
            raise TypeError("answer id type mismatch")
        occurred_at = _utc(created_at)
        expected_request = OpenAIChatRequestV4.create(
            source_plan=plan,
            generation_config=generation_config,
        )
        if response.provider_request_sha256 != expected_request.request_sha256:
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

        base = plan.base_response_plan
        base_source = base.assembled_prompt.source_request
        validate_search_capability_output_v1(
            text,
            base_source.search_capability_manifest,
        )

        memory_binding: FinalAnswerMemoryBindingV1 | None = None
        if base_source.memory_input is not None and base_source.memory_application is not None:
            application = base_source.memory_application
            memory_binding = FinalAnswerMemoryBindingV1.create(
                assembly_input=base_source.memory_input,
                owner_user_id=base.authenticated_actor_user_id,
                answer_id=answer_id,
                injected=application.injected_records,
                answer_model_exposed=application.injected_record_refs,
                applied_controls=application.applied_control_refs,
                created_at=occurred_at,
            )

        lifeswitch_binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=plan.assembled_prompt,
            authenticated_actor_user_id=base.authenticated_actor_user_id,
            answer_id=answer_id,
            created_at=occurred_at,
        )

        attestation_payload: dict[str, Any] = {
            "contract_version": ASSISTANT_ATTESTATION_VERSION,
            "authenticated_actor_user_id": base.authenticated_actor_user_id,
            "thread_id": base.thread_id,
            "answer_id": answer_id,
            "request_id_sha256": _text_sha256(base.policy_input.request_id),
            "conversation_snapshot_sha256": base.conversation_snapshot_sha256,
            "trusted_plan_sha256": plan.plan_sha256,
            "provider_request_sha256": response.provider_request_sha256,
            "provider_response_sha256": response.response_sha256,
            "provider_response_id": response.response_id,
            "output_kind": output_kind,
            "assistant_text_sha256": text_hash,
            "created_at": occurred_at,
        }
        attestation = AssistantTranscriptAttestationV1(
            **attestation_payload,
            attestation_sha256=_sha256(attestation_payload),
        )
        provenance_receipt = (
            FinalAnswerLifeSwitchProvenanceReceiptV1.create(
                prepared_context=plan.lifeswitch_context,
                binding=lifeswitch_binding,
                assistant_text_sha256=text_hash,
                attestation_sha256=attestation.attestation_sha256,
            )
            if lifeswitch_binding is not None
            else None
        )
        final_payload = {
            "contract_version": FINALIZED_LIFESWITCH_RESPONSE_V3_VERSION,
            "answer_id": answer_id,
            "output_kind": output_kind.value,
            "assistant_text_sha256": text_hash,
            "attestation_sha256": attestation.attestation_sha256,
            "memory_binding_manifest_sha256": (
                memory_binding.binding_manifest_sha256
                if memory_binding is not None
                else None
            ),
            "lifeswitch_binding_manifest_sha256": (
                lifeswitch_binding.binding_manifest_sha256
                if lifeswitch_binding is not None
                else None
            ),
            "lifeswitch_provenance_receipt_manifest_sha256": (
                provenance_receipt.receipt_manifest_sha256
                if provenance_receipt is not None
                else None
            ),
        }
        return FinalizedTrustedResponseV3(
            answer_id=answer_id,
            output_kind=output_kind,
            assistant_text=text,
            attestation=attestation,
            memory_binding=memory_binding,
            lifeswitch_binding=lifeswitch_binding,
            lifeswitch_provenance_receipt=provenance_receipt,
            finalization_sha256=_sha256(final_payload),
        )
    except LifeSwitchResponseFinalizationError:
        raise
    except Exception:
        raise LifeSwitchResponseFinalizationError(
            "LifeSwitch response finalization failed"
        ) from None


__all__ = [
    "FINALIZED_LIFESWITCH_RESPONSE_V3_VERSION",
    "FinalizedTrustedResponseV3",
    "LifeSwitchResponseFinalizationError",
    "finalize_trusted_response_v3",
]
