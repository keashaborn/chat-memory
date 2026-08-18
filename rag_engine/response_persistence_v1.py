from __future__ import annotations

"""Compatibility entry point for the canonical conversation persistence service."""

from uuid import UUID

from rag_engine.response_finalization_v1 import FinalizedTrustedResponseV1
from seebx.capabilities.conversation.persistence import (
    ConversationPersistenceError,
    persist_conversation_response,
)


class ResponsePersistenceError(RuntimeError):
    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__("finalized response persistence failed")


async def persist_finalized_response_v1(
    conn: object,
    *,
    owner_user_id: UUID,
    thread_id: UUID,
    request_id: str,
    finalized: FinalizedTrustedResponseV1,
) -> None:
    try:
        await persist_conversation_response(
            conn,
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            request_id=request_id,
            finalized=finalized,
        )
    except ConversationPersistenceError as exc:
        raise ResponsePersistenceError(exc.stage) from None


__all__ = ["ResponsePersistenceError", "persist_finalized_response_v1"]
