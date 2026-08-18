from __future__ import annotations

"""Compatibility entry point for the canonical conversation persistence service."""

from uuid import UUID

from seebx.capabilities.conversation.lifeswitch_finalization import FinalizedTrustedResponseV3
from seebx.capabilities.conversation.persistence import (
    ConversationPersistenceError,
    persist_conversation_response,
)


class ResponsePersistenceV3Error(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("versioned finalized response persistence failed")


async def persist_finalized_response_v3(
    conn: object,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    finalized: FinalizedTrustedResponseV3,
) -> None:
    """Persist the transcript and neutral attestation atomically.

    Governed Memory is persisted in its separate successor store before this
    conversation transaction and is never written here. Retired LifeSwitch
    answer-binding and prior-provenance records are not persisted.
    """

    try:
        await persist_conversation_response(
            conn,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            request_id=request_id,
            finalized=finalized,
        )
    except ConversationPersistenceError as exc:
        raise ResponsePersistenceV3Error(exc.stage) from None


__all__ = ["ResponsePersistenceV3Error", "persist_finalized_response_v3"]
