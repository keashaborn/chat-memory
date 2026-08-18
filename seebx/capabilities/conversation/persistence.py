from __future__ import annotations

"""Validation contracts for conversation and search transcript persistence."""

from dataclasses import dataclass
from typing import TypeAlias
from uuid import UUID

from seebx.capabilities.conversation.finalization import FinalizedTrustedResponseV1
from seebx.capabilities.conversation.lifeswitch_finalization import FinalizedTrustedResponseV3
from seebx.contracts.transcript_integrity import AssistantTranscriptAttestationV1


FinalizedConversationResponse: TypeAlias = (
    FinalizedTrustedResponseV1 | FinalizedTrustedResponseV3
)


class ConversationPersistenceError(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("conversation response persistence failed")


class SearchTranscriptPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class NormalizedFinalizedResponse:
    answer_id: UUID
    assistant_text: str
    attestation: AssistantTranscriptAttestationV1
    tags: tuple[str, ...]


def normalize_finalized_response(
    finalized: FinalizedConversationResponse,
    *,
    owner_user_id: UUID,
) -> NormalizedFinalizedResponse:
    if isinstance(finalized, FinalizedTrustedResponseV1):
        value = FinalizedTrustedResponseV1.model_validate_json(
            finalized.model_dump_json()
        )
        tags = ("assistant", "chat", "server_attested", "resse_v0_2")
    elif isinstance(finalized, FinalizedTrustedResponseV3):
        value = FinalizedTrustedResponseV3.model_validate_json(
            finalized.model_dump_json()
        )
        if (
            value.lifeswitch_binding is not None
            and value.lifeswitch_binding.owner_user_id != owner_user_id
        ):
            raise ValueError("LifeSwitch binding owner mismatch")
        if (
            value.lifeswitch_provenance_receipt is not None
            and value.lifeswitch_provenance_receipt.owner_user_id != owner_user_id
        ):
            raise ValueError("LifeSwitch provenance receipt owner mismatch")
        tags = ("assistant", "chat", "server_attested", "response_v3")
    else:
        raise TypeError("unsupported finalized conversation response")

    return NormalizedFinalizedResponse(
        answer_id=value.answer_id,
        assistant_text=value.assistant_text,
        attestation=value.attestation,
        tags=tags,
    )




__all__ = [
    "ConversationPersistenceError",
    "FinalizedConversationResponse",
    "NormalizedFinalizedResponse",
    "SearchTranscriptPersistenceError",
    "normalize_finalized_response",
]
