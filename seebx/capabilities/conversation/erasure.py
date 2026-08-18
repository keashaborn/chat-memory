from __future__ import annotations

"""Canonical application service for owner-scoped conversation erasure."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID


CHAT_HISTORY_CLEAR_CONTRACT_VERSION = "chat_history_clear_v1"
ChatHistoryScope = Literal["all", "recent", "thread", "message_tail"]


class ChatHistoryClearError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatHistoryClearResultV1:
    operation_id: UUID
    scope: ChatHistoryScope
    deleted_message_count: int
    deleted_thread_count: int
    deleted_outbox_count: int
    receipt_sha256: str
    completed_at: Any

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": CHAT_HISTORY_CLEAR_CONTRACT_VERSION,
            "status": "completed",
            "operation_id": str(self.operation_id),
            "scope": self.scope,
            "deleted_message_count": self.deleted_message_count,
            "deleted_thread_count": self.deleted_thread_count,
            "deleted_outbox_count": self.deleted_outbox_count,
            "receipt_sha256": self.receipt_sha256,
            "completed_at": self.completed_at,
            "memory_retained": True,
            "zep_called": False,
        }


class ConversationErasureRepository(Protocol):
    async def clear_history(
        self,
        *,
        owner_user_id: UUID,
        authorization: str,
        operation_id: UUID,
        scope: ChatHistoryScope,
        thread_id: UUID | None = None,
        recent_window_seconds: int | None = None,
    ) -> ChatHistoryClearResultV1: ...


class ZepOwnerErasureRuntime(Protocol):
    def owner_erasure_barrier(
        self,
        owner_user_id: UUID,
    ) -> AbstractAsyncContextManager[None]: ...

    async def delete_owner_memory(self, owner_user_id: UUID) -> None: ...


class ConversationErasureService:
    """Compose PostgreSQL history clearing and verified Zep owner erasure."""

    def __init__(
        self,
        *,
        repository: ConversationErasureRepository,
        zep_runtime: ZepOwnerErasureRuntime,
    ) -> None:
        if repository is None:
            raise ValueError("conversation erasure repository is required")
        if zep_runtime is None:
            raise ValueError("Zep owner erasure runtime is required")
        self._repository = repository
        self._zep_runtime = zep_runtime

    async def clear_history(
        self,
        *,
        owner_user_id: UUID,
        authorization: str,
        operation_id: UUID,
        scope: ChatHistoryScope,
        thread_id: UUID | None = None,
        recent_window_seconds: int | None = None,
    ) -> ChatHistoryClearResultV1:
        return await self._repository.clear_history(
            owner_user_id=owner_user_id,
            authorization=authorization,
            operation_id=operation_id,
            scope=scope,
            thread_id=thread_id,
            recent_window_seconds=recent_window_seconds,
        )

    async def clear_all_chat_and_memory(
        self,
        *,
        owner_user_id: UUID,
        authorization: str,
        operation_id: UUID,
    ) -> ChatHistoryClearResultV1:
        """Verify Zep erasure before committing the PostgreSQL clear.

        Zep owner deletion is provider-idempotent. If Zep is unavailable, the
        PostgreSQL repository is never called. If PostgreSQL later fails, a
        retry repeats the verified Zep deletion before attempting chat clear.
        """
        async with self._zep_runtime.owner_erasure_barrier(owner_user_id):
            await self._zep_runtime.delete_owner_memory(owner_user_id)
            return await self._repository.clear_history(
                owner_user_id=owner_user_id,
                authorization=authorization,
                operation_id=operation_id,
                scope="all",
            )


__all__ = [
    "CHAT_HISTORY_CLEAR_CONTRACT_VERSION",
    "ChatHistoryClearError",
    "ChatHistoryClearResultV1",
    "ChatHistoryScope",
    "ConversationErasureRepository",
    "ConversationErasureService",
    "ZepOwnerErasureRuntime",
]
